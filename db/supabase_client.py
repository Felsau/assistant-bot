"""Thin wrapper around the Supabase client for the bot's tables."""

from __future__ import annotations

import os
from datetime import date, timedelta

from supabase import Client, create_client

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


def list_transactions(user_id: str, start: str | None = None, limit: int = 200) -> list[dict]:
    """Return the user's transactions, newest first, optionally since ``start``."""
    q = (
        _db()
        .table("transactions")
        .select("*")
        .eq("user_id", user_id)
        .order("occurred_on", desc=True)
    )
    if start:
        q = q.gte("occurred_on", start)
    return q.limit(limit).execute().data


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
            tasks = tasks.lte("due_date", date.today().isoformat())
        elif scope == "week":
            end = (date.today() + timedelta(days=7)).isoformat()
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
        start = date.today().replace(day=1).isoformat()
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
