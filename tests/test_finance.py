"""Tests for the bookkeeping feature (clients mocked)."""

from ai import classifier
from bot import handlers
from db import supabase_client


def test_expense_is_recorded(monkeypatch):
    monkeypatch.setattr(classifier, "classify", lambda t: {
        "type": "expense",
        "data": {"kind": "expense", "amount": 60, "category": "food", "note": "coffee"},
    })
    monkeypatch.setattr(supabase_client, "insert_transaction", lambda uid, d: {"id": "x1"})

    replies = handlers.handle_message("u1", "coffee 60")
    assert "Expense" in replies[0]["text"]
    assert "60" in replies[0]["text"]
    assert replies[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "del:transactions:x1"


def test_income_is_recorded(monkeypatch):
    monkeypatch.setattr(classifier, "classify", lambda t: {
        "type": "expense",
        "data": {"kind": "income", "amount": 30000, "note": "salary"},
    })
    monkeypatch.setattr(supabase_client, "insert_transaction", lambda uid, d: {"id": "x2"})

    replies = handlers.handle_message("u1", "salary 30000 in")
    assert "Income" in replies[0]["text"]


def test_expense_without_amount_falls_back_to_note(monkeypatch):
    monkeypatch.setattr(classifier, "classify", lambda t: {
        "type": "expense", "data": {"kind": "expense", "amount": None},
    })
    monkeypatch.setattr(supabase_client, "insert_note", lambda uid, d: {"id": "n9"})

    replies = handlers.handle_message("u1", "spent some money")
    assert "Noted" in replies[0]["text"]


def test_spent_summary(monkeypatch):
    rows = [
        {"kind": "expense", "amount": 60, "category": "food"},
        {"kind": "expense", "amount": 40, "category": "food"},
        {"kind": "expense", "amount": 80, "category": "transport"},
        {"kind": "income", "amount": 1000},
    ]
    monkeypatch.setattr(supabase_client, "list_transactions", lambda uid, start: rows)

    replies = handlers.handle_message("u1", "/spent")
    text = replies[0]["text"]
    assert "Spent: 180" in text
    assert "Income: 1,000" in text
    assert "Net: 820" in text
    assert "food 100" in text
