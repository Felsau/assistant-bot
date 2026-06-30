# Personal Assistant Bot

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
| `Coffee 60` / `Salary 30000 in` | logs an **expense / income** |
| `What's on today?` / `How much did I spend?` | **queries** your data and answers |

It also reads **photos of receipts** (snap a bill → it logs the expense),
handles **voice messages**, gives every item **inline buttons** (done /
delete), and can send a **morning digest** of your day.

### Commands

- `/today` — what's on today
- `/tasks` — your open tasks, each with ✅ / 🗑 buttons
- `/done <task>` — mark a task complete (or `/done` to pick from a list)
- `/spent` — this month's income, spending, and top categories
- `/budget` — set or view monthly budgets (e.g. `/budget food 3000`,
  `/budget 20000` for an overall limit, `/budget food off` to remove)
- `/report` — this month vs last month, with a category bar chart
- `/find <text>` — search your notes, tasks, and expenses
- `/recurring` — manage monthly recurring expenses (rent, subscriptions)
- `/export` — download all your transactions as a CSV file
- `/help` — usage

Set a reminder by writing `remind me to call the bank at 3pm`. After each
expense, if a relevant budget exists, the bot appends a progress line (e.g.
`food: 3,500 / 3,000 — over by 500`). Tasks have a **+1 day** button to
reschedule.

Times use the `TIMEZONE` setting (default `Asia/Bangkok`).

These register themselves as the bot's command menu on startup.

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
- `ALLOWED_USER_IDS` – **recommended**: comma-separated Telegram user IDs
  allowed to use the bot (find yours via [@userinfobot](https://t.me/userinfobot)).
  Leaving it empty lets anyone use the bot and spend your Claude credit.
- `CRON_SECRET` – any random string, protects the daily-digest endpoint
- `OPENAI_API_KEY` – *optional*, enables voice transcription (Whisper)

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

## Morning digest (scheduled)

The app exposes `POST /cron/daily-digest`, which messages every known user a
friendly summary of their day. It's protected by `CRON_SECRET`, so point a free
scheduler at it once a day:

```
POST https://<your-app>/cron/daily-digest?secret=<CRON_SECRET>
```

Use a free cron service such as [cron-job.org](https://cron-job.org) (set it to,
say, 07:00 daily). On Render's free tier the web service sleeps when idle — the
cron request itself wakes it.

Two more cron endpoints (same `CRON_SECRET`):

- `POST /cron/reminders` — delivers timed reminders that are due. Call it
  frequently (every 1–5 minutes) so reminders fire close to their time.
- `POST /cron/recurring` — posts recurring expenses whose day-of-month is today.
  Call it once a day.

## Receipt scanning

Send the bot a **photo of a receipt or bill** and it uses Claude's vision to
read the grand total, merchant, date, and category, then logs it as an expense
(with a Delete button, like any other entry). No extra setup or API key — it
uses the same `ANTHROPIC_API_KEY`. If it can't find a total, it asks you to type
the amount instead.

## Voice messages (optional)

Send the bot a voice note and it transcribes it, then treats the transcript like
a typed message. This requires `OPENAI_API_KEY` (Whisper). Without it, voice is
disabled and the bot asks you to type. This is the only non-Claude dependency and
it's entirely optional.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests mock the Claude and Supabase clients, so they need no API keys or
network access.

## How classification works

Each message is sent to Claude with a system prompt asking it to return JSON:

```json
{
  "type": "note" | "schedule" | "task" | "expense" | "query",
  "data": { ...fields for that type... }
}
```

Examples:

```json
{"type": "note", "data": {"content": "wifi password is hunter2"}}
{"type": "schedule", "data": {"title": "Class", "day_of_week": "Monday", "start_time": "09:00", "end_time": "11:00", "location": "room 301"}}
{"type": "task", "data": {"title": "Submit OS assignment", "due_date": "2026-07-03", "priority": "normal"}}
{"type": "expense", "data": {"kind": "expense", "amount": 60, "category": "food", "note": "coffee"}}
{"type": "query", "data": {"scope": "expenses"}}
```

`note` / `schedule` / `task` / `expense` get inserted into Supabase. `query`
reads the relevant rows back and asks Claude to format a friendly reply.

Expenses are filed into a fixed set of categories, so `/spent` totals stay
consistent (anything unrecognized snaps to `other`):

- **Expense:** food, groceries, transport, shopping, bills, housing, health,
  entertainment, education, travel, personal, gifts, fees, other
- **Income:** salary, bonus, refund, interest, gift, other

The category set lives in `ai/classifier.py` (`EXPENSE_CATEGORIES` /
`INCOME_CATEGORIES`) — edit it there to add your own.
