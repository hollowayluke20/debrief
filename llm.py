"""Small, swappable interface for Daily Brief language-model calls."""

from __future__ import annotations

import json
import sys
import time
from typing import Protocol

import requests


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
REQUEST_TIMEOUT = 100

FILTER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "selected": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "section": {
                        "type": "string",
                        "enum": ["markets", "uk", "us", "ai", "international", "worth-reading"],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["id", "section", "reason"],
            },
        },
    },
    "required": ["selected"],
}


class LLMError(RuntimeError):
    """Raised when an LLM request cannot produce a usable response."""


class LLM(Protocol):
    def generate(self, prompt: str) -> str:
        """Return one text response for a prompt."""


class GeminiLLM:
    """Gemini REST implementation, matching the portfolio project's call pattern."""

    model = "gemini-3.6-flash"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise LLMError("Gemini API key is not configured.")
        self.api_key = api_key

    def generate_text(self, prompt: str) -> str:
        """One full request/response asking for prose rather than JSON."""
        return self._call({"contents": [{"role": "user", "parts": [{"text": prompt}]}]})

    def generate(self, prompt: str, response_schema: dict | None = None) -> str:
        """One full request/response. Returns the raw JSON text Gemini produced."""
        return self._call({
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": response_schema or FILTER_RESPONSE_SCHEMA,
            },
        })

    def _call(self, body: dict) -> str:
        try:
            response = requests.post(
                GEMINI_URL.format(model=self.model),
                json=body,
                headers={"x-goog-api-key": self.api_key},
                timeout=REQUEST_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as error:
            raise LLMError(f"Gemini request failed: {error}") from error

        if 400 <= response.status_code < 500:
            detail = response.text
            print(f"Gemini HTTP {response.status_code} response: {detail}", file=sys.stderr)
            raise LLMError(f"Gemini HTTP {response.status_code}: {detail}")
        if 500 <= response.status_code < 600:
            raise LLMError(f"Gemini HTTP {response.status_code}: {response.text}")
        try:
            response.raise_for_status()
            data = response.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (requests.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
            raise LLMError(f"Gemini response was unusable: {error}") from error


class GroqLLM:
    """Groq's OpenAI-compatible REST implementation for briefing prose."""

    # gpt-oss-20b: the 120b sibling shares a 200k tokens/day free-tier cap that a
    # full 23-story edition plus any retries can exhaust; 20b has its own bucket
    # and enough quality for the briefing register.
    model = "openai/gpt-oss-20b"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise LLMError("Groq API key is not configured.")
        self.api_key = api_key
        self.model = model or self.model

    def generate(
        self,
        prompt: str,
        *,
        json_object: bool = False,
        max_completion_tokens: int = 1400,
        reasoning_effort: str = "low",
    ) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            # Enough room for a three-paragraph item without a mid-sentence cut-off.
            "max_completion_tokens": max_completion_tokens,
            "temperature": 0.4,
        }
        if json_object:
            body["response_format"] = {"type": "json_object"}
        if self.model.startswith("openai/gpt-oss"):
            # These models spend hidden "reasoning" tokens; keep that budget small
            # so the visible answer is not truncated.
            body["reasoning_effort"] = reasoning_effort
        try:
            response = requests.post(
                GROQ_URL,
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=REQUEST_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as error:
            raise LLMError(f"Groq request failed: {error}") from error

        if not response.ok:
            raise LLMError(f"Groq HTTP {response.status_code}: {response.text}")
        try:
            data = response.json()
            choice = data["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise LLMError(f"Groq response was unusable: {error}") from error
        if choice.get("finish_reason") == "length":
            raise LLMError("Groq response was truncated (hit the length limit)")
        return choice["message"]["content"]


class NvidiaLLM:
    """NVIDIA NIM (OpenAI-compatible) implementation for briefing prose.

    NIM has no tight daily token cap on the free tier, so the heavy per-story
    writing load goes here with Groq as the fallback.
    """

    model = "meta/llama-3.2-90b-vision-instruct"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise LLMError("NVIDIA API key is not configured.")
        self.api_key = api_key
        self.model = model or self.model

    def generate(self, prompt: str, *, max_tokens: int = 1400) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            "max_tokens": max_tokens,
        }
        try:
            response = requests.post(
                NVIDIA_URL,
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=REQUEST_TIMEOUT,
            )
        except (requests.ConnectionError, requests.Timeout) as error:
            raise LLMError(f"NVIDIA request failed: {error}") from error
        if not response.ok:
            raise LLMError(f"NVIDIA HTTP {response.status_code}: {response.text}")
        try:
            data = response.json()
            choice = data["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise LLMError(f"NVIDIA response was unusable: {error}") from error
        if choice.get("finish_reason") == "length":
            raise LLMError("NVIDIA response was truncated (hit the length limit)")
        return choice["message"]["content"]


class WriterLLM:
    """Story writer: NVIDIA NIM primary, Groq fallback on any failure."""

    def __init__(self, nvidia_api_key: str, groq_api_key: str) -> None:
        self._nvidia = NvidiaLLM(nvidia_api_key) if nvidia_api_key else None
        self._groq = GroqLLM(groq_api_key) if groq_api_key else None
        if not self._nvidia and not self._groq:
            raise LLMError("No writer model is configured (need NVD_API_KEY or GROQ_API_KEY).")
        self.last_provider: str | None = None

    def generate(self, prompt: str) -> str:
        if self._nvidia is not None:
            try:
                text = self._nvidia.generate(prompt)
                self.last_provider = "nvidia"
                return text
            except Exception as error:  # noqa: BLE001 - fall back on anything
                if self._groq is None:
                    raise
                print(f"NVIDIA writer unavailable ({error}); falling back to Groq.", file=sys.stderr)
        text = self._groq.generate(prompt)
        self.last_provider = "groq"
        return text


_QUOTA_MARKERS = (
    "429", "quota", "rate limit", "rate-limit", "ratelimit",
    "resource_exhausted", "resource exhausted", "exceeded", "too many requests",
    "tokens per day", "tpd", "tpm", "requests per",
)


def is_quota_error(error: BaseException) -> bool:
    """True when an LLM failure looks like a free-tier quota / rate-limit block."""
    text = str(error).lower()
    return any(marker in text for marker in _QUOTA_MARKERS)


class FilterLLM:
    """Editorial-filter model with automatic provider fallback.

    Tries Gemini first (matches the earlier editions); any Gemini failure retries
    the same prompt and JSON schema on Groq when it is configured. Conforms to
    the plain ``generate(prompt) -> str`` interface so it stays swappable.
    """

    def __init__(
        self,
        gemini_api_key: str,
        groq_api_key: str,
        response_schema: dict | None = None,
    ) -> None:
        self.response_schema = response_schema or FILTER_RESPONSE_SCHEMA
        self._gemini = GeminiLLM(gemini_api_key) if gemini_api_key else None
        self._groq = GroqLLM(groq_api_key) if groq_api_key else None
        if not self._gemini and not self._groq:
            raise LLMError("No filter model is configured (need GEMINI_API_KEY or GROQ_API_KEY).")
        self.last_provider: str | None = None

    def generate(self, prompt: str) -> str:
        if self._gemini is not None:
            try:
                text = self._gemini.generate(prompt, self.response_schema)
                self.last_provider = "gemini"
                return text
            except Exception as error:
                if self._groq is None:
                    if isinstance(error, LLMError):
                        raise
                    raise LLMError(f"Gemini filter failed: {error}") from error
                print(
                    f"Gemini filter unavailable ({error}); falling back to Groq.",
                    file=sys.stderr,
                )
        if self._groq is None:
            raise LLMError("Gemini filter failed and no Groq key is configured for fallback.")
        # Groq free tier is 8k tokens/minute; keep prompt + completion under that.
        text = self._groq.generate(
            prompt,
            json_object=True,
            max_completion_tokens=2200,
            reasoning_effort="low",
        )
        self.last_provider = "groq"
        return text


def generate_with_retry(llm: LLM, prompt: str, attempts: int = 2) -> str:
    """Generate once, retrying one failure before reporting it."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return llm.generate(prompt)
        except LLMError as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2)
    raise LLMError(f"Filtering failed after {attempts} attempts: {last_error}")


def parse_json_response(response: str) -> object:
    """Parse JSON even if a model ignores the JSON-only instruction and fences it."""
    text = response.strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        lines = text.splitlines()
        if lines and lines[0].startswith(fence):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith(fence):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)
