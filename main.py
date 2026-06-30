"""FastAPI app exposing the Telegram webhook and the daily-digest cron endpoint.

Flow:
  Telegram → POST /webhook → handlers → reply via Telegram API
  Cron service → POST /cron/daily-digest → morning summary to each user
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

load_dotenv()

# Imported after load_dotenv so module-level clients see the env vars.
from ai import classifier  # noqa: E402
from bot import handlers, telegram_client, voice  # noqa: E402
from db import supabase_client  # noqa: E402

app = FastAPI(title="Personal Assistant Bot")

_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
_CRON_SECRET = os.environ.get("CRON_SECRET")

# Restrict who can use the bot (comma-separated Telegram user IDs). Empty = open.
_ALLOWED_USER_IDS = {
    uid.strip() for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",") if uid.strip()
}

# The command menu shown in Telegram when the user types "/".
_BOT_COMMANDS = [
    {"command": "start", "description": "Start and see what I can do"},
    {"command": "help", "description": "How to use me"},
    {"command": "today", "description": "What's on today"},
    {"command": "tasks", "description": "Show my open tasks"},
    {"command": "done", "description": "Mark a task complete"},
    {"command": "spent", "description": "This month's spending summary"},
]


def _is_allowed(user_id: str) -> bool:
    return not _ALLOWED_USER_IDS or str(user_id) in _ALLOWED_USER_IDS


def _public_base_url() -> str | None:
    """Best-effort public base URL, from an explicit var or the host's."""
    if os.environ.get("WEBHOOK_URL"):
        return os.environ["WEBHOOK_URL"]
    if os.environ.get("RENDER_EXTERNAL_URL"):  # provided by Render
        return os.environ["RENDER_EXTERNAL_URL"]
    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):  # provided by Railway
        return f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
    return None


@app.on_event("startup")
async def register_webhook() -> None:
    """Auto-register the command menu and Telegram webhook on boot."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return

    try:
        await telegram_client.set_my_commands(_BOT_COMMANDS)
    except Exception as exc:  # noqa: BLE001 — don't crash boot over this
        print(f"[startup] setMyCommands failed: {exc}")

    base = _public_base_url()
    if not base:
        return
    webhook_url = base.rstrip("/") + "/webhook"
    try:
        await telegram_client.set_webhook(webhook_url, _WEBHOOK_SECRET)
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] webhook registration failed: {exc}")
    else:
        print(f"[startup] webhook set to {webhook_url}")


@app.get("/")
async def health() -> dict:
    return {"status": "ok", "service": "assistant-bot"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    # Verify the secret Telegram echoes back (set via setWebhook secret_token).
    if _WEBHOOK_SECRET and x_telegram_bot_api_secret_token != _WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="invalid secret token")

    update = await request.json()

    if "callback_query" in update:
        await _handle_callback(update["callback_query"])
        return {"ok": True}

    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}  # ignore non-message updates

    chat_id = message["chat"]["id"]
    user_id = str(message["from"]["id"])

    if not _is_allowed(user_id):
        await telegram_client.send_message(chat_id, "This bot is private.")
        return {"ok": True}

    try:
        supabase_client.upsert_user(user_id, chat_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[webhook] upsert_user failed: {exc}")

    text = message.get("text")

    # Voice notes → transcribe, then treat the transcript as text.
    if not text and message.get("voice"):
        try:
            text = await voice.transcribe(message["voice"]["file_id"])
        except Exception as exc:  # noqa: BLE001
            print(f"[webhook] transcription failed: {exc}")
            text = None
        if not text:
            note = (
                "Couldn't read that voice message. Type it instead."
                if voice.enabled()
                else "Voice isn't set up. Type it instead."
            )
            await telegram_client.send_message(chat_id, note)
            return {"ok": True}
        await telegram_client.send_message(chat_id, f'"{text}"')

    try:
        replies = handlers.handle_message(user_id, text)
    except Exception as exc:  # noqa: BLE001 — never 500 back to Telegram
        replies = [{"text": f"⚠️ Sorry, something went wrong: {exc}", "reply_markup": None}]

    for r in replies:
        await telegram_client.send_message(chat_id, r["text"], r.get("reply_markup"))
    return {"ok": True}


async def _handle_callback(cb: dict) -> None:
    user_id = str(cb["from"]["id"])
    cb_id = cb["id"]
    data = cb.get("data", "")
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")

    if not _is_allowed(user_id):
        await telegram_client.answer_callback_query(cb_id, "Not allowed")
        return

    try:
        result = handlers.handle_callback(user_id, data)
    except Exception as exc:  # noqa: BLE001
        print(f"[callback] failed: {exc}")
        await telegram_client.answer_callback_query(cb_id, "Something went wrong")
        return

    await telegram_client.answer_callback_query(cb_id, result.get("answer"))
    if result.get("edit_text") and chat_id and message_id:
        try:
            await telegram_client.edit_message_text(chat_id, message_id, result["edit_text"])
        except Exception as exc:  # noqa: BLE001
            print(f"[callback] edit failed: {exc}")


@app.post("/cron/daily-digest")
async def daily_digest(
    secret: str | None = None,
    x_cron_secret: str | None = Header(default=None),
) -> dict:
    """Send each known user a friendly summary of their day.

    Protect with CRON_SECRET and call from a scheduler (e.g. cron-job.org).
    """
    provided = x_cron_secret or secret
    if not _CRON_SECRET or provided != _CRON_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    users = supabase_client.list_users()
    sent = 0
    for u in users:
        try:
            rows = supabase_client.query(u["user_id"], "today")
            text = classifier.format_query_reply(
                "Summarize what's on for today from this data. If nothing, say the day is clear.",
                rows,
            )
            await telegram_client.send_message(u["chat_id"], text)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[digest] failed for {u.get('user_id')}: {exc}")

    return {"ok": True, "sent": sent}
