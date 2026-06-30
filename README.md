# Personal Assistant Bot 🤖

A personal assistant bot for **Telegram** that understands natural-language
messages, classifies them with the **Claude API**, and stores/retrieves the
data in **Supabase (Postgres)**.

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

```bash
curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://<your-public-url>/webhook" \
  -d "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

Now message your bot on Telegram.

## Deploy

Push to GitHub and deploy on Railway / Render / Fly.io. Set the same
environment variables in the host's dashboard, then re-run the `setWebhook`
call above with your deployed URL.

```bash
git init -b main
git add .
git commit -m "Initial commit: Personal Assistant Bot"
git remote add origin <REPO_URL>
git push -u origin main
```

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
