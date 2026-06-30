"""Tests for reminders, find, recurring, snooze, report, receipt splitting."""

from ai import classifier
from bot import handlers
from db import supabase_client


def test_reminder_routing(monkeypatch):
    monkeypatch.setattr(classifier, "classify", lambda t: {
        "type": "reminder",
        "data": {"text": "call the bank", "remind_at": "2026-06-30 15:00"},
    })
    captured = {}
    monkeypatch.setattr(supabase_client, "insert_reminder",
                        lambda uid, d: captured.update(d) or {"id": "rm1"})
    replies = handlers.handle_message("u1", "remind me to call the bank at 3pm")
    assert "Reminder set for 2026-06-30 15:00" in replies[0]["text"]
    assert captured["text"] == "call the bank"


def test_find_across_tables(monkeypatch):
    def fake_search(uid, table, column, q, limit=10):
        return {
            "notes": [{"id": "n1", "content": "bank pin 1234"}],
            "tasks": [{"id": "t1", "title": "call bank"}],
            "transactions": [],
        }[table]
    monkeypatch.setattr(supabase_client, "search", fake_search)
    replies = handlers.handle_message("u1", "/find bank")
    texts = " ".join(r["text"] for r in replies)
    assert "Note: bank pin 1234" in texts
    assert "Task: call bank" in texts


def test_recurring_add(monkeypatch):
    captured = {}
    monkeypatch.setattr(supabase_client, "insert_recurring",
                        lambda uid, d: captured.update(d) or {"id": "rc1"})
    replies = handlers.handle_message("u1", "/recurring add 8000 housing rent 1")
    assert captured["amount"] == 8000.0
    assert captured["category"] == "housing"
    assert captured["note"] == "rent"
    assert captured["day_of_month"] == 1
    assert "Recurring set" in replies[0]["text"]


def test_snooze_callback(monkeypatch):
    monkeypatch.setattr(supabase_client, "get_task",
                        lambda uid, tid: {"id": tid, "title": "pay bill", "due_date": "2026-06-30"})
    saved = {}
    monkeypatch.setattr(supabase_client, "set_task_due",
                        lambda uid, tid, due: saved.update(due=due))
    result = handlers.handle_callback("u1", "snooze:t1")
    assert saved["due"] == "2026-07-01"
    assert "2026-07-01" in result["edit_text"]
    assert result["reply_markup"] is not None  # buttons preserved


def test_receipt_splits_into_items(monkeypatch):
    inserted = []
    monkeypatch.setattr(supabase_client, "insert_transaction",
                        lambda uid, d: inserted.append(d) or {"id": f"x{len(inserted)}"})
    monkeypatch.setattr(supabase_client, "get_budgets", lambda uid: {})
    data = {
        "kind": "expense", "amount": 150, "note": "Store",
        "items": [
            {"amount": 100, "category": "food", "note": "lunch"},
            {"amount": 50, "category": "transport", "note": "taxi"},
        ],
    }
    replies = handlers.record_receipt("u1", data)
    assert len(inserted) == 2
    assert len(replies) == 2
    assert {i["category"] for i in inserted} == {"food", "transport"}


def test_receipt_single_total_when_no_items(monkeypatch):
    inserted = []
    monkeypatch.setattr(supabase_client, "insert_transaction",
                        lambda uid, d: inserted.append(d) or {"id": "x1"})
    monkeypatch.setattr(supabase_client, "get_budgets", lambda uid: {})
    replies = handlers.record_receipt(
        "u1", {"kind": "expense", "amount": 150, "category": "food"})
    assert len(inserted) == 1
    assert len(replies) == 1


def test_report_text(monkeypatch):
    def fake_list(uid, start=None, end=None, limit=200):
        if end is None:  # current month
            return [{"kind": "expense", "amount": 100, "category": "food"}]
        return [{"kind": "expense", "amount": 50, "category": "food"}]
    monkeypatch.setattr(supabase_client, "list_transactions", fake_list)
    text, by_cat = handlers.report_text("u1")
    assert "Spent: 100" in text
    assert "100%" in text  # +100% vs last month
    assert by_cat == {"food": 100}


def test_budget_warning_triggers_at_80pct(monkeypatch):
    monkeypatch.setattr(supabase_client, "get_budgets", lambda uid: {"food": 100})
    monkeypatch.setattr(supabase_client, "list_transactions",
                        lambda uid, start: [{"kind": "expense", "amount": 90, "category": "food"}])
    warn = handlers.budget_warnings("u1")
    assert warn is not None
    assert "food: 90 / 100" in warn


def test_budget_warning_silent_below_threshold(monkeypatch):
    monkeypatch.setattr(supabase_client, "get_budgets", lambda uid: {"food": 100})
    monkeypatch.setattr(supabase_client, "list_transactions",
                        lambda uid, start: [{"kind": "expense", "amount": 40, "category": "food"}])
    assert handlers.budget_warnings("u1") is None
