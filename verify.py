"""Strict post-write factual-support verifier for Daily Brief stories."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from llm import GroqLLM, LLMError


ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    unsupported: list[str]


class VerifyError(RuntimeError):
    """Raised when the verifier cannot obtain or parse a result."""


def _ensure_env() -> None:
    if os.environ.get("GROQ_API_KEY"):
        return
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _parse_result(response: str) -> VerifyResult:
    cleaned = response.strip()
    fence = chr(96) * 3
    if cleaned.startswith(fence):
        cleaned = cleaned.split("\n", 1)[1].rsplit(fence, 1)[0].strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise VerifyError(f"Verifier did not return valid JSON: {response!r}") from error
    unsupported = payload.get("unsupported")
    if not isinstance(unsupported, list) or not all(isinstance(item, str) for item in unsupported):
        raise VerifyError(f"Verifier returned invalid unsupported claims: {payload!r}")
    return VerifyResult(ok=not unsupported, unsupported=unsupported)


def verify_story(written_markdown: str, research_text: str) -> VerifyResult:
    """Return unsupported factual specifics found by one strict Groq review."""
    _ensure_env()
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise VerifyError("GROQ_API_KEY is not set.")
    prompt = f"""You are a strict fact-support verifier.

Compare the WRITTEN STORY against the RESEARCH TEXT. List every factual claim,
quote, name, number, date, causal assertion, or source attribution in the story
that is not directly supported by the research text. An unsupported specific is
a failure. General framing without a new factual assertion is acceptable.

Do not use outside knowledge. Do not infer missing facts. If an item is only
partly unsupported, list the unsupported portion exactly enough to remove it.

Return JSON only with one field named unsupported containing an array of strings.

WRITTEN STORY:
{written_markdown}

RESEARCH TEXT:
{research_text}
"""
    try:
        response = GroqLLM(api_key).generate(prompt)
    except LLMError as error:
        raise VerifyError(f"Groq verification failed: {error}") from error
    return _parse_result(response)
