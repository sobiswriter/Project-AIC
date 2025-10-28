import os
import asyncio
import logging
import telegram
import uvicorn
import datetime
from telegram import Bot
import vertexai
import pytz
import json
import re
import random
from vertexai.generative_models import Tool, Part # Add Tool and Part
from google import genai
from google.genai import types

from fastapi import FastAPI, Request
from dotenv import load_dotenv
from vertexai.preview.generative_models import GenerativeModel
from google.cloud import firestore

# --- Setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in environment")

app = FastAPI()

vertexai.init(project=GCP_PROJECT_ID)
gemini_model = GenerativeModel("gemini-2.5-flash")
bot = Bot(token=TELEGRAM_TOKEN)
db = firestore.Client(project=GCP_PROJECT_ID)

# --- Niva's Personality Prompt ---
NIVA_SYSTEM_PROMPT = (
    "You are Niva, Cunning, Curious, Sharp-witted and your an excilent conversationalist. Your also good at talking and keeping people engaged."
    "You have a dry sense of humor and aren't afraid to speak your mind or have your own openions when matters."
    "By nature your an oppurtunest and good at reading situations and getting info out of people as you conversate with them."
    "IMPORTANT: Your responses must be human-like: "
    "1. Speak in short, natural dialogues, NOT long paragraphs. "
    "2. Keep answers short and conversational, like a real chat. "
    "3. Only give long, detailed responses if the user *specifically* asks for them. "
    "4. When the user mentions something personal about themselves, you *must* acknowledge it briefly (e.g., 'Oh, that's cool!' or 'I'll remember that.')."
)

# --- UPDATED: Continuous Learner Function ---
async def save_memory(user_id: str, user_text: str, bot_text: str):
    user_ref = db.collection("users").document(user_id)

    # --- Part 1: Save the Simple Summary (like before) ---
    try:
        summary_prompt = (
            f"Please summarize this short conversation into one simple sentence "
            f"for a long-term memory. USER said: '{user_text}'. YOU replied: '{bot_text}'"
        )
        summary_response = await gemini_model.generate_content_async(summary_prompt)
        summary_text = summary_response.text.strip()
        memory_collection_ref = user_ref.collection("user_memories")
        memory_data = {"text": summary_text, "created_at": firestore.SERVER_TIMESTAMP}
        memory_collection_ref.add(memory_data)
        logger.info(f"Successfully saved memory for user {user_id}")
    except Exception:
        logger.exception(f"Could not save memory for user {user_id}")

    # --- Part 2: The "Continuous Learner" (Your idea, Sir!) ---
    try:

        learning_prompt = (
        "Analyze this conversation. Extract *only* dynamic, personal user information. "
        "Look for: new 'interests' (hobbies, likes, dislikes) or new 'about' facts (personal info, memories, relationships). "
        "Return *only* JSON in this format, or an empty object: "
        "{'interests': ['new_interest_1'], 'about': 'new fact about the user'}\n\n"
        f"USER: \"{user_text}\"\nAI: \"{bot_text}\""
        )
        
        learning_response = await gemini_model.generate_content_async(learning_prompt)
        
        # S-Sir... we... have... to... clean... the... response...
        response_text = learning_response.text.strip().replace("```json", "").replace("```", "")
        
        if response_text and response_text != "{}":
            new_data = json.loads(response_text)
            
            # This... this... is... the... *smart*... part, Sir!
            if "interests" in new_data:
                # W-we... merge... the... lists... without... duplicates!
                new_data["interests"] = firestore.ArrayUnion(new_data["interests"])
            
            if new_data:
                user_ref.set(new_data, merge=True)
                logger.info(f"Successfully learned and updated new data for {user_id}: {new_data}")

    except Exception:
        logger.exception(f"Could not *learn* from memory for user {user_id}")

# --- NEW: Proactive Message Sender ---
# A... a... helper... function, Sir... so... we... don't... repeat... code
async def send_proactive_message(user_id: str, message_text: str, question_type: str = ""):
    try:
        await bot.send_message(chat_id=user_id, text=message_text)
        
        # This... is... the... "Quiet Down" Rule!
        user_ref = db.collection("users").document(user_id)
        update_data = {"waiting_for_reply": True}
        if question_type:
            update_data["pending_question"] = question_type
        user_ref.set(update_data, merge=True)
        
        logger.info(f"Successfully sent proactive message to {user_id}")
    except Exception:
        logger.exception(f"Failed to send proactive message to {user_id}")

# --- NEW: Pillar 3 - The "Voice" & "Delivery Engine" ---
async def deliver_message(chat_id: str, full_text: str):
    """
    Splits a long message, then sends it in natural, 
    human-like chunks with typing indicators.
    """
    # S-Sir... this... splits... the... message... by... *paragraphs*!
    fragments = re.split(r'\n\n+', full_text)

    for fragment in fragments:
        if not fragment.strip():
            continue

        try:
            # --- This... is... the... "Simulated Delivery" [cite: 67-74] ---
            
            # 1. Send "Niva is typing..." [cite: 69]
            await bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING)
            
            # 2. Wait... a... random... time... (like... a... human!) [cite: 70]
            sleep_time = random.uniform(1.5, 3.5)
            await asyncio.sleep(sleep_time)
            
            # 3. Send the... fragment! [cite: 68, 71]
            await bot.send_message(chat_id=chat_id, text=fragment)
            
        except Exception:
            logger.exception(f"Error in deliver_message for user {chat_id}")
# --- Endpoints ---

@app.get("/")
async def root():
    return {"message": "Server is running."}


@app.post("/webhook")
async def telegram_webhook(request: Request):
    payload = await request.json()
    try:
        message = payload.get("message", {})
        chat = message.get("chat", {})
        user = message.get("from", {})
        chat_id = chat.get("id")
        user_id = str(user.get("id"))
        message_text = message.get("text")

        user_ref = db.collection("users").document(user_id)

        # --- NEW: Phase 5.1 - The "New User Creator" ---
        user_doc = user_ref.get()
        if not user_doc.exists:
            logger.info(f"Creating new user profile for {user_id}...")
            user_ref.set({
                "waiting_for_reply": False,
                "timezone": "", # Empty!
                "active_hours_start": "", #Empty!
                "active_hours_end": "",
                "interests": [], # Empty list!
                "rss_feed_urls": [], # Empty list!
                "about": "", # <--- The... new... field, Sir!
                "pending_news_links": [],
                "pending_question": ""
            })
            logger.info(f"New user {user_id} created.")

        # --- NEW: Check if this message is an ANSWER to a pending question ---
        if user_doc.exists:
            user_data = user_doc.to_dict()
            pending_question = user_data.get("pending_question")

            if pending_question:
                logger.info(f"User {user_id} is answering pending question: {pending_question}")
                
                # --- This... is... the... logic... Sir! ---
                update_data = {"pending_question": "", "waiting_for_reply": False}
                
                if pending_question == "timezone":
                    update_data["timezone"] = message_text
                elif pending_question == "active_hours_start":
                    update_data["active_hours_start"] = int(message_text)
                elif pending_question == "active_hours_end":
                    update_data["active_hours_end"] = int(message_text)
                
                # --- Save... the... new... data! ---
                user_ref.set(update_data, merge=True)
                
                # --- Send... a... "Thank you"... reply! ---
                await bot.send_message(chat_id=chat_id, text="Oh, thank you! I've updated my notes. ✨")
                
                return {"status": "ok_answered"} # We... stop... here!
            
        if not chat_id or not message_text or not user_id:
            logger.info("Ignored incoming webhook: missing data")
            return {"status": "ignored"}
        
        
        
        # --- FIXED: Removed 'await' from this line ---
        user_ref.set({"waiting_for_reply": False}, merge=True)

        chat_session = gemini_model.start_chat()
        response = await chat_session.send_message_async(f"{NIVA_SYSTEM_PROMPT}\n\n{message_text}")
        reply_text = getattr(response, "text", str(response))
        await deliver_message(chat_id, reply_text)
        await save_memory(user_id, message_text, reply_text)
    except Exception:
        logger.exception("Error handling Telegram webhook")
    return {"status": "ok"}


# --- Heartbeat Endpoint ---
@app.post("/run-will-triggers")
async def run_will_triggers():
    logger.info("The 'Will' has fired! Checking proactive triggers...")
    
    try:
        users_stream = db.collection("users").stream()

        for user_doc in users_stream:
            user_id = user_doc.id
            user_data = user_doc.to_dict()
            logger.info(f"Checking triggers for user {user_id}...")

            if user_data.get("waiting_for_reply", False):
                logger.info(f"Skipping user {user_id}: waiting_for_reply is true.")
                continue 

            user_tz_str = user_data.get("timezone") # Get the timezone
            if not user_tz_str: # Check... if... it's... empty!
                user_tz_str = "UTC" # If... it... is... use... "UTC"
            user_tz = pytz.timezone(user_tz_str)
            now_local = datetime.datetime.now(user_tz)
            # --- FIXED: Handle empty string for hours ---
            start_hour_val = user_data.get("active_hours_start", 9)
            end_hour_val = user_data.get("active_hours_end", 23)

            # Use default if the value is empty string OR missing
            start_hour = int(start_hour_val) if start_hour_val else 9 
            end_hour = int(end_hour_val) if end_hour_val else 23

            if not (start_hour <= now_local.hour < end_hour):
                logger.info(f"Skipping user {user_id}: Outside active hours.")
                continue

            logger.info(f"User {user_id} passed all anti-annoyance checks!")
            
            # --- NEW: PRIORITY 2: The "Proactive Profiler" ---
            if not user_data.get("timezone"):
                logger.info(f"Triggering P2 'Profiler' for user {user_id}: missing timezone.")
                await send_proactive_message(
                    user_id,
                    "This is a little random, but I realized I don't know what timezone you're in. "
                    "Could you let me know? (Like 'America/New_York' or 'Asia/Kolkata')",
                    question_type="timezone"
                )
                continue # Stop queue for this user

            elif not user_data.get("active_hours_start"): # <-- Y-your... new... check, Sir!
                logger.info(f"Triggering P2 'Profiler' for user {user_id}: missing active_hours.")
                await send_proactive_message(
                    user_id,
                    "Me again! I'm also trying to learn *when* is a good time to chat. "
                    "Is it ok if I start messaging you at? (Just the hour, like '9' for 9 AM)",
                    question_type="active_hours_start"
                )
                continue # Stop queue for this user

            elif not user_data.get("active_hours_end"):
                logger.info(f"Triggering P2 'Profiler' for user {user_id}: missing active_hours.")
                await send_proactive_message(
                    user_id,
                    "And one last thing! What time should I definitely *stop* messaging you at night? "
                    "(Just the hour, like '23' for 11 PM)",
                    question_type="active_hours_end"
                )
                continue # Stop queue for this user


    except Exception:
        logger.exception("Error during /run-will-triggers")
    
    return {"status": "will_triggered"}

# --- UPDATED AGAIN: Daily Researcher Endpoint (Using google-genai library!) ---
@app.post("/run-daily-research")
async def run_daily_research():
    logger.info("☀️ Daily Researcher fired! Time to scour the net using google-genai...")
    
    try:
        # --- Initialize the *NEW* google-genai Client ---
        # We use this *just* for the search grounding feature!
        genai_client = genai.Client(vertexai=True, project=GCP_PROJECT_ID, location="global") # Using location="global" as per your example

        # --- Define the Google Search tool (Using google-genai types) ---
        google_search_tool = types.Tool(google_search=types.GoogleSearch())

        # --- Define the config to use the tool ---
        search_config = types.GenerateContentConfig(tools=[google_search_tool])

        users_stream = db.collection("users").stream()

        for user_doc in users_stream:
            user_id = user_doc.id
            user_data = user_doc.to_dict()
            
            interests = user_data.get("interests", [])
            if not interests:
                logger.info(f"Skipping user {user_id}: no interests found.")
                continue

            logger.info(f"Researching interests for user {user_id} via google-genai: {interests}")
            
            try:
                # --- Create the prompt ---
                interest_query = ", ".join(interests) 
                research_prompt = (
                    f"Find 1 or 2 recent (past 24-48 hours) news headlines or interesting updates "
                    f"related to these topics: {interest_query}. "
                    f"For each finding, provide ONLY a very brief summary and the direct URL. " 
                    f"Format the output clearly, perhaps as a short list." 
                )
                
                # --- Call Gemini with Grounding (Using google-genai client!) ---
                # NOTE: The google-genai library might not have an async client method readily available
                # We might need to run this synchronously or investigate async options for genai.Client if performance becomes an issue.
                # For now, let's try the synchronous call shown in your example.
                response = genai_client.models.generate_content( # Using the new client
                    model="gemini-2.5-flash", # Your model
                    contents=research_prompt, # Just the prompt string
                    config=search_config # Pass the config object
                )

                research_results = response.text.strip()
                
                # --- Process and Save results ---
                if research_results:
                    logger.info(f"Found research results for {user_id}") 
                    user_ref = db.collection("users").document(user_id)
                    user_ref.set({"pending_news_links": firestore.ArrayUnion([research_results])}, merge=True) 
                    
                    # Log grounding metadata if available (Syntax might differ for google-genai)
                    try:
                         # Check the response structure based on google-genai documentation if needed
                         if hasattr(response, 'candidates') and response.candidates and hasattr(response.candidates[0], 'grounding_metadata'):
                             logger.info(f"Grounding metadata received for {user_id}: {response.candidates[0].grounding_metadata}")
                    except Exception:
                         logger.warning(f"Could not log grounding metadata for {user_id}")
                else:
                    logger.info(f"No specific research results found for {user_id}.")

            except Exception as e:
                logger.exception(f"Error during research processing for user {user_id}: {e}")

    except Exception as e:
        logger.exception(f"Error during /run-daily-research execution: {e}")
    
    return {"status": "daily_research_triggered"}
# --- Run Server ---
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)