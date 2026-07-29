"""Tests for the appointments/events system: classification handling, local
time display, and the pre-event heads-up sent by /cron/reminders."""

import asyncio
from datetime import datetime

from ai import classifier
from bot import clock, handlers, telegram_client
from db import supabase_client


def test_event_is_stored_with_aware_local_time(monkeypatch):
    monkeypatch.setattr(classifier, "classify", lambda t: {
        "type": "event",
        "data": {"title": "Dentist", "date": "2026-07-20", "start_time": "14:00",
                 "end_time": "15:00", "location": "clinic"},
    })
    captured = {}
    monkeypatch.setattr(supabase_client, "insert_event",
                        lambda uid, d: captured.update(d) or {"id": "ev1"})
    replies = handlers.handle_message("u1", "dentist July 20 at 2pm")

    assert captured["starts_at"] == "2026-07-20T14:00:00+07:00"
    assert captured["end_at"] == "2026-07-20T15:00:00+07:00"
    assert "Dentist" in replies[0]["text"]
    assert "I'll ping you 30 min before." in replies[0]["text"]
    # Delete button targets the events table so Undo can restore it.
    assert replies[0]["reply_markup"] == {"inline_keyboard": [[
        {"text": "Delete", "callback_data": "del:events:ev1"},
    ]]}


def test_event_with_date_but_no_time_becomes_task(monkeypatch):
    monkeypatch.setattr(classifier, "classify", lambda t: {
        "type": "event",
        "data": {"title": "Dentist", "date": "2026-07-20", "start_time": None},
    })
    captured = {}
    monkeypatch.setattr(supabase_client, "insert_task",
                        lambda uid, d: captured.update(d) or {"id": "t1"})
    replies = handlers.handle_message("u1", "dentist on July 20")

    assert captured == {"title": "Dentist", "due_date": "2026-07-20"}
    assert replies[0]["text"].startswith("Task added:")


def test_event_with_no_date_becomes_note(monkeypatch):
    monkeypatch.setattr(classifier, "classify", lambda t: {
        "type": "event", "data": {"title": "Dentist sometime"},
    })
    captured = {}
    monkeypatch.setattr(supabase_client, "insert_note",
                        lambda uid, d: captured.update(d) or {"id": "n1"})
    replies = handlers.handle_message("u1", "dentist sometime soon")

    assert captured["content"] == "dentist sometime soon"
    assert replies[0]["text"].startswith("Noted:")


def test_describe_event_shows_local_time_for_utc_row():
    # Supabase returns starts_at in UTC; 07:00Z is 14:00 in Asia/Bangkok.
    text = handlers._describe_event({
        "title": "Dentist", "starts_at": "2026-07-20T07:00:00+00:00",
        "location": "clinic",
    })
    assert "14:00" in text
    assert "07:00" not in text
    assert "at clinic" in text


def _cron_env(monkeypatch, events):
    """Wire up fire_reminders with no due reminders and the given events."""
    import main

    monkeypatch.setattr(main, "_CRON_SECRET", "testsecret")
    monkeypatch.setattr(supabase_client, "list_users",
                        lambda: [{"user_id": "u1", "chat_id": 111}])
    monkeypatch.setattr(supabase_client, "due_reminders", lambda before: [])
    monkeypatch.setattr(supabase_client, "unnotified_events", lambda before: events)
    marked = []
    monkeypatch.setattr(supabase_client, "mark_event_notified", marked.append)
    sent = []

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append((chat_id, text))

    monkeypatch.setattr(telegram_client, "send_message", fake_send)
    return main, sent, marked


def test_cron_sends_event_heads_up(monkeypatch):
    # Freeze "now" at 10:00 Bangkok; the event starts at 10:20 (stored as UTC).
    monkeypatch.setattr(clock, "now",
                        lambda: datetime.fromisoformat("2026-07-13T10:00:00+07:00"))
    main, sent, marked = _cron_env(monkeypatch, [{
        "id": "ev1", "user_id": "u1", "title": "dentist",
        "starts_at": "2026-07-13T03:20:00+00:00", "location": "clinic",
    }])

    result = asyncio.run(main.fire_reminders(secret="testsecret", x_cron_secret=None))

    assert result == {"ok": True, "sent": 0, "events": 1}
    assert sent == [(111, "Coming up at 10:20: dentist (clinic)")]
    assert marked == ["ev1"]


def test_cron_retires_stale_event_without_announcing(monkeypatch):
    monkeypatch.setattr(clock, "now",
                        lambda: datetime.fromisoformat("2026-07-13T10:00:00+07:00"))
    main, sent, marked = _cron_env(monkeypatch, [{
        "id": "ev1", "user_id": "u1", "title": "dentist",
        "starts_at": "2026-07-10T03:20:00+00:00",  # three days ago
    }])

    result = asyncio.run(main.fire_reminders(secret="testsecret", x_cron_secret=None))

    # Marked as handled so it stops being scanned, but no late "coming up" ping.
    assert result == {"ok": True, "sent": 0, "events": 0}
    assert sent == []
    assert marked == ["ev1"]
