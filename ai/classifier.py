"""Claude-powered message classifier.

Takes a free-form user message and asks Claude to classify it into one of
``note`` / ``schedule`` / ``task`` / ``query`` and extract structured data
matching the Supabase schema. Returns a plain ``dict``::

    {"type": "task", "data": {"title": "...", "due_date": "2026-07-03", ...}}
"""

from __future__ import annotations

import json
import os
from datetime import date

from anthropic import Anthropic

_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

_client_instance: Anthropic | None = None


def _client() -> Anthropic:
    """Lazily build the Anthropic client (so imports don't need a key).

    ``max_retries`` lets the SDK ride out transient 429/5xx with backoff.
    """
    global _client_instance
    if _client_instance is None:
        _client_instance = Anthropic(max_retries=4)
    return _client_instance


_SYSTEM_PROMPT = """\
You are the routing brain of a personal assistant bot. Classify the user's \
message into exactly one of these types and extract structured data for it.

Respond with ONLY a single JSON object, no prose, no markdown fences:

  {{"type": "<type>", "data": {{ ... }}}}

Types and their data fields:

- "note"     general info to remember.
             data: {{"content": string}}

- "schedule" a recurring/timed event (class, meeting, etc.).
             data: {{"title": string,
                     "day_of_week": string|null,   // e.g. "Monday"
                     "start_time": "HH:MM"|null,    // 24h
                     "end_time": "HH:MM"|null,      // 24h
                     "location": string|null,
                     "notes": string|null}}

- "task"     a to-do with an optional deadline.
             data: {{"title": string,
                     "due_date": "YYYY-MM-DD"|null,
                     "priority": "low"|"normal"|"high"}}

- "query"    the user is asking about their stored data.
             data: {{"scope": "today"|"week"|"tasks"|"schedule"|"notes"|"all"}}

Rules:
- Today's date is {today}. Resolve relative dates ("tomorrow", "Friday") to an
  absolute YYYY-MM-DD.
- Times are 24-hour "HH:MM". Use null for anything not stated.
- Default task priority is "normal".
- If the message is a question about what the user has saved, it's a "query".
- Match the user's language in any text you echo back.
"""


def classify(message: str) -> dict:
    """Classify ``message`` and return ``{"type": ..., "data": {...}}``."""
    system = _SYSTEM_PROMPT.format(today=date.today().isoformat())

    response = _client().messages.create(
        model=_MODEL,
        max_tokens=1024,
        # Marked cacheable so the static instructions are reused once the prompt
        # is large enough to cross the model's minimum cache size.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": message}],
    )

    text = "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()

    return _parse_json(text)


def _parse_json(text: str) -> dict:
    """Parse Claude's reply into a dict, tolerating stray markdown fences."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: pull out the outermost {...} block.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    # Give up gracefully — treat the whole thing as a note.
    return {"type": "note", "data": {"content": text}}


def format_query_reply(question: str, rows: dict) -> str:
    """Ask Claude to turn raw query rows into a friendly natural-language reply."""
    response = _client().messages.create(
        model=_MODEL,
        max_tokens=1024,
        system=(
            "You are a friendly personal assistant. Given the user's question "
            "and their stored data (as JSON), answer concisely and clearly. "
            "Reply in the user's language. If there's nothing relevant, say so."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Stored data:\n{json.dumps(rows, ensure_ascii=False, default=str)}"
                ),
            }
        ],
    )
    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
