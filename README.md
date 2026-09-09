# Debrief

A nightly project reviewer. Each evening it reads the day's work in a watched
repo, grades it against that repo's plan, and emails tomorrow's task list.

## How a project adopts it

Every watched project carries its plan as a Wayfinder map: `map.md` plus its
issue files, in `.scratch/<effort>/` inside the watched repo. Paste the map,
the reviewer runs — no other setup in the watched repo.

## What lands each morning

A separate email with four sections: where the project stands, what happened
yesterday, on track or not (blunt), and tomorrow as 8–12 tasks in fixed
2-hour tiers, most important first. Tiers learn Luke's pace from the
completion rate. Reports are also committed into the watched repo's
`reviews/` folder as history.

## Layout

- `scripts/review_project.py` — the reviewer
- `scripts/propose_plan.py` — proposes staged plans for non-map projects
- `scripts/pace.py` — completion-rate tier sizing
- `docs/REVIEWER_SPEC.md` — the full rulebook
- `.github/workflows/project-review.yml` — the 02:00 run; takes `repo` and
  `plan` inputs so one job watches any project
