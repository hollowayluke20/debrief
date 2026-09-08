"""Regression test: Wayfinder maps provide the reviewer with project state."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

SCRIPT = Path(__file__).with_name("review_project.py")
SPEC = importlib.util.spec_from_file_location("review_project", SCRIPT)
review_project = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(review_project)


def test_map_summary_lists_destination_open_and_resolved_issues(tmp_path: Path) -> None:
    map_path = tmp_path / "map.md"
    issues = tmp_path / "issues"
    issues.mkdir()
    map_path.write_text("## Destination\nShip a reliable daily brief.\n", encoding="utf-8")
    (issues / "open.md").write_text("# Add the review map\nStatus: open\n", encoding="utf-8")
    (issues / "resolved.md").write_text("# Parse source feeds\nStatus: resolved\n", encoding="utf-8")

    summary = review_project.map_plan_summary(map_path)
    open_issues, resolved_issues = summary.split("OPEN ISSUES (REMAINING WORK):", 1)[1].split("RESOLVED ISSUES (DONE):", 1)

    assert "Ship a reliable daily brief." in summary
    assert "Add the review map" in open_issues
    assert "Parse source feeds" in resolved_issues
    assert "Parse source feeds" not in open_issues
    assert "Add the review map" not in resolved_issues


def test_map_mode_overrides_stage_and_done_when_instructions(monkeypatch) -> None:
    captured = {}

    class FakeGemini:
        def __init__(self, _api_key: str) -> None:
            pass

        def generate_text(self, prompt: str) -> str:
            captured["prompt"] = prompt
            return "## WHERE WE ARE\n## WHAT HAPPENED TODAY\n## ON TRACK OR NOT\n## TOMORROW"

    monkeypatch.setitem(sys.modules, "llm", types.SimpleNamespace(GeminiLLM=FakeGemini, GroqLLM=object))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    review_project.review("WAYFINDER MAP", "evidence", map_mode=True)

    assert "MAP MODE: This plan is a Wayfinder map, so the stage instructions above do not apply." in captured["prompt"]
    assert "do not name a stage or repeat a Done when test" in captured["prompt"]
    assert "STUCK fires when the same open-issue set repeats" in captured["prompt"]
