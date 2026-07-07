"""Tests for Group 2 features: recurring reminders, weekly summary, undo."""

from datetime import datetime

from ai import classifier
from bot import clock, handlers
from db import supabase_client


# --- recurring reminders ---------------------------------------------------

def test_repeating_reminder_is_stored_and_announced(monkeypatch):
    monkeypatch.setattr(classifier, "classify", lambda t: {
        "type": "reminder",
        "data": {"text": "stretch", "remind_at": "2026-07-06 09:00", "repeat": "weekly"},
    })
    captured = {}
    monkeypatch.setattr(supabase_client, "insert_reminder",
                        lambda uid, d: captured.update(d) or {"id": "rm1"})
    replies = handlers.handle_message("u1", "remind me to stretch every Monday 9am")
    assert captured["repeat"] == "weekly"
    assert captured["remind_at"].endswith("+07:00")   # still timezone-stamped
    assert "(repeats weekly)" in replies[0]["text"]


def test_next_occurrence_daily_and_weekly():
    now = clock.now()
    start = (now.replace(hour=9, minute=0, second=0, microsecond=0)
             ).replace(day=1)  # earlier this month at 09:00

    nxt = datetime.fromisoformat(clock.next_occurrence(start.isoformat(), "daily"))
    assert nxt > now and nxt.hour == 9 and nxt.minute == 0

    nxt_w = datetime.fromisoformat(clock.next_occurrence(start.isoformat(), "weekly"))
    assert nxt_w > now and nxt_w.weekday() == start.weekday()


def test_next_occurrence_monthly_clamps_day():
    # Jan 31 + 1 month → Feb 28 (2026 is not a leap year).
    jan31 = datetime(2026, 1, 31, 9, 0, tzinfo=clock._TZ)
    feb = clock._add_month(jan31)
    assert (feb.month, feb.day) == (2, 28)
    # December rolls the year over.
    dec = clock._add_month(datetime(2026, 12, 15, 9, 0, tzinfo=clock._TZ))
    assert (dec.year, dec.month, dec.day) == (2027, 1, 15)


def test_next_occurrence_none_for_oneoff_and_junk():
    assert clock.next_occurrence("2026-06-30T15:00:00+07:00", None) is None
    assert clock.next_occurrence("2026-06-30T15:00:00+07:00", "yearly") is None
    assert clock.next_occurrence("not a date", "daily") is None


# --- weekly summary --------------------------------------------------------

def test_week_summary(monkeypatch):
    rows = [
        {"kind": "expense", "amount": 60, "category": "food"},
        {"kind": "expense", "amount": 40, "category": "transport"},
        {"kind": "income", "amount": 500},
    ]
    monkeypatch.setattr(supabase_client, "list_transactions", lambda uid, start: rows)
    text = handlers.handle_message("u1", "/week")[0]["text"]
    assert "This week" in text
    assert "Spent: 100" in text
    assert "Income: 500" in text
    assert "Net: 400" in text


def test_week_summary_empty(monkeypatch):
    monkeypatch.setattr(supabase_client, "list_transactions", lambda uid, start: [])
    text = handlers.handle_message("u1", "/week")[0]["text"]
    assert "Nothing logged this week" in text


# --- undo after delete -----------------------------------------------------

def test_delete_offers_undo_button(monkeypatch):
    handlers._UNDO.clear()
    monkeypatch.setattr(supabase_client, "delete_row",
                        lambda uid, table, rid: {"id": "n1", "content": "buy milk"})
    result = handlers.handle_callback("u1", "del:notes:n1")
    assert result["answer"] == "Deleted"
    button = result["reply_markup"]["inline_keyboard"][0][0]
    assert button["callback_data"] == "undo:n1"
    assert "n1" in handlers._UNDO


def test_undo_restores_the_row(monkeypatch):
    handlers._UNDO.clear()
    handlers._remember_undo("n1", "u1", "notes", {"id": "n1", "content": "buy milk"})
    restored = {}
    monkeypatch.setattr(supabase_client, "insert_note",
                        lambda uid, d: restored.update(d) or {"id": "n2"})
    result = handlers.handle_callback("u1", "undo:n1")
    assert result["answer"] == "Restored"
    assert restored["content"] == "buy milk"
    assert "n1" not in handlers._UNDO   # consumed


def test_undo_rejects_unknown_or_foreign_token():
    handlers._UNDO.clear()
    handlers._remember_undo("n1", "u1", "notes", {"id": "n1", "content": "x"})
    assert handlers.handle_callback("u2", "undo:n1")["answer"] == "Nothing to undo"
    assert handlers.handle_callback("u1", "undo:nope")["answer"] == "Nothing to undo"


def test_delete_missing_row_reports_gone(monkeypatch):
    monkeypatch.setattr(supabase_client, "delete_row", lambda uid, table, rid: None)
    result = handlers.handle_callback("u1", "del:notes:zzz")
    assert result["answer"] == "Already gone"
    assert result["edit_text"] is None
