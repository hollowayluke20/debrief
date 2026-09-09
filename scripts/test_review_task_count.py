"""Regression test: the reviewer prompt and spec both demand 8-12 TOMORROW tasks."""

from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).with_name("review_project.py")
SPEC = Path(__file__).resolve().parent.parent / "docs" / "REVIEWER_SPEC.md"


def test_prompt_demands_ten_to_twelve_tasks() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "8" in text and "12" in text
    assert "8–12 tasks" in text or "8-12 tasks" in text or "8 to 12 tasks" in text


def test_spec_promises_ten_to_twelve_tasks() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert "8–12" in text or "8-12" in text or "8 to 12" in text
    assert "TOMORROW" in text


if __name__ == "__main__":
    test_prompt_demands_ten_to_twelve_tasks()
    test_spec_promises_ten_to_twelve_tasks()
