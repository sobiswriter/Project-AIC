import os
import asyncio
import logging
import uvicorn
import datetime
from telegram import Bot
import vertexai
import pytz

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


# --- Memory Function ---
async def save_memory(user_id: int, user_text: str, bot_text: str):
    try:
        summary_prompt = (
            f"Please summarize this short conversation into one simple sentence "
            f"for a long-term memory. USER said: '{user_text}'. YOU replied: '{bot_text}'"
        )
        
        summary_response = await gemini_model.generate_content_async(summary_prompt)
        summary_text = summary_response.text.strip()
        memory_collection_ref = db.collection(f"users/{user_id}/user_memories")
        memory_data = {"text": summary_text, "created_at": firestore.SERVER_TIMESTAMP}
        
        # --- FIXED: Removed 'await' from this line ---
        memory_collection_ref.add(memory_data) 
        
        logger.info(f"Successfully saved memory for user {user_id}: {summary_text}")
    except Exception:
        logger.exception(f"Could not save memory for user {user_id}")


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
        user_id = user.get("id")
        message_text = message.get("text")

        if not chat_id or not message_text or not user_id:
            logger.info("Ignored incoming webhook: missing data")
            return {"status": "ignored"}
        
        user_ref = db.collection("users").document(str(user_id))
        
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

            user_tz = pytz.timezone(user_data.get("timezone", "UTC"))
            now_local = datetime.datetime.now(user_tz)
            start_hour = int(user_data.get("active_hours_start", 9))
            end_hour = int(user_data.get("active_hours_end", 23))

            if not (start_hour <= now_local.hour < end_hour):
                logger.info(f"Skipping user {user_id}: Outside active hours.")
                continue

            logger.info(f"User {user_id} passed all anti-annoyance checks!")
            
            break

    except Exception:
        logger.exception("Error during /run-will-triggers")
    
    return {"status": "will_triggered"}


# --- Run Server ---
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)