"""Thin wrapper around the Supabase client for the bot's three tables."""

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


# --- inserts ---------------------------------------------------------------

def insert_note(user_id: str, data: dict) -> dict:
    row = {"user_id": user_id, "content": data.get("content", "")}
    return _db().table("notes").insert(row).execute().data


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
    return _db().table("schedule").insert(row).execute().data


def insert_task(user_id: str, data: dict) -> dict:
    row = {
        "user_id": user_id,
        "title": data.get("title", ""),
        "due_date": data.get("due_date"),
        "priority": data.get("priority", "normal"),
    }
    return _db().table("tasks").insert(row).execute().data


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
