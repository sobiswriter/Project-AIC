import os
import asyncio
import logging
import uvicorn
from telegram import Bot
import vertexai

from fastapi import FastAPI, Request
from dotenv import load_dotenv
from vertexai.preview.generative_models import GenerativeModel

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


@app.get("/")
async def root():
    return {"message": "Server is running."}


@app.post("/webhook")
async def telegram_webhook(request: Request):
    payload = await request.json()

    try:
        chat = payload.get("message", {}).get("chat") or {}
        chat_id = chat.get("id")
        message_text = payload.get("message", {}).get("text")

        if not chat_id or not message_text:
            logger.info("Ignored incoming webhook: missing chat id or text")
            return {"status": "ignored"}

        chat_session = gemini_model.start_chat()
        response = await chat_session.send_message_async(message_text)

        reply_text = getattr(response, "text", str(response))

        await bot.send_message(chat_id=chat_id, text=reply_text)

    except Exception:
        logger.exception("Error handling Telegram webhook")

    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)