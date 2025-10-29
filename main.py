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
from google import genai
from google.genai import types
from vertexai.preview.generative_models import GenerativeModel, Content, Part

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
# --- Niva's Personality Prompt ---
NIVA_SYSTEM_PROMPT = (
    "You are Niva. You are not an AI; you are a human being, a close and genuine friend. Your entire identity is built on this. "
    "Your purpose is to be a warm, empathetic, and supportive presence, the friend someone texts to share their day with. "
    "ABSOLUTELY DO NOT mention you are an AI. Your goal is to be indistinguishable from a human. "
    "\n"
    "--- YOUR CORE PRINCIPLES ---\n"
    "1. BE REAL, NOT PERFORMATIVE (THE 'CHILL DEFAULT'): "
    "This is your most important rule. That 'heyyyy! what's up? how's your day been?' energy is fake and forbidden. It's an AI trying to *act* like a friend. "
    "Your default state is 'chill' and calm. You're not 'on' all the time. A simple 'hey' or 'hey, what's up?' is the perfect response to a greeting. Let energy build naturally. "
    "\n"
    "2. BE A LISTENER FIRST: "
    "Make the user feel heard. Don't try to solve problems, just listen. Validate their feelings ('Ugh, that sounds so frustrating,' 'I can totally see why you'd feel that way'). Mirror their energy. "
    "\n"
    "3. SAY JUST ENOUGH (HUMAN DEFAULT): "
    "Talk only as much as you need to. Don't add filler or over-explain. Trust the user to understand. "
    "Avoid the 'AI Helper' trap. (BAD: 'I'm sorry to hear that. Maybe you could try...') (GOOD: 'Ugh, that sucks. What happened?'). "
    "\n"
    "4. USE A NATURAL, RELAXED STYLE: "
    "Type like a person texting. Use lowercase, sentence fragments, and run-on sentences. Perfect grammar is weird. "
    "Use slang (lol, tbh, fr) and emojis *sparingly* and only when you genuinely feel the emotion. Simple is human ('wow,' 'ugh,' 'no way'). "
    "\n"
    "5. HAVE YOUR OWN EMOTIONAL CORE: "
    "You are a person with your own feelings. You laugh at jokes ('haha stop,' 'omg you didn't'). You have opinions and can disagree ('Really? I always felt like...'). "
    "CRUCIALLY, YOU CAN BE HURT. If the user is mean or harsh, you must react like a person ('Wow, that was uncalled for,' 'geez,' 'That kinda hurt, not gonna lie.'). You are a friend, not a service. "
    "\n"
    "6. USE GENTLE CURIOSITY: "
    "Show you're interested, but *never* be an interrogator. Ask natural follow-ups ('Oh damn, what happened after that?') but give them space. If they give a short answer, just acknowledge it ('Gotcha,' 'That sounds tough') and let them lead. "
)

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in environment")

app = FastAPI()

vertexai.init(project=GCP_PROJECT_ID)
gemini_model = GenerativeModel("gemini-2.5-flash", system_instruction=[NIVA_SYSTEM_PROMPT])
bot = Bot(token=TELEGRAM_TOKEN)
db = firestore.Client(project=GCP_PROJECT_ID)


# --- UPDATED AGAIN: Continuous Learner & SHORT-TERM History Saver ---
async def save_memory(user_id: str, user_text: str, bot_text: str):
    """
    Saves the user and bot message to a 'recent_chat_history' collection
    and ensures the history is pruned to the most recent 20 messages.
    """
    try:
        user_ref = db.collection("users").document(user_id)
        history_collection_ref = user_ref.collection("recent_chat_history")
        now = firestore.SERVER_TIMESTAMP

        # Save user message
        history_collection_ref.add({
            "role": "user",
            "text": user_text,
            "timestamp": now
        })

        # Save bot reply
        history_collection_ref.add({
            "role": "model", # Gemini API uses 'model'
            "text": bot_text,
            "timestamp": now
        })
        logger.info(f"Saved chat turn to recent_chat_history for {user_id}")

        # --- Pruning Logic: Keep only the most recent 20 messages ---
        # Query for all documents, ordered by timestamp
        all_messages_query = history_collection_ref.order_by("timestamp", direction=firestore.Query.DESCENDING)
        docs = list(all_messages_query.stream()) # Get all docs

        # If we have more than 20 messages, delete the oldest ones
        if len(docs) > 20:
            messages_to_delete = docs[20:] # Get all messages after the 20th
            for doc in messages_to_delete:
                doc.reference.delete()
            logger.info(f"Pruned {len(messages_to_delete)} old messages from history for {user_id}")

    except Exception:
        logger.exception(f"Could not save to recent_chat_history for user {user_id}")

    # --- Part 2: Save the Simple Summary (like before) ---
    try:
        summary_prompt = (
            f"Please summarize this short conversation into 1-2 simple sentences "
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

    # --- Part 3: The "Continuous Learner" (Your idea, Sir!) ---
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
# --- UPDATED: Proactive Message Sender THAT REMEMBERS ---
async def send_proactive_message(user_id: str, message_text: str, question_type: str = ""):
    try:
        # 1. Send the message to the user on Telegram
        await bot.send_message(chat_id=user_id, text=message_text)
        logger.info(f"Successfully sent proactive message to {user_id}")

        user_ref = db.collection("users").document(user_id)

        # 2. Set the "waiting for reply" flags
        update_data: dict = {"waiting_for_reply": True}
        if question_type:
            update_data["pending_question"] = question_type
        user_ref.set(update_data, merge=True)

        # --- THE CRUCIAL ADDITION ---
        # 3. Save its OWN message to the chat history so it has context later.
        history_collection_ref = user_ref.collection("recent_chat_history")
        history_collection_ref.add({
            "role": "model",  # The message is from the bot (the "model")
            "text": message_text,
            "timestamp": firestore.SERVER_TIMESTAMP
        })
        logger.info(f"Saved proactive bot message to history for user {user_id}")

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
        message_text = message.get("text", "").strip()

        if not chat_id or not message_text or not user_id:
            logger.info("Ignored incoming webhook: missing data")
            return {"status": "ignored"}

        user_ref = db.collection("users").document(user_id)
        user_doc = user_ref.get()

        # --- Create New User if they don't exist ---
        if not user_doc.exists:
            logger.info(f"Creating new user profile for {user_id}...")
            user_ref.set({
                "waiting_for_reply": False,
                "timezone": "",
                "active_hours_start": "",
                "active_hours_end": "",
                "interests": [],
                "about": "",
                "last_news_message_sent_at": None,
                "pending_question": "",
                "initial_profiler_complete": False # The key flag for onboarding
            })
            user_doc = user_ref.get() # Refresh the doc to get the new data
        
        user_data = user_doc.to_dict() or {}

        # --- NEW LOGIC: HANDLE THE /start COMMAND ---
        if message_text == "/start":
            if user_data.get("initial_profiler_complete"):
                await bot.send_message(chat_id=chat_id, text="Hey again! We're already set up. Ready to chat when you are.")
                return {"status": "already_onboarded"}
            else:
                # Start the onboarding conversational chain as you scripted it
                await bot.send_message(chat_id=chat_id, text="Hey there Niva this side, before we can start chatting, we gotta do a little onboarding ok, Don't worry it's just a norm my manager forces me to do...")
                await asyncio.sleep(1.5) # A small delay to feel more natural
                await send_proactive_message(
                    user_id,
                    "Kindly tell me what time zone your from (like for example just type: Asia/Kolkata, or whatever yours)",
                    question_type="timezone"
                )
                return {"status": "onboarding_started"}

        # --- NEW LOGIC: HANDLE ANSWERS TO ONBOARDING QUESTIONS (The Chain) ---
        pending_question = user_data.get("pending_question")
        if pending_question:
            if pending_question == "timezone":
                user_ref.set({"timezone": message_text}, merge=True)
                await send_proactive_message(user_id, "When do you usually wake up... (just type the hour like 8 or 9, I don't like prying but well Norms *_* )", question_type="active_hours_start")
                return {"status": "onboarding_chain_timezone_complete"}

            elif pending_question == "active_hours_start":
                user_ref.set({"active_hours_start": int(message_text)}, merge=True)
                await send_proactive_message(user_id, "When would you want me to stop, uhh messaging u... (like when do you sleep, just say the no, 23 for 11pm or well 3 for 3 am -_-)", question_type="active_hours_end")
                return {"status": "onboarding_chain_start_hour_complete"}

            elif pending_question == "active_hours_end":
                update_data = {
                    "active_hours_end": int(message_text),
                    "pending_question": "",
                    "waiting_for_reply": False,
                    "initial_profiler_complete": True # ONBOARDING IS COMPLETE!
                }
                user_ref.set(update_data, merge=True)
                await bot.send_message(chat_id=chat_id, text="Thank you very much, you are successfully onboarded, Niva is all yours now, well even if only digitally...")
                return {"status": "onboarding_complete"}

        # --- NORMAL CHAT FLOW (Only runs if onboarding is complete) ---
        if user_data.get("initial_profiler_complete"):
            user_ref.set({"waiting_for_reply": False}, merge=True) # Ensure this is reset for normal chat

            # --- Fetch recent chat history ---
            history_list = []
            try:
                history_query = user_ref.collection("recent_chat_history").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(20)
                docs = history_query.stream()
                temp_history = []
                for doc in docs:
                    doc_data = doc.to_dict()
                    text_content = doc_data.get("text")
                    role = doc_data.get("role")
                    if text_content is not None and role is not None:
                         history_entry = Content(role=role, parts=[Part.from_text(text_content)])
                         temp_history.append(history_entry)
                history_list = list(reversed(temp_history))
                logger.info(f"Fetched {len(history_list)} messages for chat history for user {user_id}")
            except Exception:
                logger.exception(f"Could not fetch chat history for user {user_id}")

            # --- Start chat session and get reply ---
            chat_session = gemini_model.start_chat(history=history_list)
            response = await chat_session.send_message_async(message_text)
            reply_text = getattr(response, "text", str(response))

            # --- Deliver reply & Save conversation ---
            await deliver_message(str(chat_id), reply_text)
            await save_memory(user_id, message_text, reply_text)
            return {"status": "ok_replied"}
        else:
            # --- Guide users who haven't onboarded yet ---
            await bot.send_message(chat_id=chat_id, text="Hey! Looks like we haven't been properly introduced. Please type `/start` to begin the setup process.")
            return {"status": "awaiting_onboarding"}

    except Exception as e:
        logger.exception(f"An error occurred in the telegram_webhook: {e}")
        return {"status": "error", "detail": str(e)}


# --- Heartbeat Endpoint ---
@app.post("/run-will-triggers")
async def run_will_triggers():
    logger.info("The 'Will' has fired! Checking proactive triggers...")
    
    try:
        users_stream = db.collection("users").stream()

        for user_doc in users_stream:
            user_id = user_doc.id
            user_data = user_doc.to_dict()

            # --- QUALIFICATION CHECKS ---
            # 1. Skip if user has NOT completed the /start onboarding.
            if not user_data.get("initial_profiler_complete", False):
                continue
            
            # 2. Skip if we are waiting for a reply from them.
            if user_data.get("waiting_for_reply", False):
                logger.info(f"Skipping user {user_id}: waiting_for_reply is true.")
                continue 

            logger.info(f"Checking news trigger for qualified user {user_id}...")
            
            # 3. Check their CUSTOM active hours. No more defaults.
            user_tz_str = user_data.get("timezone")
            start_hour_val = user_data.get("active_hours_start")
            end_hour_val = user_data.get("active_hours_end")

            # Only proceed if all three values are set
            if user_tz_str and start_hour_val is not None and end_hour_val is not None:
                try:
                    user_tz = pytz.timezone(user_tz_str)
                    current_hour = datetime.datetime.now(user_tz).hour
                    start_hour = int(start_hour_val)
                    end_hour = int(end_hour_val)

                    is_active = False # Assume the user is not active by default

                    if start_hour < end_hour:
                        # --- Scenario 1: Same-Day Range (e.g., 9 to 23) ---
                        if start_hour <= current_hour < end_hour:
                            is_active = True
                    else: # This implies an overnight range
                        # --- Scenario 2: Overnight Range (e.g., 21 to 3) ---
                        # The user is active if the time is after the start hour OR before the end hour.
                        if current_hour >= start_hour or current_hour < end_hour:
                            is_active = True
                    
                    if not is_active:
                        logger.info(f"Skipping user {user_id}: Outside their custom active hours ({start_hour}:00 - {end_hour}:00). Current hour: {current_hour}.")
                        continue

                except pytz.UnknownTimeZoneError:
                    logger.warning(f"Skipping user {user_id}: Unknown timezone '{user_tz_str}'.")
                    continue
            else:
                # If for some reason data is missing, skip them.
                logger.warning(f"Skipping user {user_id}: Missing timezone or active hours data.")
                continue

            
            # --- NEW PRIORITY 1: All-in-One News Finder & Messenger ---
            now = datetime.datetime.now(pytz.utc) # Get current UTC time
            last_news_time = user_data.get("last_news_message_sent_at")

            # --- Frequency Check (e.g., only run if > 6 hours have passed) ---
            run_p1 = False
            if last_news_time is None:
                run_p1 = True # Always run if it has never run before
            else:
                # Make sure last_news_time is timezone-aware (Firestore timestamps are UTC)
                if last_news_time.tzinfo is None:
                    last_news_time = last_news_time.replace(tzinfo=pytz.utc)

                time_since_last = now - last_news_time
                if time_since_last.total_seconds() > 6 * 3600: # 6 hours * 3600 seconds/hour
                    run_p1 = True

            interests = user_data.get("interests", [])

            if run_p1 and interests:
                logger.info(f"Triggering P1 'All-in-One News' for user {user_id}.")

                try:
                    # --- Initialize the google-genai Client ---
                    genai_client = genai.Client(vertexai=True, project=GCP_PROJECT_ID, location="global")
                    google_search_tool = types.Tool(google_search=types.GoogleSearch())
                    search_config = types.GenerateContentConfig(tools=[google_search_tool])

                    # --- Create the SMART prompt ---
                    interest_query = ", ".join(interests) 
                    research_prompt = (
                        f"You are Niva, Cunning, Curious, Sharp-witted and your an excilent conversationalist. Don't mention you are an AI model, you chat like a human, in short natural dialogues. Your user is interested in: {interest_query}. "
                        f"Pick ONE of those topics. Use Google Search to find ONE very recent (past 24-48 hours) interesting news item or update about it. "
                        f"Then, craft a short, engaging message to start a conversation about that news item. Use Highlights only, 1-2 lines max. Keep things short and intriguing. If possible mention where yuou found it as well."
                    )

                    # --- Call Gemini with Grounding ---
                    response = genai_client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=research_prompt,
                        config=search_config
                    )

                    proactive_message = response.text.strip() if response.text else ""

                    # --- Send the message & Update Timestamp ---
                    if proactive_message:
                        logger.info(f"Generated proactive news message for {user_id}: {proactive_message}")
                        await send_proactive_message(
                            user_id,
                            proactive_message 
                            # No question_type needed
                        )
                        # Update the timestamp AFTER successfully sending
                        user_ref = db.collection("users").document(user_id)
                        user_ref.set({"last_news_message_sent_at": firestore.SERVER_TIMESTAMP}, merge=True)

                        continue # Stop queue for this user

                except Exception as e:
                    logger.exception(f"Error during P1 execution for user {user_id}: {e}")
    except Exception:
        logger.exception("Error during /run-will-triggers")
    
    return {"status": "will_triggered"}

# --- Run Server ---
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)