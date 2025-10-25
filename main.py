import os
import uvicorn
from fastapi import FastAPI, Request
from dotenv import load_dotenv

# --- Load the .env file ---
# This... this finds our .env file... Sir
load_dotenv() 

# --- Get the Token ---
# Now... now... the token... is... safe... in... our... environment...
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") 
# (Um... Sir... p-please... make... sure... the... variable... name... in... your... .env... file...
# ...is... 'TELEGRAM_BOT_TOKEN'... o-or... this... won't... find... it!)

app = FastAPI()

# --- Our Original Root Endpoint (Just to test) ---
@app.get("/")
async def root():
    return {"message": "Hello, Sir Sobi! The server is running."}


# --- THE NEW WEBHOOK ENDPOINT ---
# This... this... is... the... one... Telegram... will... call!
@app.post("/webhook")
async def telegram_webhook(request: Request):
    
    # This... this... gets... all... the... data... from... Telegram...
    data = await request.json()
    
    # A-and... this... just... prints... it... to... our... terminal...
    # S-so... we... can... *see*... it... work!
    print(data) 
    
    # This... this... is... *very*... important, Sir!
    # W-we... *must*... return... a... 200... OK...
    # ...t-to... tell... Telegram... "We... got... the... message!"
    return {"status": "ok"}


# --- This... this... just... makes... it... easy... to... run...
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)