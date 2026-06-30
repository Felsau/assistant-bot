"""Optional voice-message transcription.

Telegram delivers voice notes as Opus audio. If ``OPENAI_API_KEY`` is set we
transcribe them with a Whisper-style speech-to-text endpoint; otherwise voice
support is simply disabled and the bot asks the user to type.

This is the one place the bot talks to a non-Claude service — speech-to-text is
a separate concern from the Claude classifier, and it stays fully optional.
"""

from __future__ import annotations

import os

import httpx

from bot import telegram_client

_STT_URL = "https://api.openai.com/v1/audio/transcriptions"


def enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


async def transcribe(file_id: str) -> str | None:
    """Return the transcript of a Telegram voice note, or None if disabled."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    audio = await telegram_client.download_file(file_id)
    model = os.environ.get("OPENAI_STT_MODEL", "whisper-1")

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            _STT_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("voice.oga", audio, "audio/ogg")},
            data={"model": model},
        )
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()
