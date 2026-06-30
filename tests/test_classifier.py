"""Tests for the classifier's JSON parsing (no network / no API key needed)."""

from ai.classifier import _parse_json


def test_plain_json():
    out = _parse_json('{"type": "note", "data": {"content": "hi"}}')
    assert out["type"] == "note"
    assert out["data"]["content"] == "hi"


def test_json_in_markdown_fence():
    out = _parse_json('```json\n{"type": "task", "data": {}}\n```')
    assert out["type"] == "task"


def test_garbage_becomes_note():
    out = _parse_json("this is not json at all")
    assert out["type"] == "note"
    assert out["data"]["content"] == "this is not json at all"
