#!/usr/bin/env python3
"""Build and deliver the Daily Brief from RSS headlines."""

from __future__ import annotations

import argparse
import html
import json
import markdown as markdown_lib
import os
import re
import smtplib
import sys
import time
import traceback
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Iterable

from briefing import (
    distinct_reputable_domains,
    headline_words,
    is_paywalled,
    normalise_output,
    research_story,
    source_checks,
    write_story,
)
from llm import FilterLLM, LLMError, generate_with_retry, parse_json_response
from marketdata import MarketDataError, get_snapshot
from verify import VerifyError, verify_story


ROOT = Path(__file__).resolve().parent
EDITIONS_DIR = ROOT / "editions"
EDITORIAL_PATH = ROOT / "docs" / "EDITORIAL.md"
LOOKBACK = timedelta(hours=24)
USER_AGENT = "daily-brief/0.2 (+https://github.com/hollowayluke20/daily-brief)"
TARGET_SECTIONS = ("markets", "uk", "us", "ai", "international", "worth-reading")
SECTION_LABELS = {
    "markets": "Markets & economy",
    "uk": "UK",
    "us": "US",
    "ai": "AI & big tech",
    "international": "International",
    "worth-reading": "Worth reading",
}
SOURCE_AUDIT_LINES: list[str] = []


def _audit_line(line: str) -> None:
    SOURCE_AUDIT_LINES.append(line)
    print(line, file=sys.stderr)


def _ensure_env() -> None:
    if os.environ.get("GEMINI_API_KEY"):
        return
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Feed:
    source: str
    section: str
    url: str


FEEDS = (
    Feed("BBC", "Politics", "https://feeds.bbci.co.uk/news/politics/rss.xml"),
    Feed("BBC", "Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    Feed("BBC", "UK", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
    Feed("BBC", "World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    Feed("BBC", "Technology", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
    Feed("The Guardian", "Politics", "https://www.theguardian.com/politics/rss"),
    Feed("The Guardian", "US", "https://www.theguardian.com/us-news/rss"),
    Feed("The Guardian", "Business", "https://www.theguardian.com/business/rss"),
    Feed("The Guardian", "World", "https://www.theguardian.com/world/rss"),
    Feed("The Guardian", "Technology", "https://www.theguardian.com/technology/rss"),
    Feed("Sky News", "UK", "https://feeds.skynews.com/feeds/rss/uk.xml"),
    Feed("Sky News", "World", "https://feeds.skynews.com/feeds/rss/world.xml"),
    Feed("Sky News", "Top Stories", "https://feeds.skynews.com/feeds/rss/home.xml"),
    Feed("Financial Times", "World", "https://www.ft.com/world?format=rss"),
    Feed("Financial Times", "Companies", "https://www.ft.com/companies?format=rss"),
)


@dataclass(frozen=True)
class Headline:
    headline: str
    source: str
    url: str
    section: str
    published: datetime


@dataclass(frozen=True)
class SelectedHeadline:
    item: Headline
    target_section: str
    reason: str


@dataclass(frozen=True)
class StoryBriefing:
    selected: SelectedHeadline
    markdown: str
    failed: bool = False


@dataclass(frozen=True)
class Edition:
    raw_count: int
    selected: list[SelectedHeadline]
    filtering_failed: bool = False
    briefings: list[StoryBriefing] = field(default_factory=list)
    candidates: list[Headline] = field(default_factory=list)
    market_snapshot: dict = field(default_factory=dict)
    market_commentary: str = ""
    market_error: str | None = None


class VerificationFailure(RuntimeError):
    """Raised when a story cannot be made source-verifiable in one rewrite."""


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(element: ET.Element, names: set[str]) -> str | None:
    for child in element:
        if local_name(child.tag) in names and child.text:
            return child.text.strip()
    return None


def entry_url(element: ET.Element) -> str | None:
    for child in element:
        if local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href and child.attrib.get("rel", "alternate") == "alternate":
            return href.strip()
        if child.text:
            return child.text.strip()
    return None


def parse_published(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_feed(feed: Feed, cutoff: datetime) -> list[Headline]:
    request = urllib.request.Request(feed.url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        root = ET.fromstring(response.read())
    headlines: list[Headline] = []
    for element in root.iter():
        if local_name(element.tag) not in {"item", "entry"}:
            continue
        title = _clean_title(child_text(element, {"title"}))
        url = entry_url(element)
        published = parse_published(child_text(element, {"pubdate", "published", "updated", "date"}))
        if title and url and published and published >= cutoff:
            headlines.append(Headline(title, feed.source, url, feed.section, published))
    return headlines


def _clean_title(title: str | None) -> str | None:
    """Some feeds (Sky's live blogs) put raw HTML in <title>. Strip tags and
    unescape entities, and drop anything that still doesn't look like a headline."""
    if not title:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", title)).strip()
    text = re.sub(r"\s+", " ", text)
    if not text or "http" in text.split(" ")[0] or len(text) < 12:
        return None
    return text


def normalise_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def deduplicate(headlines: Iterable[Headline]) -> list[Headline]:
    seen_titles: set[str] = set()
    seen_urls: set[str] = set()
    result: list[Headline] = []
    for item in sorted(headlines, key=lambda headline: headline.published, reverse=True):
        title_key = normalise_title(item.headline)
        url_key = item.url.rstrip("/").lower()
        if title_key not in seen_titles and url_key not in seen_urls:
            seen_titles.add(title_key)
            seen_urls.add(url_key)
            result.append(item)
    return result


def gather_headlines(now: datetime) -> list[Headline]:
    cutoff = now - LOOKBACK
    items: list[Headline] = []
    for feed in FEEDS:
        try:
            fetched = fetch_feed(feed, cutoff)
            print(f"Fetched {len(fetched)} recent items from {feed.source} {feed.section}.")
            items.extend(fetched)
        except Exception as error:  # A broken feed must not prevent the other feeds running.
            print(f"Warning: could not fetch {feed.source} {feed.section}: {error}", file=sys.stderr)
    return deduplicate(items)


def editorial_filter() -> str:
    editorial = EDITORIAL_PATH.read_text(encoding="utf-8")
    start = editorial.index("## THE FILTER")
    end = editorial.index("## FORMAT", start)
    return editorial[start:end].strip()


def filter_prompt(headlines: list[Headline]) -> str:
    # Compact candidates (numeric id + headline only) so the whole prompt fits
    # inside Groq's 8k tokens/minute free-tier ceiling when Gemini falls back to
    # it. Source outlet is dropped: the filter surfaces stories regardless of
    # source politics, and paywall handling happens downstream.
    candidates = [
        {"id": index, "h": item.headline} for index, item in enumerate(headlines)
    ]
    return f"""You are the editorial filter for Luke's Daily Brief. Apply the editorial policy below to the supplied RSS candidates. Select only stories that pass. Return AT MOST 20, and fewer when the day is thin - never pad to reach a number. If more than 20 pass, keep the 20 most consequential.

Return JSON only, with exactly this shape:
{{"selected": [{{"id": <the integer id from the input>, "section": "markets|uk|us|ai|international|worth-reading", "reason": "one concise sentence explaining why it passes"}}]}}

Never invent an id; only use ids present in the input. Use only the six allowed section values. Treat analysis/opinion as worth-reading where appropriate.

EDITORIAL FILTER:
{editorial_filter()}

OVERRIDING EXCLUSIONS — these override the editorial policy's "Markets & macro — near-total interest" line:
- Even within markets/business, EXCLUDE routine M&A and takeover bids unless they involve one of the two or three largest firms in a major economy.
- EXCLUDE individual executive appointments or departures, single-company merger-negotiation detail, and wage/pay settlements.
- EXCLUDE a politician's offhand remark, social-media post, or non-committal answer ("I'll look at it", "we'll see", "everything is under review"). A story is a DECISION, an ACTION, or a measurable SHIFT — not a quote reacting to a question.
- EXCLUDE a lone opinion column unless its subject is itself a consequential decision or a genuine landscape shift; a columnist musing on a topic is not enough.
- EXCLUDE a single new lawsuit filing UNLESS it targets one of the largest firms in its sector or sets a clear legal precedent for an industry.

PRIORITIES:
- Lean into the standing threads: AI/big tech, the far-right's institutional standing, money in politics, and monetary and fiscal policy.
- A story about an electoral shift or a party's condition always passes. This includes a socialist running in a red state or a leader polling behind their party.

CALIBRATION EXAMPLES:
- EXCLUDE: "AA could face £5bn takeover move by German insurer Allianz, reports claim" — a routine takeover bid.
- EXCLUDE: "CEO of India's largest private bank to step down" — an individual executive departure.
- EXCLUDE: "Trump hints at possible US review of support for UK over the Falkland Islands" — an offhand "I always review every position" answer, not a decision.
- EXCLUDE: "Trump criticizes communities opposing datacenter projects" — a social-media post attacking people; the datacentre-backlash trend itself is fine, this framing is noise.
- EXCLUDE: "Defence spending is an insatiable beast. John Healey is right to tame it" — a columnist's take with no new decision behind it.
- INCLUDE: "'Grassroots will beat big money every day': can a DSA member flip a Republican Senate seat?" — an electoral shift/party-condition story.
- INCLUDE: "Amazon faces $20bn advertising overcharge lawsuit from FTC and 22 states" — a regulator's action against one of the largest firms in the sector.

RSS CANDIDATES:
{json.dumps(candidates, ensure_ascii=False)}"""


# Groq's free tier caps a single request at 8k tokens; the filter prompt is the
# editorial policy (~1k) plus one line per candidate. Cap the candidate list so
# the prompt stays well under that even on a heavy news day. Feeds are sorted
# newest-first, so this drops the stale tail, not today's stories.
MAX_FILTER_CANDIDATES = 130


def filtered_shortlist(headlines: list[Headline]) -> Edition:
    # Cap only what the filter LLM sees. The full list is still kept as
    # `candidates` for cross-outlet corroboration downstream.
    for_filter = headlines[:MAX_FILTER_CANDIDATES]
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("API_KEY") or ""
    groq_key = os.environ.get("GROQ_API_KEY") or ""
    try:
        model = FilterLLM(gemini_key, groq_key)
        response = generate_with_retry(model, filter_prompt(for_filter))
        if model.last_provider:
            print(f"Filter provider used: {model.last_provider}.", file=sys.stderr)
        payload = parse_json_response(response)
        selections = payload["selected"]
        if not isinstance(selections, list):
            raise ValueError("'selected' is not a list")
        selected: list[SelectedHeadline] = []
        seen_ids: set[int] = set()
        for choice in selections:
            if not isinstance(choice, dict):
                continue
            try:
                index = int(choice.get("id"))
            except (TypeError, ValueError):
                continue
            section = choice.get("section")
            reason = str(choice.get("reason", "")).strip()
            if (
                0 <= index < len(for_filter)
                and index not in seen_ids
                and section in TARGET_SECTIONS
                and reason
            ):
                seen_ids.add(index)
                selected.append(SelectedHeadline(for_filter[index], section, reason))
        return Edition(raw_count=len(headlines), selected=selected, candidates=headlines)
    except (LLMError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"Warning: filtering failed; using raw list instead: {error}", file=sys.stderr)
        return Edition(
            len(headlines),
            [SelectedHeadline(item, "international", "") for item in headlines],
            True,
            candidates=headlines,
        )


def _fallback_briefing(selected: SelectedHeadline, error: Exception) -> StoryBriefing:
    item = selected.item
    print(f"Warning: briefing failed for {item.url}: {error}", file=sys.stderr)
    verification_note = (
        "*Flagged: could not be fully verified against sources.*"
        if isinstance(error, VerificationFailure) else
        "*Flagged: this story could not be sourced from a reputable free outlet, "
        "so only the filter-stage summary above is shown.*"
    )
    markdown = (
        f"**{normalise_output(item.headline)}**\n\n"
        f"{normalise_output(selected.reason)}\n\n"
        f"{verification_note}\n\n"
        f"Sources: {item.url}"
    )
    return StoryBriefing(selected, markdown, True)


def _corrective_rewrite(writer, draft: str, research_text: str, unsupported: list[str]) -> str:
    claims = "\n".join(f"- {claim}" for claim in unsupported)
    prompt = f"""Rewrite this Daily Brief story in British English.

These claims are unsupported by the supplied sources. Remove them entirely; do
not replace them with guesses or outside knowledge:
{claims}

Keep only claims directly supported by the research. Preserve a bold title,
two or three paragraphs where the sourced material supports them, and a final
**Why it matters:** line. Return Markdown only.

DRAFT:
{draft}

RESEARCH:
{research_text}
"""
    return writer.generate(prompt).strip()


def _verify_or_correct(writer, story: str, research_text: str, headline: str) -> str:
    try:
        first = verify_story(story, research_text)
    except VerifyError as error:
        raise VerificationFailure(f"verification call failed: {error}") from error
    if first.ok:
        _audit_line(f"VERIFICATION|story={headline!r}|status=verified clean")
        return story

    try:
        corrected = _corrective_rewrite(writer, story, research_text, first.unsupported)
        second = verify_story(corrected, research_text)
    except (LLMError, VerifyError) as error:
        raise VerificationFailure(f"corrective verification failed: {error}") from error
    if second.ok:
        _audit_line(f"VERIFICATION|story={headline!r}|status=rewritten then clean")
        return corrected
    _audit_line(f"VERIFICATION|story={headline!r}|status=flagged|unsupported={second.unsupported!r}")
    raise VerificationFailure("corrective rewrite still contained unsupported claims")


def _audit_sources(headline: str, research) -> list[str]:
    """Log and return only fetched, fresh, same-event source URLs."""
    checks = {check.url: check for check in source_checks()}
    verified: list[str] = []
    for url in research.source_urls:
        check = checks.get(url)
        fetched = bool(check and check.fetched)
        date_ok = bool(check and check.date_ok)
        same_event_ok = bool(check and check.same_event_ok)
        published = check.published_at.isoformat() if check and check.published_at else "missing"
        role = "primary" if url == research.article_url else "corroborating"
        cited = fetched and date_ok and same_event_ok
        _audit_line(
            f"SOURCE_AUDIT|story={headline!r}|role={role}|url={url}|"
            f"fetched={fetched}|published={published}|date_ok={date_ok}|"
            f"same_event_ok={same_event_ok}|cited={cited}",
        )
        if cited:
            verified.append(url)
    primary = checks.get(research.article_url)
    if not primary or not (primary.fetched and primary.date_ok and primary.same_event_ok):
        raise ValueError("No verified in-window primary source")
    return list(dict.fromkeys(verified))


def _merge_same_event(selected: list[SelectedHeadline]) -> list[SelectedHeadline]:
    """Collapse shortlisted stories that cover the same event into one item."""
    groups: list[list[SelectedHeadline]] = []
    for choice in selected:
        words = headline_words(choice.item.headline)
        for group in groups:
            gwords = headline_words(group[0].item.headline)
            shared = words & gwords
            union = words | gwords
            if len(shared) >= 3 and union and len(shared) / len(union) >= 0.3:
                group.append(choice)
                break
        else:
            groups.append([choice])

    merged: list[SelectedHeadline] = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
            continue
        # Prefer a free-to-read representative, then the most descriptive headline.
        representative = min(
            group, key=lambda c: (is_paywalled(c.item.url), -len(c.item.headline))
        )
        others = ", ".join(c.item.headline for c in group if c is not representative)
        print(
            f"Merged {len(group)} same-event stories into "
            f"'{representative.item.headline}' (dropped: {others})",
            file=sys.stderr,
        )
        merged.append(representative)
    return merged


def write_briefings(edition: Edition) -> Edition:
    """Research and write every shortlisted story with a per-story fallback."""
    if edition.filtering_failed:
        return edition
    if not edition.briefings:
        deduped = _merge_same_event(list(edition.selected))
        if len(deduped) != len(edition.selected):
            edition = Edition(
                edition.raw_count,
                deduped,
                candidates=edition.candidates,
            )
    groq_key = os.environ.get("GROQ_API_KEY", "")
    nvidia_key = os.environ.get("NVD_API_KEY", "")
    if not groq_key and not nvidia_key:
        error = RuntimeError("no writer model configured (need NVD_API_KEY or GROQ_API_KEY)")
        return Edition(
            edition.raw_count,
            edition.selected,
            briefings=[_fallback_briefing(selected, error) for selected in edition.selected],
            candidates=edition.candidates,
        )

    from llm import WriterLLM

    writer = WriterLLM(nvidia_key, groq_key)
    briefings: list[StoryBriefing] = []
    rss_candidates = [
        (candidate.headline, candidate.url, candidate.source)
        for candidate in edition.candidates
    ]
    for index, selected in enumerate(edition.selected):
        if index:
            # Groq's free tier is ~8k tokens/minute; pace the writer calls so the
            # per-call retry/backoff is the exception, not the rule.
            time.sleep(15)
        item = selected.item
        checks_before = {check.url for check in source_checks()}
        try:
            research = research_story(item.headline, item.url, rss_candidates)
            sources = _audit_sources(item.headline, research)
            domains = distinct_reputable_domains(sources)
            single_source = len(domains) < 2
            story = normalise_output(write_story(
                headline=item.headline,
                source=item.source,
                section=selected.target_section,
                filter_reason=selected.reason,
                research=research,
                writer=writer,
                single_source=single_source,
            ))
            if not story:
                raise ValueError("Groq returned an empty briefing")
            research_text = research.article_text + "\n\n" + research.context
            # The writer can consume most of Groq's minute token budget. Give the
            # separate verification request room rather than treating a 429 as a
            # factual-verification failure.
            time.sleep(15)
            story = normalise_output(_verify_or_correct(
                writer, story, research_text, item.headline
            ))
            if single_source:
                only = domains[0] if domains else "the original outlet"
                story += (
                    f"\n\n*Single-sourced from {only}; no second reputable outlet was found "
                    "to corroborate it, so this item stays close to that report.*"
                )
                print(f"Single-sourced: {item.headline} ({only})", file=sys.stderr)
            briefings.append(StoryBriefing(
                selected,
                f"{story}\n\nSources: {', '.join(sources)}",
            ))
        except Exception as error:
            for check in source_checks():
                if check.url not in checks_before:
                    published = check.published_at.isoformat() if check.published_at else "missing"
                    _audit_line(
                        f"SOURCE_AUDIT|story={item.headline!r}|role=rejected|url={check.url}|"
                        f"fetched={check.fetched}|published={published}|date_ok={check.date_ok}|"
                        f"same_event_ok={check.same_event_ok}|cited=False"
                    )
            _audit_line(
                f"SOURCE_AUDIT|story={item.headline!r}|result=fallback|reason={error}"
            )
            briefings.append(_fallback_briefing(selected, error))
    return Edition(
        edition.raw_count,
        edition.selected,
        briefings=briefings,
        candidates=edition.candidates,
    )


def _format_market_level(label: str, level: float) -> str:
    if label == "GBP/USD":
        return f"{level:.4f}"
    if label.startswith("US Treasury"):
        return f"{level:.2f}%"
    if label == "Bitcoin":
        return "$" + f"{level:,.0f}"
    if label in {"Gold", "WTI oil"}:
        return "$" + f"{level:,.2f}"
    return f"{level:,.2f}"


def market_snapshot_markdown(snapshot: dict, commentary: str = "") -> str:
    lines = ["## Markets snapshot", "", "| Instrument | Level | Day |", "|---|---:|---:|"]
    for label, item in snapshot.items():
        lines.append(
            f"| {label} | {_format_market_level(label, float(item['level']))} | "
            f"{float(item['daily_change_pct']):+.2f}% |"
        )
    if commentary:
        lines += ["", commentary]
    return "\n".join(lines)


def add_market_snapshot(edition: Edition, snapshot: dict | None = None) -> Edition:
    """Fetch snapshot data and write one grounded explanation from market stories."""
    try:
        snapshot = snapshot or get_snapshot()
    except MarketDataError as error:
        _audit_line(f"MARKET_SNAPSHOT|status=skipped|reason={error}")
        return replace(edition, market_error=str(error))

    market_stories = [
        f"- {story.selected.item.headline}: {story.selected.reason}\n{story.markdown[:1_500]}"
        for story in edition.briefings
        if story.selected.target_section == "markets" and not story.failed
    ]
    if not market_stories:
        _audit_line("MARKET_SNAPSHOT|status=skipped|reason=no written market stories")
        return replace(edition, market_snapshot=snapshot, market_error="No written market stories were available.")

    from llm import GroqLLM

    try:
        writer = GroqLLM(os.environ.get("GROQ_API_KEY", ""))
        data = "\n".join(
            f"{label}: level={item['level']}, daily_change_pct={item['daily_change_pct']}"
            for label, item in snapshot.items()
        )
        commentary = writer.generate(f"""Write one compact British-English Daily Brief paragraph explaining the day's market moves.

Use only the snapshot figures and the written market stories below. Do not add a
fact, cause, or number not present in them. If the stories do not explain a
move, say it was not established by the available reporting.

SNAPSHOT:
{data}

MARKET STORIES:
{chr(10).join(market_stories)}
""").strip()
        _audit_line("MARKET_SNAPSHOT|status=written")
        return replace(edition, market_snapshot=snapshot, market_commentary=normalise_output(commentary))
    except (LLMError, ValueError) as error:
        _audit_line(f"MARKET_SNAPSHOT|status=skipped|reason={error}")
        return replace(edition, market_snapshot=snapshot, market_error=str(error))


def display_date(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def long_date(value: datetime) -> str:
    return f"{value.strftime('%A')} {value.day} {value.strftime('%B')}"


def short_date(value: datetime) -> str:
    return f"{value.strftime('%a')} {value.day} {value.strftime('%b')}"


def grouped_items(edition: Edition) -> Iterable[tuple[str, list[SelectedHeadline]]]:
    if edition.filtering_failed:
        yield "Unfiltered headlines", edition.selected
        return
    for section in TARGET_SECTIONS:
        stories = [item for item in edition.selected if item.target_section == section]
        if stories:
            yield SECTION_LABELS[section], stories


def _timing_footer(now: datetime) -> str:
    """Record how this run was triggered and how late it was, so 'is the
    schedule fixed' is answerable from the archive, not from memory."""
    trigger = os.environ.get("BRIEF_TRIGGER")
    if not trigger:
        return ""
    due = os.environ.get("BRIEF_DUE_UTC", "n/a")
    started = os.environ.get("BRIEF_STARTED_UTC", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    delay = os.environ.get("BRIEF_DELAY_MIN", "n/a")
    _audit_line(f"TIMING|trigger={trigger}|due={due}|started={started}|delay_min={delay}")
    return (
        "\n---\n"
        f"*Run: {trigger}. Due {due} UTC, started {started} UTC"
        + (f", {delay} min late." if delay not in ("n/a", "") else ".")
        + "*\n"
    )


def latest_project_review(date: datetime) -> str | None:
    """The most recent project review, for the Project section of the email.

    The reviews live in whichever project was reviewed; PROJECT_REVIEWS_DIR says
    where the workflow checked it out to. Absent or empty means no section.
    """
    folder = os.environ.get("PROJECT_REVIEWS_DIR", "").strip()
    if not folder:
        return None
    directory = Path(folder)
    if not directory.is_dir():
        return None
    eligible = [path for path in directory.glob("????-??-??.md") if path.stem <= date.date().isoformat()]
    if not eligible:
        return None
    return max(eligible).read_text(encoding="utf-8").strip() or None


def project_review_for_newsletter(review: str) -> str:
    """Nest the report's fixed headings below the email's own Project heading."""
    return re.sub(r"(?m)^## (?=(?:WHERE WE ARE|WHAT HAPPENED TODAY|ON TRACK OR NOT|TOMORROW)$)", "### ", review)


def edition_markdown(edition: Edition, date: datetime) -> str:
    if edition.briefings:
        total = len(edition.briefings)
        failed_count = sum(story.failed for story in edition.briefings)
        written = total - failed_count
        lines = [
            f"# Daily Brief - {date:%Y-%m-%d}",
            "",
            f"{written} of {total} shortlisted stories written in full, "
            f"drawn from {edition.raw_count} headlines gathered in the last 24 hours.",
            "",
        ]
        review = latest_project_review(date)
        if review:
            lines += ["## Project", "", project_review_for_newsletter(review), ""]
        if failed_count:
            noun = "story" if failed_count == 1 else "stories"
            verb = "is" if failed_count == 1 else "are"
            lines += [
                f"*{failed_count} {noun} below {verb} flagged: no reputable free source "
                "could be reached, so only the one-line filter summary is shown.*",
                "",
            ]
        if edition.market_snapshot:
            lines += [market_snapshot_markdown(edition.market_snapshot, edition.market_commentary), ""]
        elif edition.market_error:
            lines += ["## Markets snapshot", "", f"*Unavailable: {edition.market_error}*", ""]
        for section in TARGET_SECTIONS:
            stories = [story for story in edition.briefings if story.selected.target_section == section]
            if not stories:
                continue
            lines += [f"## {SECTION_LABELS[section]}", ""]
            for story in stories:
                lines += [story.markdown, ""]
        return "\n".join(lines).rstrip() + "\n"

    not_selected = max(0, edition.raw_count - len(edition.selected))
    lines = [f"# Daily Brief — {date:%Y-%m-%d}", ""]
    if edition.filtering_failed:
        lines += ["**Filtering failed after a retry; this is the raw, unfiltered RSS list.**", ""]
    else:
        lines += [f"Filtered shortlist: {len(edition.selected)} of {edition.raw_count} RSS headlines.", f"Not selected: {not_selected}.", ""]
    if not edition.selected:
        lines.append("No recent headlines were available from the configured feeds.")
    for section, stories in grouped_items(edition):
        lines += [f"## {section}", ""]
        for story in stories:
            item = story.item
            lines.append(f"- [{item.headline}]({item.url}) — {item.source}, {item.section} ({display_date(item.published)})")
            if story.reason:
                lines.append(f"  - Why selected: {story.reason}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def briefing_html(markdown: str) -> str:
    """Render briefing Markdown, including tables and linked source lines, for email."""
    source_lines = re.sub(
        r"(?m)^Source:\s*(https?://[^\s]+)\s*$",
        r"[Source: \1](\1)",
        markdown.strip(),
    )
    rendered = markdown_lib.markdown(
        html.escape(source_lines),
        extensions=["tables", "nl2br"],
        output_format="html5",
    )
    return rendered


def edition_html(edition: Edition, date: datetime) -> str:
    if edition.briefings:
        groups = []
        review = latest_project_review(date)
        if review:
            groups.append(f"<h2>Project</h2>{briefing_html(project_review_for_newsletter(review))}")
        if edition.market_snapshot:
            groups.append(f"<h2>Markets snapshot</h2>{briefing_html(market_snapshot_markdown(edition.market_snapshot, edition.market_commentary))}")
        elif edition.market_error:
            groups.append(f"<h2>Markets snapshot</h2><p><em>Unavailable: {html.escape(edition.market_error)}</em></p>")
        for section in TARGET_SECTIONS:
            stories = [story for story in edition.briefings if story.selected.target_section == section]
            if not stories:
                continue
            articles = "".join(
                f'<article style="margin-bottom: 1.5em;">{briefing_html(story.markdown)}</article>'
                for story in stories
            )
            groups.append(f"<h2>{html.escape(SECTION_LABELS[section])}</h2>{articles}")
        return f"""<!doctype html>
<html><body style="font-family: Arial, sans-serif; line-height: 1.5; color: #1f2937;">
  <h1>Daily Brief - {long_date(date)}</h1>
  <p><strong>{len(edition.briefings) - sum(s.failed for s in edition.briefings)}</strong> of {len(edition.briefings)} shortlisted stories written in full, from {edition.raw_count} headlines in the last 24 hours.</p>
  {''.join(groups)}
</body></html>"""

    not_selected = max(0, edition.raw_count - len(edition.selected))
    summary = ("<p><strong>Filtering failed after a retry; this is the raw, unfiltered RSS list.</strong></p>"
               if edition.filtering_failed else
               f"<p>Filtered shortlist: <strong>{len(edition.selected)}</strong> of {edition.raw_count} RSS headlines. Not selected: {not_selected}.</p>")
    groups = []
    for section, stories in grouped_items(edition):
        rows = "".join(
            "<li>"
            f'<a href="{html.escape(story.item.url, quote=True)}">{html.escape(story.item.headline)}</a>'
            f"<br><small>{html.escape(story.item.source)} · {html.escape(story.item.section)} · {html.escape(display_date(story.item.published))}</small>"
            + (f"<br><em>Why selected: {html.escape(story.reason)}</em>" if story.reason else "")
            + "</li>"
            for story in stories
        )
        groups.append(f"<h2>{html.escape(section)}</h2><ol>{rows}</ol>")
    if not groups:
        groups.append("<p>No recent headlines were available from the configured feeds.</p>")
    return f"""<!doctype html>
<html><body style="font-family: Arial, sans-serif; line-height: 1.5; color: #1f2937;">
  <h1>Daily Brief — {long_date(date)}</h1>
  {summary}
  {''.join(groups)}
</body></html>"""


def send_email(body: str, date: datetime, *, subject: str | None = None) -> None:
    address = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not address or not password:
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must both be set.")
    message = EmailMessage()
    message["Subject"] = subject or f"Daily Brief — {short_date(date)}"
    message["From"] = address
    recipients = [item.strip() for item in os.environ.get("EMAIL_RECIPIENTS", address).split(",") if item.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_RECIPIENTS must contain at least one address.")
    message["To"] = ", ".join(recipients)
    message["Date"] = format_datetime(datetime.now().astimezone())
    message.set_content("Your email client does not support HTML. See the archived Markdown edition.")
    message.add_alternative(body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(address, password)
        smtp.send_message(message, to_addrs=recipients)


# A fixed development subset: one of every research path (FT news, FT analysis,
# a Guardian opinion column, a BBC news story, and stories that already worked)
# so Phase 3 can be exercised in ~10 minutes instead of a 35-minute full run.
SUBSET_STORIES: tuple[tuple[str, str, str, str], ...] = (
    (
        "Rising bond yields add tens of billions to G7 countries' debt costs",
        "https://www.ft.com/content/bbe90db5-64ac-441d-87e4-e984c5ef8629?syn-25a6b1a6=1",
        "markets",
        "Sovereign debt-servicing costs are rising across the G7 as bond yields climb - core markets and fiscal-policy territory.",
    ),
    (
        "Warsh charts a forward-looking path for the Fed at Jackson Hole",
        "https://www.ft.com/content/4319c6b7-5a25-41a7-836f-ca068ac4fe60?syn-25a6b1a6=1",
        "markets",
        "The new Fed chair's Jackson Hole signal on the rate path is a first-order monetary-policy event.",
    ),
    (
        "PC Harper's killers to stay in jail as PM announces changes to early release scheme",
        "https://www.bbc.co.uk/news/articles/crr4wd81pdpo?at_medium=RSS&at_campaign=rss",
        "uk",
        "A statutory change to England's sentencing and early-release rules - a consequential government decision.",
    ),
    (
        "Cities are the big winners in Burnham's devolution drive. But what happens to the rest of England? | John Harris",
        "https://www.theguardian.com/commentisfree/2026/aug/30/andy-burnham-mayors-devolution-regions-england-labour",
        "worth-reading",
        "A column on how English devolution redraws institutional power - an argument about a structural shift.",
    ),
    (
        "Iceland rejects EU accession talks in tightly-won referendum",
        "https://www.theguardian.com/world/2026/aug/30/iceland-eu-accession-talks-referendum-result",
        "international",
        "A national referendum settling a country's EU trajectory - a clear landscape shift.",
    ),
    (
        "'Grassroots will beat big money every day': can a DSA member flip a Republican Senate seat?",
        "https://www.theguardian.com/us-news/2026/aug/30/angie-nixon-florida-senate-candidate",
        "us",
        "An electoral-realignment test as a DSA candidate contests a Republican Senate seat in a reddening state.",
    ),
    (
        "Big business has shown small firms what to do - and what not to do - with AI | Gene Marks",
        "https://www.theguardian.com/technology/2026/aug/30/ai-small-business",
        "ai",
        "A column drawing practical AI-adoption lessons for smaller firms from big-company experiments.",
    ),
)


def _subset_edition(now: datetime) -> Edition:
    candidates = [
        Headline(headline=h, source="", url=u, section=s, published=now)
        for h, u, s, _ in SUBSET_STORIES
    ]
    selected = [
        SelectedHeadline(item, section, reason)
        for item, (_, _, section, reason) in zip(candidates, SUBSET_STORIES)
    ]
    return Edition(raw_count=len(candidates), selected=selected, candidates=candidates)


def build_failure_email(error: str, edition: Edition | None, date: datetime) -> str:
    """Return a deliberately dependency-free emergency email body.

    This path must remain usable if rendering, verification, or the market
    snapshot has failed.  A filtered shortlist is retained whenever filtering
    completed, so the delivery email is still useful for diagnosing the run.
    """
    if edition is None or not edition.selected:
        partial = "<p>No shortlist was available before the build failed.</p>"
    else:
        rows = "".join(
            "<li>"
            f'<a href="{html.escape(item.item.url, quote=True)}">{html.escape(item.item.headline)}</a>'
            + (f"<br><em>Why selected: {html.escape(item.reason)}</em>" if item.reason else "")
            + "</li>"
            for item in edition.selected
        )
        partial = (
            "<p>The build reached the filtered shortlist; its headlines are included below.</p>"
            f"<ol>{rows}</ol>"
        )
    return f"""<!doctype html>
<html><body style="font-family: Arial, sans-serif; line-height: 1.5; color: #1f2937;">
  <h1>Daily Brief — build failed</h1>
  <p><strong>The briefing could not be completed. The full error is included below.</strong></p>
  <pre style="white-space: pre-wrap; background: #f3f4f6; padding: 1em;">{html.escape(error)}</pre>
  <h2>Partial content</h2>
  {partial}
</body></html>"""


def main() -> int:
    _ensure_env()
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-email", action="store_true", help="Write the edition without sending it.")
    parser.add_argument("--stories", action="store_true", help="Run only the fixed development subset (see SUBSET_STORIES).")
    parser.add_argument(
        "--final",
        action="store_true",
        help="Last scheduled attempt of the day: deliver whatever was built and mark the day done.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild and resend even if today is already marked done (manual override).",
    )
    args = parser.parse_args()
    now = datetime.now(timezone.utc)

    # Duplicate guard — checked BEFORE any metered call (RSS, Gemini, Tavily,
    # NVIDIA, Groq, Twelve Data). The workflow's bash guard is the first line of
    # defence; this is the backstop that also protects the daily API quota when
    # the run starts anyway (checkout race, .ok not yet committed, direct run).
    ok_path = EDITIONS_DIR / f"{now.date().isoformat()}.ok"
    if ok_path.exists() and not args.force and not args.stories:
        print(f"{ok_path.name} exists: today's edition is already done. Exiting without building.")
        return 0

    if args.stories:
        edition = write_briefings(_subset_edition(now))
        markdown = edition_markdown(edition, now)
        EDITIONS_DIR.mkdir(exist_ok=True)
        subset_path = EDITIONS_DIR / "_subset.md"
        subset_path.write_text(markdown, encoding="utf-8")
        failed = sum(story.failed for story in edition.briefings)
        print(f"Wrote {subset_path.relative_to(ROOT)}: {len(edition.briefings) - failed} of {len(edition.briefings)} written in full.")
        return 0
    # Keep the filtered edition outside the try block: it is the minimum useful
    # partial content we can deliver if a later writer, verifier, or snapshot
    # integration raises unexpectedly.
    edition: Edition | None = None
    try:
        edition = filtered_shortlist(gather_headlines(now))
        edition = write_briefings(edition)
        edition = add_market_snapshot(edition)
        markdown = edition_markdown(edition, now) + _timing_footer(now)
        html_body = edition_html(edition, now)
        EDITIONS_DIR.mkdir(exist_ok=True)
        archive_path = EDITIONS_DIR / f"{now.date().isoformat()}.md"
        archive_path.write_text(markdown, encoding="utf-8")
        audit_path = EDITIONS_DIR / f"{now.date().isoformat()}.sources.log"
        audit_path.write_text("\n".join(SOURCE_AUDIT_LINES) + "\n", encoding="utf-8")
        print(f"Wrote {archive_path.relative_to(ROOT)} with {len(edition.selected)} displayed headlines.")

        written_full = sum(1 for story in edition.briefings if not story.failed)
        target = max(1, len(edition.selected) // 2)
        good = (not edition.filtering_failed) and written_full >= target
        finish = good or args.final

        if not finish:
            print(
                f"Edition not good enough ({written_full}/{len(edition.selected)} written in full, "
                f"filtering_failed={edition.filtering_failed}); leaving it for the next scheduled attempt.",
                file=sys.stderr,
            )
            return 0

        if not args.no_email:
            send_email(html_body, now)
            print("Email sent.")
        ok_path.write_text("ok\n", encoding="utf-8")
        print(f"Marked {ok_path.name}: today's edition is complete.")
        return 0
    except Exception:
        error = traceback.format_exc()
        print(error, file=sys.stderr)
        if not args.final or args.no_email:
            print("Build failed; leaving it for the next scheduled attempt.", file=sys.stderr)
            return 1
        try:
            send_email(
                build_failure_email(error, edition, now),
                now,
                subject=f"Daily Brief — build failed {now:%Y-%m-%d}",
            )
            (EDITIONS_DIR / f"{now.date().isoformat()}.ok").write_text("ok\n", encoding="utf-8")
            print("Build-failure email sent; day marked done.", file=sys.stderr)
        except Exception:
            print("Could not send build-failure email:", file=sys.stderr)
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
