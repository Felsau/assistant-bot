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
    monkeypatch.setattr(supabase_client, "list_transactions", lambda uid, start: rows)
    text = handlers.handle_message("u1", "/spent")[0]["text"]
    assert "Spent: 100" in text          # not 140 — USD is not summed in
    assert "140" not in text
    assert "Other currencies (not included):" in text
    assert "USD 40" in text


def test_foreign_currency_not_counted_in_budget(monkeypatch):
    monkeypatch.setattr(supabase_client, "get_budgets", lambda uid: {"food": 100})
    monkeypatch.setattr(supabase_client, "list_transactions", lambda uid, start: [
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
