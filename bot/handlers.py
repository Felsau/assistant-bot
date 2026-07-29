"""Message routing: classify a message, store it / answer queries, and
handle inline-button taps (complete, delete).

``handle_message`` returns a *list* of replies, each a dict::

    {"text": str, "reply_markup": dict | None}

so a single command (e.g. ``/tasks``) can send several messages, each with its
own buttons. ``handle_callback`` handles taps on those buttons.
"""

from __future__ import annotations

import csv
import io
import os
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta

from ai import classifier
from bot import clock
from db import supabase_client

# Recently deleted rows, kept briefly so an "Undo" tap can restore them. Keyed by
# the deleted row's id. In-memory and per-process (like the webhook de-dup): fine
# for a single instance, and undo is inherently short-lived anyway.
_UNDO: "OrderedDict[str, tuple]" = OrderedDict()
_UNDO_MAX = 200

# Pending edits awaiting a "which one?" choice, keyed by a short token that the
# candidate buttons carry. Same in-memory, per-process caveat as _UNDO.
_PENDING_EDITS: "OrderedDict[str, tuple]" = OrderedDict()
_PENDING_MAX = 200

# target word -> (table, column searched to find the entry)
_EDIT_TARGETS = {
    "task": ("tasks", "title"),
    "expense": ("transactions", "note"),
    "note": ("notes", "content"),
}


def _remember(store: "OrderedDict[str, tuple]", limit: int, token: str, value: tuple) -> None:
    store[token] = value
    store.move_to_end(token)
    while len(store) > limit:
        store.popitem(last=False)


def _remember_undo(token: str, user_id: str, table: str, row: dict) -> None:
    _remember(_UNDO, _UNDO_MAX, token, (user_id, table, row))

# Totals and budgets are kept in one base currency. Foreign-currency entries are
# tracked and reported separately rather than summed in (we have no FX rates, so
# adding 40 USD to a THB total would be wrong). Entries with no currency are
# assumed to be in the base currency.
BASE_CURRENCY = os.environ.get("BASE_CURRENCY", "THB").upper()

# How far ahead of an appointment the heads-up ping goes out.
EVENT_LEAD_MIN = int(os.environ.get("EVENT_LEAD_MINUTES", "30"))

START_TEXT = (
    "I sort what you send into notes, schedule, tasks, and expenses, and "
    "answer questions about them. Just write normally:\n\n"
    "wifi password is hunter2\n"
    "Math Monday 9-11 room 301\n"
    "dentist July 20 at 2pm\n"
    "submit the report Friday\n"
    "coffee 60   /   salary 30000 in\n"
    "remind me to call the bank at 3pm\n"
    "what's on today?\n\n"
    "Commands: /today /tasks /done /week /spent /budget /report /find /recurring "
    "/export /help. Voice and receipt photos work too."
)

HELP_TEXT = (
    "Write a note, schedule item, task, or amount and I file it. Ask a "
    "question and I look it up.\n\n"
    "/today   what's on today\n"
    "/tasks   open tasks, with buttons to finish or delete\n"
    "/done <task>   mark a task done\n"
    "/week   this week's totals by category\n"
    "/spent   this month's totals by category\n"
    "/budget   set or view monthly budgets (e.g. /budget food 3000)\n"
    "/report   spending vs last month, with a chart\n"
    "/find <text>   search notes, tasks, expenses\n"
    "/recurring   manage monthly recurring expenses\n"
    "/export   download your transactions as CSV\n\n"
    "Appointments with a date and time (\"dentist July 20 at 2pm\") get a "
    "heads-up ping before they start and show up in /today. "
    "Set reminders by writing \"remind me to X at 3pm\" (or \"every Monday 9am\" "
    "for a repeating one). Change saved items in words: \"change coffee to 80\", "
    "\"rename task X to Y\". Deleting anything shows an Undo button. Log money with "
    "\"taxi 80\" or by sending a receipt photo. Voice messages get transcribed."
)


def _reply(text: str, reply_markup: dict | None = None) -> dict:
    return {"text": text, "reply_markup": reply_markup}


def _task_markup(task_id: str | None) -> dict | None:
    if not task_id:
        return None
    return {
        "inline_keyboard": [[
            {"text": "Done", "callback_data": f"done:{task_id}"},
            {"text": "+1 day", "callback_data": f"snooze:{task_id}"},
            {"text": "Delete", "callback_data": f"del:tasks:{task_id}"},
        ]]
    }


def _delete_markup(table: str, row_id: str | None) -> dict | None:
    if not row_id:
        return None
    return {
        "inline_keyboard": [[
            {"text": "Delete", "callback_data": f"del:{table}:{row_id}"},
        ]]
    }


def handle_message(user_id: str, text: str) -> list[dict]:
    """Process one user message and return a list of replies."""
    text = (text or "").strip()
    if not text:
        return [_reply("Send a note, schedule item, task, expense, or a question.")]

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
    if text.startswith("/week"):
        return _week_summary(user_id)
    if text.startswith("/spent") or text.startswith("/expenses"):
        return _month_summary(user_id)
    if text.startswith("/budget"):
        return _handle_budget(user_id, text[len("/budget"):].strip())
    if text.startswith("/find"):
        return _handle_find(user_id, text[len("/find"):].strip())
    if text.startswith("/recurring"):
        return _handle_recurring(user_id, text[len("/recurring"):].strip())

    result = classifier.classify(text)
    msg_type = result.get("type", "note")
    data = result.get("data", {}) or {}

    if msg_type == "note":
        row = supabase_client.insert_note(user_id, data)
        return [_reply(
            f"Noted: {data.get('content', text)}",
            _delete_markup("notes", row.get("id")),
        )]

    if msg_type == "schedule":
        row = supabase_client.insert_schedule(user_id, data)
        return [_reply(
            "Added: " + _describe_schedule(data),
            _delete_markup("schedule", row.get("id")),
        )]

    if msg_type == "task":
        row = supabase_client.insert_task(user_id, data)
        return [_reply(
            "Task added: " + _describe_task(data),
            _task_markup(row.get("id")),
        )]

    if msg_type == "event":
        return _handle_new_event(user_id, text, data)

    if msg_type == "reminder":
        if not data.get("remind_at"):
            row = supabase_client.insert_note(user_id, {"content": text})
            return [_reply(f"Noted: {text}", _delete_markup("notes", row.get("id")))]
        # Echo the friendly local time, but store an offset-stamped instant so
        # the timestamptz column doesn't shift it to UTC.
        friendly = data["remind_at"]
        data["remind_at"] = clock.to_aware_iso(data["remind_at"])
        supabase_client.insert_reminder(user_id, data)
        repeat = data.get("repeat")
        suffix = f" (repeats {repeat})" if repeat in ("daily", "weekly", "monthly") else ""
        return [_reply(f"Reminder set for {friendly}{suffix}: {data.get('text', '').strip()}")]

    if msg_type == "expense":
        if data.get("amount") in (None, ""):
            # No amount detected — keep it as a note rather than lose it.
            row = supabase_client.insert_note(user_id, {"content": text})
            return [_reply(f"Noted: {text}", _delete_markup("notes", row.get("id")))]
        return record_expense(user_id, data)

    if msg_type == "edit":
        return _handle_edit(user_id, data)

    if msg_type == "query":
        rows = supabase_client.query(user_id, data.get("scope", "all"))
        return [_reply(classifier.format_query_reply(text, rows))]

    # Unknown type — fall back to saving a note so nothing is lost.
    row = supabase_client.insert_note(user_id, {"content": text})
    return [_reply(f"Noted: {text}", _delete_markup("notes", row.get("id")))]


def record_expense(user_id: str, data: dict) -> list[dict]:
    """Insert an expense/income transaction and confirm it. Shared by the
    text path and the receipt-photo path. Appends a budget line if relevant."""
    row = supabase_client.insert_transaction(user_id, data)
    text = _describe_transaction(data)
    alert = _budget_alert(user_id, data)
    if alert:
        text += "\n" + alert
    return [_reply(text, _delete_markup("transactions", row.get("id")))]


def _handle_new_event(user_id: str, text: str, data: dict) -> list[dict]:
    """Store an appointment (date + time). Falls back to a task when the
    classifier produced a date but no time, and to a note with neither."""
    date_s = (data.get("date") or "").strip()
    time_s = (data.get("start_time") or "").strip()

    if not date_s or not time_s:
        if date_s:
            task = {"title": data.get("title") or text, "due_date": date_s}
            row = supabase_client.insert_task(user_id, task)
            return [_reply("Task added: " + _describe_task(task), _task_markup(row.get("id")))]
        row = supabase_client.insert_note(user_id, {"content": text})
        return [_reply(f"Noted: {text}", _delete_markup("notes", row.get("id")))]

    event = {
        "title": data.get("title") or text,
        "starts_at": clock.to_aware_iso(f"{date_s} {time_s}"),
        "end_at": clock.to_aware_iso(f"{date_s} {data['end_time']}") if data.get("end_time") else None,
        "location": data.get("location"),
        "notes": data.get("notes"),
    }
    row = supabase_client.insert_event(user_id, event)
    reply = (
        "Event: " + _describe_event(event)
        + f"\nI'll ping you {EVENT_LEAD_MIN} min before."
    )
    return [_reply(reply, _delete_markup("events", row.get("id")))]


def _describe_event(data: dict) -> str:
    out = data.get("title", "(untitled)")
    start = clock.to_local(data.get("starts_at"))
    if start:
        when = f"{start:%a %b %d, %H:%M}"
        end = clock.to_local(data.get("end_at"))
        if end:
            when += f"-{end:%H:%M}"
        out += f" — {when}"
    if data.get("location"):
        out += f", at {data['location']}"
    return out


def handle_callback(user_id: str, data: str) -> dict:
    """Handle an inline-button tap. Returns {"answer", "edit_text"}."""
    parts = (data or "").split(":")
    action = parts[0] if parts else ""

    if action == "done" and len(parts) == 2:
        row = supabase_client.complete_task(user_id, parts[1])
        if row:
            return {"answer": "Done", "edit_text": f"Done: {row['title']}"}
        return {"answer": "Task not found", "edit_text": None}

    if action == "snooze" and len(parts) == 2:
        task = supabase_client.get_task(user_id, parts[1])
        if not task:
            return {"answer": "Task not found", "edit_text": None}
        base = _parse_date(task.get("due_date")) or clock.today()
        new_due = (base + timedelta(days=1)).isoformat()
        supabase_client.set_task_due(user_id, parts[1], new_due)
        task["due_date"] = new_due
        return {
            "answer": "Moved to " + new_due,
            "edit_text": _describe_task(task),
            "reply_markup": _task_markup(parts[1]),  # keep the buttons
        }

    if action == "del" and len(parts) == 3:
        row = supabase_client.delete_row(user_id, parts[1], parts[2])
        if not row:
            return {"answer": "Already gone", "edit_text": None}
        markup = None
        if isinstance(row, dict) and row.get("id"):
            _remember_undo(row["id"], user_id, parts[1], row)
            markup = {"inline_keyboard": [[
                {"text": "Undo", "callback_data": f"undo:{row['id']}"},
            ]]}
        return {"answer": "Deleted", "edit_text": "Deleted", "reply_markup": markup}

    if action == "undo" and len(parts) == 2:
        entry = _UNDO.pop(parts[1], None)
        if not entry or entry[0] != user_id:
            return {"answer": "Nothing to undo", "edit_text": None}
        _, table, row = entry
        _reinsert(user_id, table, row)
        return {"answer": "Restored", "edit_text": "Restored: " + _undo_label(table, row)}

    if action == "edit" and len(parts) == 3:
        row_id, token = parts[1], parts[2]
        entry = _PENDING_EDITS.get(token)
        if not entry or entry[0] != user_id:
            return {"answer": "That edit expired", "edit_text": None}
        # Pop it: the token is shared by every candidate's button, so leaving it
        # live would let a second tap re-apply the same change to another row.
        del _PENDING_EDITS[token]
        _, table, changes = entry
        updated = supabase_client.update_row(user_id, table, row_id, changes)
        if not updated:
            return {"answer": "Nothing changed", "edit_text": None}
        return {"answer": "Updated", "edit_text": "Updated: " + _describe_row(table, updated)}

    return {"answer": "Unknown action", "edit_text": None}


def _handle_edit(user_id: str, data: dict) -> list[dict]:
    """Apply a natural-language edit to an existing task / expense / note."""
    target = (data.get("target") or "").lower()
    if target not in _EDIT_TARGETS:
        return [_reply("I can edit tasks, expenses, and notes.")]
    changes = _clean_changes(target, data.get("changes") or {})
    if not changes:
        return [_reply('Tell me what to change, e.g. "change coffee to 80".')]
    match = (data.get("match") or "").strip()
    if not match:
        return [_reply('Which one? Name it, e.g. "change the coffee expense to 80".')]

    table, column = _EDIT_TARGETS[target]
    matches = supabase_client.search(user_id, table, column, match, limit=6)
    if not matches and table == "transactions":
        # An expense might be identified by its category rather than its note.
        matches = supabase_client.search(user_id, table, "category", match, limit=6)
    if not matches:
        return [_reply(f'Couldn\'t find a {target} matching "{match}".')]

    if len(matches) == 1:
        updated = supabase_client.update_row(user_id, table, matches[0]["id"], changes)
        if not updated:
            return [_reply("Nothing to change there.")]
        return [_reply("Updated: " + _describe_row(table, updated), _row_markup(table, updated))]

    # Ambiguous — stash the change and let the user pick the entry.
    token = uuid.uuid4().hex[:8]
    _remember(_PENDING_EDITS, _PENDING_MAX, token, (user_id, table, changes))
    replies = [_reply(f"More than one {target} matches. Which one?")]
    for m in matches:
        replies.append(_reply(_describe_row(table, m), {
            "inline_keyboard": [[
                {"text": "Edit this", "callback_data": f"edit:{m['id']}:{token}"},
            ]],
        }))
    return replies


def _clean_changes(target: str, changes: dict) -> dict:
    """Coerce/normalize the requested changes; drop empties."""
    out: dict = {}
    for key, value in changes.items():
        if value in (None, ""):
            continue
        if key == "amount":
            try:
                out["amount"] = float(value)
            except (TypeError, ValueError):
                pass  # unparseable amount — leave the existing value untouched
        elif key == "category" and target == "expense":
            out["category"] = classifier._normalize_category("expense", value)
        else:
            out[key] = value
    return out


def _describe_row(table: str, row: dict) -> str:
    if table == "tasks":
        return _describe_task(row)
    if table == "transactions":
        return _describe_transaction(row)
    if table == "notes":
        return row.get("content", "")
    if table == "schedule":
        return _describe_schedule(row)
    if table == "events":
        return _describe_event(row)
    return str(row.get("id", ""))


def _row_markup(table: str, row: dict) -> dict | None:
    if table == "tasks":
        return _task_markup(row.get("id"))
    return _delete_markup(table, row.get("id"))


def _reinsert(user_id: str, table: str, row: dict) -> None:
    """Re-create a previously deleted row (used by Undo)."""
    inserters = {
        "notes": supabase_client.insert_note,
        "schedule": supabase_client.insert_schedule,
        "tasks": supabase_client.insert_task,
        "transactions": supabase_client.insert_transaction,
        "events": supabase_client.insert_event,
    }
    insert = inserters.get(table)
    if insert:
        insert(user_id, row)


def _undo_label(table: str, row: dict) -> str:
    if table == "notes":
        return row.get("content", "note")
    if table == "tasks":
        return _describe_task(row)
    if table == "transactions":
        return _describe_transaction(row)
    if table == "schedule":
        return _describe_schedule(row)
    if table == "events":
        return _describe_event(row)
    return "entry"


def _list_open_tasks(user_id: str) -> list[dict]:
    tasks = supabase_client.open_tasks(user_id)
    if not tasks:
        return [_reply("No open tasks.")]
    replies = [_reply("Open tasks:")]
    for t in tasks:
        replies.append(_reply(_describe_task(t), _task_markup(t["id"])))
    return replies


def _handle_done(user_id: str, arg: str) -> list[dict]:
    if not arg:
        tasks = supabase_client.open_tasks(user_id)
        if not tasks:
            return [_reply("No open tasks.")]
        replies = [_reply("Which one did you finish?")]
        for t in tasks:
            replies.append(_reply(_describe_task(t), _task_markup(t["id"])))
        return replies

    matches = supabase_client.open_tasks(user_id, like=arg)
    if not matches:
        return [_reply(f'No open task matches "{arg}".')]
    if len(matches) == 1:
        t = matches[0]
        supabase_client.complete_task(user_id, t["id"])
        return [_reply(f"Done: {t['title']}")]

    replies = [_reply("More than one match. Tap the one you finished:")]
    for t in matches:
        replies.append(_reply(_describe_task(t), _task_markup(t["id"])))
    return replies


def _is_base_currency(row: dict) -> bool:
    return (row.get("currency") or BASE_CURRENCY).upper() == BASE_CURRENCY


def _aggregate(rows: list[dict]) -> dict:
    """Aggregate transactions in the base currency for the given rows.

    Returns ``{"income", "expense", "by_category", "foreign"}`` where ``foreign``
    maps each non-base currency to its ``{"expense", "income"}`` totals
    (surfaced separately, never mixed into the base-currency numbers)."""
    income = expense = 0.0
    by_category: dict[str, float] = {}
    foreign: dict[str, dict[str, float]] = {}
    for r in rows:
        amount = _num(r.get("amount"))
        is_income = r.get("kind") == "income"
        if not _is_base_currency(r):
            cur = (r.get("currency") or "").upper()
            bucket = foreign.setdefault(cur, {"expense": 0.0, "income": 0.0})
            bucket["income" if is_income else "expense"] += amount
            continue
        if is_income:
            income += amount
        else:
            expense += amount
            cat = r.get("category") or "other"
            by_category[cat] = by_category.get(cat, 0) + amount
    return {"income": income, "expense": expense, "by_category": by_category, "foreign": foreign}


# The default query limit (200) is fine for /export, but an active user can
# log more than 200 transactions in a month/week — summaries need the full set.
_SUMMARY_LIMIT = 5000


def _month_rows(user_id: str) -> list[dict]:
    start = clock.today().replace(day=1)
    return supabase_client.list_transactions(user_id, start.isoformat(), limit=_SUMMARY_LIMIT)


def _foreign_lines(foreign: dict[str, dict[str, float]]) -> list[str]:
    """Render a "not included" note for any foreign-currency activity."""
    if not foreign:
        return []
    lines = ["", "Other currencies (not included):"]
    for cur, amts in sorted(foreign.items()):
        expense, income = amts.get("expense", 0.0), amts.get("income", 0.0)
        if expense and income:
            lines.append(f"  {cur} {_fmt(expense)} spent, {_fmt(income)} income")
        elif income:
            lines.append(f"  {cur} {_fmt(income)} income")
        else:
            lines.append(f"  {cur} {_fmt(expense)}")
    return lines


def _month_expenses(user_id: str):
    """Return (income, expense_total, {category: spent}) for the current month,
    counting only base-currency entries."""
    agg = _aggregate(_month_rows(user_id))
    return agg["income"], agg["expense"], agg["by_category"]


def _week_summary(user_id: str) -> list[dict]:
    today = clock.today()
    monday = today - timedelta(days=today.weekday())
    agg = _aggregate(supabase_client.list_transactions(user_id, monday.isoformat(), limit=_SUMMARY_LIMIT))
    income, expense, by_category = agg["income"], agg["expense"], agg["by_category"]
    if not income and not expense and not agg["foreign"]:
        return [_reply("Nothing logged this week yet.")]

    lines = [
        f"This week (since {monday:%b %d})",
        f"Spent: {_fmt(expense)}",
        f"Income: {_fmt(income)}",
        f"Net: {_fmt(income - expense)}",
    ]
    if by_category:
        lines.append("")
        lines.append("By category:")
        for cat, amt in sorted(by_category.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"  {cat} {_fmt(amt)}")
    lines += _foreign_lines(agg["foreign"])
    return [_reply("\n".join(lines))]


def _month_summary(user_id: str) -> list[dict]:
    agg = _aggregate(_month_rows(user_id))
    income, expense, by_category = agg["income"], agg["expense"], agg["by_category"]
    if not income and not expense and not agg["foreign"]:
        return [_reply("Nothing logged this month yet.")]

    lines = [
        clock.today().strftime("%B %Y"),
        f"Spent: {_fmt(expense)}",
        f"Income: {_fmt(income)}",
        f"Net: {_fmt(income - expense)}",
    ]
    if by_category:
        lines.append("")
        lines.append("By category:")
        for cat, amt in sorted(by_category.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"  {cat} {_fmt(amt)}")
    lines += _foreign_lines(agg["foreign"])
    return [_reply("\n".join(lines))]


def _handle_budget(user_id: str, arg: str) -> list[dict]:
    if not arg:
        return _budget_status(user_id)

    parts = arg.split()
    amount_token = parts[-1].lower()
    raw_category = " ".join(parts[:-1]).lower().strip()

    if not raw_category:
        category = "total"
    elif raw_category in ("total", "overall", "all"):
        category = "total"
    else:
        category = classifier._normalize_category("expense", raw_category)

    if amount_token in ("off", "none", "remove", "0"):
        supabase_client.delete_budget(user_id, category)
        return [_reply(f"Removed the {category} budget.")]

    try:
        amount = float(amount_token.replace(",", ""))
    except ValueError:
        return [_reply(
            "Usage: /budget food 3000  (or /budget 20000 for an overall limit, "
            "/budget food off to remove)"
        )]
    if amount <= 0:
        supabase_client.delete_budget(user_id, category)
        return [_reply(f"Removed the {category} budget.")]

    supabase_client.set_budget(user_id, category, amount)
    return [_reply(f"Budget set: {category} {_fmt(amount)} / month.")]


def _budget_status(user_id: str) -> list[dict]:
    budgets = supabase_client.get_budgets(user_id)
    if not budgets:
        return [_reply(
            "No budgets set. Try \"/budget food 3000\" or \"/budget 20000\" for "
            "an overall monthly limit."
        )]
    _, total, by_category = _month_expenses(user_id)
    lines = [f"Budgets, {clock.today():%B %Y}"]
    if "total" in budgets:
        lines.append(_budget_line("total", total, budgets["total"]))
    for cat in sorted(c for c in budgets if c != "total"):
        lines.append(_budget_line(cat, by_category.get(cat, 0), budgets[cat]))
    return [_reply("\n".join(lines))]


def _budget_alert(user_id: str, data: dict) -> str | None:
    """A short budget status line to append after logging an expense."""
    if data.get("kind") == "income":
        return None
    budgets = supabase_client.get_budgets(user_id)
    if not budgets:
        return None
    category = data.get("category")
    _, total, by_category = _month_expenses(user_id)
    lines = []
    if category and category in budgets:
        lines.append(_budget_line(category, by_category.get(category, 0), budgets[category]))
    if "total" in budgets:
        lines.append(_budget_line("total", total, budgets["total"]))
    return "\n".join(lines) if lines else None


def _budget_line(name: str, spent: float, limit: float) -> str:
    if limit and spent > limit:
        return f"{name}: {_fmt(spent)} / {_fmt(limit)} — over by {_fmt(spent - limit)}"
    pct = (spent / limit * 100) if limit else 0
    return f"{name}: {_fmt(spent)} / {_fmt(limit)} ({pct:.0f}%)"


def _handle_find(user_id: str, q: str) -> list[dict]:
    if not q:
        return [_reply("Usage: /find <text> — searches your notes, tasks, and expenses.")]
    replies: list[dict] = []
    for note in supabase_client.search(user_id, "notes", "content", q):
        replies.append(_reply("Note: " + note.get("content", ""),
                              _delete_markup("notes", note["id"])))
    for task in supabase_client.search(user_id, "tasks", "title", q):
        replies.append(_reply("Task: " + _describe_task(task), _task_markup(task["id"])))
    for ev in supabase_client.search(user_id, "events", "title", q):
        replies.append(_reply("Event: " + _describe_event(ev),
                              _delete_markup("events", ev["id"])))
    for tx in supabase_client.search(user_id, "transactions", "note", q):
        replies.append(_reply(_describe_transaction(tx),
                              _delete_markup("transactions", tx["id"])))
    if not replies:
        return [_reply(f'Nothing matches "{q}".')]
    return replies[:15]


def _handle_recurring(user_id: str, arg: str) -> list[dict]:
    parts = arg.split()
    if not parts or parts[0] == "list":
        items = supabase_client.list_recurring(user_id)
        if not items:
            return [_reply(
                "No recurring entries. Add one with: "
                "/recurring add 8000 housing rent 1  (amount category note day)"
            )]
        lines = ["Recurring (monthly):"]
        for r in items:
            lines.append(
                f"  [{r['id'][:8]}] {_fmt(_num(r.get('amount')))} "
                f"{r.get('category') or 'other'}"
                f"{(' ' + r['note']) if r.get('note') else ''} on day {r.get('day_of_month', 1)}"
            )
        lines.append("\nRemove with: /recurring remove <id>")
        return [_reply("\n".join(lines))]

    if parts[0] == "remove" and len(parts) >= 2:
        ok = _remove_recurring_by_prefix(user_id, parts[1])
        return [_reply("Removed." if ok else "No recurring entry with that id.")]

    if parts[0] == "add":
        rest = parts[1:]
        if not rest:
            return [_reply("Usage: /recurring add <amount> [category] [note] [day]")]
        try:
            amount = float(rest[0].replace(",", ""))
        except ValueError:
            return [_reply("Usage: /recurring add <amount> [category] [note] [day]")]
        day = 1
        if len(rest) >= 2 and rest[-1].isdigit():
            day = max(1, min(28, int(rest[-1])))
            rest = rest[:-1]
        category = classifier._normalize_category("expense", rest[1]) if len(rest) >= 2 else None
        note = " ".join(rest[2:]) if len(rest) >= 3 else None
        supabase_client.insert_recurring(user_id, {
            "kind": "expense", "amount": amount, "category": category,
            "note": note, "day_of_month": day,
        })
        return [_reply(
            f"Recurring set: {_fmt(amount)} {category or 'other'} on day {day} each month."
        )]

    return [_reply("Usage: /recurring add <amount> [category] [note] [day], or /recurring remove <id>")]


def _remove_recurring_by_prefix(user_id: str, prefix: str) -> bool:
    for r in supabase_client.list_recurring(user_id):
        if r["id"].startswith(prefix) or r["id"][:8] == prefix:
            return supabase_client.delete_recurring(user_id, r["id"])
    return False


def report_text(user_id: str):
    """Build a this-month-vs-last-month report. Returns (text, by_category)."""
    today = clock.today()
    cur_start = today.replace(day=1)
    prev_end = cur_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)

    cur = supabase_client.list_transactions(user_id, cur_start.isoformat(), limit=_SUMMARY_LIMIT)
    prev = supabase_client.list_transactions(
        user_id, prev_start.isoformat(), prev_end.isoformat(), limit=_SUMMARY_LIMIT
    )

    cur_agg = _aggregate(cur)
    cur_exp = cur_agg["expense"]
    prev_exp = _aggregate(prev)["expense"]
    by_category = cur_agg["by_category"]

    delta = cur_exp - prev_exp
    if prev_exp:
        trend = f"{'+' if delta >= 0 else ''}{delta / prev_exp * 100:.0f}% vs last month"
    else:
        trend = "no spending last month to compare"

    lines = [
        f"Report, {today:%B %Y}",
        f"Spent: {_fmt(cur_exp)} ({trend})",
        f"Last month: {_fmt(prev_exp)}",
    ]
    if by_category:
        lines.append("")
        lines.append("By category:")
        for cat, amt in sorted(by_category.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {cat} {_fmt(amt)}")
    lines += _foreign_lines(cur_agg["foreign"])
    return "\n".join(lines), by_category


def budget_warnings(user_id: str) -> str | None:
    """Categories at/over 80% of budget this month — for the morning digest."""
    budgets = supabase_client.get_budgets(user_id)
    if not budgets:
        return None
    _, total, by_category = _month_expenses(user_id)
    lines = []
    for name, limit in budgets.items():
        spent = total if name == "total" else by_category.get(name, 0)
        if limit and spent >= 0.8 * limit:
            lines.append(_budget_line(name, spent, limit))
    if not lines:
        return None
    return "Budget watch:\n" + "\n".join(lines)


def record_receipt(user_id: str, data: dict) -> list[dict]:
    """Log a receipt: split into items when given, else a single total."""
    total = _num(data.get("amount"))
    items = [it for it in (data.get("items") or []) if isinstance(it, dict) and it.get("amount")]
    item_sum = sum(_num(it.get("amount")) for it in items)

    # Only split when 2+ items roughly add up to the total (avoid double counting).
    if len(items) >= 2 and total and abs(item_sum - total) <= max(1.0, total * 0.05):
        merchant = data.get("note")
        replies = []
        for it in items:
            entry = {
                "kind": "expense",
                "amount": _num(it.get("amount")),
                "currency": data.get("currency"),
                "category": it.get("category") or "other",
                "note": it.get("note") or merchant,
                "occurred_on": data.get("occurred_on"),
            }
            replies.extend(record_expense(user_id, entry))
        return replies

    return record_expense(user_id, data)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


# A note/category starting with one of these opens as a live formula in
# Excel/Sheets when the CSV is opened (CSV formula injection). Prefix with an
# apostrophe to force it to be read as plain text.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _csv_safe(value: str) -> str:
    return "'" + value if value.startswith(_CSV_FORMULA_PREFIXES) else value


def build_transactions_csv(rows: list[dict]) -> bytes:
    """Serialize transactions to CSV bytes (UTF-8 BOM, Excel-friendly)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "kind", "amount", "currency", "category", "note"])
    for r in rows:
        writer.writerow([
            r.get("occurred_on", "") or "",
            r.get("kind", "expense") or "",
            r.get("amount", "") if r.get("amount") is not None else "",
            r.get("currency", "") or "",
            _csv_safe(r.get("category", "") or ""),
            _csv_safe(r.get("note", "") or ""),
        ])
    return buf.getvalue().encode("utf-8-sig")


def _describe_transaction(data: dict) -> str:
    label = "Income" if data.get("kind") == "income" else "Expense"
    amount = _fmt(_num(data.get("amount")))
    currency = data.get("currency")
    head = f"{label}: {amount}{(' ' + currency) if currency else ''}"
    extras = [x for x in (data.get("category"), data.get("note")) if x]
    if extras:
        head += " (" + ", ".join(extras) + ")"
    if data.get("occurred_on"):
        head += f" on {data['occurred_on']}"
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
            span += f"-{data['end_time']}"
        parts.append(span)
    if data.get("location"):
        parts.append(f"in {data['location']}")
    return ", ".join(parts)


def _describe_task(data: dict) -> str:
    out = data.get("title", "(untitled)")
    if data.get("due_date"):
        out += f", due {data['due_date']}"
    priority = data.get("priority", "normal")
    if priority and priority != "normal":
        out += f", {priority} priority"
    return out
