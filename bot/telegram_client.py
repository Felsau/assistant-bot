"""Minimal Telegram Bot API client (send messages)."""

from __future__ import annotations

import os

import httpx

_API_BASE = "https://api.telegram.org"


def _token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


async def send_message(chat_id: int | str, text: str) -> None:
    """Send a text message to a chat."""
    url = f"{_API_BASE}/bot{_token()}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
