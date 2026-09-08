"""Learn a realistic size for tomorrow's review tiers from yesterday's plan."""

from __future__ import annotations

import re

STOP_WORDS = {
    "about", "after", "along", "also", "area", "before", "between", "each",
    "from", "into", "that", "their", "then", "this", "through", "with",
}


def completion(report_text: str, commit_subjects: list[str]) -> dict[str, int | float]:
    """Return how many TOMORROW tasks have three matching significant words."""
    tomorrow = re.search(r"^## TOMORROW\s*$\n(.*?)(?=^## |\Z)", report_text, re.MULTILINE | re.DOTALL)
    task_lines = [] if tomorrow is None else re.findall(
        r"^(?:-\s+|\d+[.)]\s+)(.+)$", tomorrow.group(1), re.MULTILINE
    )
    subject_words = set(re.findall(r"[a-z]{4,}", " ".join(commit_subjects).lower()))
    done = 0
    for task in task_lines:
        words = {
            word for word in re.findall(r"[a-z]{4,}", task.lower())
            if word not in STOP_WORDS
        }
        if len(words & subject_words) >= 3:
            done += 1
    total = len(task_lines)
    return {"done": done, "total": total, "rate": done / total if total else 0.0}


def suggest_tiers(rate: float) -> int:
    """Choose a demanding but realistic number of two-hour tiers."""
    if rate >= 0.8:
        return 6
    if rate >= 0.5:
        return 5
    if rate >= 0.3:
        return 4
    return 3
