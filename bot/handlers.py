"""Message routing: classify a message, then store it or answer a query."""

from __future__ import annotations

from ai import classifier
from db import supabase_client


def handle_message(user_id: str, text: str) -> str:
    """Process one user message and return the reply text."""
    text = (text or "").strip()
    if not text:
        return "Send me a note, a schedule entry, a task, or a question. 🙂"

    if text.startswith("/start"):
        return (
            "Hi! I'm your personal assistant. 🤖\n\n"
            "Just talk to me naturally:\n"
            "• \"Remember my locker code is 4821\" → I save a note\n"
            "• \"Math class Monday 9-11 in room 301\" → I save your schedule\n"
            "• \"Submit the report by Friday\" → I save a task\n"
            "• \"What's on today?\" → I look it up and tell you"
        )
    if text.startswith("/help"):
        return (
            "Send me anything. I sort it into notes, schedule, tasks, or "
            "answer questions about what you've saved."
        )
    if text.startswith("/today"):
        rows = supabase_client.query(user_id, "today")
        return classifier.format_query_reply("What's on today?", rows)
    if text.startswith("/tasks"):
        rows = supabase_client.query(user_id, "tasks")
        return classifier.format_query_reply("Show my open tasks.", rows)

    result = classifier.classify(text)
    msg_type = result.get("type", "note")
    data = result.get("data", {}) or {}

    if msg_type == "note":
        supabase_client.insert_note(user_id, data)
        return f"📝 Noted: {data.get('content', text)}"

    if msg_type == "schedule":
        supabase_client.insert_schedule(user_id, data)
        return "📅 Added to your schedule: " + _describe_schedule(data)

    if msg_type == "task":
        supabase_client.insert_task(user_id, data)
        return "✅ Task added: " + _describe_task(data)

    if msg_type == "query":
        scope = data.get("scope", "all")
        rows = supabase_client.query(user_id, scope)
        return classifier.format_query_reply(text, rows)

    # Unknown type — fall back to saving a note so nothing is lost.
    supabase_client.insert_note(user_id, {"content": text})
    return f"📝 Noted: {text}"


def _describe_schedule(data: dict) -> str:
    parts = [data.get("title", "(untitled)")]
    if data.get("day_of_week"):
        parts.append(data["day_of_week"])
    if data.get("start_time"):
        span = data["start_time"]
        if data.get("end_time"):
            span += f"–{data['end_time']}"
        parts.append(span)
    if data.get("location"):
        parts.append(f"@ {data['location']}")
    return " · ".join(parts)


def _describe_task(data: dict) -> str:
    out = data.get("title", "(untitled)")
    if data.get("due_date"):
        out += f" (due {data['due_date']})"
    priority = data.get("priority", "normal")
    if priority != "normal":
        out += f" [{priority}]"
    return out
