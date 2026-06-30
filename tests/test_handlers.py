"""Tests for message routing and button handling (classifier + DB are mocked)."""

from ai import classifier
from bot import handlers
from db import supabase_client


def test_note_is_saved_with_delete_button(monkeypatch):
    monkeypatch.setattr(classifier, "classify",
                        lambda t: {"type": "note", "data": {"content": "buy milk"}})
    monkeypatch.setattr(supabase_client, "insert_note", lambda uid, d: {"id": "n1"})

    replies = handlers.handle_message("u1", "remember buy milk")
    assert "Noted" in replies[0]["text"]
    button = replies[0]["reply_markup"]["inline_keyboard"][0][0]
    assert button["callback_data"] == "del:notes:n1"


def test_task_is_saved_with_done_and_delete_buttons(monkeypatch):
    monkeypatch.setattr(classifier, "classify",
                        lambda t: {"type": "task", "data": {"title": "submit report"}})
    monkeypatch.setattr(supabase_client, "insert_task",
                        lambda uid, d: {"id": "t1", "title": "submit report"})

    replies = handlers.handle_message("u1", "submit report by friday")
    assert "Task added" in replies[0]["text"]
    row = replies[0]["reply_markup"]["inline_keyboard"][0]
    assert row[0]["callback_data"] == "done:t1"
    assert row[1]["callback_data"] == "del:tasks:t1"


def test_query_is_formatted(monkeypatch):
    monkeypatch.setattr(classifier, "classify",
                        lambda t: {"type": "query", "data": {"scope": "today"}})
    monkeypatch.setattr(supabase_client, "query", lambda uid, scope: {"tasks": []})
    monkeypatch.setattr(classifier, "format_query_reply", lambda q, rows: "nothing today")

    replies = handlers.handle_message("u1", "what's on today?")
    assert replies[0]["text"] == "nothing today"


def test_done_with_single_match_completes(monkeypatch):
    monkeypatch.setattr(supabase_client, "open_tasks",
                        lambda uid, like=None: [{"id": "t1", "title": "call mom"}] if like else [])
    completed = {}

    def fake_complete(uid, tid):
        completed["id"] = tid
        return {"id": tid, "title": "call mom"}

    monkeypatch.setattr(supabase_client, "complete_task", fake_complete)

    replies = handlers.handle_message("u1", "/done call mom")
    assert "Done: call mom" in replies[0]["text"]
    assert completed["id"] == "t1"


def test_tasks_lists_open_items(monkeypatch):
    monkeypatch.setattr(supabase_client, "open_tasks",
                        lambda uid, like=None, limit=20: [
                            {"id": "t1", "title": "a"}, {"id": "t2", "title": "b"}])
    replies = handlers.handle_message("u1", "/tasks")
    assert len(replies) == 3  # header + 2 tasks
    assert replies[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "done:t1"


def test_callback_done(monkeypatch):
    monkeypatch.setattr(supabase_client, "complete_task",
                        lambda uid, tid: {"id": tid, "title": "x"})
    result = handlers.handle_callback("u1", "done:t1")
    assert result["edit_text"] == "Done: x"


def test_callback_delete(monkeypatch):
    monkeypatch.setattr(supabase_client, "delete_row", lambda uid, table, rid: True)
    result = handlers.handle_callback("u1", "del:notes:n1")
    assert "Deleted" in result["answer"]
