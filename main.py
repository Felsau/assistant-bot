"""FastAPI app exposing the Telegram webhook endpoint.

Flow:
  Telegram → POST /webhook → handlers.handle_message → reply via Telegram API
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

load_dotenv()

# Imported after load_dotenv so module-level clients see the env vars.
from bot import handlers, telegram_client  # noqa: E402

app = FastAPI(title="Personal Assistant Bot")

_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET")


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
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}  # ignore non-message updates

    chat_id = message["chat"]["id"]
    user_id = str(message["from"]["id"])
    text = message.get("text", "")

    try:
        reply = handlers.handle_message(user_id, text)
    except Exception as exc:  # noqa: BLE001 — never 500 back to Telegram
        reply = f"⚠️ Sorry, something went wrong: {exc}"

    await telegram_client.send_message(chat_id, reply)
    return {"ok": True}
