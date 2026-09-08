"""Offline regression tests for the reviewer's pace calculation."""

from __future__ import annotations

from pace import completion, suggest_tiers

REPORT = """## WHERE WE ARE
Here.
## TOMORROW
- Add pace completion calculation module.
- Write offline pace regression tests.
- Wire pace evidence into review prompt.
- Update reviewer specification pace rule.
## NOTES
Done.
"""


def test_completion_and_tier_suggestion() -> None:
    result = completion(REPORT, [
        "Add completion calculation pace module",
        "Write offline pace regression tests",
        "Wire pace evidence review prompt",
    ])
    assert result == {"done": 3, "total": 4, "rate": 0.75}
    assert suggest_tiers(result["rate"]) == 5


def test_empty_tomorrow_is_zero_without_crashing() -> None:
    assert completion("## TOMORROW\nNo tasks today.\n", ["anything at all"]) == {
        "done": 0, "total": 0, "rate": 0.0,
    }


def test_unrelated_commits_have_a_low_rate() -> None:
    result = completion(REPORT, ["Refresh package artwork and release notes"])
    assert result["rate"] < 0.3


def test_tier_thresholds_and_boundaries() -> None:
    assert suggest_tiers(1.0) == 6
    assert suggest_tiers(0.8) == 6
    assert suggest_tiers(0.79) == 5
    assert suggest_tiers(0.5) == 5
    assert suggest_tiers(0.49) == 4
    assert suggest_tiers(0.3) == 4
    assert suggest_tiers(0.29) == 3
    assert suggest_tiers(0.0) == 3

