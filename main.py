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
    """Auto-register the Telegram webhook on boot, if we can figure out the URL."""
    base = _public_base_url()
    if not base or not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return
    webhook_url = base.rstrip("/") + "/webhook"
    try:
        await telegram_client.set_webhook(webhook_url, _WEBHOOK_SECRET)
    except Exception as exc:  # noqa: BLE001 — don't crash boot over this
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
