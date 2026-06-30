"""Tests for budgets, budget alerts, and CSV export (clients mocked)."""

from bot import handlers
from db import supabase_client


def test_set_category_budget(monkeypatch):
    calls = {}
    monkeypatch.setattr(supabase_client, "set_budget",
                        lambda uid, cat, amt: calls.update(cat=cat, amt=amt))
    replies = handlers.handle_message("u1", "/budget food 3000")
    assert calls == {"cat": "food", "amt": 3000.0}
    assert "food" in replies[0]["text"]


def test_set_overall_budget(monkeypatch):
    calls = {}
    monkeypatch.setattr(supabase_client, "set_budget",
                        lambda uid, cat, amt: calls.update(cat=cat, amt=amt))
    handlers.handle_message("u1", "/budget 20000")
    assert calls == {"cat": "total", "amt": 20000.0}


def test_remove_budget(monkeypatch):
    removed = {}
    monkeypatch.setattr(supabase_client, "delete_budget",
                        lambda uid, cat: removed.update(cat=cat))
    handlers.handle_message("u1", "/budget food off")
    assert removed == {"cat": "food"}


def test_budget_status_shows_progress(monkeypatch):
    monkeypatch.setattr(supabase_client, "get_budgets",
                        lambda uid: {"total": 20000, "food": 3000})
    monkeypatch.setattr(supabase_client, "list_transactions", lambda uid, start: [
        {"kind": "expense", "amount": 3500, "category": "food"},
        {"kind": "expense", "amount": 1000, "category": "transport"},
    ])
    text = handlers.handle_message("u1", "/budget")[0]["text"]
    assert "total: 4,500 / 20,000" in text
    assert "food: 3,500 / 3,000 — over by 500" in text


def test_expense_appends_budget_alert(monkeypatch):
    monkeypatch.setattr(supabase_client, "insert_transaction", lambda uid, d: {"id": "x1"})
    monkeypatch.setattr(supabase_client, "get_budgets", lambda uid: {"food": 100})
    monkeypatch.setattr(supabase_client, "list_transactions", lambda uid, start: [
        {"kind": "expense", "amount": 120, "category": "food"},
    ])
    text = handlers.record_expense("u1", {"kind": "expense", "amount": 120, "category": "food"})[0]["text"]
    assert "Expense: 120" in text
    assert "food: 120 / 100 — over by 20" in text


def test_no_budget_means_no_alert(monkeypatch):
    monkeypatch.setattr(supabase_client, "insert_transaction", lambda uid, d: {"id": "x2"})
    monkeypatch.setattr(supabase_client, "get_budgets", lambda uid: {})
    text = handlers.record_expense("u1", {"kind": "expense", "amount": 50, "category": "food"})[0]["text"]
    assert "\n" not in text  # just the confirmation line


def test_build_csv():
    rows = [
        {"occurred_on": "2026-06-30", "kind": "expense", "amount": 60,
         "currency": "THB", "category": "food", "note": "coffee"},
    ]
    out = handlers.build_transactions_csv(rows).decode("utf-8-sig")
    assert "date,kind,amount,currency,category,note" in out
    assert "2026-06-30,expense,60,THB,food,coffee" in out
