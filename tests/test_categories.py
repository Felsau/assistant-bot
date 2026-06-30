"""Tests for expense-category normalization (no network)."""

from ai.classifier import (
    EXPENSE_CATEGORIES,
    INCOME_CATEGORIES,
    _normalize_category,
)


def test_known_category_passes_through():
    assert _normalize_category("expense", "food") == "food"


def test_case_insensitive():
    assert _normalize_category("expense", "Transport") == "transport"


def test_synonym_maps_to_canonical():
    assert _normalize_category("expense", "coffee") == "food"
    assert _normalize_category("expense", "fuel") == "transport"
    assert _normalize_category("expense", "rent") == "housing"
    assert _normalize_category("expense", "utilities") == "bills"


def test_unknown_becomes_other():
    assert _normalize_category("expense", "zzzz") == "other"


def test_empty_stays_none():
    assert _normalize_category("expense", None) is None
    assert _normalize_category("expense", "") is None


def test_income_uses_income_set():
    assert _normalize_category("income", "wage") == "salary"
    assert _normalize_category("income", "bonus") == "bonus"
    # an expense category isn't valid for income → other
    assert _normalize_category("income", "groceries") == "other"


def test_category_lists_are_sane():
    assert "other" in EXPENSE_CATEGORIES
    assert "other" in INCOME_CATEGORIES
    assert "salary" in INCOME_CATEGORIES
