"""Claude-powered message classifier.

Takes a free-form user message and asks Claude to classify it into one of
``note`` / ``schedule`` / ``task`` / ``query`` and extract structured data
matching the Supabase schema. Returns a plain ``dict``::

    {"type": "task", "data": {"title": "...", "due_date": "2026-07-03", ...}}
"""

from __future__ import annotations

import base64
import json
import os

from anthropic import Anthropic

from bot import clock

_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Canonical bookkeeping categories. The classifier is told to pick from these,
# and _normalize_category snaps anything else to the closest match (or "other").
EXPENSE_CATEGORIES = [
    "food", "groceries", "transport", "shopping", "bills", "housing",
    "health", "entertainment", "education", "travel", "personal",
    "gifts", "fees", "other",
]
INCOME_CATEGORIES = ["salary", "bonus", "refund", "interest", "gift", "other"]

_CATEGORY_SYNONYMS = {
    "dining": "food", "restaurant": "food", "coffee": "food", "drinks": "food",
    "snacks": "food", "lunch": "food", "dinner": "food", "breakfast": "food",
    "grocery": "groceries", "supermarket": "groceries",
    "transportation": "transport", "fuel": "transport", "gas": "transport",
    "taxi": "transport", "bus": "transport", "train": "transport", "car": "transport",
    "clothes": "shopping", "clothing": "shopping",
    "utilities": "bills", "utility": "bills", "phone": "bills", "internet": "bills",
    "subscription": "bills", "subscriptions": "bills",
    "rent": "housing", "mortgage": "housing", "home": "housing",
    "medical": "health", "pharmacy": "health", "doctor": "health", "fitness": "health",
    "movies": "entertainment", "movie": "entertainment", "games": "entertainment",
    "tuition": "education", "books": "education", "course": "education",
    "trip": "travel", "flight": "travel", "hotel": "travel",
    "gift": "gifts", "donation": "gifts",
    "fee": "fees", "tax": "fees", "taxes": "fees", "charges": "fees",
    "wage": "salary", "wages": "salary", "payroll": "salary", "income": "salary",
}


def _normalize_category(kind: str, category) -> str | None:
    """Snap a free-text category onto the canonical set for its kind."""
    if not category:
        return None
    allowed = INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES
    c = str(category).strip().lower()
    if c in allowed:
        return c
    c = _CATEGORY_SYNONYMS.get(c, c)
    return c if c in allowed else "other"

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

- "expense"  money spent or received (bookkeeping).
             data: {{"kind": "expense"|"income",
                     "amount": number,
                     "currency": string|null,    // e.g. "THB", "USD"
                     "category": string|null,    // e.g. "food", "transport"
                     "note": string|null,
                     "occurred_on": "YYYY-MM-DD"|null}}

- "reminder" the user wants to be pinged at a specific time.
             data: {{"text": string,                    // what to remind about
                     "remind_at": "YYYY-MM-DD HH:MM"}}   // 24h, absolute

- "query"    the user is asking about their stored data.
             data: {{"scope": "today"|"week"|"tasks"|"schedule"|"notes"|"expenses"|"all"}}

Rules:
- Today's date is {today}; the current time is {now}. Resolve relative dates and
  times ("tomorrow", "Friday", "in 30 minutes", "at 3pm") to absolute values.
- A "reminder" has an explicit time to ping the user ("remind me to X at/in ...").
  A "task" is a to-do, possibly with a due date but no ping time. If the message
  asks to be reminded at a time, it is a "reminder".
- Times are 24-hour "HH:MM". Use null for anything not stated.
- Default task priority is "normal".
- For "expense": a bare amount with a thing bought (e.g. "coffee 60", "lunch 120
  baht") is kind "expense"; words like salary/refund/received/income mean kind
  "income". Extract a numeric "amount". Default occurred_on to today if no date
  is given.
- For an expense "category", choose the single best fit from this list:
  {expense_categories}. Use "other" if nothing fits. For income, use one of:
  {income_categories}.
- Questions about spending/budget/how much was spent are "query" with scope
  "expenses".
- Match the user's language in any text you echo back.
"""


def classify(message: str) -> dict:
    """Classify ``message`` and return ``{"type": ..., "data": {...}}``."""
    system = _SYSTEM_PROMPT.format(
        today=clock.today().isoformat(),
        now=clock.now().strftime("%Y-%m-%d %H:%M"),
        expense_categories=", ".join(EXPENSE_CATEGORIES),
        income_categories=", ".join(INCOME_CATEGORIES),
    )

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

    result = _parse_json(text)
    if result.get("type") == "expense":
        data = result.get("data") or {}
        data["category"] = _normalize_category(data.get("kind", "expense"), data.get("category"))
        result["data"] = data
    return result


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


_RECEIPT_SYSTEM = """\
You read a photo of a receipt or bill and return ONLY a JSON object, no prose:

  {{"kind": "expense", "amount": number, "currency": string|null,
    "category": string|null, "note": string|null, "occurred_on": "YYYY-MM-DD"|null,
    "items": [{{"amount": number, "category": string, "note": string}}]}}

- "amount" is the grand total actually paid (after tax and discounts). Number only.
- "note" is the merchant / store name if visible.
- "category" is the single best fit from: {expense_categories}. Use "other" if unsure.
- "occurred_on" is the date printed on the receipt (YYYY-MM-DD); if none, use {today}.
- "currency" is a short code if determinable (e.g. THB, USD), else null.
- "items": ONLY include this when the receipt clearly lists a few (2-6) distinct
  line items in DIFFERENT categories worth splitting; each item's amount and its
  own category. Omit it (or use []) for single-purpose receipts or long grocery
  lists — in that case just give the one total.
- If the image is not a receipt, or no total is readable, return {{"amount": null}}.
"""


def extract_receipt(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    """Read a receipt photo and return expense data (``amount`` may be None).

    May include an ``items`` list when the receipt is worth splitting.
    """
    system = _RECEIPT_SYSTEM.format(
        today=clock.today().isoformat(),
        expense_categories=", ".join(EXPENSE_CATEGORIES),
    )
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")

    response = _client().messages.create(
        model=_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": "Extract this receipt as JSON."},
            ],
        }],
    )

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    data = _parse_json(text)
    if data.get("type"):  # _parse_json wrapped a non-JSON reply as a note
        return {"amount": None}
    data["kind"] = "expense"
    data["category"] = _normalize_category("expense", data.get("category"))
    items = data.get("items")
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict):
                it["category"] = _normalize_category("expense", it.get("category"))
    return data


def format_query_reply(question: str, rows: dict) -> str:
    """Ask Claude to turn raw query rows into a friendly natural-language reply."""
    response = _client().messages.create(
        model=_MODEL,
        max_tokens=1024,
        system=(
            "Answer the user's question from their saved data (JSON of notes, "
            "schedule, tasks, expenses). Answer directly, first sentence first. "
            "No preamble, no sign-off, no filler like 'Here's' or 'Sure'. No "
            "emoji. Keep it short. Reply in the user's language. If nothing is "
            "relevant, say so plainly."
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
