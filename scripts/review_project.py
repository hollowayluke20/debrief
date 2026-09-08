"""Create the daily project review from the agreed plan and repository activity."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "PLAN.md"
REVIEWS = ROOT / "reviews"
LONDON = ZoneInfo("Europe/London")
# The job runs in the small hours, so a "day" runs 02:00 to 02:00 and the report
# is dated for the day that just ended, not the one that has barely started.
REVIEW_HOUR = 2
HEADINGS = ("## WHERE WE ARE", "## WHAT HAPPENED TODAY", "## ON TRACK OR NOT", "## TOMORROW")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def shown(path: Path) -> str:
    """Path relative to the project when it sits inside it, otherwise in full."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def command(*args: str, repo: Path = ROOT) -> str:
    """Run a source-control command against the selected project repository."""
    result = subprocess.run(args, cwd=repo, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def agreed_plan(plan_path: Path = PLAN) -> str:
    text = plan_path.read_text(encoding="utf-8")
    if not re.search(r"^status:\s*agreed\s*$", text, re.MULTILINE | re.IGNORECASE):
        raise ValueError("Reviewer will not run until docs/PLAN.md says 'status: agreed'.")
    stages = re.findall(r"^## Stage \d+:.*?(?=^## Stage |\Z)", text, re.MULTILINE | re.DOTALL)
    if not stages or any("Done when:" not in stage for stage in stages):
        raise ValueError("The agreed plan needs numbered stages, each with a Done when line.")
    return text


def map_plan_summary(plan_path: Path) -> str:
    """Summarise a Wayfinder map and the issue files stored beside it."""
    map_text = plan_path.read_text(encoding="utf-8")
    destination = re.search(r"^## Destination\b.*?\n(.*?)(?=^## |\Z)", map_text, re.MULTILINE | re.DOTALL)
    if not destination:
        raise ValueError("Wayfinder map needs a ## Destination section.")
    open_issues = []
    resolved_issues = []
    for issue_path in sorted((plan_path.parent / "issues").glob("*.md")):
        issue_text = issue_path.read_text(encoding="utf-8")
        title = re.search(r"^#\s+(.+?)\s*$", issue_text, re.MULTILINE)
        issue_title = title.group(1) if title else issue_path.stem
        if re.search(r"^Status:\s*resolved\s*$", issue_text, re.MULTILINE | re.IGNORECASE):
            resolved_issues.append(issue_title)
        else:
            open_issues.append(issue_title)
    return (
        "WAYFINDER MAP:\n"
        f"DESTINATION:\n{destination.group(1).strip()}\n\n"
        "OPEN ISSUES (REMAINING WORK):\n"
        + ("\n".join(f"- {title}" for title in open_issues) or "- None")
        + "\n\nRESOLVED ISSUES (DONE):\n"
        + ("\n".join(f"- {title}" for title in resolved_issues) or "- None")
    )


def is_wayfinder_map(plan_path: Path) -> bool:
    """Whether this plan file is a Wayfinder map rather than a staged plan."""
    return bool(re.search(r"^## Destination\b", plan_path.read_text(encoding="utf-8"), re.MULTILINE))


def last_run_start(today: date) -> datetime:
    """Start of the window: where the previous report stopped, so gaps are covered."""
    existing = sorted(path for path in REVIEWS.glob("????-??-??.md") if path.stem < today.isoformat())
    if existing:
        previous = date.fromisoformat(existing[-1].stem)
        return datetime.combine(previous + timedelta(days=1), time(REVIEW_HOUR), LONDON)
    return datetime.combine(today, time(REVIEW_HOUR), LONDON)


def window_end(today: date) -> datetime:
    """A reported day ends when the next one begins."""
    return datetime.combine(today + timedelta(days=1), time(REVIEW_HOUR), LONDON)


def reporting_date(now: datetime) -> date:
    """Before 06:00 the run belongs to the day that has just ended."""
    return now.date() - timedelta(days=1) if now.hour < 6 else now.date()


def github_repo(repo: Path) -> str:
    """Return the GitHub owner/name for a repository, via its origin remote."""
    origin = command("git", "remote", "get-url", "origin", repo=repo)
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", origin)
    if not match:
        raise RuntimeError(f"origin is not a GitHub remote: {origin}")
    return f"{match.group(1)}/{match.group(2)}"


def activity(repo: Path, since: datetime, until: datetime | None = None, since_commit: str | None = None) -> str:
    since_iso = since.isoformat()
    git_args = ["git", "log", f"--after={since_iso}"]
    if since_commit:
        git_args = ["git", "log", f"{since_commit}..HEAD"]
    elif until is not None:
        git_args.append(f"--before={until.isoformat()}")
    git_args += ["--format=commit %H%nAuthor: %an%nDate: %cI%nSubject: %s", "--stat", "--no-ext-diff", "--", ".", ":(exclude)reviews"]
    commits = command(*git_args, repo=repo)
    if not commits:
        commits = "NO COMMITS: Nothing was committed today."
    prs = "GitHub CLI is unavailable; no pull-request metadata was collected."
    try:
        gh_repo = github_repo(repo)
        raw = command("gh", "pr", "list", "--repo", gh_repo, "--state", "all", "--limit", "100", "--json", "number,title,state,author,createdAt,updatedAt,mergedAt,url", repo=repo)
        recent = []
        excluded_after_cutoff = 0
        for pr in json.loads(raw):
            changed = [value for key in ("createdAt", "updatedAt", "mergedAt") if (value := pr.get(key))]
            timestamps = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in changed]
            upper_bound = until.astimezone(ZoneInfo("UTC")) if until else None
            # `gh pr diff` has no historical revision option. A PR changed after
            # this snapshot could expose a newer diff (or edited title), so omit
            # it rather than leaking future work into a historical review.
            if upper_bound and any(timestamp > upper_bound for timestamp in timestamps):
                excluded_after_cutoff += 1
                continue
            if any(timestamp > since.astimezone(ZoneInfo("UTC")) and (upper_bound is None or timestamp <= upper_bound) for timestamp in timestamps):
                diff = command("gh", "pr", "diff", "--repo", gh_repo, str(pr["number"]), repo=repo)
                pr["diff"] = diff[:12_000] + ("\n[diff truncated]" if len(diff) > 12_000 else "")
                recent.append(pr)
        if recent:
            prs = json.dumps(recent, indent=2)
        elif excluded_after_cutoff:
            prs = "No pull-request snapshot was safe to include; later updates would expose post-cutoff content."
        else:
            prs = "No pull requests were opened, updated, or merged since the last run."
    except RuntimeError:
        pass
    header = f"REPOSITORY ON DISK: {repo}" + chr(10) * 2
    period = f"COMMITS AFTER {since_iso}" + (f" AND UP TO {until.isoformat()}" if until else "")
    return header + f"{period}:\n{commits}\n\nPULL REQUESTS:\n{prs}"


def recent_reports(limit: int = 3) -> str:
    """Return the last few reports so the reviewer can notice it is repeating itself."""
    if not REVIEWS.exists():
        return "No previous reports."
    paths = sorted(p for p in REVIEWS.glob("*.md") if not p.name.startswith("evidence-"))
    if not paths:
        return "No previous reports."
    chosen = paths[-limit:]
    parts = [f"--- {p.stem} ---" + chr(10) + p.read_text(encoding="utf-8").strip() for p in chosen]
    return (chr(10) * 2).join(parts)


def plan_changes(repo: Path, plan_path: Path, since: datetime, until: datetime) -> str:
    """What changed in the plan itself during this window, if it lives in the repo.

    A day spent rewording the plan otherwise looks identical to a day spent
    building, and a moved finish-line makes the reviewer appear to contradict
    itself for reasons that have nothing to do with the project.
    """
    try:
        relative = plan_path.resolve().relative_to(repo.resolve())
    except ValueError:
        return "The plan is not kept in this repository, so changes to it cannot be seen."
    try:
        changed = command(
            "git", "log", f"--since={since.isoformat()}", f"--before={until.isoformat()}",
            "--format=commit %h %s", "--patch", "--no-ext-diff", "--", str(relative).replace(chr(92), "/"),
            repo=repo,
        )
    except RuntimeError as error:
        return f"Could not read the plan's history: {error}"
    if not changed:
        return "The plan was not changed during this period."
    limit = 15_000
    if len(changed) > limit:
        return changed[:limit] + (chr(10) * 2) + "[plan diff truncated - the plan was rewritten substantially today]"
    return changed


def work_in_flight(repo: Path) -> str:
    """Anything the project says is running right now.

    The reviewer runs on a server against a pushed copy, so it cannot see a batch
    grinding away on Luke's machine. The runner leaves a note instead; without it,
    a long overnight job looks like an idle night and gets proposed as tomorrow's
    work while it is already half done.
    """
    note = repo / "IN-PROGRESS.md"
    if not note.is_file():
        return "Nothing is recorded as running."
    text = note.read_text(encoding="utf-8").strip()
    return text or "Nothing is recorded as running."


def project_inventory(repo: Path) -> str:
    """What already exists in the reviewed repo, independent of today's commits.

    The reviewer grades today's activity, but a stage's Done when test describes an
    END state that may have been reached days ago. Without a picture of what already
    exists, the reviewer re-derives state from today's commit messages and keeps
    re-declaring done work as undone. This gives it a current snapshot: the project
    state file if there is one, plus the top-level Python files actually on disk.
    """
    parts = []
    state = repo / "PROJECT-STATE.md"
    if state.is_file():
        text = state.read_text(encoding="utf-8").strip()
        parts.append(f"PROJECT STATE FILE ({state.name}):\n{text[:2500]}")
    else:
        parts.append("PROJECT STATE FILE: none found (no PROJECT-STATE.md in this repo).")
    pyfiles = sorted(
        p.name for p in repo.iterdir()
        if p.is_file() and p.suffix == ".py" and not p.name.startswith("_")
    )
    if pyfiles:
        parts.append("TOP-LEVEL PYTHON FILES ON DISK:\n" + ", ".join(pyfiles))
    else:
        parts.append("TOP-LEVEL PYTHON FILES ON DISK: none found.")
    return "\n\n".join(parts)


def review(plan: str, work: str, plan_diff: str = "not checked", in_flight: str = "not checked", inventory: str = "not checked", map_mode: bool = False, pace_evidence: str = "PACE: yesterday completion 0/0 (0.0%); suggested tiers: 3") -> str:
    from llm import GeminiLLM, GroqLLM

    map_mode_rules = """\n\nMAP MODE: This plan is a Wayfinder map, so the stage instructions above do not apply. The current position is the open-issue list against the Destination. In WHERE WE ARE name the Destination and what remains open; do not name a stage or repeat a Done when test. STUCK fires when the same open-issue set repeats, using that open set wherever the stuck instructions refer to a stage or its Done when test.\n""" if map_mode else ""

    prompt = f"""You are a blunt daily software-project reviewer. Call sideways or wasted work exactly what it is; never soften a bad day to sound encouraging. The plan below is read-only: never invent, rename, reorder, or redefine its stages or Done when tests. Determine the current stage as the first stage whose Done when test is not yet satisfied, using the supplied repository work. If the evidence is insufficient, say so plainly and treat that stage as current.\n\nWrite Markdown with exactly these four level-2 headings, in this order, and no title or extra sections:\n## WHERE WE ARE\n## WHAT HAPPENED TODAY\n## ON TRACK OR NOT\n## TOMORROW\n\nUse plain English for someone who began coding three months ago. In WHERE WE ARE name the current stage, repeat its exact Done when test, and honestly say how close it is. WHAT HAPPENED TODAY: if no commits reached this repository, do not assume the day was idle - work may have been done and simply not pushed. Say exactly: 'Nothing reached GitHub today. Either it was a quiet day, or the day's work was not pushed - worth checking before acting on this report.' Then stop; do not pad. In that case TOMORROW should not repeat yesterday's tasks as though nothing happened; say that the tasks depend on what was actually done. ON TRACK OR NOT must make a direct judgement and call out sideways or later-stage work. TOMORROW must be small, finishable tasks - each one a piece of work someone could complete and tick off, not a day-long project. Give 10 to 12 tasks that push hard, split into fixed 2-hour tiers ordered by importance, labeled exactly FIRST 2 HOURS, NEXT 2 HOURS, and so on. Each tier holds the most important remaining work. Split a big piece into several small ones rather than writing one large task. Each names the files or area involved and why it advances the current stage. When more than one project is reviewed, combine them into one list: give a full day of tiers per project, put the most important project first, and make the combined list deliberately overfull; never trim it to fit one day. On a genuinely quiet day honesty wins over the count: say the day was quiet and give only what the work warrants, never invent filler to reach 10. Grow tomorrow's tiers when yesterday's completion rate was high, and shrink them when it was low; the rate is supplied as evidence.\n\nREAD THE CODE RULE: the project's files are on disk at the repository path named in the evidence header. You may and should open them. If a commit message is uninformative (a bare version number, 'update', 'fixes'), do not conclude that nothing happened - open the files the commit touched and see what is there. Judge the plan's stages against the code you can read, not only against what the commit messages happen to say.

EVIDENCE RULE: you cannot run this program and you never will - you see the files on disk plus commit messages, file names and line counts. So judge a Done when test on whether the repository plainly looks like it does that thing: code that clearly implements it, or tests that clearly cover it, are enough. Do not demand proof you can never be given, and do not hold a stage open because nobody has demonstrated the behaviour to you. The commit messages are a pointer, not the whole picture - if a stage's Done when test describes something, open the relevant files on disk and check whether it actually exists, even if no commit today mentioned it. If you are unsure, say what would settle it in one line and move on. Judging whether the work is any GOOD is not your job.

VERIFIED DONE RULE: the project state file (PROJECT-STATE.md) has a section called VERIFIED DONE. Anything listed there has already been finished and proved by breaking it on purpose. Treat every item in that section as genuinely done - do not propose it as tomorrow's work, do not hold its stage open, and do not ask for proof of it again. Only a stage that is NOT in that section, or whose specific Done when test is genuinely missing, should be treated as current or incomplete.

SERVES-THE-GOAL RULE: the plan is read-only, but you may judge it. If a Done when test is blocking progress while the work itself clearly serves the END GOAL, say so plainly, and propose a specific replacement wording for that one Done when test. Present it as a suggestion for the humans to accept or reject - never treat it as already agreed, and never write to the plan yourself. Hold the project to the end goal, not to the exact words of a test that no longer serves it.

WORK IN FLIGHT: you are told what the project says was running while you were not watching - typically a long batch started overnight. Treat that work as done: do not propose it as a task, and do not report the night as idle because nothing was committed during it. Instead assume it finished, say plainly that its results need checking first, and propose what follows from it. If it turns out to have failed, that becomes tomorrow's problem, not a reason to tell someone to start something they have already started.

PLAN CHANGES: you are told whether the plan itself was edited during this period. If it was, say so plainly in WHERE WE ARE - which stage was reworded and what changed about it - so that a report which disagrees with yesterday's is understood as a moved finish-line rather than lost ground. Editing the plan is work, but it is not progress toward a stage: do not credit it as such.

STUCK RULE: You are also given your own recent reports. Only consider yourself stuck once there are at least three previous reports and the same stage has been current in all three of them, or you are about to propose a task you already proposed, the plan has stopped working. In that case do NOT produce a task list. Under ## TOMORROW write only a short statement that the plan has stopped working, which stage is stuck, why its Done when test is not being met - in particular whether it describes something this project does not actually do - and what you would change about that stage. A reviewer that repeats itself is a reviewer nobody reads. If a previous report has already declared the plan stuck for the same stage and the plan has not changed since, do not argue it again: under ## TOMORROW write a single sentence saying the plan is still stuck on that stage and no tasks can be given until it is rewritten.

{map_mode_rules}

RUNNING WHILE YOU WERE NOT WATCHING:
{in_flight}

CHANGES TO THE PLAN ITSELF DURING THIS PERIOD:
{plan_diff}

YOUR RECENT REPORTS:
{recent_reports()}

WHAT ALREADY EXISTS (read this before judging what still needs building):
{inventory}

{pace_evidence}

AGREED PLAN:\n{plan}\n\nTODAY'S EVIDENCE (commit and PR diffs/metadata):\n{work}"""
    # Gemini first: the review is a long, plain-English judgement and the small Groq
    # model handles neither the length nor the nuance well. Groq stays as the fallback.
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if gemini_key:
        raw = GeminiLLM(gemini_key).generate_text(prompt)
    else:
        raw = GroqLLM(os.environ.get("GROQ_API_KEY", "")).generate(prompt, max_completion_tokens=1800)
    # Models routinely leave trailing spaces on headings; that is not a malformed report.
    text = chr(10).join(line.rstrip() for line in raw.strip().splitlines())
    found = tuple(re.findall(r"^## [A-Z ]+$", text, re.MULTILINE))
    if found != HEADINGS:
        raise ValueError("Reviewer model did not return the four required report sections.")
    return text + "\n"


def email_report(report: str, today: date) -> None:
    """Deliver the standalone review using the briefing's configured mailer."""
    from daily_brief import briefing_html, send_email

    body = (
        "<!doctype html><html><body style=\"font-family: Arial, sans-serif; line-height: 1.5; color: #1f2937;\">"
        f"<h1>Project review - {today.isoformat()}</h1>{briefing_html(report)}</body></html>"
    )
    send_email(body, datetime.combine(today, time(REVIEW_HOUR), LONDON), subject=f"Project review - {today.isoformat()}")


def main() -> int:
    from daily_brief import _ensure_env
    import pace

    _ensure_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, help="Review date in YYYY-MM-DD (default: today in London).")
    parser.add_argument("--repo", type=Path, default=ROOT, help="Git repository to review (default: this repository).")
    parser.add_argument("--plan", type=Path, default=PLAN, help="Plan file to grade against (default: docs/PLAN.md).")
    parser.add_argument("--reviews-dir", type=Path, help="Where to write reports (default: a reviews/ folder inside the reviewed repo).")
    parser.add_argument("--since-commit", help="Review only commits after this one (ignores dates; use for rebased history).")
    parser.add_argument("--evidence-only", action="store_true", help="Write the gathered evidence to reviews/evidence-DATE.md and stop, calling no model.")
    parser.add_argument("--email", action="store_true", help="Email the report after writing it.")
    args = parser.parse_args()
    today = args.date or reporting_date(datetime.now(LONDON))
    global REVIEWS
    if args.reviews_dir:
        REVIEWS = args.reviews_dir.resolve()
    else:
        REVIEWS = args.repo.resolve() / "reviews"
    source_repo = args.repo.resolve()
    try:
        plan_path = args.plan.resolve()
        map_mode = is_wayfinder_map(plan_path)
        plan = map_plan_summary(plan_path) if map_mode else agreed_plan(plan_path)
        if command("git", "rev-parse", "--is-inside-work-tree", repo=source_repo) != "true":
            raise ValueError(f"Not a git repository: {source_repo}")
        until = window_end(today)
        destination = REVIEWS / f"{today.isoformat()}.md"
        if destination.exists() and not args.evidence_only:
            print(f"{shown(destination)} already exists; nothing to do.")
            return 0
        work = activity(source_repo, last_run_start(today), until, args.since_commit)
        if args.evidence_only:
            REVIEWS.mkdir(exist_ok=True)
            path = REVIEWS / f"evidence-{today.isoformat()}.md"
            path.write_text(work, encoding="utf-8")
            print(f"Wrote {shown(path)} ({len(work)} chars)")
            return 0
        prior_reports = sorted(path for path in REVIEWS.glob("????-??-??.md") if path.stem < today.isoformat())
        prior_report = prior_reports[-1].read_text(encoding="utf-8") if prior_reports else ""
        subjects = re.findall(r"^Subject:\s*(.+)$", work, re.MULTILINE)
        pace_result = pace.completion(prior_report, subjects)
        pace_evidence = (
            f"PACE: yesterday completion {pace_result['done']}/{pace_result['total']} "
            f"({pace_result['rate']:.1%}); suggested tiers: {pace.suggest_tiers(pace_result['rate'])}"
        )
        report = review(plan, work, plan_changes(source_repo, args.plan.resolve(), last_run_start(today), until), work_in_flight(source_repo), project_inventory(source_repo), map_mode, pace_evidence)
        REVIEWS.mkdir(parents=True, exist_ok=True)
        destination.write_text(report, encoding="utf-8")
        print(f"Wrote {shown(destination)}")
        if args.email:
            email_report(report, today)
            print("Review email sent.")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Project review failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
