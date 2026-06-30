"""Thin wrapper around the Supabase client for the bot's tables."""

from __future__ import annotations

import os
from datetime import timedelta

from supabase import Client, create_client

from bot import clock

_client: Client | None = None


def _db() -> Client:
    """Lazily create and cache the Supabase client."""
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_KEY"]
        _client = create_client(url, key)
    return _client


def _first(data) -> dict:
    """Return the first row of an insert/update result, or an empty dict."""
    return (data or [{}])[0] if isinstance(data, list) else (data or {})


# --- users -----------------------------------------------------------------

def upsert_user(user_id: str, chat_id: int) -> None:
    """Remember this user so scheduled jobs know which chat to message."""
    _db().table("users").upsert(
        {"user_id": user_id, "chat_id": chat_id}, on_conflict="user_id"
    ).execute()


def list_users() -> list[dict]:
    return _db().table("users").select("user_id, chat_id").execute().data


# --- inserts ---------------------------------------------------------------

def insert_note(user_id: str, data: dict) -> dict:
    row = {"user_id": user_id, "content": data.get("content", "")}
    return _first(_db().table("notes").insert(row).execute().data)


def insert_schedule(user_id: str, data: dict) -> dict:
    row = {
        "user_id": user_id,
        "title": data.get("title", ""),
        "day_of_week": data.get("day_of_week"),
        "start_time": data.get("start_time"),
        "end_time": data.get("end_time"),
        "location": data.get("location"),
        "notes": data.get("notes"),
    }
    return _first(_db().table("schedule").insert(row).execute().data)


def insert_task(user_id: str, data: dict) -> dict:
    row = {
        "user_id": user_id,
        "title": data.get("title", ""),
        "due_date": data.get("due_date"),
        "priority": data.get("priority", "normal"),
    }
    return _first(_db().table("tasks").insert(row).execute().data)


def insert_transaction(user_id: str, data: dict) -> dict:
    row = {
        "user_id": user_id,
        "kind": data.get("kind", "expense"),
        "amount": data.get("amount"),
        "currency": data.get("currency"),
        "category": data.get("category"),
        "note": data.get("note"),
        "occurred_on": data.get("occurred_on"),
    }
    return _first(_db().table("transactions").insert(row).execute().data)


def list_transactions(
    user_id: str, start: str | None = None, end: str | None = None, limit: int = 200
) -> list[dict]:
    """Return the user's transactions, newest first, within an optional range."""
    q = (
        _db()
        .table("transactions")
        .select("*")
        .eq("user_id", user_id)
        .order("occurred_on", desc=True)
    )
    if start:
        q = q.gte("occurred_on", start)
    if end:
        q = q.lte("occurred_on", end)
    return q.limit(limit).execute().data


def get_task(user_id: str, task_id: str) -> dict | None:
    res = _db().table("tasks").select("*").eq("user_id", user_id).eq("id", task_id).execute()
    return res.data[0] if res.data else None


def set_task_due(user_id: str, task_id: str, due_date: str) -> dict | None:
    res = (
        _db()
        .table("tasks")
        .update({"due_date": due_date})
        .eq("user_id", user_id)
        .eq("id", task_id)
        .execute()
    )
    return res.data[0] if res.data else None


def search(user_id: str, table: str, column: str, q: str, limit: int = 10) -> list[dict]:
    """Case-insensitive substring search within one column the user owns."""
    if table not in ("notes", "schedule", "tasks", "transactions"):
        return []
    return (
        _db()
        .table(table)
        .select("*")
        .eq("user_id", user_id)
        .ilike(column, f"%{q}%")
        .limit(limit)
        .execute()
        .data
    )


# --- reminders -------------------------------------------------------------

def insert_reminder(user_id: str, data: dict) -> dict:
    row = {"user_id": user_id, "text": data.get("text", ""), "remind_at": data.get("remind_at")}
    return _first(_db().table("reminders").insert(row).execute().data)


def due_reminders(before_iso: str, limit: int = 100) -> list[dict]:
    return (
        _db()
        .table("reminders")
        .select("*")
        .eq("sent", False)
        .lte("remind_at", before_iso)
        .order("remind_at")
        .limit(limit)
        .execute()
        .data
    )


def mark_reminder_sent(reminder_id: str) -> None:
    _db().table("reminders").update({"sent": True}).eq("id", reminder_id).execute()


# --- recurring expenses ----------------------------------------------------

def insert_recurring(user_id: str, data: dict) -> dict:
    row = {
        "user_id": user_id,
        "kind": data.get("kind", "expense"),
        "amount": data.get("amount"),
        "currency": data.get("currency"),
        "category": data.get("category"),
        "note": data.get("note"),
        "day_of_month": data.get("day_of_month", 1),
    }
    return _first(_db().table("recurring").insert(row).execute().data)


def list_recurring(user_id: str) -> list[dict]:
    return (
        _db()
        .table("recurring")
        .select("*")
        .eq("user_id", user_id)
        .order("day_of_month")
        .execute()
        .data
    )


def all_recurring(limit: int = 1000) -> list[dict]:
    return _db().table("recurring").select("*").limit(limit).execute().data


def delete_recurring(user_id: str, recurring_id: str) -> bool:
    res = (
        _db().table("recurring").delete().eq("user_id", user_id).eq("id", recurring_id).execute()
    )
    return bool(res.data)


def mark_recurring_posted(recurring_id: str, posted_on: str) -> None:
    _db().table("recurring").update({"last_posted": posted_on}).eq("id", recurring_id).execute()


# --- task completion / deletion -------------------------------------------

def open_tasks(user_id: str, like: str | None = None, limit: int = 20) -> list[dict]:
    """Return the user's incomplete tasks, optionally filtered by title."""
    q = (
        _db()
        .table("tasks")
        .select("*")
        .eq("user_id", user_id)
        .eq("is_done", False)
        .order("due_date")
    )
    if like:
        q = q.ilike("title", f"%{like}%")
    return q.limit(limit).execute().data


def complete_task(user_id: str, task_id: str) -> dict | None:
    """Mark a task done; return the updated row, or None if not found."""
    res = (
        _db()
        .table("tasks")
        .update({"is_done": True})
        .eq("user_id", user_id)
        .eq("id", task_id)
        .execute()
    )
    return res.data[0] if res.data else None


def delete_row(user_id: str, table: str, row_id: str) -> bool:
    """Delete a row the user owns from one of the content tables."""
    if table not in ("notes", "schedule", "tasks", "transactions"):
        return False
    res = (
        _db()
        .table(table)
        .delete()
        .eq("user_id", user_id)
        .eq("id", row_id)
        .execute()
    )
    return bool(res.data)


# --- budgets ---------------------------------------------------------------

def set_budget(user_id: str, category: str, amount: float) -> None:
    _db().table("budgets").upsert(
        {"user_id": user_id, "category": category, "amount": amount},
        on_conflict="user_id,category",
    ).execute()


def delete_budget(user_id: str, category: str) -> None:
    _db().table("budgets").delete().eq("user_id", user_id).eq("category", category).execute()


def get_budgets(user_id: str) -> dict[str, float]:
    rows = (
        _db().table("budgets").select("category, amount").eq("user_id", user_id).execute().data
    )
    return {r["category"]: float(r["amount"]) for r in rows}


# --- queries ---------------------------------------------------------------

def query(user_id: str, scope: str = "all") -> dict:
    """Return relevant rows for a ``query`` request, keyed by table name."""
    db = _db()
    result: dict[str, list] = {}

    if scope in ("today", "week", "tasks", "all"):
        tasks = (
            db.table("tasks")
            .select("*")
            .eq("user_id", user_id)
            .eq("is_done", False)
            .order("due_date")
        )
        if scope == "today":
            tasks = tasks.lte("due_date", clock.today().isoformat())
        elif scope == "week":
            end = (clock.today() + timedelta(days=7)).isoformat()
            tasks = tasks.lte("due_date", end)
        result["tasks"] = tasks.execute().data

    if scope in ("today", "week", "schedule", "all"):
        result["schedule"] = (
            db.table("schedule")
            .select("*")
            .eq("user_id", user_id)
            .order("start_time")
            .execute()
            .data
        )

    if scope in ("expenses", "all"):
        start = clock.today().replace(day=1).isoformat()
        result["transactions"] = (
            db.table("transactions")
            .select("*")
            .eq("user_id", user_id)
            .gte("occurred_on", start)
            .order("occurred_on", desc=True)
            .execute()
            .data
        )

    if scope in ("notes", "all"):
        result["notes"] = (
            db.table("notes")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(20)
            .execute()
            .data
        )

    return result
