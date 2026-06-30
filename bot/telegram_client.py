"""Minimal Telegram Bot API client."""

from __future__ import annotations

import os

import httpx

_API_BASE = "https://api.telegram.org"


def _token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


async def _post(method: str, payload: dict) -> dict:
    url = f"{_API_BASE}/bot{_token()}/{method}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()


async def send_message(
    chat_id: int | str, text: str, reply_markup: dict | None = None
) -> None:
    """Send a text message, optionally with an inline keyboard."""
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    await _post("sendMessage", payload)


async def edit_message_text(
    chat_id: int | str,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    """Replace the text (and keyboard) of a message we previously sent."""
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    payload["reply_markup"] = reply_markup or {"inline_keyboard": []}
    await _post("editMessageText", payload)


async def answer_callback_query(callback_query_id: str, text: str | None = None) -> None:
    """Acknowledge a button tap (shows an optional toast)."""
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    await _post("answerCallbackQuery", payload)


async def send_document(
    chat_id: int | str,
    filename: str,
    content: bytes,
    caption: str | None = None,
    mime: str = "text/csv",
) -> None:
    """Upload a file to a chat (used for CSV export)."""
    url = f"{_API_BASE}/bot{_token()}/sendDocument"
    data: dict = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url, data=data, files={"document": (filename, content, mime)}
        )
        resp.raise_for_status()


async def send_photo(
    chat_id: int | str, image: bytes, caption: str | None = None
) -> None:
    """Send a photo (used for the spending chart)."""
    url = f"{_API_BASE}/bot{_token()}/sendPhoto"
    data: dict = {"chat_id": str(chat_id)}
    if caption:
        data["caption"] = caption
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            url, data=data, files={"photo": ("chart.png", image, "image/png")}
        )
        resp.raise_for_status()


async def download_file(file_id: str) -> bytes:
    """Resolve a Telegram file_id and download its bytes (used for voice)."""
    async with httpx.AsyncClient(timeout=30) as client:
        meta = await client.get(
            f"{_API_BASE}/bot{_token()}/getFile", params={"file_id": file_id}
        )
        meta.raise_for_status()
        file_path = meta.json()["result"]["file_path"]
        content = await client.get(f"{_API_BASE}/file/bot{_token()}/{file_path}")
        content.raise_for_status()
        return content.content


async def set_my_commands(commands: list[dict]) -> dict:
    """Register the bot's command menu (the list shown when typing '/')."""
    return await _post("setMyCommands", {"commands": commands})


async def set_webhook(url: str, secret_token: str | None = None) -> dict:
    """Register ``url`` as this bot's webhook with Telegram."""
    payload: dict[str, str] = {"url": url}
    if secret_token:
        payload["secret_token"] = secret_token
    return await _post("setWebhook", payload)
