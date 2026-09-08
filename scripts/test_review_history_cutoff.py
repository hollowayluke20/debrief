"""Regression test: historical reviews never include later commits."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, time
from pathlib import Path


SCRIPT = Path(__file__).with_name("review_project.py")
SPEC = importlib.util.spec_from_file_location("review_project", SCRIPT)
review_project = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(review_project)


class HistoryCutoffTest(unittest.TestCase):
    def git(self, repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
        subprocess.run(("git", *args), cwd=repo, check=True, text=True, env=env, stdout=subprocess.PIPE)

    def commit(self, repo: Path, message: str, timestamp: str) -> None:
        (repo / "work.txt").write_text(message, encoding="utf-8")
        environment = os.environ.copy()
        environment.update({"GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp})
        self.git(repo, "add", "work.txt", env=environment)
        self.git(repo, "commit", "-m", message, env=environment)

    def test_day_snapshot_excludes_later_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            self.git(repo, "init")
            self.git(repo, "config", "user.name", "Review test")
            self.git(repo, "config", "user.email", "review@example.com")
            self.commit(repo, "day one commit", "2026-01-01T12:00:00+00:00")
            self.commit(repo, "day two commit", "2026-01-02T12:00:00+00:00")
            self.commit(repo, "day three commit", "2026-01-03T12:00:00+00:00")

            original_command = review_project.command

            def no_github(*args: str, repo: Path = review_project.ROOT) -> str:
                if args[0] == "gh":
                    raise RuntimeError("GitHub disabled for this local regression test")
                return original_command(*args, repo=repo)

            review_project.command = no_github
            try:
                day_one = datetime.combine(__import__("datetime").date(2026, 1, 1), time(21), review_project.LONDON)
                evidence = review_project.activity(
                    repo,
                    datetime.combine(__import__("datetime").date(2025, 12, 31), time(21), review_project.LONDON),
                    day_one,
                )
            finally:
                review_project.command = original_command

            self.assertIn("day one commit", evidence)
            self.assertNotIn("day two commit", evidence)
            self.assertNotIn("day three commit", evidence)


if __name__ == "__main__":
    unittest.main()
