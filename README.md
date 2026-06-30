# Personal Assistant Bot 🤖

A personal assistant bot for **Telegram** that understands natural-language
messages, classifies them with the **Claude API**, and stores/retrieves the
data in **Supabase (Postgres)**.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Felsau/assistant-bot)

Send it a message and it figures out what you meant:

| You type | The bot does |
| --- | --- |
| `Remember that my wifi password is hunter2` | saves a **note** |
| `Class on Monday 09:00–11:00 in room 301` | saves a **schedule** entry |
| `Submit the OS assignment by Friday` | saves a **task** |
| `What's on today?` | **queries** your data and answers |

## Architecture

```
Telegram  ──webhook──►  FastAPI (/webhook)
                              │
                              ▼
                      ai/classifier.py  ──►  Claude API
                              │            (classify + extract JSON)
                              ▼
                      db/supabase_client.py  ──►  Supabase (Postgres)
                              │
                              ▼
                      reply back to Telegram
```

**Stack**

- **Bot interface:** Telegram Bot API
- **Backend:** Python + FastAPI
- **Database:** Supabase (Postgres)
- **AI:** Claude API (`claude-sonnet-4-6`)
- **Hosting:** Railway / Render / Fly.io (free tier)

## Project structure

```
assistant-bot/
├── .env.example
├── .gitignore
├── requirements.txt
├── main.py               # FastAPI app, webhook endpoint
├── bot/
│   ├── handlers.py       # routes a message → classify → store/query → reply
│   └── telegram_client.py
├── ai/
│   └── classifier.py     # calls Claude API, classifies + extracts JSON
├── db/
│   ├── supabase_client.py
│   └── schema.sql
└── README.md
```

## Setup

### 1. Clone & install

```bash
git clone <REPO_URL>
cd assistant-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:

- `TELEGRAM_BOT_TOKEN` – from [@BotFather](https://t.me/BotFather)
- `ANTHROPIC_API_KEY` – from the [Anthropic Console](https://console.anthropic.com/)
- `SUPABASE_URL` / `SUPABASE_KEY` – from your Supabase project settings
- `TELEGRAM_WEBHOOK_SECRET` – any random string (used to verify webhook calls)

### 3. Create the database

In your Supabase project's SQL editor, run [`db/schema.sql`](db/schema.sql).

### 4. Run locally

```bash
uvicorn main:app --reload --port 8000
```

Expose it with a tunnel so Telegram can reach the webhook:

```bash
ngrok http 8000        # or: lt --port 8000
```

### 5. Register the webhook with Telegram

The app **registers its own webhook on startup** whenever it can determine its
public URL (from `WEBHOOK_URL`, or automatically on Render/Railway). For local
development, set `WEBHOOK_URL` to your tunnel URL in `.env` and restart — done.

If you'd rather do it by hand:

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://<your-public-url>/webhook" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

Now message your bot on Telegram.

## Deploy

### One click (Render)

1. Click the **Deploy to Render** button at the top of this README.
2. Render reads [`render.yaml`](render.yaml) and prompts for the four secrets
   (`TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`).
   The webhook secret is generated automatically.
3. On boot the app detects its `RENDER_EXTERNAL_URL` and registers the Telegram
   webhook itself — no extra step.

### Other hosts (Railway / Fly.io)

A [`Procfile`](Procfile) is included, so Railway and similar platforms run the
app directly. Set the same environment variables in the host's dashboard. On
Railway the public URL is auto-detected; elsewhere set `WEBHOOK_URL` to the
deployed base URL.

## How classification works

Each message is sent to Claude with a system prompt asking it to return JSON:

```json
{
  "type": "note" | "schedule" | "task" | "query",
  "data": { ...fields for that type... }
}
```

Examples:

```json
{"type": "note", "data": {"content": "wifi password is hunter2"}}
{"type": "schedule", "data": {"title": "Class", "day_of_week": "Monday", "start_time": "09:00", "end_time": "11:00", "location": "room 301"}}
{"type": "task", "data": {"title": "Submit OS assignment", "due_date": "2026-07-03", "priority": "normal"}}
{"type": "query", "data": {"scope": "today"}}
```

`note` / `schedule` / `task` get inserted into Supabase. `query` reads the
relevant rows back and asks Claude to format a friendly reply.
