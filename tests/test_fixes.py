"""Tests for the correctness fixes: reminder timezone, multi-currency totals,
Telegram message splitting, and webhook de-duplication."""

from bot import clock, handlers, telegram_client
from db import supabase_client


# --- reminder timezone -----------------------------------------------------

def test_to_aware_iso_stamps_local_offset():
    # Default timezone is Asia/Bangkok (+07:00).
    out = clock.to_aware_iso("2026-06-30 15:00")
    assert out.startswith("2026-06-30T15:00")
    assert out.endswith("+07:00")


def test_to_aware_iso_passes_through_offset_and_junk():
    assert clock.to_aware_iso("2026-06-30T15:00:00+00:00") == "2026-06-30T15:00:00+00:00"
    assert clock.to_aware_iso("not a date") == "not a date"
    assert clock.to_aware_iso(None) is None


def test_reminder_is_stored_with_timezone(monkeypatch):
    from ai import classifier
    monkeypatch.setattr(classifier, "classify", lambda t: {
        "type": "reminder",
        "data": {"text": "call the bank", "remind_at": "2026-06-30 15:00"},
    })
    captured = {}
    monkeypatch.setattr(supabase_client, "insert_reminder",
                        lambda uid, d: captured.update(d) or {"id": "rm1"})
    replies = handlers.handle_message("u1", "remind me to call the bank at 3pm")
    # Stored value carries the offset so timestamptz doesn't shift it to UTC...
    assert captured["remind_at"].endswith("+07:00")
    # ...but the reply still shows the friendly local time.
    assert "Reminder set for 2026-06-30 15:00" in replies[0]["text"]


# --- multi-currency totals -------------------------------------------------

def test_spent_excludes_foreign_currency(monkeypatch):
    rows = [
        {"kind": "expense", "amount": 100, "category": "food"},              # THB (base)
        {"kind": "expense", "amount": 40, "currency": "USD", "category": "food"},
        {"kind": "income", "amount": 1000},
    ]
    monkeypatch.setattr(supabase_client, "list_transactions", lambda uid, start, **kw: rows)
    text = handlers.handle_message("u1", "/spent")[0]["text"]
    assert "Spent: 100" in text          # not 140 — USD is not summed in
    assert "140" not in text
    assert "Other currencies (not included):" in text
    assert "USD 40" in text


def test_foreign_currency_not_counted_in_budget(monkeypatch):
    monkeypatch.setattr(supabase_client, "get_budgets", lambda uid: {"food": 100})
    monkeypatch.setattr(supabase_client, "list_transactions", lambda uid, start, **kw: [
        {"kind": "expense", "amount": 50, "category": "food"},
        {"kind": "expense", "amount": 500, "currency": "USD", "category": "food"},
    ])
    warn = handlers.budget_warnings("u1")
    # 50 THB is under 80% of the 100 budget; the 500 USD must not tip it over.
    assert warn is None


# --- Telegram message splitting --------------------------------------------

def test_split_short_message_is_one_chunk():
    assert telegram_client._split_message("hello") == ["hello"]


def test_split_long_message_respects_limit():
    text = "x" * 5000
    chunks = telegram_client._split_message(text)
    assert len(chunks) == 2
    assert all(len(c) <= 4096 for c in chunks)
    assert "".join(chunks) == text


def test_split_prefers_newline_boundaries():
    text = "\n".join(["line"] * 2000)  # ~10k chars, breakable on newlines
    chunks = telegram_client._split_message(text)
    assert all(len(c) <= 4096 for c in chunks)
    assert all(not c.startswith("\n") for c in chunks)


# --- webhook de-duplication ------------------------------------------------

def test_already_processed_dedups_update_ids():
    import main
    uid = 987654321
    assert main._already_processed(uid) is False   # first sight
    assert main._already_processed(uid) is True     # redelivery
    assert main._already_processed(uid + 1) is False
    assert main._already_processed(None) is False    # no id → never dedup


# --- missing occurred_on defaulting -----------------------------------------

class _FakeTable:
    """Minimal stand-in for the supabase query-builder chain."""

    def __init__(self, captured: dict):
        self._captured = captured

    def insert(self, row):
        self._captured.update(row)
        return self

    def execute(self):
        return type("Res", (), {"data": [dict(self._captured, id="tx1")]})()


class _FakeDB:
    def __init__(self, captured: dict):
        self._captured = captured

    def table(self, name):
        return _FakeTable(self._captured)


def test_insert_transaction_defaults_missing_occurred_on(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(supabase_client, "_db", lambda: _FakeDB(captured))
    supabase_client.insert_transaction("u1", {"kind": "expense", "amount": 60, "category": "food"})
    # An explicit null would override the column's `default current_date`, and
    # every summary filters on occurred_on — so a NULL date silently vanishes
    # from /spent, /week, /report, and budget alerts.
    assert captured["occurred_on"] == clock.today().isoformat()


def test_insert_transaction_keeps_explicit_occurred_on(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(supabase_client, "_db", lambda: _FakeDB(captured))
    supabase_client.insert_transaction("u1", {"amount": 60, "occurred_on": "2026-01-15"})
    assert captured["occurred_on"] == "2026-01-15"


# --- monthly reminder day drift ---------------------------------------------

def test_next_occurrence_monthly_uses_anchor_day_not_clamped_day(monkeypatch):
    from datetime import datetime
    # Freeze "now" between Jan 31 and Feb 28 so each call only rolls forward
    # one month, isolating the clamp-then-recover behavior we're testing.
    monkeypatch.setattr(clock, "now", lambda: datetime.fromisoformat("2026-02-05T00:00:00+07:00"))
    due = "2026-01-31T09:00:00+07:00"
    next_at = clock.next_occurrence(due, "monthly", anchor_day=31)
    assert next_at.startswith("2026-02-28")  # clamped for February

    monkeypatch.setattr(clock, "now", lambda: datetime.fromisoformat("2026-03-05T00:00:00+07:00"))
    # Roll forward again from the (clamped) Feb value, still passing anchor_day.
    next_at2 = clock.next_occurrence(next_at, "monthly", anchor_day=31)
    assert next_at2.startswith("2026-03-31")  # back to the real day in March


def test_next_occurrence_monthly_without_anchor_day_stays_clamped(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(clock, "now", lambda: datetime.fromisoformat("2026-02-05T00:00:00+07:00"))
    # Without an anchor_day (e.g. old rows from before the migration), the
    # existing clamped-day behavior is preserved rather than erroring.
    next_at = clock.next_occurrence("2026-01-31T09:00:00+07:00", "monthly")
    assert next_at.startswith("2026-02-28")


def test_insert_reminder_captures_anchor_day(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(supabase_client, "_db", lambda: _FakeDB(captured))
    supabase_client.insert_reminder("u1", {
        "text": "pay rent", "remind_at": "2026-01-31T09:00:00+07:00", "repeat": "monthly",
    })
    assert captured["anchor_day"] == 31


def test_next_occurrence_steps_in_local_calendar_not_utc(monkeypatch):
    from datetime import datetime
    # A reminder anchored on the 1st at 00:30 Bangkok local is stored by
    # Supabase as UTC: Dec 31 2025 17:30Z. Stepping the raw UTC value would
    # advance (year, month) from Dec 2025, landing on Jan 2026 — one local
    # calendar month short of the correct Feb 2026 target.
    monkeypatch.setattr(clock, "now", lambda: datetime.fromisoformat("2026-01-01T01:00:00+07:00"))
    next_at = clock.next_occurrence("2025-12-31T17:30:00+00:00", "monthly", anchor_day=1)
    local = datetime.fromisoformat(next_at).astimezone(clock._TZ)
    assert (local.year, local.month, local.day) == (2026, 2, 1)
    assert (local.hour, local.minute) == (0, 30)


# --- recurring-expense cron catch-up ----------------------------------------

def test_cron_recurring_catches_up_a_missed_day(monkeypatch):
    import asyncio
    from datetime import date

    import main

    monkeypatch.setattr(main, "_CRON_SECRET", "testsecret")
    monkeypatch.setattr(clock, "today", lambda: date(2026, 7, 13))  # cron missed the 10th
    monkeypatch.setattr(supabase_client, "all_recurring", lambda: [
        {"id": "r1", "user_id": "u1", "kind": "expense", "amount": 500,
         "currency": None, "category": "housing", "note": "rent",
         "day_of_month": 10, "last_posted": None},
    ])
    inserted: dict = {}
    posted_calls = []
    monkeypatch.setattr(supabase_client, "insert_transaction",
                        lambda uid, data: inserted.update(data) or {"id": "tx1"})
    monkeypatch.setattr(supabase_client, "mark_recurring_posted",
                        lambda rid, posted_on: posted_calls.append((rid, posted_on)))

    result = asyncio.run(main.post_recurring(secret="testsecret", x_cron_secret=None))
    assert result == {"ok": True, "posted": 1}
    # Backdated to the intended day, not the late catch-up date.
    assert inserted["occurred_on"] == "2026-07-10"
    assert posted_calls == [("r1", "2026-07-13")]


def test_cron_recurring_skips_before_target_day(monkeypatch):
    import asyncio
    from datetime import date

    import main

    monkeypatch.setattr(main, "_CRON_SECRET", "testsecret")
    monkeypatch.setattr(clock, "today", lambda: date(2026, 7, 5))  # before the 10th
    monkeypatch.setattr(supabase_client, "all_recurring", lambda: [
        {"id": "r1", "user_id": "u1", "amount": 500, "day_of_month": 10, "last_posted": None},
    ])
    called = []
    monkeypatch.setattr(supabase_client, "insert_transaction",
                        lambda uid, data: called.append(data) or {"id": "tx1"})

    result = asyncio.run(main.post_recurring(secret="testsecret", x_cron_secret=None))
    assert result == {"ok": True, "posted": 0}
    assert called == []


# --- natural-language edit: unparseable amount ------------------------------

def test_edit_amount_unparseable_is_dropped_not_zeroed(monkeypatch):
    monkeypatch.setattr(supabase_client, "search", lambda uid, table, col, q, **kw: [
        {"id": "tx1", "amount": 60, "category": "food", "note": "coffee"},
    ])
    updated = {}
    monkeypatch.setattr(supabase_client, "update_row",
                        lambda uid, table, rid, changes: updated.update(changes) or None)
    handlers._handle_edit("u1", {
        "target": "expense", "match": "coffee", "changes": {"amount": "not a number"},
    })
    # The bad amount must be dropped, not silently coerced to 0.
    assert "amount" not in updated


# --- foreign-currency income surfaced ---------------------------------------

def test_foreign_income_is_surfaced_not_dropped(monkeypatch):
    rows = [
        {"kind": "income", "amount": 2000, "currency": "USD"},
    ]
    monkeypatch.setattr(supabase_client, "list_transactions", lambda uid, start, **kw: rows)
    text = handlers.handle_message("u1", "/spent")[0]["text"]
    assert "Other currencies (not included):" in text
    assert "USD 2,000 income" in text


# --- Telegram empty message guard -------------------------------------------

def test_send_message_never_sends_empty_text(monkeypatch):
    import asyncio

    posts = []

    async def fake_post(method, payload):
        posts.append(payload)
        return {}

    monkeypatch.setattr(telegram_client, "_post", fake_post)
    asyncio.run(telegram_client.send_message(123, ""))
    assert posts and posts[0]["text"] == " "
