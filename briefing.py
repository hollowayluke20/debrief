"""Research and write one Daily Brief story using web pages and Groq."""

from __future__ import annotations

import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
import trafilatura
from ddgs import DDGS
from lxml import html

from llm import GroqLLM


ROOT = Path(__file__).resolve().parent
EDITORIAL_PATH = ROOT / "docs" / "EDITORIAL.md"
REQUEST_TIMEOUT = 20
MAX_SOURCE_AGE = timedelta(hours=48)
# Kept deliberately tight: Groq's free tier allows only ~8k tokens per minute,
# so every character in the writer prompt slows the whole edition down.
MAX_ARTICLE_CHARS = 5_000
MAX_CONTEXT_CHARS = 2_500
MAX_CONTEXT_PAGE_CHARS = 2_000
# An article this long stands on its own; skip the extra web search that would
# otherwise hammer DuckDuckGo's rate limiter once per story.
CONTEXT_SEARCH_MIN_ARTICLE = 3_500
PAYWALL_DOMAINS = ("ft.com", "wsj.com", "telegraph.co.uk", "telegraph.com", "economist.com", "bloomberg.com")
# These outlets routinely reject the full-page request used after Tavily has
# identified a candidate, so do not spend a result slot on them.
TAVILY_FETCH_BLOCKED_DOMAINS = ("reuters.com", "wsj.com")
# Recognised outlets whose full text is free to read - safe to research from directly.
STRAIGHT_SOURCE_DOMAINS = (
    "bbc.co.uk", "bbc.com", "reuters.com", "apnews.com", "theguardian.com",
    "skynews.com", "cnbc.com", "aljazeera.com", "npr.org", "politico.com",
    "politico.eu", "axios.com", "rte.ie", "pbs.org", "abc.net.au", "dw.com",
)
RECOGNISED_NEWS_DOMAINS = STRAIGHT_SOURCE_DOMAINS + (
    "ft.com", "wsj.com", "telegraph.co.uk", "telegraph.com", "economist.com",
    "bloomberg.com", "nytimes.com", "washingtonpost.com",
)
PAYWALL_MARKERS = (
    "subscribe to unlock", "try unlimited access", "complete digital access",
    "to read this article", "sign in to read", "for full access",
    "register to continue", "subscription required",
)
# A plain browser UA: several outlets (Reuters especially) 401 anything that
# self-identifies as a bot.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Web search bans burst traffic hard. Keep every search in the process at least
# this many seconds apart, no matter which helper made the last one, and rotate
# the underlying engine so no single provider is hit 23+ times in a run.
_SEARCH_MIN_INTERVAL = 6.0
_search_last_call = 0.0
_SEARCH_BACKENDS = ("duckduckgo", "bing", "mullvad", "yandex")


@dataclass(frozen=True)
class Research:
    article_url: str
    article_text: str
    context: str
    source_urls: tuple[str, ...]
    is_opinion: bool = False


@dataclass
class SourceCheck:
    url: str
    published_at: datetime | None
    fetched: bool
    date_ok: bool
    same_event_ok: bool = False


_source_checks: dict[str, SourceCheck] = {}


def source_checks() -> tuple[SourceCheck, ...]:
    """Audit records for sources fetched during the current process."""
    return tuple(_source_checks.values())


def _parse_publication_date(page: str) -> datetime | None:
    document = html.fromstring(page)
    values = document.xpath(
        "//meta[@property='article:published_time']/@content | "
        "//meta[@name='publication_date']/@content | "
        "//meta[@name='date']/@content | "
        "//meta[@itemprop='datePublished']/@content | "
        "//meta[@name='parsely-pub-date']/@content | //time/@datetime"
    )
    for value in values:
        raw = str(value).strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
            except (TypeError, ValueError, IndexError):
                continue
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
    return None


def _date_is_current(published_at: datetime | None) -> bool:
    if published_at is None:
        return False
    age = datetime.now(timezone.utc) - published_at
    return timedelta(0) <= age <= MAX_SOURCE_AGE


def _mark_same_event(url: str, matched: bool) -> None:
    check = _source_checks.get(url)
    if check:
        check.same_event_ok = matched


def _normalise_text(parts: list[str], limit: int) -> str:
    text = "\n".join(" ".join(part.split()) for part in parts if part.strip())
    return text[:limit]


def _fetch_text(url: str) -> str:
    response = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    declared_encoding = response.encoding
    encoding = (
        response.apparent_encoding
        if not declared_encoding or declared_encoding.lower() == "iso-8859-1"
        else declared_encoding
    ) or "utf-8"
    try:
        page = response.content.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError):
        page = response.content.decode("utf-8", errors="replace")
    published_at = _parse_publication_date(page)
    date_ok = _date_is_current(published_at)
    _source_checks[url] = SourceCheck(url, published_at, True, date_ok)
    if not date_ok:
        date_text = published_at.isoformat() if published_at else "missing"
        raise ValueError(f"Rejected stale or undated source ({date_text}): {url}")
    extracted = trafilatura.extract(
        page, output_format="txt", include_comments=False, favor_recall=True
    )
    if extracted:
        text = _normalise_text([extracted], MAX_ARTICLE_CHARS)
    else:
        document = html.fromstring(page)
        article_parts = document.xpath("//article//p//text()") or document.xpath("//p//text()")
        text = _normalise_text(article_parts, MAX_ARTICLE_CHARS)
    if not text:
        raise ValueError(f"No article text found at {url}")
    return text


def _looks_paywalled(text: str) -> bool:
    """True when a fetch returned a subscribe wall instead of the article."""
    lowered = text.lower()
    return len(text) < 1_500 and any(marker in lowered for marker in PAYWALL_MARKERS)


def _looks_like_article(url: str, text: str) -> bool:
    """Reject section hubs and live-index pages masquerading as a story."""
    if len(text) < 1_200:
        return False
    return _looks_like_article_url(url)


def _is_paywalled(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in PAYWALL_DOMAINS)


def _is_recognised_news_source(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in RECOGNISED_NEWS_DOMAINS)


def _ddg_search(query: str, max_results: int) -> list[dict]:
    """Web search, globally spaced, rotating engines so bursts don't get us banned."""
    global _search_last_call
    last_error: Exception | None = None
    for attempt, backend in enumerate(_SEARCH_BACKENDS):
        wait = _SEARCH_MIN_INTERVAL - (time.monotonic() - _search_last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            _search_last_call = time.monotonic()
            results = list(DDGS().text(query, backend=backend, max_results=max_results))
            if results:
                return results
        except Exception as error:  # "no results", rate limit, transport error
            last_error = error
            print(f"Search backend {backend} failed ({error}); trying next", file=sys.stderr)
            time.sleep(3)
    if last_error:
        raise RuntimeError(f"All search backends failed: {last_error}")
    return []


def _tavily_search(query: str, max_results: int) -> list[dict]:
    """Return fresh, fetchable Tavily candidates in the legacy result shape."""
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is not configured")
    response = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": query, "max_results": max_results},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    if not isinstance(results, list):
        raise ValueError("Tavily returned an invalid results payload")
    candidates: list[dict] = []
    undated_candidates: list[dict] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        url = str(result.get("url", "")).strip()
        host = urlparse(url).netloc.lower()
        if not url or any(host == domain or host.endswith(f".{domain}") for domain in TAVILY_FETCH_BLOCKED_DOMAINS):
            continue
        published_at = _parse_search_date(result.get("published_date"))
        candidate = {
            "href": url,
            "title": result.get("title", ""),
            "body": result.get("content", ""),
            "published_date": published_at.isoformat() if published_at else None,
            "tavily": True,
        }
        if published_at is None:
            undated_candidates.append(candidate)
        elif _date_is_current(published_at):
            candidates.append(candidate)
    # Tavily often omits published_date. Check metadata concurrently so stale
    # results are filtered before candidate selection without serial delays.
    if undated_candidates:
        with ThreadPoolExecutor(max_workers=min(5, len(undated_candidates))) as pool:
            dates = pool.map(_search_result_date_or_none, (item["href"] for item in undated_candidates))
            for candidate, published_at in zip(undated_candidates, dates):
                if _date_is_current(published_at):
                    candidate["published_date"] = published_at.isoformat()
                    candidates.append(candidate)
    return candidates


def _search(query: str, max_results: int) -> list[dict]:
    """Use Tavily's extracted article text, falling back to DDG only if needed."""
    try:
        results = _tavily_search(query, max_results)
        if results:
            return results
        print("Tavily returned no results; trying DuckDuckGo fallback", file=sys.stderr)
    except (requests.RequestException, ValueError, RuntimeError) as error:
        print(f"Tavily search failed ({error}); trying DuckDuckGo fallback", file=sys.stderr)
    return _ddg_search(query, max_results)


def _parse_search_date(value: object) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _fetch_search_result_date(url: str) -> datetime | None:
    """Read only page metadata when Tavily does not supply a publication date."""
    response = requests.get(url, headers=BROWSER_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    declared_encoding = response.encoding
    encoding = (
        response.apparent_encoding
        if not declared_encoding or declared_encoding.lower() == "iso-8859-1"
        else declared_encoding
    ) or "utf-8"
    try:
        page = response.content.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError):
        page = response.content.decode("utf-8", errors="replace")
    return _parse_publication_date(page)


def _search_result_date_or_none(url: str) -> datetime | None:
    try:
        return _fetch_search_result_date(url)
    except (requests.RequestException, ValueError, OSError):
        return None


def _search_result_text(url: str, result: dict) -> str:
    """Use provider extracted content, with a metadata-only staleness check."""
    text = _normalise_text([str(result.get("body", ""))], MAX_ARTICLE_CHARS)
    published_at = _parse_search_date(result.get("published_date"))
    if published_at is None:
        published_at = _fetch_search_result_date(url)
    date_ok = _date_is_current(published_at)
    _source_checks[url] = SourceCheck(url, published_at, bool(text), date_ok)
    if not date_ok:
        date_text = published_at.isoformat() if published_at else "missing"
        raise ValueError(f"Rejected stale or undated search result ({date_text}): {url}")
    if not text:
        raise ValueError(f"Search result contained no extracted text: {url}")
    return text


def _looks_like_search_article(url: str, text: str) -> bool:
    """Tavily excerpts are shorter than a fetched page, but still need substance."""
    return len(text) >= 200 and _looks_like_article_url(url)


def _looks_like_article_url(url: str) -> bool:
    segments = [s for s in urlparse(url).path.split("/") if s]
    if len(segments) < 2:
        return False
    slug = segments[-1]
    if slug in {"bonds", "markets", "business", "economy", "world", "live", "latest"}:
        return False
    return bool(re.search(r"/20\d\d/", url) or slug.count("-") >= 2 or slug.isdigit())


def _result_text(url: str, result: dict) -> str:
    """Fetch Tavily-discovered articles in full, retaining its excerpt as a fallback."""
    if result.get("tavily"):
        try:
            text = _fetch_text(url)
            result["tavily_full_fetch"] = True
            return text
        except (requests.RequestException, ValueError, OSError) as error:
            # Tavily's result is still useful when a free outlet blocks or breaks
            # our fetch. Its date is independently checked before it is used.
            print(f"Full fetch failed for Tavily result {url}: {error}; using excerpt", file=sys.stderr)
            result["tavily_excerpt_fallback"] = True
            return _search_result_text(url, result)
    return _fetch_text(url)


def _result_looks_like_article(url: str, text: str, result: dict) -> bool:
    if result.get("tavily_full_fetch"):
        # _fetch_text already established a current date and non-empty article
        # extraction. Do not reject a valid Guardian/BBC page merely because its
        # extractor output is shorter than the generic page-length heuristic.
        return True
    if result.get("tavily_excerpt_fallback"):
        return _looks_like_search_article(url, text)
    return _looks_like_article(url, text)


_HEADLINE_STOP_WORDS = {
    "the", "and", "for", "with", "from", "that", "this", "after", "into", "over",
    "amid", "says", "could", "would", "will", "than", "then", "have", "been",
}


def _headline_words(headline: str) -> set[str]:
    # Drop a trailing " | Columnist Name" byline before comparing headlines.
    headline = headline.split("|")[0]
    return {
        word for word in re.findall(r"[a-z]{4,}", headline.lower())
        if word not in _HEADLINE_STOP_WORDS
    }


# Public aliases for the edition builder (dedup, corroboration checks).
headline_words = _headline_words


def is_paywalled(url: str) -> bool:
    return _is_paywalled(url)


def _registrable_domain(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "gov", "ac", "net"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def distinct_reputable_domains(urls: Iterable[str]) -> list[str]:
    """Distinct recognised outlets behind a story, keyed by brand.

    bbc.co.uk and bbc.com are one outlet, not two independent sources.
    """
    seen_brands: set[str] = set()
    out: list[str] = []
    for url in urls:
        if not url or not _is_recognised_news_source(url):
            continue
        domain = _registrable_domain(url)
        brand = domain.split(".")[0]
        if brand not in seen_brands:
            seen_brands.add(brand)
            out.append(domain)
    return out


def _article_matches_headline(headline: str, text: str, title: str = "") -> bool:
    """Guard against re-sourcing to an unrelated article that merely shares a word.

    Require that a real share of the headline's distinctive words actually appear
    in the fetched article; otherwise the story should fall back, not be written
    from the wrong source.
    """
    wanted = _headline_words(headline)
    if len(wanted) < 2:
        return True  # too little signal to judge - trust the search result
    haystack = f"{title} {text[:3000]}".lower()
    hits = sum(1 for word in wanted if word in haystack)
    # Re-sourced stories are the ones that go wrong, so demand a strong overlap:
    # most of the headline's distinctive words must actually be in the article.
    need = max(3, math.ceil(0.6 * len(wanted))) if len(wanted) >= 4 else len(wanted)
    return hits >= need


def _is_free_source(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(
        (host == domain or host.endswith(f".{domain}"))
        for domain in STRAIGHT_SOURCE_DOMAINS
    )


def _rss_straight_source_result(
    headline: str,
    rss_candidates: Iterable[tuple[str, str, str]],
) -> str | None:
    """Find a same-story free-to-read URL already present in today's RSS batch.

    Deliberately strict: a weak one- or two-word overlap ("equity", "oil") is how
    a story ends up written from an unrelated article, so require a real match.
    """
    wanted = _headline_words(headline)
    if len(wanted) < 3:
        return None
    best_url: str | None = None
    best_score = 0.0
    for candidate_headline, candidate_url, _ in rss_candidates:
        if not _is_free_source(candidate_url):
            continue
        words = _headline_words(candidate_headline)
        overlap = wanted & words
        if len(overlap) < 3:
            continue
        score = len(overlap) / max(1, min(len(wanted), len(words)))
        if score > best_score:
            best_score = score
            best_url = candidate_url
    return best_url if best_score >= 0.6 else None


def _free_source_article(headline: str) -> tuple[str, str]:
    """Search the open web for a free recognised-outlet version and fetch its text."""
    core = headline.split("|")[0].strip()
    tried: set[str] = set()
    for query in (core, f"{core} Reuters", f"{core} explained"):
        try:
            results = _search(query, max_results=10)
        except RuntimeError:
            continue
        for result in results:
            url = str(result.get("href", "")).strip()
            if not url or url in tried or not _is_free_source(url) or _is_paywalled(url):
                continue
            tried.add(url)
            try:
                text = _result_text(url, result)
            except (requests.RequestException, ValueError, OSError):
                continue
            title = str(result.get("title", ""))
            # A fetched article can use a different headline from Tavily's
            # result. Include its excerpt in the same-event comparison too.
            if result.get("tavily"):
                title = f"{title} {result.get('body', '')}"
            if (
                not _looks_paywalled(text)
                and _result_looks_like_article(url, text, result)
                and _article_matches_headline(headline, text, title)
            ):
                _mark_same_event(url, True)
                return url, text
    raise ValueError("No readable free recognised-outlet version confirmed for the same event")


def _rss_corroborating_sources(
    headline: str,
    rss_candidates: Iterable[tuple[str, str, str]],
    exclude_url: str,
    limit: int = 2,
) -> list[tuple[str, str]]:
    """Same-event articles from *other* outlets already in today's RSS batch.

    A search-free way to give a story a second independent source.
    """
    wanted = _headline_words(headline)
    if len(wanted) < 3:
        return []
    exclude_brand = _registrable_domain(exclude_url).split(".")[0]
    scored: list[tuple[float, str, str]] = []
    for cand_headline, cand_url, _ in rss_candidates:
        if cand_url == exclude_url or _is_paywalled(cand_url):
            continue
        if not _is_recognised_news_source(cand_url):
            continue
        if _registrable_domain(cand_url).split(".")[0] == exclude_brand:
            continue
        overlap = wanted & _headline_words(cand_headline)
        if len(overlap) < 3:
            continue
        scored.append((len(overlap) / max(1, len(wanted)), cand_headline, cand_url))
    out: list[tuple[str, str]] = []
    for score, _, url in sorted(scored, reverse=True):
        if score < 0.4:
            break
        try:
            text = _fetch_text(url)
        except (requests.RequestException, ValueError, OSError):
            continue
        if not _looks_paywalled(text) and _article_matches_headline(headline, text):
            _mark_same_event(url, True)
            out.append((url, text[:MAX_CONTEXT_PAGE_CHARS]))
        if len(out) >= limit:
            break
    return out


def _context_search(headline: str) -> tuple[str, tuple[str, ...]]:
    results = _search(headline.split("|")[0].strip(), max_results=10)
    pages = []
    source_urls = []
    seen_urls = set()
    for result in results:
        url = str(result.get("href", "")).strip()
        host = urlparse(url).netloc.lower()
        if (
            not url
            or url in seen_urls
            or not _is_recognised_news_source(url)
            or _is_paywalled(url)
        ):
            continue
        seen_urls.add(url)
        try:
            full = _result_text(url, result)
        except (requests.RequestException, ValueError, OSError):
            continue
        if (
            _looks_paywalled(full)
            or not _result_looks_like_article(url, full, result)
            or not _article_matches_headline(
                headline,
                full,
                f"{result.get('title', '')} {result.get('body', '')}"
                if result.get("tavily") else str(result.get("title", "")),
            )
        ):
            continue
        _mark_same_event(url, True)
        text = full[:MAX_CONTEXT_PAGE_CHARS]
        title = str(result.get("title", "")).strip()
        pages.append(f"Source: {url}\nTitle: {title}\n{text}")
        source_urls.append(url)
        if len(pages) == 3:
            break
    text = _normalise_text(pages, MAX_CONTEXT_CHARS)
    if not text:
        raise ValueError("Search returned no usable current source excerpts")
    return text, tuple(source_urls)


def _is_opinion(url: str, headline: str) -> bool:
    path = urlparse(url).path.lower()
    if any(seg in path for seg in ("/commentisfree/", "/opinion/", "/comment/", "/columnists/")):
        return True
    tail = headline.rsplit("|", 1)[-1].strip().lower()
    return bool(re.match(r"[a-z.'-]+ [a-z.'-]+$", tail)) or tail == "analysis"


def _resolve_article(
    headline: str,
    url: str,
    rss_candidates: Iterable[tuple[str, str, str]],
) -> tuple[str, str]:
    """Return (article_url, article_text), routing round paywalls to a free source."""
    if not _is_paywalled(url):
        try:
            text = _fetch_text(url)
            if not _looks_paywalled(text) and _article_matches_headline(headline, text):
                _mark_same_event(url, True)
                return url, text
        except (requests.RequestException, ValueError, OSError) as error:
            print(f"Direct fetch failed for {url}: {error}", file=sys.stderr)

    rss_url = _rss_straight_source_result(headline, list(rss_candidates))
    if rss_url:
        try:
            text = _fetch_text(rss_url)
            if not _looks_paywalled(text) and _article_matches_headline(headline, text):
                _mark_same_event(rss_url, True)
                return rss_url, text
        except (requests.RequestException, ValueError, OSError):
            pass
    return _free_source_article(headline)


def research_story(
    headline: str,
    url: str,
    rss_candidates: Iterable[tuple[str, str, str]] = (),
) -> Research:
    """Fetch a source story and independently searchable background."""
    rss_candidates = list(rss_candidates)
    article_url, article_text = _resolve_article(headline, url, rss_candidates)
    if _looks_paywalled(article_text):
        raise ValueError(f"Only a paywall stub was retrievable for: {headline}")

    context_blocks: list[str] = []
    context_urls: list[str] = []

    # First, a search-free second source: a same-event article from another
    # outlet already in today's RSS batch. This is what corroborates the story.
    for sib_url, sib_text in _rss_corroborating_sources(headline, rss_candidates, article_url):
        context_blocks.append(f"Source: {sib_url}\n{sib_text}")
        context_urls.append(sib_url)

    # Then top up with a web search when the article is thin, was re-sourced, or
    # still has no corroborating outlet.
    if (
        not context_urls
        or len(article_text) < CONTEXT_SEARCH_MIN_ARTICLE
        or article_url != url
    ):
        try:
            searched, searched_urls = _context_search(headline)
            if searched:
                context_blocks.append(searched)
                context_urls.extend(searched_urls)
        except Exception as error:
            print(f"Context search skipped for {headline!r}: {error}", file=sys.stderr)

    context = _normalise_text(context_blocks, MAX_CONTEXT_CHARS)
    return Research(
        article_url=article_url,
        article_text=article_text,
        context=context,
        source_urls=(article_url, *dict.fromkeys(context_urls)),
        is_opinion=_is_opinion(url, headline),
    )


def format_guidance() -> str:
    """FORMAT rules plus the two prose worked samples.

    The full editorial has four samples; sending all of them on every story would
    push the per-story prompt past Groq's daily token budget across a 23-item run,
    so keep the two that best show the target paragraph depth and spectrum.
    """
    editorial = EDITORIAL_PATH.read_text(encoding="utf-8")
    samples_start = editorial.index("## WORKED SAMPLES")
    format_rules = editorial[editorial.index("## FORMAT"):samples_start].strip()

    blocks = re.split(r"(?=### Sample)", editorial[samples_start:])
    wanted = [
        b.strip() for b in blocks
        if "a Markets & economy item" in b or "a UK item" in b
    ]
    return f"{format_rules}\n\n## WORKED SAMPLES - match this depth\n\n" + "\n\n".join(wanted)


def write_story(
    *,
    headline: str,
    source: str,
    section: str,
    filter_reason: str = "",
    research: Research,
    writer: GroqLLM,
    single_source: bool = False,
) -> str:
    if research.is_opinion:
        register = (
            "This is an opinion/analysis column. Summarise the ARGUMENT the writer makes: "
            "their central claim, the reasoning and evidence they give for it, and any "
            "counter-argument they acknowledge. Attribute it to the columnist by name if the "
            "research gives one (e.g. \"John Harris argues...\"). Do not present the column's "
            "opinions as settled fact."
        )
    else:
        register = (
            "Write in a straight news register. Explain what happened, why it happened, and the "
            "broader context. For a politics item, include the spectrum of views (what the other "
            "side/other parties say) only when the research supports it."
        )
    corroboration = (
        "ONLY ONE source article is available (no second outlet corroborated this). Use ONLY the "
        "primary article text below. Do not add any fact, figure, name, quote or context that is "
        "not in it. Do not use the context section. Keep the item shorter if the article is thin."
        if single_source else
        "Draw on both the primary article and the independent context below; every fact must still "
        "appear in one of them."
    )
    prompt = f"""You are writing one item for Luke's Daily Brief.

Use ONLY the research text below. Every sentence must be supported by it. If a fact is not in the research, leave it out - do not supply it from your own knowledge, and do not guess. A shorter, thinner story is correct; an embellished one is a failure. Every fact, name, number and quote must appear in the supplied research text.

{corroboration}

Write a complete Markdown briefing item in British English. Format, exactly:
- First line: a bold title in SENTENCE CASE - capitalise only the first word and proper nouns, exactly as the Guardian or BBC would ("Bank of England warns G20 of AI risk", never "Bank Of England Warns G20 Of AI Risk"). This is a hard requirement.
- Then two or three substantial paragraphs.
- Then a final line that begins `**Why it matters:**` followed by one or two sentences ON THE SAME LINE.
{register} Do not mention that you are an AI or describe the research process. Do not add a Sources line; the application adds it from the research URLs.

Original source: {source}
Target section: {section}

EDITORIAL FORMAT AND WORKED SAMPLES:
{format_guidance()}

STORY HEADLINE:
{headline}

PRIMARY ARTICLE ({research.article_url}):
{research.article_text}

INDEPENDENT CONTEXT AND REACTION SEARCH RESULTS:
{research.context if not single_source else "(withheld - single-sourced story)"}
"""
    from llm import LLMError

    last_error: Exception | None = None
    for attempt in range(4):
        try:
            text = writer.generate(prompt).strip()
            if text and _has_nonempty_why_it_matters(text):
                return text
            last_error = ValueError(
                "writer response was empty or incomplete (missing or empty 'Why it matters' line)"
            )
        except LLMError as error:
            last_error = error
            # A daily-token-cap breach will not clear inside a retry window.
            if "tokens per day" in str(error) or "TPD" in str(error):
                raise RuntimeError(f"writer hit the Groq daily token cap: {error}") from error
        if attempt < 3:
            time.sleep(_retry_after(str(last_error), default=15 * (attempt + 1)))
    raise RuntimeError(f"writer failed after retries: {last_error}")


def _has_nonempty_why_it_matters(text: str) -> bool:
    """Require the required final label to contain prose on its own line."""
    return bool(re.search(
        r"(?im)^\s*\*\*why it matters:\*\*[ \t]*\S+[^\n]*$",
        text,
    ))


def _retry_after(message: str, default: float) -> float:
    """Honour Groq's 'try again in 7.15s' hint when it rate-limits us."""
    match = re.search(r"try again in ([0-9.]+)s", message)
    if match:
        return float(match.group(1)) + 2.0
    return default


def normalise_output(text: str) -> str:
    """Use portable plain punctuation in generated archive and email text."""
    text = (
        text.replace("\u2018", "'").replace("\u2019", "'")
        .replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u2010", "-").replace("\u2011", "-")
        .replace("\u2012", "-").replace("\u2013", "-").replace("\u2014", "-")
        .replace("\u00a0", " ")
    )
    # The writer sometimes prefixes the bold title with a markdown list marker,
    # which turns the title (and the paragraph after it) into a bullet on render.
    text = text.lstrip("\n")
    text = re.sub(r"^\s*[-*+]\s+(\*\*)", r"\1", text)
    text = _sentence_case_title(text)
    return text


_TITLE_MINOR = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor",
    "of", "on", "onto", "or", "over", "the", "to", "up", "vs", "via", "with",
}


def _sentence_case_title(text: str) -> str:
    """The writer often emits a Title-Cased headline despite the instruction.
    Lower-case only capitalised minor words that are not the first word - this
    fixes 'Strikes Launchers In Strait Of Hormuz' without touching proper nouns
    (NHS, McTernan, iPhone are all left alone)."""
    lines = text.split("\n", 1)
    m = re.match(r"^\*\*(.+?)\*\*\s*$", lines[0].strip())
    if not m:
        return text
    words = m.group(1).split(" ")
    for i, w in enumerate(words[1:], start=1):
        core = w.strip(".,;:!?")
        if core.lower() in _TITLE_MINOR and core[:1].isupper() and core[1:].islower():
            words[i] = w[0].lower() + w[1:]
    lines[0] = f"**{' '.join(words)}**"
    return "\n".join(lines)
