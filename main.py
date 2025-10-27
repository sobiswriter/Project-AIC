import os
import asyncio
import logging
import uvicorn
import datetime
from telegram import Bot
import vertexai
import pytz
import json

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
                "active_hours_start": 9,
                "active_hours_end": 23,
                "interests": [], # Empty list!
                "rss_feed_urls": [], # Empty list!
                "about": "", # <--- The... new... field, Sir!
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
        response = await chat_session.send_message_async(message_text)
        reply_text = getattr(response, "text", str(response))
        await bot.send_message(chat_id=chat_id, text=reply_text)
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
            start_hour = int(user_data.get("active_hours_start", 9))
            end_hour = int(user_data.get("active_hours_end", 23))

            if not (start_hour <= now_local.hour < end_hour):
                logger.info(f"Skipping user {user_id}: Outside active hours.")
                continue

            logger.info(f"User {user_id} passed all anti-annoyance checks!")
            
            # --- NEW: PRIORITY 2: The "Proactive Profiler" ---
            if not user_data.get("timezone"):
                logger.info(f"Triggering P2 'Profiler' for user {user_id}: missing timezone.")
                await send_proactive_message(
                    user_id,
                    "This is a little random, but I'm trying to be a better companion! "
                    "I realized I don't know what timezone you're in. 😅 "
                    "Could you let me know? (Like 'America/New_York' or 'Asia/Kolkata')",
                    question_type="timezone"
                )
                continue # Stop queue for this user

            elif not user_data.get("active_hours_start"): # <-- Y-your... new... check, Sir!
                logger.info(f"Triggering P2 'Profiler' for user {user_id}: missing active_hours.")
                await send_proactive_message(
                    user_id,
                    "Me again! I'm also trying to learn *when* is a good time to chat. "
                    "Could you tell me a good time to start messaging you? (Just the hour, like '9' for 9 AM)",
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


# --- Run Server ---
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)