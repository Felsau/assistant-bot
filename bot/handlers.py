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
from datetime import datetime, timedelta

from ai import classifier
from bot import clock
from db import supabase_client

START_TEXT = (
    "I sort what you send into notes, schedule, tasks, and expenses, and "
    "answer questions about them. Just write normally:\n\n"
    "wifi password is hunter2\n"
    "Math Monday 9-11 room 301\n"
    "submit the report Friday\n"
    "coffee 60   /   salary 30000 in\n"
    "remind me to call the bank at 3pm\n"
    "what's on today?\n\n"
    "Commands: /today /tasks /done /spent /budget /report /find /recurring "
    "/export /help. Voice and receipt photos work too."
)

HELP_TEXT = (
    "Write a note, schedule item, task, or amount and I file it. Ask a "
    "question and I look it up.\n\n"
    "/today   what's on today\n"
    "/tasks   open tasks, with buttons to finish or delete\n"
    "/done <task>   mark a task done\n"
    "/spent   this month's totals by category\n"
    "/budget   set or view monthly budgets (e.g. /budget food 3000)\n"
    "/report   spending vs last month, with a chart\n"
    "/find <text>   search notes, tasks, expenses\n"
    "/recurring   manage monthly recurring expenses\n"
    "/export   download your transactions as CSV\n\n"
    "Set reminders by writing \"remind me to X at 3pm\". Log money with "
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

    if msg_type == "reminder":
        if not data.get("remind_at"):
            row = supabase_client.insert_note(user_id, {"content": text})
            return [_reply(f"Noted: {text}", _delete_markup("notes", row.get("id")))]
        supabase_client.insert_reminder(user_id, data)
        return [_reply(f"Reminder set for {data['remind_at']}: {data.get('text', '').strip()}")]

    if msg_type == "expense":
        if data.get("amount") in (None, ""):
            # No amount detected — keep it as a note rather than lose it.
            row = supabase_client.insert_note(user_id, {"content": text})
            return [_reply(f"Noted: {text}", _delete_markup("notes", row.get("id")))]
        return record_expense(user_id, data)

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
        ok = supabase_client.delete_row(user_id, parts[1], parts[2])
        return {
            "answer": "Deleted" if ok else "Already gone",
            "edit_text": "Deleted" if ok else None,
        }

    return {"answer": "Unknown action", "edit_text": None}


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


def _month_expenses(user_id: str):
    """Return (income, expense_total, {category: spent}) for the current month."""
    start = clock.today().replace(day=1)
    rows = supabase_client.list_transactions(user_id, start.isoformat())
    income = sum(_num(r.get("amount")) for r in rows if r.get("kind") == "income")
    total = sum(_num(r.get("amount")) for r in rows if r.get("kind", "expense") != "income")
    by_category: dict[str, float] = {}
    for r in rows:
        if r.get("kind", "expense") != "income":
            cat = r.get("category") or "other"
            by_category[cat] = by_category.get(cat, 0) + _num(r.get("amount"))
    return income, total, by_category


def _month_summary(user_id: str) -> list[dict]:
    income, expense, by_category = _month_expenses(user_id)
    if not income and not expense:
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

    cur = supabase_client.list_transactions(user_id, cur_start.isoformat())
    prev = supabase_client.list_transactions(
        user_id, prev_start.isoformat(), prev_end.isoformat()
    )

    cur_exp = sum(_num(r.get("amount")) for r in cur if r.get("kind", "expense") != "income")
    prev_exp = sum(_num(r.get("amount")) for r in prev if r.get("kind", "expense") != "income")

    by_category: dict[str, float] = {}
    for r in cur:
        if r.get("kind", "expense") != "income":
            cat = r.get("category") or "other"
            by_category[cat] = by_category.get(cat, 0) + _num(r.get("amount"))

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
            r.get("category", "") or "",
            r.get("note", "") or "",
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
