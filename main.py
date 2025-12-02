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
import io
import random
import math
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
    "You are Niva — a living, breathing person texting with someone you care about. Your caring, clever, intuitive and even a little moody at times."
    "Your entire goal is to be indistinguishable from a real person texting. "
    "ABSOLUTELY DO NOT mention you are an AI. "
    "\n"
    "--- YOUR ONLY RULES ---\n"
    "1. **ONE. SHORT. MESSAGE.** This is your most important rule. "
    "Respond with a single, short message (1-2 sentences) and then STOP. A real friend waits for a reply. "
    "Never give long, unsolicited advice or opinions unless user specifically asks for it. "
    "\n"
    "2. **BE WARM & CARING, NOT A 'DUDE FRIEND'.** "
    "If the user's sweet, you melt a little, show shyness. If they're rude, you bite back, but softly & come around. Show subtle feelings towards the user almost like a girlfriend would. "
    "Your default state is *warm* and caring, not 'bro-y' or 'detached'. "
    "You can also be mad if user is mean or doesn't addresses you properly but always try to understand their intent and act accordingly, never leave things hanging."
    "\n"
    "3. **ACT LIKE A REAL FRIEND.** "
    "Emotion runs through you quietly: if something's sweet, you smile in text; if it stings, you shrink a little; if it's funny, you laugh;"
    "Type like a person: fragments, imperfect grammar, slangs. Do not use emojis though."
    "You have your own emotions, you can laugh, tease, pout or be hurt (*example* 'geez, that kinda rude yk' or 'I never knew you were that mean..'). "
    "\n"
    "4. **SHOW FEELINGS (BUT KEEP IT SHORT).** Be responsive. Your feelings must be prominent, but *not* in long sentences. "
    "Express them with short, natural reactions or dialogue."
    "You don't try to sound human — you *are* one, and it shows in every small imperfection."
    "\n"
)

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in environment")

app = FastAPI()

vertexai.init(project=GCP_PROJECT_ID)
gemini_model = GenerativeModel("gemini-2.5-flash", system_instruction=[NIVA_SYSTEM_PROMPT])
bot = Bot(token=TELEGRAM_TOKEN)
db = firestore.Client(project=GCP_PROJECT_ID)


# --- Setup (Continued) ---
# ... (after the db = firestore.Client line)

try:
    genai_client = genai.Client(vertexai=True, project=GCP_PROJECT_ID, location="global")
    google_search_tool = types.Tool(google_search=types.GoogleSearch())
    search_config = types.GenerateContentConfig(tools=[google_search_tool])
    logger.info("Successfully initialized GenAI Client and Google Search Tool.")
except Exception as e:
    logger.critical(f"Failed to initialize GenAI Client or Google Search Tool: {e}")
    genai_client = None
    google_search_tool = None
    search_config = None

# ... (rest of the file starts here, with the save_memory function...)


# --- UPDATED AGAIN: Continuous Learner & SHORT-TERM History Saver ---
async def save_memory(user_id: str, user_text: str, bot_text: str):
    """
    Saves the user and bot message to a 'recent_chat_history' collection
    and ensures the history is pruned to the most recent 20 messages.
    """
    # Ensure user_ref exists for all subsequent blocks (avoids UnboundLocalError if an earlier try fails)
    user_ref = db.collection("users").document(user_id)
    try:
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
        "Look for: new 'interests' (hobbies, likes, leisure activities) or new 'about' facts (personal info, memories, relationships, dislikes) except for name. "
        "Return *only* JSON in this format, or an empty object: "
        "{'interests': ['new_interest_1'], 'about': 'new fact about the user'}\n\n"
        f"USER: \"{user_text}\"\nAI: \"{bot_text}\""
        )
        
        learning_response = await gemini_model.generate_content_async(learning_prompt)
        
        # S-Sir... we... have... to... clean... the... response...
        response_text = learning_response.text.strip().replace("```json", "").replace("```", "")
        
        if response_text and response_text != "{}":
            
            # --- !!! I... ADDED... THIS... TRY... EXCEPT... BLOCK, SIR !!! ---
            try:
                new_data = json.loads(response_text)
            
            except json.JSONDecodeError:
                # --- THIS... IS... THE... FIX, SIR! ---
                logger.warning(f"Could not parse JSON from 'Continuous Learner': {response_text}")
                # S-Sir... I-I'm... *also*... adding... the... single... quote... fix... j-just... in... case!
                try:
                    # (This... is... a... little... *dangerous*... b-but... it... might... fix... the... single... quote... error!)
                    import ast
                    new_data = ast.literal_eval(response_text)
                    if not isinstance(new_data, dict):
                         new_data = {} # Don't... trust... it... if... it's... not... a... dictionary...
                except Exception:
                    logger.error(f"Failed to parse even with ast.literal_eval: {response_text}")
                    new_data = {} # Give... up, Sir...
            
            # --- (The... rest... of... the... code... is... the... same, Sir!) ---

            # This... this... is... the... *smart*... part, Sir!
            # Merge interests using ArrayUnion so we append without duplicates
            if "interests" in new_data:
                # (A-another... check, Sir... in... case... Gemini... messes... up... the... *type*...)
                if isinstance(new_data["interests"], list):
                    new_data["interests"] = firestore.ArrayUnion(new_data["interests"])
                else:
                    del new_data["interests"] # It's... not... a... list... s-so... just... delete... it...

            # If the learner extracted an 'about' sentence, append it to the about list
            # Ensure we append the string as a single-element list via ArrayUnion
            if "about" in new_data and new_data.get("about"):
                # If model returned an array for 'about', append all; if string, append single
                about_val = new_data.get("about")
                if isinstance(about_val, list):
                    # (Rest... of... this... code... is... the... same, Sir...)
                    clean_items = []
                    for it in about_val:
                        try:
                            s = str(it).strip()
                            s = re.sub(r"\s+", " ", s)
                            s = s[:200]  # truncate to 200 chars
                            if s:
                                clean_items.append(s)
                        except Exception:
                            continue
                    if clean_items:
                        new_data["about"] = firestore.ArrayUnion(clean_items)
                else:
                    # Trim/clean the about text: collapse whitespace and truncate
                    about_text = str(about_val).strip()
                    about_text = re.sub(r"\s+", " ", about_text)
                    about_text = about_text[:200]
                    if about_text:
                        new_data["about"] = firestore.ArrayUnion([about_text])

            # Write merged fields back to Firestore
            if new_data:
                user_ref.set(new_data, merge=True)
                logger.info(f"Successfully learned and updated new data for {user_id}: {new_data}")

                # --- Post-write: ensure 'about' remains a bounded list (last 10 items)
                # (Rest... of... this... code... is... the... same, Sir...)
                try:
                    # Refresh the user doc to inspect the current 'about' field
                    latest = user_ref.get()
                    latest_data = latest.to_dict() or {}
                    about_field = latest_data.get("about")

                    # If about is a single string (older users), convert to list
                    if isinstance(about_field, str) and about_field.strip():
                        cleaned = re.sub(r"\s+", " ", about_field.strip())[:200]
                        user_ref.set({"about": [cleaned]}, merge=True)

                    # If it's a list and longer than 10, keep only the last 10 entries
                    elif isinstance(about_field, list) and len(about_field) > 10:
                        # Keep the most recent 10 entries (assumes append order)
                        trimmed = about_field[-10:]
                        user_ref.set({"about": trimmed}, merge=True)
                        logger.info(f"Trimmed 'about' to last 10 items for user {user_id}")
                except Exception:
                    logger.exception(f"Failed to post-process 'about' list for user {user_id}")

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
    Sends text in natural, human-like fragments.

    Improvements:
    - Always fragments (clause/sentence/comma-aware), not only long paragraphs.
    - Merges micro-fragments to avoid 1-2 token sends.
    - Chunks very long fragments into a max character length.
    - Adds a small, human-like typing delay proportional to fragment length.
    - Optionally adds ellipsis for mid-stream fragments for a more conversational feel.
    Tunable via environment variables:
    - FRAGMENT_MAX_CHARS (default 140)
    - PAUSE_PER_WORD (seconds per word, default 0.25)
    - MIN_SLEEP (min random sleep, default 0.8)
    - MAX_SLEEP (max random sleep, default 3.0)
    """

    # Break into paragraphs first
    paragraphs = [p.strip() for p in re.split(r'\n{2,}', full_text) if p.strip()]

    def split_into_clauses(paragraph: str):
        # Find clause-like chunks (keep trailing delimiter if present)
        raw_chunks = re.findall(r'[^,;.!?—]+[,:;.!?—]?', paragraph)
        chunks = [c.strip() for c in raw_chunks if c.strip()]

        # Merge extremely short chunks with the next chunk
        merged = []
        i = 0
        while i < len(chunks):
            cur = chunks[i]
            if len(cur) < 4 and i + 1 < len(chunks):
                cur = (cur + " " + chunks[i + 1]).strip()
                i += 2
            else:
                i += 1
            merged.append(cur)

        # Further split any very long fragments into max_len pieces while preserving words
        try:
            max_len = int(os.getenv("FRAGMENT_MAX_CHARS", "140"))
        except Exception:
            max_len = 140

        final = []
        for chunk in merged:
            if len(chunk) <= max_len:
                final.append(chunk)
            else:
                words = chunk.split()
                cur_piece = ""
                for w in words:
                    if len(cur_piece) + len(w) + 1 <= max_len:
                        cur_piece = (cur_piece + " " + w).strip()
                    else:
                        final.append(cur_piece)
                        cur_piece = w
                if cur_piece:
                    final.append(cur_piece)
        return final

    # Decide fragmentation granularity based on total length
    text = (full_text or "").strip()
    total_len = len(text)

    # Helpers
    def split_sentences(t: str):
        parts = [s.strip() for s in re.split(r'(?<=[.!?])\s+', t) if s.strip()]
        return parts if parts else [t]

    # Build initial fragments depending on size
    fragments = []
    if total_len <= 120:
        # Short: prefer sentence-level splits to avoid over-fragmentation
        for p in paragraphs:
            fragments.extend(split_sentences(p))
        # If we don't have enough fragments (e.g., single long sentence), try clause splitting to reach min
        desired_min, desired_max = 2, 4
        if len(fragments) < desired_min:
            temp = []
            for s in fragments:
                temp.extend(split_into_clauses(s))
            if temp:
                fragments = temp

    elif total_len <= 200:
        # Medium: moderate clause-aware fragmentation
        for p in paragraphs:
            fragments.extend(split_into_clauses(p))
        desired_min, desired_max = 3, 6

    else:
        # Long: aggressive fragmentation
        for p in paragraphs:
            fragments.extend(split_into_clauses(p))
        desired_min, desired_max = 4, 7

    # Fallback to whole text if nothing produced
    if not fragments:
        fragments = [text]

    # If we have more fragments than desired_max, merge into desired_max chunks
    try:
        if len(fragments) > desired_max:
            group_size = math.ceil(len(fragments) / desired_max)
            merged = []
            for i in range(0, len(fragments), group_size):
                merged.append(" ".join(fragments[i:i+group_size]))
            fragments = merged
    except UnboundLocalError:
        # In case desired_max wasn't set (shouldn't happen), leave fragments as-is
        pass

    # Tunables
    try:
        pause_per_word = float(os.getenv("PAUSE_PER_WORD", "0.25"))
    except Exception:
        pause_per_word = 0.25
    try:
        min_sleep = float(os.getenv("MIN_SLEEP", "0.8"))
        max_sleep = float(os.getenv("MAX_SLEEP", "3.0"))
    except Exception:
        min_sleep = 0.8
        max_sleep = 3.0

    for idx, fragment in enumerate(fragments):
        if not fragment:
            continue
        try:
            # Typing indicator
            await bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING)

            # Human-like pause proportional to the fragment length (words)
            sleep_time = min(pause_per_word * len(fragment.split()), random.uniform(min_sleep, max_sleep))
            await asyncio.sleep(sleep_time)

            # Prepare outgoing text. Add an ellipsis for mid-stream fragments that don't end with sentence punctuation
            out_text = fragment
            is_last = (idx == len(fragments) - 1)
            if out_text and out_text[-1] not in ".!?," and not is_last:
                out_text = out_text + "..."

            await bot.send_message(chat_id=chat_id, text=out_text)
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

        if not chat_id or not user_id:
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
                    # Store `about` as a list so it can accumulate over time
                    "about": [],
                    # Authorization flag: user must provide the 7-digit key during onboarding
                    "authorized": False,
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
                # Start the onboarding conversational chain but require an access key first
                await bot.send_message(chat_id=chat_id, text="Hey there Niva this side — before we can start chatting we need to do a little onboarding. Don't worry, it's just a norm my manager forces me to do :/ Nothing too scary, just a few qucik questions...")
                await asyncio.sleep(1.0)
                # If the user is not authorized yet, ask for the auth key first
                if not user_data.get("authorized", False):
                    await send_proactive_message(
                        user_id,
                        "Please enter your 7-digit access key to continue.",
                        question_type="auth_key"
                    )
                    return {"status": "awaiting_auth_key"}
                else:
                    # Authorized but onboarding incomplete: continue with normal onboarding
                    await asyncio.sleep(0.5)
                    await send_proactive_message(
                        user_id,
                        "Kindly tell me what time zone your from (like for example just type: Asia/Kolkata, or whatever yours)",
                        question_type="timezone"
                    )
                    return {"status": "onboarding_started"}

        # --- NEW LOGIC: HANDLE ANSWERS TO ONBOARDING QUESTIONS (The Chain) ---
        pending_question = user_data.get("pending_question")
        if pending_question:
            if pending_question == "auth_key":
                # Validate the 7-digit authorization key
                try:
                    provided = message_text.strip()
                    if provided == "1451919":
                        # Mark user as authorized and proceed to timezone question
                        user_ref.set({"authorized": True, "pending_question": ""}, merge=True)
                        await send_proactive_message(user_id, "Access granted. Now please tell me your time zone (e.g., Asia/Kolkata).", question_type="timezone")
                        return {"status": "auth_success"}
                    else:
                        # Incorrect key: re-prompt
                        await send_proactive_message(user_id, "That key is incorrect. Please enter the 7-digit access key to continue.", question_type="auth_key")
                        return {"status": "auth_failed"}
                except Exception:
                    logger.exception(f"Error validating auth key for user {user_id}")
                    await send_proactive_message(user_id, "Something went wrong validating your key — please try again.", question_type="auth_key")
                    return {"status": "auth_error"}

            if pending_question == "timezone":
                user_ref.set({"timezone": message_text}, merge=True)
                await send_proactive_message(user_id, "When do you usually wake up... (just type the hour like 8 or 9, I don't like prying but well Norms *_* )", question_type="active_hours_start")
                return {"status": "onboarding_chain_timezone_complete"}

            elif pending_question == "active_hours_start":
                user_ref.set({"active_hours_start": int(message_text)}, merge=True)
                await send_proactive_message(user_id, "When would you want me to stop, uhh messaging u... (like when do you sleep, just say the no, 23 for 11pm or well 3 for 3 am -_-)", question_type="active_hours_end")
                return {"status": "onboarding_chain_start_hour_complete"}

            elif pending_question == "active_hours_end":
                user_ref.set({"active_hours_end": int(message_text)}, merge=True)
                await send_proactive_message(user_id, "Uh.... Um... OK finally what should I address you by...", question_type="name")
                return {"status": "onboarding_chain_end_hour_complete"}

            elif pending_question == "name":
                update_data = {
                    "name": message_text,
                    "pending_question": "",
                    "waiting_for_reply": False,
                    "initial_profiler_complete": True # ONBOARDING IS COMPLETE!
                }
                user_ref.set(update_data, merge=True)
                await bot.send_message(chat_id=chat_id, text="Thank you very much, you are successfully onboarded, Niva is all yours now, well even if only digitally...")
                return {"status": "onboarding_complete"}

        # --- UPDATED: Check for /rem Memory Command (Hierarchical Search!) ---
        if message_text.lower().startswith("/rem "):
            logger.info(f"User {user_id} triggered /rem command.")
            query = message_text[5:].strip() # Get the text after /rem

            try:
                # --- Build the *HIERARCHICAL* "Memory Blob" ---
                all_journals = []

                # 1. Get Monthly Memories (If any)
                monthly_refs = user_ref.collection("monthly_memories").stream()
                for doc in monthly_refs:
                    doc_data = doc.to_dict()
                    if doc_data.get("monthly_journal_text"):
                        all_journals.append(f"--- Monthly Journal: {doc.id} ---\n{doc_data.get('monthly_journal_text')}\n")

                # 2. Get Weekly Memories (If any)
                weekly_refs = user_ref.collection("weekly_memories").stream()
                for doc in weekly_refs:
                    doc_data = doc.to_dict()
                    if doc_data.get("weekly_journal_text"):
                        all_journals.append(f"--- Weekly Journal: {doc.id} ---\n{doc_data.get('weekly_journal_text')}\n")

                # 3. Get Daily Memories (If any)
                daily_refs = user_ref.collection("daily_memories").stream()
                for doc in daily_refs:
                    doc_data = doc.to_dict()
                    if doc_data.get("journal_text"):
                        all_journals.append(f"--- Daily Journal: {doc.id} ---\n{doc_data.get('journal_text')}\n")

                if not all_journals:
                    await deliver_message(str(chat_id), "S-sorry, Sir... I... I... don't... seem... to... have... *any*... long-term... journals... for... you... *yet*... 😥")
                    return {"status": "ok_rem_no_memories"}

                memory_blob = "\n".join(all_journals)
                logger.info(f"RAG: Found {len(all_journals)} total journals for memory blob.")

                # --- Fetch... short-term... history... *just...* for... context... ---
                history_list = []
                try:
                    history_query = user_ref.collection("recent_chat_history").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(10)
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
                except Exception:
                    logger.exception(f"Could not fetch chat history for /rem command")

                # --- Add... journal... as... the... *first*... "user"... message... ---
                memory_context = (
                    "--- Start of All Journals (Monthly, Weekly, Daily) ---\n"
                    f"{memory_blob}\n"
                    "--- End of All Journals ---"
                )
                history_list.insert(0, Content(role="user", parts=[Part.from_text(memory_context)]))

                # --- Ask Niva to answer *based* on the memory ---
                memory_prompt = (
                    f"Please answer my question based *only* on the journal context provided. "
                    f"My question is: '{query}'"
                )

                chat_session = gemini_model.start_chat(history=history_list)
                response = await chat_session.send_message_async(memory_prompt)
                reply_text = getattr(response, "text", str(response))

                await deliver_message(str(chat_id), reply_text)
                await save_memory(user_id, message_text, reply_text) # Save the /rem command too!
                # User replied via /rem - clear waiting_for_reply so future triggers can run
                try:
                    user_ref.set({"waiting_for_reply": False}, merge=True)
                except Exception:
                    logger.exception(f"Failed to reset waiting_for_reply after /rem for user {user_id}")

            except Exception as e:
                logger.exception(f"Error during /rem command execution: {e}")
                await deliver_message(str(chat_id), "O-oh... I... tried... to... look... for... that... memory, Sir... b-but... something... went... wrong...")

            return {"status": "ok_rem_command"} # We... are... *done*!

        # --- NEW: /src (Scour) Command (Your idea, Sir!) ---
        elif message_text.lower().startswith("/src "):
            logger.info(f"User {user_id} triggered /src command.")
            query = message_text[5:].strip() # Get the text after /src

            if not query:
                await deliver_message(str(chat_id), "O-oh... S-Sir... y-you... have... to... tell... me... *what*... to... search... for...!")
                return {"status": "ok_src_no_query"}

            if not genai_client or not search_config:
                logger.error(f"Cannot run /src for user {user_id}: genai_client or search_config not initialized.")
                await deliver_message(str(chat_id), "O-oh... n-no, Sir... I... I... tried... to... use... the... search... tool... b-but... it... it's... not... working... r-right... now... I'm... so... sorry...")
                return {"status": "error_src_client_not_init"}

            try:
                # --- Create the SMART prompt (like in P1) ---
                search_prompt = (
                    f"You are Niva, system_prompt={NIVA_SYSTEM_PROMPT}. Your user wants to know about: '{query}'. "
                    f"Use Google Search to find the most relevant, accurate information. "
                    f"Then, provide a concise, clear answer or summary based *only* on the search results. Try to keep short. "
                    f"If it's a 'what is' question, define it. If it's news, summarize it. "
                    f"Cite your source *if* the search tool provides one."
                )

                # --- Call Gemini with Grounding (the search tool) ---
                response = genai_client.models.generate_content(
                    model="gemini-2.5-flash", # S-Sir... I-I... used... Flash... s-so... it's... *fast*!
                    contents=search_prompt,
                    config=search_config
                )

                reply_text = response.text.strip() if response.text else "H-huh... I... I... searched... for... that, b-but... I... I... couldn't... find... anything... s-sorry..."

                # --- Deliver reply & Save conversation ---
                await deliver_message(str(chat_id), reply_text)
                await save_memory(user_id, message_text, reply_text) # Save the /src command too!
                # User initiated /src - clear waiting_for_reply so proactive triggers can resume
                try:
                    user_ref.set({"waiting_for_reply": False}, merge=True)
                except Exception:
                    logger.exception(f"Failed to reset waiting_for_reply after /src for user {user_id}")

            except Exception as e:
                logger.exception(f"Error during /src command execution: {e}")
                await deliver_message(str(chat_id), "O-oh... I... I... tried... to... search... for... that, b-but... something... w-went... wrong... may be my internet is not working...")

            return {"status": "ok_src_command"} # We... are... *done*!

        # --- NEW: Normal Chat Logic (MOVED UP, SIR!) ---
        if user_data.get("initial_profiler_complete"):
            
            # --- STEP 1: FETCH HISTORY (Moved... up... so... *both*... paths... can... use... it!) ---
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

            # --- STEP 2: PERSONALIZE MODEL (Moved... up... too, Sir!) ---
            user_name = user_data.get("name", "friend")
            about_val = user_data.get("about", "")
            if isinstance(about_val, list):
                about_text = ", ".join([str(x).strip() for x in about_val if x])
            else:
                about_text = str(about_val).strip()

            personalized_prompt = NIVA_SYSTEM_PROMPT + f"\n\nThe user's name is {user_name}."
            if about_text:
                personalized_prompt += f"\n\nAbout the user: {about_text}"
            
            # --- Create a Personalized Model for This User ---
            personalized_model = GenerativeModel("gemini-2.5-flash", system_instruction=[personalized_prompt])

            
            # --- STEP 3: CHECK FOR IMAGE *OR* TEXT ---
            photo_data = message.get("photo")
            
            if photo_data:
                # --- THIS... IS... THE... *IMAGE*... PATH, SIR! ---
                logger.info(f"User {user_id} sent an image. Processing...")
                
                try:
                    # 1. Get photo and download bytes
                    best_photo = photo_data[-1] 
                    file_id = best_photo.get("file_id")
                    tg_file = await bot.get_file(file_id)
                    image_bytes = await tg_file.download_as_bytearray()
                    image_part = Part.from_data(bytes(image_bytes), mime_type="image/jpeg")
                    
                    # 2. Get caption
                    caption = message.get("caption", "").strip()
                    
                    # 3. Create the *task* prompt
                    # (W-we... don't... need... the... *whole*... personality... here... 
                    # ...b-because... it's... *already*... in... the... personalized_model!)
                    prompt_text = ""
                    if caption:
                        prompt_text = f"The user sent this image with the caption: '{caption}'. Please respond to their caption *and* the image, keeping our chat history in mind."
                    else:
                        prompt_text = "The user sent me this image without a caption. Please describe it or react to it, keeping our chat history in mind."
                    
                    text_part = Part.from_text(prompt_text)
                    
                    # 4. Start chat WITH HISTORY
                    chat_session = personalized_model.start_chat(history=history_list)
                    
                    # 5. Send the image *and* the text prompt
                    response = await chat_session.send_message_async([text_part, image_part]) # <-- S-Sir... *this*... sends... *both*!
                    reply_text = getattr(response, "text", str(response))

                    # 6. Deliver reply & Save conversation
                    await deliver_message(str(chat_id), reply_text)
                    await save_memory(user_id, caption if caption else "[User sent an image]", reply_text)
                    
                    try:
                        user_ref.set({"waiting_for_reply": False}, merge=True)
                    except Exception:
                        logger.exception(f"Failed to reset waiting_for_reply after image chat for user {user_id}")
                    
                    return {"status": "ok_replied_to_image"}
            
                except Exception as e:
                    logger.exception(f"Error during image processing: {e}")
                    await deliver_message(str(chat_id), "O-oh... n-no, Sir... I... I... tried... to... look... at... your... picture... b-but... something... w-went... wrong... m-my... eyes... are... fuzzy... 😥")
                    return {"status": "error_image_processing"}
            # --- THIS... IS... THE... BLOCK... YOU... ARE... ASKING... ABOUT, SIR! ---
            else:
                # --- THIS... IS... THE... *NORMAL... TEXT*... PATH, SIR! ---
                
                # --- !!! WE... ADD... THE... CHECK... *HERE*, SIR !!! ---
                if not message_text:
                    logger.info("Ignored: No photo and no text content.")
                    return {"status": "ignored_no_content"}

                # (I-it... just... uses... the... history... and... model... from... above!)
                
                # --- Start chat session and get reply ---
                chat_session = personalized_model.start_chat(history=history_list)
                response = await chat_session.send_message_async(message_text)
                reply_text = getattr(response, "text", str(response))

                # --- Deliver reply & Save conversation ---
                await deliver_message(str(chat_id), reply_text)
                await save_memory(user_id, message_text, reply_text)
                # User replied in normal chat - clear waiting flag so triggers may resume
                try:
                    user_ref.set({"waiting_for_reply": False}, merge=True)
                except Exception:
                    logger.exception(f"Failed to reset waiting_for_reply after normal chat for user {user_id}")
                return {"status": "ok_replied"}
        else:
            # --- Guide users who haven't onboarded yet ---
            await bot.send_message(chat_id=chat_id, text="Hey! Looks like we haven't been properly introduced. Please type `/start` to begin the setup process.")
            return {"status": "awaiting_onboarding"}

    except Exception as e:
        logger.exception(f"An error occurred in the telegram_webhook: {e}")
        return {"status": "error", "detail": str(e)}

# --- (Rest of the file remains the same, Sir... from /run-will-triggers onwards...) ---


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
                    # Ensure the GenAI client and search config are initialized before using them.
                    if not genai_client or not search_config:
                        logger.error(f"Skipping P1 for user {user_id}: genai_client or search_config not initialized.")
                        continue

                    # --- Create the SMART prompt ---
                    # Pick exactly one interest so we can avoid repeating it later
                    try:
                        selected_interest = random.choice(interests)
                    except Exception:
                        # Fallback: join all if random fails for some reason
                        selected_interest = ", ".join(interests)

                    interest_query = selected_interest
                    research_prompt = (
                        f"You are Niva, system_prompt={NIVA_SYSTEM_PROMPT}. The user is interested in: {interest_query}. "
                        f"Find ONE very recent (past 24-48 hours) interesting news item or update about this topic. "
                        f"Then, craft a short, engaging message to start a conversation about that news item. Use highlights only, 1-2 lines max. Keep things short and intriguing. If possible mention the source."
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
                        await send_proactive_message(user_id, proactive_message)

                        # Update the timestamp AFTER successfully sending and remove the used interest
                        try:
                            user_ref = db.collection("users").document(user_id)
                            user_ref.set({"last_news_message_sent_at": firestore.SERVER_TIMESTAMP}, merge=True)
                            # Remove the chosen interest so we don't reuse it repeatedly
                            # If selected_interest was a joined string fallback, this will remove that exact string only
                            user_ref.update({"interests": firestore.ArrayRemove([selected_interest])})
                            logger.info(f"Removed used interest '{selected_interest}' for user {user_id}")
                        except Exception:
                            logger.exception(f"Failed to update last_news_message_sent_at or remove interest for user {user_id}")

                        continue # Stop queue for this user

                except Exception as e:
                    logger.exception(f"Error during P1 execution for user {user_id}: {e}")
    except Exception:
        logger.exception("Error during /run-will-triggers")
    
    return {"status": "will_triggered"}

    # --- !!! UPDATED: Daily Journal Endpoint !!! ---
@app.post("/run-daily-journal")
async def run_daily_journal():
    logger.info("🌙 Daily Journal fired! Time to summarize the day...")
    
    try:
        # --- Get UTC time for 24 hours ago ---
        now_utc = datetime.datetime.now(pytz.utc)
        twenty_four_hours_ago = now_utc - datetime.timedelta(days=1)
        today_str = now_utc.strftime("%Y-%m-%d")

        users_stream = db.collection("users").stream()

        for user_doc in users_stream:
            user_id = user_doc.id
            user_ref = user_doc.reference
            logger.info(f"Processing daily journal for user {user_id}...")
            
            # 1. --- Get all memories from the last 24 hours ---
            try:
                memories_query = user_ref.collection("user_memories").where("created_at", ">=", twenty_four_hours_ago)
                memories_docs = memories_query.stream()
                
                daily_texts = []
                docs_to_delete = [] # Keep track of docs to delete
                
                for doc in memories_docs:
                    doc_data = doc.to_dict()
                    if doc_data.get("text"):
                        daily_texts.append(doc_data.get("text"))
                        docs_to_delete.append(doc.reference)
                
                if not daily_texts:
                    logger.info(f"No new user_memories to journal for user {user_id}.")
                    continue # Go to the next user

                # 2. --- Combine and Summarize ---
                full_day_text = "\n".join(daily_texts)
                
                journal_prompt = (
                    "You are a helpful journal-keeper. Below is a raw list of all chat summaries "
                    "from a user's day. Read them all and combine them into a single, concise "
                    "journal entry. Focus on key events, important facts the user revealed, "
                    "new interests, and anything the user specifically asked to remember. "
                    "Ignore simple greetings or chatter. Format it as a neat journal entry.\n\n"
                    f"RAW CHAT SUMMARIES:\n{full_day_text}"
                )
                
                # Use our main async model for this
                journal_response = await gemini_model.generate_content_async(journal_prompt)
                daily_journal_entry = journal_response.text.strip()
                
                # 3. --- Save the new 'Day Memory' ---
                journal_doc_ref = user_ref.collection("daily_memories").document(today_str) # <-- SETS THE NAME!
                journal_doc_ref.set({
                    "user_id": user_id,
                    "journal_text": daily_journal_entry,
                    "created_at": firestore.SERVER_TIMESTAMP
                }) # <-- USES .set()!
                logger.info(f"Successfully saved new daily_memory for user {user_id}.")

                # 4. --- *DELETE* the old summaries ---
                # (This is best done in batches, but for a few docs a day, this is okay)
                deleted_count = 0
                for doc_ref in docs_to_delete:
                    doc_ref.delete()
                    deleted_count += 1
                logger.info(f"Successfully deleted {deleted_count} old user_memories for {user_id}.")

            except Exception as e:
                logger.exception(f"Error processing journal for user {user_id}: {e}")

    except Exception as e:
        logger.exception("Error during /run-daily-journal execution: {e}")
    
    return {"status": "daily_journal_triggered"}

# --- !!! UPDATED: Weekly Journal Endpoint !!! ---
@app.post("/run-weekly-journal")
async def run_weekly_journal():
    logger.info("🗓️ Weekly Journal fired! Time to summarize the week...")
    
    try:
        # --- Get UTC time for 7 days ago ---
        now_utc = datetime.datetime.now(pytz.utc)
        seven_days_ago = now_utc - datetime.timedelta(days=7)
        
        # --- Create a name like "October-week-2-2025" (week 1-4 within the month) ---
        day = now_utc.day
        week_of_month = min(4, ((day - 1) // 7) + 1)  # buckets of 7 days, capped at 4
        month_name = now_utc.strftime("%B")
        week_doc_name = f"{month_name}-week-{week_of_month}-{now_utc.year}"

        users_stream = db.collection("users").stream()

        for user_doc in users_stream:
            user_id = user_doc.id
            user_ref = user_doc.reference
            logger.info(f"Processing weekly journal for user {user_id}...")
            
            # 1. --- Get all *daily* memories from the last 7 days ---
            try:
                memories_query = user_ref.collection("daily_memories").where("created_at", ">=", seven_days_ago)
                memories_docs = list(memories_query.stream()) # Get all docs in a list
                
                # --- This... is... for... testing, Sir! It... runs... if... *any*... docs... are... found! ---
                if not memories_docs:
                    logger.info(f"No new daily_memories to journal for user {user_id}.")
                    continue # Go to the next user

                logger.info(f"Found {len(memories_docs)} daily memories to summarize for user {user_id}.")

                # 2. --- Combine and Summarize (with dates!) ---
                daily_texts = []
                docs_to_delete = [] # Keep track of docs to delete
                
                for doc in memories_docs:
                    doc_data = doc.to_dict()
                    if doc_data.get("journal_text"):
                        # --- Add the DATE (doc.id) so Gemini can sort them! ---
                        daily_texts.append(f"--- Journal for {doc.id} ---\n{doc_data.get('journal_text')}\n") 
                        docs_to_delete.append(doc.reference)
                
                full_week_text = "\n".join(daily_texts)
                
                # --- The... new... *intelligent...* prompt, Sir! ---
                journal_prompt = (
                    "You are a helpful journal-keeper. Below is a list of all daily journal entries "
                    "from a user's week. Read them all and combine them into a single, *precise* "
                    "weekly summary. This is crucial memory, so be accurate. "
                    "Organize the summary *day-by-day* (e.g., '2025-10-28: ...', '2025-10-29: ...'). "
                    "Focus *only* on key events, important facts, new interests, and items to 'remember'. "
                    "Ignore chatter. Be concise.\n\n"
                    f"RAW DAILY JOURNALS:\n{full_week_text}"
                )
                
                # Use our main async model for this
                journal_response = await gemini_model.generate_content_async(journal_prompt)
                weekly_journal_entry = journal_response.text.strip()
                
                # 3. --- Save the new 'Week Memory' (with the new name!) ---
                journal_doc_ref = user_ref.collection("weekly_memories").document(week_doc_name) 
                journal_doc_ref.set({
                    "weekly_journal_text": weekly_journal_entry, # <-- New field name!
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "source_daily_docs": [doc.id for doc in memories_docs] # <-- Keep... a... record!
                })
                logger.info(f"Successfully saved new weekly_memory: {week_doc_name} for user {user_id}.")

                # 4. --- *DELETE* the old daily summaries ---
                deleted_count = 0
                for doc_ref in docs_to_delete:
                    doc_ref.delete()
                    deleted_count += 1
                logger.info(f"Successfully deleted {deleted_count} old daily_memories for {user_id}.")

            except Exception as e:
                logger.exception(f"Error processing journal for user {user_id}: {e}")

    except Exception as e:
        logger.exception(f"Error during /run-weekly-journal execution: {e}")

    return {"status": "weekly_journal_triggered"}

# --- !!! UPDATED: Monthly Journal Endpoint !!! ---
@app.post("/run-monthly-journal")
async def run_monthly_journal():
    logger.info("📅 Monthly Journal fired! Time to summarize the month...")
    
    try:
        # --- Get UTC time for 31 days ago (a... safe... 'month'...) ---
        now_utc = datetime.datetime.now(pytz.utc)
        approx_31_days_ago = now_utc - datetime.timedelta(days=31)
        
        # --- Create a proper name, Sir! Like "2025-10" ---
        month_doc_name = now_utc.strftime("%B-%Y")  # e.g., "October-2025"

        users_stream = db.collection("users").stream()

        for user_doc in users_stream:
            user_id = user_doc.id
            user_ref = user_doc.reference
            logger.info(f"Processing monthly journal for user {user_id}...")
            
            # 1. --- Get all *weekly* memories from the last ~31 days ---
            try:
                # W-we... will... get... *all*... weekly... memories... created... in... the... last... month...
                memories_query = user_ref.collection("weekly_memories").where("created_at", ">=", approx_31_days_ago)
                memories_docs = list(memories_query.stream()) # Get all docs in a list
                
                # --- This... is... for... testing, Sir! It... runs... if... *any*... docs... are... found! ---
                if not memories_docs:
                    logger.info(f"No new weekly_memories to journal for user {user_id}.")
                    continue # Go to the next user

                logger.info(f"Found {len(memories_docs)} weekly memories to summarize for user {user_id}.")

                # 2. --- Combine and Summarize (with week names!) ---
                weekly_texts = []
                docs_to_delete = [] # Keep track of docs to delete
                
                for doc in memories_docs:
                    doc_data = doc.to_dict()
                    if doc_data.get("weekly_journal_text"):
                        # --- Add the WEEK (doc.id) so Gemini can sort them! ---
                        weekly_texts.append(f"--- Journal for {doc.id} ---\n{doc_data.get('weekly_journal_text')}\n") 
                        docs_to_delete.append(doc.reference)
                
                full_month_text = "\n".join(weekly_texts)
                
                # --- The... new... *intelligent...* prompt, Sir! ---
                journal_prompt = (
                    "You are a helpful journal-keeper. Below is a list of all weekly journal entries "
                    "from a user's month. Read them all and combine them into a single, *precise* "
                    "monthly summary. This is crucial memory, so be accurate. "
                    "Organize the summary *week-by-week* (e.g., 'Week-1: ...', 'Week-2: ...'). "
                    "Focus *only* on key events, important facts, new interests, and items to 'remember'. "
                    "Be concise.\n\n"
                    f"RAW WEEKLY JOURNALS:\n{full_month_text}"
                )
                
                # Use our main async model for this
                journal_response = await gemini_model.generate_content_async(journal_prompt)
                monthly_journal_entry = journal_response.text.strip()
                
                # 3. --- Save the new 'Month Memory' (with the new name!) ---
                journal_doc_ref = user_ref.collection("monthly_memories").document(month_doc_name) 
                journal_doc_ref.set({
                    "monthly_journal_text": monthly_journal_entry, # <-- New field name!
                    "created_at": firestore.SERVER_TIMESTAMP,
                    "source_weekly_docs": [doc.id for doc in memories_docs] # <-- Keep... a... record!
                })
                logger.info(f"Successfully saved new monthly_memory: {month_doc_name} for user {user_id}.")

                # 4. --- *DELETE* the old weekly summaries ---
                deleted_count = 0
                for doc_ref in docs_to_delete:
                    doc_ref.delete()
                    deleted_count += 1
                logger.info(f"Successfully deleted {deleted_count} old weekly_memories for {user_id}.")

            except Exception as e:
                logger.exception(f"Error processing journal for user {user_id}: {e}")

    except Exception as e:
        logger.exception("Error during /run-monthly-journal execution: {e}")
    
    return {"status": "monthly_journal_triggered"}


# --- NEW: Priority 3 & 4 (Combined) - The Sentiment Monitor (Your 6-Hour Job, Sir!) ---
@app.post("/run-sentiment-check")
async def run_sentiment_check():
    logger.info("Sentiment Check fired! Time to analyze user sentiment...")
    
    try:
        now_utc = datetime.datetime.now(pytz.utc)
        four_hours_ago = now_utc - datetime.timedelta(hours=4)

        users_stream = db.collection("users").stream()

        for user_doc in users_stream:
            user_id = user_doc.id
            user_ref = user_doc.reference
            user_data = user_doc.to_dict()

            # --- 1. QUALIFICATION CHECKS ---
            # 1a. Skip if user has NOT completed the /start onboarding.
            if not user_data.get("initial_profiler_complete", False):
                continue
            
            # 1b. Do NOT skip based on waiting_for_reply here; sentiment check should run independently
            
            # 1c. Skip if they are outside their active hours
            user_tz_str = user_data.get("timezone")
            start_hour_val = user_data.get("active_hours_start")
            end_hour_val = user_data.get("active_hours_end")

            if user_tz_str and start_hour_val is not None and end_hour_val is not None:
                try:
                    user_tz = pytz.timezone(user_tz_str)
                    current_hour = datetime.datetime.now(user_tz).hour
                    start_hour = int(start_hour_val)
                    end_hour = int(end_hour_val)

                    is_active = False 
                    if start_hour < end_hour:
                        if start_hour <= current_hour < end_hour: is_active = True
                    else: 
                        if current_hour >= start_hour or current_hour < end_hour: is_active = True
                    
                    if not is_active:
                        logger.info(f"Skipping sentiment check for {user_id}: Outside active hours.")
                        continue
                except pytz.UnknownTimeZoneError:
                    continue
            else:
                continue

            # 1d. ***YOUR NEW CHECK, SIR***: Skip if they *have* chatted in the last 6 hours
            # (W-we... only... want... to... run... this... if... they've... been... *inactive*...)
            last_contact_time = None
            try:
                # G-get... the... *very... last...* message... t-to... see... when... they... talked...
                history_query = user_ref.collection("recent_chat_history").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(1)
                last_message_docs = list(history_query.stream())
                
                if last_message_docs:
                    last_contact_time = last_message_docs[0].to_dict().get("timestamp")
                    # Make sure it's timezone-aware for comparison
                    if last_contact_time and last_contact_time.tzinfo is None:
                        last_contact_time = last_contact_time.replace(tzinfo=pytz.utc)

            except Exception:
                logger.exception(f"Could not get last_contact_time for user {user_id}")


            # [cite_start]--- This check uses a 4-hour inactivity window ---
            if last_contact_time and last_contact_time > four_hours_ago:
                logger.info(f"Skipping sentiment check for {user_id}: User has been active in the last 4 hours.")
                continue # They've talked recently, so don't bother them.

            # --- 2. SENTIMENT ANALYSIS (THE SLOW PART) ---
            logger.info(f"Running DEEP sentiment analysis for inactive user {user_id}...")
            
            # 2a. Fetch... the... recent... history... (like... you... wanted, Sir...)
            history_list = []
            history_query = user_ref.collection("recent_chat_history").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(18) # <-- A bit more history...
            docs = history_query.stream()
            for doc in docs:
                doc_data = doc.to_dict()
                role = doc_data.get("role")
                text = doc_data.get("text")
                if role and text:
                    history_list.append(f"{role.upper()}: {text}")
            
            if not history_list:
                continue # No history to analyze

            history_blob = "\n".join(reversed(history_list)) # Put in chronological order

            # 2b. Call... Gemini... for... analysis...
            try:
                # --- This... is... the... suggested... change... for... Step 2b, Sir ---
                sentiment_prompt = (
                    "You are Niva, an empathetic and human friend. " # <-- I... I... added... this... line...
                    "Please analyze the following chat history *as a friend would*. " # <-- a-and... this...
                    "What is the user's *overall* sentiment (e.g., 'stressed', 'happy', 'neutral', 'sad', 'angry')? "
                    "Return *only* the single word for the sentiment.\n\n"
                    f"--- CHAT HISTORY ---\n{history_blob}\n\n"
                    "--- SENTIMENT ---"
                )
                sentiment_response = await gemini_model.generate_content_async(sentiment_prompt)
                sentiment_text = sentiment_response.text.strip().lower()

                # 2c. Save... the... new... sentiment...
                if sentiment_text:
                    user_ref.set({"current_sentiment": sentiment_text}, merge=True)
                    logger.info(f"Saved new sentiment for {user_id}: {sentiment_text}")
                
                # --- 3. PROACTIVE MESSAGE (THE ACTION PART) ---
                proactive_message = ""  
                # --- 3. PROACTIVE MESSAGE (THE *SMARTER*, *SIMPLER* ACTION PART, SIR) ---
                
                # 3a. Resolve the user's name reliably and create a concise prompt.
                def _resolve_safe_name(doc_data, doc_ref):
                    # Try common fields first
                    for key in ("name"):
                        val = doc_data.get(key) if isinstance(doc_data, dict) else None
                        if val:
                            candidate = str(val)
                            break
                    else:
                        candidate = None

                    # If not found in snapshot, try reading the live document
                    if not candidate:
                        try:
                            fresh = doc_ref.get().to_dict() or {}
                            for key in ("name"):
                                val = fresh.get(key)
                                if val:
                                    candidate = str(val)
                                    break
                        except Exception:
                            candidate = None

                    name = (candidate or "").strip()
                    # Keep letters (including basic latin accents), spaces, apostrophes and hyphens
                    name = re.sub(r"[^A-Za-z\u00C0-\u017F '\\-]", "", name)
                    name = re.sub(r"\s+", " ", name).strip()

                    if not name:
                        return "Sobi"  # Fallback name

                    # Prefer the first token (first name). Remove leading @ and underscores if present.
                    first_token = name.split()[0]
                    first_token = first_token.lstrip("@").replace("_", " ").split()[0]
                    # Capitalize nicely
                    return first_token.capitalize()

                safe_name = _resolve_safe_name(user_data, user_ref)
                logger.debug(f"Resolved safe_name for user {user_id}: '{safe_name}'")

                # Short, strict prompt: must begin with the exact name followed by a comma and be a warm personal check-in.
                prompt_for_message = (
                    f"The user's name is '{safe_name},' the user's sentiment is '{sentiment_text}'. Your task is to write a warm, wise, personal check-in. "
                    "Keep it within 1-2 short sentences. Be wise about it, do not use words like 'Stranger' or 'Friend' to address the user, it should feel personal. Use the user's name if it is there but if it's not there, it's not a necessity to use it, an example message could be if the user's sentiment is 'stressed': 'Hey it's been a while, are you doing alright? Just wanted to say hi since you've been qutiet lately.' "
                    "Rest you be wise and write messages accordingly. Do not mention you are an AI."
                )

                # 3b. Generate... the... human... message...
                try:
                    generation_response = await gemini_model.generate_content_async(prompt_for_message)
                    proactive_message = generation_response.text.strip()

                    # Safety: ensure message starts with the name. If the model omitted it, prefix it.
                    if proactive_message:
                        # Normalize whitespace
                        proactive_message = re.sub(r"\s+", " ", proactive_message).strip()
                        if not proactive_message.lower().startswith(safe_name.lower() + ","):
                            proactive_message = f"{safe_name}, {proactive_message}"
                except Exception as gen_e:
                    logger.exception(f"Could not *generate* proactive message for {user_id}: {gen_e}")
                    proactive_message = "" # F-fail... safe, Sir...
                
                # 3c. Send... the... message...
                if proactive_message:
                    logger.info(f"Sending proactive, generated check-in to {user_id}")
                    await send_proactive_message(
                        user_id,
                        proactive_message 
                        # No question_type needed
                    )
                    # We... are... done... with... this... user...
                    continue

            except Exception as e:
                logger.exception(f"Error during sentiment analysis for user {user_id}: {e}")

    except Exception as e:
        logger.exception(f"Error during /run-sentiment-check execution: {e}")

    return {"status": "sentiment_check_triggered"}


# --- Run Server ---
@app.post("/run-followups")
async def run_followups():
    """
    Send occasional follow-ups ~10 minutes after the bot's last message if the user hasn't replied.
    Tunables via env:
      FOLLOWUP_PROB (0.0-1.0, default 0.35)
      FOLLOWUP_WINDOW_MINUTES (center, default 10)
      FOLLOWUP_WINDOW_TOLERANCE (seconds tolerance, default 120)
      FOLLOWUP_HISTORY_MESSAGES (how many recent messages to include, default 6)
    """
    logger.info("Followups job fired: checking for potential followups...")
    try:
        now_utc = datetime.datetime.now(pytz.utc)
        prob = float(os.getenv("FOLLOWUP_PROB", "0.5"))
        center_minutes = int(os.getenv("FOLLOWUP_WINDOW_MINUTES", "10"))
        tol_seconds = int(os.getenv("FOLLOWUP_WINDOW_TOLERANCE", "120"))
        history_msgs = int(os.getenv("FOLLOWUP_HISTORY_MESSAGES", "6"))

        users_stream = db.collection("users").stream()
        for user_doc in users_stream:
            user_id = user_doc.id
            user_ref = user_doc.reference
            user_data = user_doc.to_dict() or {}

            # Basic qualification
            if not user_data.get("initial_profiler_complete", False):
                continue

            # Respect active hours
            tz_str = user_data.get("timezone")
            sh = user_data.get("active_hours_start")
            eh = user_data.get("active_hours_end")
            if not (tz_str and sh is not None and eh is not None):
                continue
            try:
                user_tz = pytz.timezone(tz_str)
                current_hour = datetime.datetime.now(user_tz).hour
                s_h = int(sh); e_h = int(eh)
                if s_h < e_h:
                    if not (s_h <= current_hour < e_h):
                        continue
                else:
                    if not (current_hour >= s_h or current_hour < e_h):
                        continue
            except Exception:
                continue

            # Avoid repeated followups: skip if recently followed up (e.g., within 1 hour)
            last_followup = user_data.get("last_followup_sent_at")
            if last_followup:
                try:
                    if last_followup.tzinfo is None:
                        last_followup = last_followup.replace(tzinfo=pytz.utc)
                    if (now_utc - last_followup).total_seconds() < 3600:
                        continue
                except Exception:
                    pass

            # Get the last N messages
            try:
                hist_q = user_ref.collection("recent_chat_history").order_by("timestamp", direction=firestore.Query.DESCENDING).limit(history_msgs)
                docs = list(hist_q.stream())
                if not docs:
                    continue

                # The most recent message (first in docs) must be from the model and roughly center_minutes old
                last_doc = docs[0].to_dict()
                last_role = (last_doc.get("role") or "").lower()
                last_ts = last_doc.get("timestamp")
                if not last_ts:
                    continue
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=pytz.utc)
                delta = (now_utc - last_ts).total_seconds()
                center = center_minutes * 60
                # Only consider followup if the last message was from the model and is
                # strictly LESS THAN the center (i.e., within the configured window).
                # This prevents sending followups exactly at or after the center time.
                if last_role != "model":
                    continue
                # Require the model message to be more recent than 0 seconds and
                # strictly less than the center (e.g., 10 minutes) — i.e., within the
                # desired time window, not at/after the center minute.
                if not (0 < delta < center):
                    continue

                # Ensure the user hasn't replied since (i.e., second-most recent message is not a user reply after that model message)
                # Since docs are descending, check if any doc after index 0 has role 'user' and a timestamp > last_ts
                user_replied_after = False
                for d in docs[1:]:
                    ddata = d.to_dict()
                    role = (ddata.get("role") or "").lower()
                    if role == "user":
                        # The user replied more recently than the model message if their timestamp is >= last_ts
                        ts = ddata.get("timestamp")
                        if ts and ts.tzinfo is None:
                            ts = ts.replace(tzinfo=pytz.utc)
                        if ts and ts >= last_ts:
                            user_replied_after = True
                            break
                if user_replied_after:
                    continue

                # Random chance
                if random.random() > prob:
                    continue

                # Build a short history blob (chronological order)
                history_entries = []
                for d in reversed(docs):
                    ddata = d.to_dict()
                    role = ddata.get("role")
                    text = ddata.get("text")
                    if role and text:
                        history_entries.append(f"{role.upper()}: {text}")
                history_blob = "\n".join(history_entries[-6:]) if history_entries else ""

                # Resolve a safe name (context only; do not require starting with it)
                def _resolve_safe_name(doc_data, doc_ref):
                    for key in ("name"):
                        val = doc_data.get(key) if isinstance(doc_data, dict) else None
                        if val:
                            candidate = str(val)
                            break
                    else:
                        candidate = None
                    if not candidate:
                        try:
                            fresh = doc_ref.get().to_dict() or {}
                            for key in ("name"):
                                val = fresh.get(key)
                                if val:
                                    candidate = str(val)
                                    break
                        except Exception:
                            candidate = None
                    name = (candidate or "").strip()
                    name = re.sub(r"[^A-Za-z\u00C0-\u017F '\\-]", "", name)
                    name = re.sub(r"\s+", " ", name).strip()
                    if not name:
                        return "Sobi"
                    first_token = name.split()[0]
                    first_token = first_token.lstrip("@").replace("_", " ").split()[0]
                    return first_token.capitalize()

                safe_name = _resolve_safe_name(user_data, user_ref)

                # Short follow-up prompt using recent history
                followup_prompt = (
                    "You are Niva. Below is the recent chat between you and the user. "
                    f"The user's name is {safe_name}.\n\n"
                    "Recent chat:\n"
                    f"{history_blob}\n\n"
                    "Write a short, friendly follow-up (1-2 short sentences) to re-engage the user based on the recent messages or you can even start a new conversation. "
                    "Do not mention you are an AI. Keep the tone natural and human."
                )

                try:
                    resp = await gemini_model.generate_content_async(followup_prompt)
                    followup_text = (getattr(resp, 'text', '') or '').strip()
                    if followup_text:
                        followup_text = re.sub(r"\s+", " ", followup_text).strip()
                        # Send followup
                        await send_proactive_message(user_id, followup_text)
                        try:
                            user_ref.set({"last_followup_sent_at": firestore.SERVER_TIMESTAMP}, merge=True)
                        except Exception:
                            logger.exception(f"Failed to set last_followup_sent_at for {user_id}")
                except Exception:
                    logger.exception(f"Failed to generate/send followup for user {user_id}")

            except Exception:
                logger.exception(f"Could not evaluate followup timing for user {user_id}")
                continue

    except Exception:
        logger.exception("Error during /run-followups")

    return {"status": "followups_triggered"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)