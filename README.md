# Project AIC

Professional README for the `Project-AIC` repository.

---

## Table of Contents

- Project Overview
- Features
- Architecture & Components
- Tech Stack
- Key Files
- Environment Variables
- Configuration & Tunables
- Installation (local)
- Running (development)
- Running with Docker
- Testing
- Deployment notes
- Security & Secrets
- Troubleshooting
- Contribution Guidelines
- License
- Contact

---

## Project Overview

Project AIC is an opinionated, conversational agent built on Google/Vertex AI generative models and Telegram as a delivery channel. It operates as a FastAPI app that receives Telegram webhook events, interacts with Gemini/Vertex models (and Google GenAI utilities), saves short-term and long-term memories to Firestore, and runs scheduled "Will"/trigger jobs (followups, daily/weekly/monthly journals, sentiment checks, and news push).

The bot ("Niva") is intentionally designed to behave like a warm, short-text human conversation partner, with proactive messaging capabilities and a continuous learning loop that preserves summarized memories to Firestore.

## Features

- Conversational chatbot powered by Gemini/Vertex AI (generative model integration)
- Telegram webhook receiver (FastAPI + python-telegram-bot)
- Short-term chat history (Firestore collection `recent_chat_history`) and long-term memories (`user_memories`, `daily_memories`, `weekly_memories`, `monthly_memories`)
- Proactive messages and follow-ups (configurable timing and probability)
- Onboarding flow (7-digit auth key, timezone, active hours, name)
- Media (image) handling with multimodal input to the model
- Continuous Learner that extracts structured personal interests/about facts and merges them into Firestore
- Daily/Weekly/Monthly journaling (summarize small memories into higher-order memories)
- Sentiment monitor that generates empathetic check-ins when inactivity and sentiment conditions are met
- Modular delivery engine that fragments messages to feel human (typing indicators & pauses)

## Architecture & Components

- FastAPI app (`main.py`) — webhook endpoints and scheduled trigger endpoints
- Telegram Bot (python-telegram-bot's asynchronous Bot API) — sends/receives Telegram messages
- Vertex AI / Google GenAI (`vertexai`, `google.genai`) — core generative model calls and search grounding
- Firestore (Google Cloud) — user profiles and memory stores
- Running environment: can be executed directly (Uvicorn) or run inside Docker (included `Dockerfile`)
- Optional tunneling for local dev (ngrok is listed in `requirements.txt`)

Flow overview:
1. Telegram webhook posts message -> `/webhook` in FastAPI
2. `main.py` validates/creates user record, runs onboarding checks or personalizes a Gemini chat
3. Responses are delivered via `deliver_message()` (fragmentation + typing emulation)
4. Conversations are saved to `recent_chat_history` and summarized to `user_memories`
5. Scheduled endpoints (`/run-will-triggers`, `/run-followups`, `/run-daily-journal`, etc.) process proactive actions

## Tech Stack

- Python 3.10+ (recommend latest 3.11+ runtime)
- FastAPI (web server framework)
- Uvicorn (ASGI server)
- python-telegram-bot (async Telegram Bot client)
- Google Vertex AI / GenAI (generative models; e.g., gemini-2.5-flash)
- google-cloud-firestore (Firestore client)
- pytz (timezone handling)
- google-cloud-vision (optional image pipeline)
- python-dotenv (local .env support)
- Docker (containerization)

## Key Files

- `main.py` — application entrypoint. Contains endpoints, message delivery, memory saving, proactive triggers, onboarding, and scheduling endpoints.
- `requirements.txt` — Python dependencies used by the project.
- `Dockerfile` — containerization instructions for production-like runs.
- `oldfiles/` — archived older code versions (do not rely on this for current logic)

## Environment Variables

The project reads environment variables via `os.getenv` and `python-dotenv`. At minimum, the following are required to run in a real environment:

- `TELEGRAM_BOT_TOKEN` (required) — Telegram bot token for the bot account.
- `GCP_PROJECT_ID` (required) — Google Cloud project ID used by Vertex AI & Firestore.

Optional / tunable environment variables used in `main.py` (with defaults):

- `FRAGMENT_MAX_CHARS` — max chars per fragment for the delivery engine (default: 140)
- `PAUSE_PER_WORD` — seconds pause per word to emulate typing (default: 0.25)
- `MIN_SLEEP` — min random sleep between fragments (default: 0.8)
- `MAX_SLEEP` — max random sleep between fragments (default: 3.0)
- `FOLLOWUP_PROB` — probability to send a followup when eligible (0.0–1.0; default in code: 0.5)
- `FOLLOWUP_WINDOW_MINUTES` — center time for followup window (default: 10)
- `FOLLOWUP_WINDOW_TOLERANCE` — followup timing tolerance in seconds (default: 120) — note: code may use strict < checks depending on logic
- `FOLLOWUP_HISTORY_MESSAGES` — how many recent messages to include when generating followup prompts (default: 6)

Note: If you run locally for development, create a `.env` file with at least `TELEGRAM_BOT_TOKEN` and `GCP_PROJECT_ID`. For production, prefer secrets managed by your cloud provider.

## Configuration & Tunables (exposed in `main.py`)

- Onboarding auth key in source: `1451919` (currently hard-coded; change for production)
- Memory pruning: `recent_chat_history` is pruned to 25 entries per user in `save_memory()`
- Journal rollups: daily -> weekly -> monthly endpoints run summarization and delete source docs as implemented
- Sentiment/Proactive timings are controlled by time-based endpoints (`/run-sentiment-check`, `/run-followups`, `/run-will-triggers`)

## Installation (local development)

Requirements: Python 3.10+ and a Google Cloud Project with Firestore enabled.

1. Clone the repo

```pwsh
git clone <repo-url>
cd "c:\Users\soura\OneDrive\Desktop\Wroking Right now\Project AIC"
```

2. Create & activate a virtual environment (PowerShell example):

```pwsh
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

3. Create a `.env` file in the project root containing at minimum:

```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
GCP_PROJECT_ID=your_gcp_project_id_here
# Optional: other tunables like FOLLOWUP_PROB, FOLLOWUP_WINDOW_MINUTES
```

4. Ensure Google Application Credentials are available in your environment (for Firestore and Vertex):

```pwsh
# Example: set the path to a service account JSON key
$env:GOOGLE_APPLICATION_CREDENTIALS = "C:\path\to\service-account.json"
```

5. (Optional) If you want to test webhooks locally, use ngrok to expose a port and set it in Telegram's webhook settings.

## Running (development)

Start the FastAPI app with Uvicorn:

```pwsh
uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

- The app registers endpoints like `/webhook`, `/run-followups`, `/run-will-triggers`, `/run-daily-journal`, etc.
- Use ngrok (or another tunneling service) to expose `http://localhost:8080/webhook` to Telegram while developing.

## Running with Docker

A `Dockerfile` exists in the repo. To build and run the container locally:

```pwsh
# Build
docker build -t project-aic:latest .

# Run (example; pass environment vars)
docker run -it --env TELEGRAM_BOT_TOKEN="${env:TELEGRAM_BOT_TOKEN}" --env GCP_PROJECT_ID="${env:GCP_PROJECT_ID}" -p 8080:8080 project-aic:latest
```

Note: Mounting service account credentials into the container is necessary when using GCP services. Use secure secret management in production.

## Testing

This repo currently does not include an automated test suite. Suggested lightweight tests to add:

- Unit tests for `deliver_message()` fragmentation logic and pause calculation
- Unit tests for `save_memory()` pruning behavior
- Integration tests that mock Firestore and Gemini/Vertex clients to ensure endpoints return expected payloads

You can use `pytest` and `pytest-asyncio` to add async tests.

## Deployment notes

- Ensure you have a GCP project with Vertex API and Firestore enabled and proper IAM roles for the service account used by the bot.
- Use managed compute (Cloud Run, GKE, or Cloud Run for Anthos) or a VM + process manager to host the FastAPI app.
- Use a secure secrets store for `TELEGRAM_BOT_TOKEN` and `GOOGLE_APPLICATION_CREDENTIALS`. Avoid committing secrets to the repo.
- Configure a scheduler (Cloud Scheduler / cron) to invoke endpoints like `/run-followups` and `/run-will-triggers` at desired intervals, or implement a single periodic worker.

## Security & Secrets

- DO NOT store `TELEGRAM_BOT_TOKEN` or Google service account keys in source control. Use environment variables or secret managers.
- The onboarding `auth_key` is currently hard-coded (`1451919`) for development; rotate or replace with a secure flow before production.
- Validate and rate-limit incoming webhook events if deploying to public endpoints.

## Troubleshooting

- Bot not responding: check that `TELEGRAM_BOT_TOKEN` is valid and `main.py` runs without import errors.
- Firestore permission errors: ensure `GOOGLE_APPLICATION_CREDENTIALS` points to a service account with Firestore access.
- Webhooks not arriving locally: use ngrok and ensure Telegram webhook is set to your public ngrok URL.

## Contribution Guidelines

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/your-feature`
3. Run tests (if added) and verify changes
4. Submit a pull request with a clear description of changes

Keep changes small, document behavior changes in code comments, and update the README or other docs when adding features.

## License

Add your project license here (e.g., MIT, Apache-2.0). If you have no license chosen, add one to the repo root as `LICENSE`.

## Contact

For questions about the repository, reach out to the repo owner or project maintainer.


---

_Last updated: 2025-11-07_
