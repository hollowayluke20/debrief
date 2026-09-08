"""Propose an editable, undated project plan from docs/PLAN.md's END GOAL."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "PLAN.md"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def end_goal(text: str) -> str:
    match = re.search(r"^## END GOAL\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        raise ValueError("docs/PLAN.md needs a '## END GOAL' heading.")
    goal = re.sub(r"<!--.*?-->", "", match.group(1), flags=re.DOTALL)
    goal = re.sub(r"^status:\s*.*$", "", goal, flags=re.MULTILINE).strip()
    if not goal:
        raise ValueError("Write the END GOAL paragraph before proposing a plan.")
    return goal


def proposal(goal: str) -> str:
    from llm import GroqLLM

    key = os.environ.get("GROQ_API_KEY", "")
    model = GroqLLM(key)
    prompt = f"""You are helping plan a software project. Its end goal is:\n\n{goal}\n\nPropose 5 to 7 rough stages in the required order. Stages must be undated. Return Markdown only, with this exact shape for every stage:\n\n## Stage N: short name\nTwo or three sentences describing what it covers.\n\nDone when: one observable test\n\nEvery Done when test must be visible in the repository itself - in the code, the tests, or the documentation. Nobody can run the program to check, so never write a test that depends on watching the program behave ('the file reappears', 'the screen fills with rectangles'); write what would be in the repository if that behaviour existed ('there is a test that deletes a file and asserts it comes back', 'a rendering module draws each entry as a rectangle sized by its byte count'). Never write a test that depends on approval, sign-off, review, agreement, a decision being made, or a diagram existing — nobody records those in a repository, so the stage could never be completed and would block every daily report. Prefer tests a user could run.

The first stage must be real work that produces something, not planning or architecture.

Do not add status lines, dates, or any text before the first stage."""
    result = model.generate(prompt, max_completion_tokens=1800).strip()
    stages = re.findall(r"^## Stage \d+:", result, re.MULTILINE)
    if not 5 <= len(stages) <= 7 or "Done when:" not in result:
        raise ValueError("The model returned an invalid plan proposal; please run it again.")
    tests = re.findall(r"^.*Done when:.*$", result, re.MULTILINE)
    banned = ("approv", "sign-off", "signed off", "sign off", "agreed", "reviewed", "diagram", "decided", "chosen")
    unverifiable = [line for line in tests if any(word in line.lower() for word in banned)]
    if unverifiable:
        raise ValueError("A Done when test is not checkable from the repository: " + unverifiable[0].strip())
    return result + "\n"


def main() -> int:
    from daily_brief import _ensure_env

    _ensure_env()
    try:
        original = PLAN.read_text(encoding="utf-8")
        goal = end_goal(original)
        generated = proposal(goal)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Could not propose plan: {error}", file=sys.stderr)
        return 1

    prefix = original[: original.find("## END GOAL")]
    PLAN.write_text(
        f"{prefix}## END GOAL\n\n{goal}\n\nstatus: proposed\n\n{generated}", encoding="utf-8"
    )
    print("Wrote proposed stages to docs/PLAN.md. Review them, then change status to agreed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
