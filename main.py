import os
import asyncio
import logging
import uvicorn
import datetime  # Added for timestamps
from telegram import Bot
import vertexai

from fastapi import FastAPI, Request
from dotenv import load_dotenv
from vertexai.preview.generative_models import GenerativeModel
from google.cloud import firestore  # Added for Firestore

# --- Your Setup (It's... really good, Sir) ---
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

# --- NEW: Firestore Client ---
db = firestore.Client(project=GCP_PROJECT_ID)


# --- NEW: Save Memory Function ---
async def save_memory(user_id: int, user_text: str, bot_text: str):
    """
    Summarizes the chat and saves it to Firestore.
    """
    try:
        summary_prompt = (
            f"Please summarize this short conversation into one simple sentence "
            f"for a long-term memory. USER said: '{user_text}'. YOU replied: '{bot_text}'"
        )
        
        summary_response = await gemini_model.generate_content_async(summary_prompt)
        summary_text = summary_response.text.strip()

        memory_collection_ref = db.collection(f"users/{user_id}/user_memories")
        
        memory_data = {
            "text": summary_text,
            "created_at": firestore.SERVER_TIMESTAMP
        }
        
        await memory_collection_ref.add(memory_data)
        logger.info(f"Successfully saved memory for user {user_id}: {summary_text}")
    
    except Exception:
        logger.exception(f"Could not save memory for user {user_id}")


# --- Your Endpoints (I... I just updated the webhook) ---

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
            logger.info("Ignored incoming webhook: missing chat_id, user_id, or text")
            return {"status": "ignored"}

        chat_session = gemini_model.start_chat()
        response = await chat_session.send_message_async(message_text)
        reply_text = getattr(response, "text", str(response))

        await bot.send_message(chat_id=chat_id, text=reply_text)
        
        # --- NEW: Call the save_memory function ---
        await save_memory(user_id, message_text, reply_text)

    except Exception:
        logger.exception("Error handling Telegram webhook")

    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)