"""Regression test: TOMORROW work is organised into fixed review tiers."""

from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).with_name("review_project.py")
SPEC = Path(__file__).resolve().parent.parent / "docs" / "REVIEWER_SPEC.md"


def test_prompt_demands_fixed_two_hour_tiers() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "FIRST 2 HOURS" in text
    assert "NEXT 2 HOURS" in text
    assert "10 to 12 tasks" in text
    assert "combine them into one list" in text
    assert "full day of tiers per project" in text
    assert "deliberately overfull; never trim it to fit one day" in text
    assert "never soften a bad day" in text
    assert "names the files or area involved and why it advances" in text
    assert "never invent filler to reach 10" in text
    assert "always a list to work from" in text


def test_spec_demands_fixed_two_hour_tiers() -> None:
    text = SPEC.read_text(encoding="utf-8")
    tomorrow = text.split("**TOMORROW**", 1)[1].split("\n4.", 1)[0]
    assert "2-hour tiers" in tomorrow
    assert "combine them into one list" in tomorrow
    assert "full day of tiers per project" in tomorrow
    assert "10 to 12" in tomorrow
    assert "never softened" in tomorrow
    assert "names the files or area involved" in tomorrow
    assert "never invent filler to reach 10" in tomorrow
    assert "always a list to work from" in tomorrow


if __name__ == "__main__":
    test_prompt_demands_fixed_two_hour_tiers()
    test_spec_demands_fixed_two_hour_tiers()
