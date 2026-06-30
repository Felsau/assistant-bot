"""Message routing: classify a message, store it / answer queries, and
handle inline-button taps (complete, delete).

``handle_message`` returns a *list* of replies, each a dict::

    {"text": str, "reply_markup": dict | None}

so a single command (e.g. ``/tasks``) can send several messages, each with its
own buttons. ``handle_callback`` handles taps on those buttons.
"""

from __future__ import annotations

from datetime import date

from ai import classifier
from db import supabase_client

START_TEXT = (
    "Hi! I'm your personal assistant. 🤖\n\n"
    "Just talk to me naturally:\n"
    "• \"Remember my locker code is 4821\" → I save a note\n"
    "• \"Math class Monday 9-11 in room 301\" → I save your schedule\n"
    "• \"Submit the report by Friday\" → I save a task\n"
    "• \"Coffee 60\" / \"Salary 30000 in\" → I log money in/out\n"
    "• \"What's on today?\" → I look it up and tell you\n\n"
    "Commands: /today  /tasks  /done  /spent  /help\n"
    "You can also send a voice message."
)

HELP_TEXT = (
    "Send me anything — I sort it into notes, schedule, tasks, or answer "
    "questions about what you've saved.\n\n"
    "• /today — what's on today\n"
    "• /tasks — your open tasks (tap ✅ to finish, 🗑 to delete)\n"
    "• /done <task> — mark a task complete\n"
    "• /spent — this month's income, spending, and top categories\n"
    "Tip: log money by typing things like \"taxi 80\" or \"bonus 5000 in\", "
    "and send a voice message and I'll transcribe it."
)


def _reply(text: str, reply_markup: dict | None = None) -> dict:
    return {"text": text, "reply_markup": reply_markup}


def _task_markup(task_id: str | None) -> dict | None:
    if not task_id:
        return None
    return {
        "inline_keyboard": [[
            {"text": "✅ Done", "callback_data": f"done:{task_id}"},
            {"text": "🗑 Delete", "callback_data": f"del:tasks:{task_id}"},
        ]]
    }


def _delete_markup(table: str, row_id: str | None) -> dict | None:
    if not row_id:
        return None
    return {
        "inline_keyboard": [[
            {"text": "🗑 Delete", "callback_data": f"del:{table}:{row_id}"},
        ]]
    }


def handle_message(user_id: str, text: str) -> list[dict]:
    """Process one user message and return a list of replies."""
    text = (text or "").strip()
    if not text:
        return [_reply("Send me a note, a schedule entry, a task, or a question. 🙂")]

    if text.startswith("/start"):
        return [_reply(START_TEXT)]
    if text.startswith("/help"):
        return [_reply(HELP_TEXT)]
    if text.startswith("/today"):
        rows = supabase_client.query(user_id, "today")
        return [_reply(classifier.format_query_reply("What's on today?", rows))]
    if text.startswith("/tasks"):
        return _list_open_tasks(user_id)
    if text.startswith("/done"):
        return _handle_done(user_id, text[len("/done"):].strip())
    if text.startswith("/spent") or text.startswith("/expenses"):
        return _month_summary(user_id)

    result = classifier.classify(text)
    msg_type = result.get("type", "note")
    data = result.get("data", {}) or {}

    if msg_type == "note":
        row = supabase_client.insert_note(user_id, data)
        return [_reply(
            f"📝 Noted: {data.get('content', text)}",
            _delete_markup("notes", row.get("id")),
        )]

    if msg_type == "schedule":
        row = supabase_client.insert_schedule(user_id, data)
        return [_reply(
            "📅 Added to your schedule: " + _describe_schedule(data),
            _delete_markup("schedule", row.get("id")),
        )]

    if msg_type == "task":
        row = supabase_client.insert_task(user_id, data)
        return [_reply(
            "✅ Task added: " + _describe_task(data),
            _task_markup(row.get("id")),
        )]

    if msg_type == "expense":
        if data.get("amount") in (None, ""):
            # No amount detected — don't lose it; keep it as a note.
            row = supabase_client.insert_note(user_id, {"content": text})
            return [_reply(f"📝 Noted: {text}", _delete_markup("notes", row.get("id")))]
        row = supabase_client.insert_transaction(user_id, data)
        return [_reply(
            _describe_transaction(data),
            _delete_markup("transactions", row.get("id")),
        )]

    if msg_type == "query":
        rows = supabase_client.query(user_id, data.get("scope", "all"))
        return [_reply(classifier.format_query_reply(text, rows))]

    # Unknown type — fall back to saving a note so nothing is lost.
    row = supabase_client.insert_note(user_id, {"content": text})
    return [_reply(f"📝 Noted: {text}", _delete_markup("notes", row.get("id")))]


def handle_callback(user_id: str, data: str) -> dict:
    """Handle an inline-button tap. Returns {"answer", "edit_text"}."""
    parts = (data or "").split(":")
    action = parts[0] if parts else ""

    if action == "done" and len(parts) == 2:
        row = supabase_client.complete_task(user_id, parts[1])
        if row:
            return {"answer": "Marked done ✅", "edit_text": f"✅ {row['title']} — done"}
        return {"answer": "Task not found", "edit_text": None}

    if action == "del" and len(parts) == 3:
        ok = supabase_client.delete_row(user_id, parts[1], parts[2])
        return {
            "answer": "Deleted 🗑" if ok else "Already gone",
            "edit_text": "🗑 Deleted" if ok else None,
        }

    return {"answer": "Unknown action", "edit_text": None}


def _list_open_tasks(user_id: str) -> list[dict]:
    tasks = supabase_client.open_tasks(user_id)
    if not tasks:
        return [_reply("🎉 No open tasks. You're all caught up!")]
    replies = [_reply("🗒 Your open tasks:")]
    for t in tasks:
        replies.append(_reply("• " + _describe_task(t), _task_markup(t["id"])))
    return replies


def _handle_done(user_id: str, arg: str) -> list[dict]:
    if not arg:
        tasks = supabase_client.open_tasks(user_id)
        if not tasks:
            return [_reply("🎉 No open tasks to complete.")]
        replies = [_reply("Which task did you finish? Tap ✅")]
        for t in tasks:
            replies.append(_reply("• " + _describe_task(t), _task_markup(t["id"])))
        return replies

    matches = supabase_client.open_tasks(user_id, like=arg)
    if not matches:
        return [_reply(f"Couldn't find an open task matching “{arg}”.")]
    if len(matches) == 1:
        t = matches[0]
        supabase_client.complete_task(user_id, t["id"])
        return [_reply(f"✅ Done: {t['title']}")]

    replies = [_reply("Multiple matches — tap the one you finished:")]
    for t in matches:
        replies.append(_reply("• " + _describe_task(t), _task_markup(t["id"])))
    return replies


def _month_summary(user_id: str) -> list[dict]:
    start = date.today().replace(day=1)
    rows = supabase_client.list_transactions(user_id, start.isoformat())
    if not rows:
        return [_reply("No money logged this month yet. Try \"coffee 60\". 💸")]

    income = sum(_num(r.get("amount")) for r in rows if r.get("kind") == "income")
    expense = sum(_num(r.get("amount")) for r in rows if r.get("kind", "expense") != "income")

    by_category: dict[str, float] = {}
    for r in rows:
        if r.get("kind", "expense") != "income":
            cat = r.get("category") or "Other"
            by_category[cat] = by_category.get(cat, 0) + _num(r.get("amount"))

    lines = [
        f"📊 {start.strftime('%B %Y')}",
        f"💸 Spent: {_fmt(expense)}",
        f"💰 Income: {_fmt(income)}",
        f"💼 Net: {_fmt(income - expense)}",
    ]
    if by_category:
        lines.append("\nTop categories:")
        for cat, amt in sorted(by_category.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"  • {cat}: {_fmt(amt)}")
    return [_reply("\n".join(lines))]


def _describe_transaction(data: dict) -> str:
    kind = data.get("kind", "expense")
    label = "💰 Income" if kind == "income" else "💸 Expense"
    amount = _fmt(_num(data.get("amount")))
    currency = data.get("currency")
    head = f"{label}: {amount}{(' ' + currency) if currency else ''}"
    extras = [x for x in (data.get("category"), data.get("note")) if x]
    if extras:
        head += " — " + " · ".join(extras)
    if data.get("occurred_on"):
        head += f" ({data['occurred_on']})"
    return head


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fmt(n: float) -> str:
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:,.2f}"


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
    if priority and priority != "normal":
        out += f" [{priority}]"
    return out
