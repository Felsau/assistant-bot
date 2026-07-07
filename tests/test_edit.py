"""Tests for natural-language editing of saved entries."""

from ai import classifier
from bot import handlers
from db import supabase_client


def _edit(target, match, changes):
    return {"type": "edit", "data": {"target": target, "match": match, "changes": changes}}


def test_edit_expense_single_match(monkeypatch):
    monkeypatch.setattr(classifier, "classify",
                        lambda t: _edit("expense", "coffee", {"amount": 80}))
    monkeypatch.setattr(supabase_client, "search",
                        lambda uid, table, col, q, limit=6:
                        [{"id": "x1", "note": "coffee", "amount": 60, "kind": "expense"}])
    captured = {}
    monkeypatch.setattr(supabase_client, "update_row",
                        lambda uid, table, rid, ch: captured.update(table=table, rid=rid, ch=ch)
                        or {"id": rid, "kind": "expense", "amount": 80, "note": "coffee"})
    replies = handlers.handle_message("u1", "change coffee to 80")
    assert captured == {"table": "transactions", "rid": "x1", "ch": {"amount": 80.0}}
    assert "Updated" in replies[0]["text"]
    assert "80" in replies[0]["text"]


def test_edit_task_due_date(monkeypatch):
    monkeypatch.setattr(classifier, "classify",
                        lambda t: _edit("task", "report", {"due_date": "2026-07-10"}))
    monkeypatch.setattr(supabase_client, "search",
                        lambda uid, table, col, q, limit=6: [{"id": "t1", "title": "report"}])
    monkeypatch.setattr(supabase_client, "update_row",
                        lambda uid, table, rid, ch: {"id": rid, "title": "report", **ch})
    replies = handlers.handle_message("u1", "move the report task to July 10")
    assert "Updated" in replies[0]["text"]
    assert "2026-07-10" in replies[0]["text"]
    # a task keeps its Done / +1 day / Delete buttons
    assert replies[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "done:t1"


def test_edit_no_match(monkeypatch):
    monkeypatch.setattr(classifier, "classify",
                        lambda t: _edit("expense", "yacht", {"amount": 80}))
    monkeypatch.setattr(supabase_client, "search", lambda uid, table, col, q, limit=6: [])
    replies = handlers.handle_message("u1", "change yacht to 80")
    assert 'Couldn\'t find' in replies[0]["text"]


def test_edit_ambiguous_offers_choice(monkeypatch):
    handlers._PENDING_EDITS.clear()
    monkeypatch.setattr(classifier, "classify",
                        lambda t: _edit("expense", "lunch", {"amount": 120}))
    monkeypatch.setattr(supabase_client, "search",
                        lambda uid, table, col, q, limit=6: [
                            {"id": "x1", "note": "lunch", "kind": "expense", "amount": 60},
                            {"id": "x2", "note": "lunch", "kind": "expense", "amount": 90},
                        ])
    replies = handlers.handle_message("u1", "change lunch to 120")
    assert "Which one" in replies[0]["text"]
    # one button per candidate, all sharing the same pending token
    tokens = {r["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[2]
              for r in replies[1:]}
    assert len(tokens) == 1
    assert tokens.pop() in handlers._PENDING_EDITS


def test_edit_callback_applies_change(monkeypatch):
    handlers._PENDING_EDITS.clear()
    handlers._remember(handlers._PENDING_EDITS, handlers._PENDING_MAX,
                       "tok123", ("u1", "transactions", {"amount": 120.0}))
    captured = {}
    monkeypatch.setattr(supabase_client, "update_row",
                        lambda uid, table, rid, ch: captured.update(rid=rid, ch=ch)
                        or {"id": rid, "kind": "expense", "amount": 120, "note": "lunch"})
    result = handlers.handle_callback("u1", "edit:x2:tok123")
    assert result["answer"] == "Updated"
    assert captured == {"rid": "x2", "ch": {"amount": 120.0}}


def test_edit_callback_rejects_foreign_or_expired_token():
    handlers._PENDING_EDITS.clear()
    handlers._remember(handlers._PENDING_EDITS, handlers._PENDING_MAX,
                       "tok123", ("u1", "transactions", {"amount": 1}))
    assert handlers.handle_callback("u2", "edit:x2:tok123")["answer"] == "That edit expired"
    assert handlers.handle_callback("u1", "edit:x2:nope")["answer"] == "That edit expired"


def test_unknown_edit_target(monkeypatch):
    monkeypatch.setattr(classifier, "classify",
                        lambda t: _edit("schedule", "class", {"start_time": "10:00"}))
    replies = handlers.handle_message("u1", "change my class to 10am")
    assert "tasks, expenses, and notes" in replies[0]["text"]


def test_update_row_whitelist_blocks_unknown_fields():
    # Pure early-return paths: no DB client needed.
    assert supabase_client.update_row("u1", "tasks", "t1", {"amount": 5}) is None
    assert supabase_client.update_row("u1", "bogus", "t1", {"title": "x"}) is None
    assert supabase_client.update_row("u1", "notes", "n1", {}) is None
