# Daily Project Reviewer — build spec

Build a "daily project reviewer" into this repo.

## What it is

An agent that runs at the end of each day, reads the work done in a target git
repo, grades it against an agreed plan, and writes tomorrow's tasks. It is
delivered as a section in the existing morning newsletter email, and kept as a
dated file in the repo for history.

The core idea: commits and PRs tell you **what happened**, never whether it was
the **right** thing — "right" only exists relative to a plan. So the repo carries
a human-written plan, and this agent's job is the comparison.

## Setup (one-off, run by a human)

1. A human writes an END GOAL into `docs/PLAN.md` — one paragraph, what the
   finished project looks like. A pasted Wayfinder map can be used instead.
2. A script at `scripts/propose_plan.py` reads that end goal and proposes 5–7
   rough stages in order. Stages are **undated**. Each stage has:
   - a name
   - 2–3 sentences on what it covers
   - one line: `Done when: <observable test>`
3. It writes the proposal into `docs/PLAN.md` under the end goal, marked
   `status: proposed`. Humans edit it directly and change the status line to
   `status: agreed`. The reviewer refuses to run until it says `agreed`. A
   pasted Wayfinder map can be used instead.
4. The agent **never** edits `docs/PLAN.md` after that. It is read-only to the
   reviewer. If the agent thinks the plan is wrong, it says so in the report.
   A pasted Wayfinder map is likewise read-only.

## Daily run

Scheduled at 21:00 local, via the same GitHub Action pattern the newsletter uses.

1. Read all commits merged since the last run, and all PRs opened, updated or
   merged since the last run. Include diffs, not just commit messages.
2. Work out which stage in `docs/PLAN.md` is current — the first one whose
   `Done when` test is not yet satisfied. A pasted Wayfinder map can be used
   instead; its Destination is the end goal, resolved issues are done, and all
   other issues are remaining work. In map mode, the current position is that
   open-issue list against the Destination: WHERE WE ARE names the Destination
   plus what is open, and STUCK fires when the same open set repeats.
3. Produce a report with exactly these sections:
   - **WHERE WE ARE** — which stage, and honestly how close the `Done when` test is.
   - **WHAT HAPPENED TODAY** — plain-English summary of the day's commits and
     PRs. No jargon. Assume the reader started coding three months ago.
   - **ON TRACK OR NOT** — does today's work move toward the current stage? Say
     so directly. If work went sideways or belongs to a later stage, say that
     plainly rather than finding a way to call it progress. Being critical here
     is the whole point of the tool.
   - **TOMORROW** — 10 to 12 small, tickable tasks that push hard, split into
     fixed 2-hour tiers ordered by importance and labeled exactly **FIRST 2
     HOURS**, **NEXT 2 HOURS**, and so on. Each tier holds the most important
     remaining work. Each task names the files or area involved and why it
     moves the current stage forward. When more than one project is reviewed,
     combine them into one list: give a full day of tiers per project, put the
     most important project first, and make the combined list deliberately
     overfull; never trim it to fit one day. On a genuinely quiet day honesty
     wins over the count: say the day was quiet and give only what the work
     warrants, never invent filler to reach 10. The reviewer is blunt:
     sideways work is named plainly, never softened.
4. Write the report to `reviews/YYYY-MM-DD.md` and commit it.
5. Email the report as its own message, immediately after writing it. Reuse the
   newsletter's existing Gmail sender and recipient configuration.

## Deferred newsletter integration

The review is currently delivered separately. To put it back into the morning
newsletter, re-add a `REVIEWS_DIR = ROOT / "reviews"` constant in
`daily_brief.py`, a helper that reads the latest `reviews/YYYY-MM-DD.md`, and
an optional `project_review` argument to `edition_markdown` and `edition_html`.
Render it as `## Project` before the first news section, and pass the helper's
result from `main()` to both render functions. Keep the report headings nested
under that heading (convert their `##` markers to `###` for the newsletter only).

## Constraints

- Do not invent stages, reorder them, or quietly redefine a `Done when` test.
- If nothing was committed that day, say exactly that. Do not pad.
- Reuse the repo's existing email sending, scheduling and commit-back code — do
  not build a second copy of any of it.
- Send list stays a config value, so a second address can be added later.
- Follow the existing layout: `docs/` for the plan, `scripts/` for one-off
  commands, and the daily job as a second GitHub Action alongside
  `.github/workflows/daily-brief.yml`.

## Before you start

Tell me your plan and where each piece will live, and flag anything above you
think is a mistake.

## Rules added after replay testing (2026-09-07)

Three rules earned by running the reviewer against real finished repos (kage, leaves). Each exists because its absence caused a concrete failure.

1. **Every Done when test must be visible in the repository itself** - in the code, the tests, or the documentation. Two ways this goes wrong, both seen in testing: tests needing approval, sign-off or a diagram can never come true; and tests describing what you would *see the program do* ('the file reappears') can never be confirmed either, because the reviewer only reads commit messages and file names. Write what would be in the repository if the behaviour existed. Enforced in `scripts/propose_plan.py`.
2. **Stuck rule.** The reviewer is given its own last three reports. If the same stage has been current throughout, or it is about to repeat a task it has already given, it stops producing a task list and instead says the plan has stopped working, which stage is stuck, and what it would change. Trigger is repetition, not slowness. If it has already said this and the plan has not changed, it says so in one sentence rather than arguing again.
3. **Serves-the-goal rule.** The plan stays read-only, but the reviewer may judge it. If a Done when test blocks progress while the work plainly serves the END GOAL, it says so and proposes replacement wording for that one test, as a suggestion for the humans to accept or reject. It never edits the plan. This exists because a single word ("every") in a stage test sent the reviewer chasing empty files for twelve days while real work went unremarked.

4. **Evidence rule.** The reviewer cannot run the program and never will. It judges a Done when test on whether the repository plainly looks like it does that thing - code that clearly implements it, or tests that clearly cover it, are enough. It must not demand proof it can never be given. Luke's framing, 2026-09-07: *the walls could be made of sawdust, but as long as they look built it should say ok and move on to the roof.* Judging whether the work is any **good** belongs to code review, not here - which does mean this reviewer can be fooled by work that only looks finished.
5. **The stuck rule needs three real prior reports** before it fires. It once fired on day 2 with a single previous report and silenced the rest of an 11-day run.

6. **The report lives with the project it reviews.** Reports are written to a `reviews/` folder inside the repo being reviewed and committed there each day, so the history of how a project was built sits with the project. To stop the reviewer reading its own writing back as work, the `reviews/` folder is excluded when gathering a day's commits.
7. **Plan changes are shown to the reviewer.** Each run is told what changed in the plan file itself during the period under review. A day spent rewording the plan otherwise looks identical to a day spent building, and a moved finish-line makes the reviewer appear to contradict yesterday for reasons that have nothing to do with the project. Editing the plan is work, but it is not progress toward a stage, and the report must not credit it as such. Only visible when the plan lives inside the repo being reviewed.
8. **Work in flight.** The reviewer runs on a server against a pushed copy of the project, so it cannot see a batch running on the author's machine. The project leaves a note at `IN-PROGRESS.md` describing what is running; the reviewer treats that work as done, does not propose it as a task, does not call the night idle because nothing was committed, and instead says its results need checking and proposes what follows from it.
9. **An empty day is ambiguous.** No commits reaching the repository can mean a quiet day or unpushed work, and the reviewer cannot tell which. It must say so rather than reporting an idle day, and must not re-issue yesterday's tasks as though nothing happened.
10. **Pace rule.** The reviewer compares yesterday's TOMORROW tasks with today's commit subjects, treating a task as done when at least three significant words match. It supplies the resulting completion rate as evidence and grows tomorrow's tiers after a high rate or shrinks them after a low one.
