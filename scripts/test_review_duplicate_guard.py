"""The reviewer must stop before any metered call if today's report already exists."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DuplicateGuardTest(unittest.TestCase):
    def test_existing_report_stops_before_the_model(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            reviews = Path(folder)
            (reviews / "2026-06-14.md").write_text("already written", encoding="utf-8")
            environment = dict(os.environ)
            # No key at all: if the guard fails, the run reaches the model and fails loudly.
            environment.pop("GROQ_API_KEY", None)
            environment["GEMINI_API_KEY"] = "blocks .env loading"
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "review_project.py"),
                 "--repo", str(ROOT), "--plan", str(ROOT / "docs" / "replay" / "PLAN.undo.md"),
                 "--reviews-dir", str(reviews), "--date", "2026-06-14"],
                cwd=ROOT, text=True, encoding="utf-8", errors="replace",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already exists", result.stdout)
            self.assertEqual((reviews / "2026-06-14.md").read_text(encoding="utf-8"), "already written")


if __name__ == "__main__":
    unittest.main()
