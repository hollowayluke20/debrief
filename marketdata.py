"""Market-snapshot data layer.

Sources:
- Twelve Data /quote batch endpoint: SPY (S&P 500 proxy), QQQ (Nasdaq proxy),
  gold, USO (WTI oil proxy), bitcoin, and GBP/USD. A comma-separated symbol
  request is one HTTP call. The free plan does not expose the SPX index or
  WTI/USD symbols directly.
- U.S. Treasury Daily Par Yield Curve CSV: 2-, 10-, and 30-year Treasury
  yields, including the preceding published curve for daily change.

Deployment: set TWELVEDATA_API_KEY locally in .env and add the same secret to
the GitHub Actions repository before this module is used in the workflow.
"""

from __future__ import annotations

import csv
import io
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parent
TWELVE_DATA_QUOTE_URL = "https://api.twelvedata.com/quote"
TWELVE_DATA_TIME_SERIES_URL = "https://api.twelvedata.com/time_series"
TREASURY_CSV_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
    "daily-treasury-rates.csv/{year}/all"
)
REQUEST_TIMEOUT = 30
MARKET_SYMBOLS = {
    "S&P 500 (SPY)": "SPY",
    "Nasdaq (QQQ)": "QQQ",
    "Gold": "XAU/USD",
    "WTI oil": "USO",
    "Bitcoin": "BTC/USD",
    "GBP/USD": "GBP/USD",
}
TREASURY_MATURITIES = {
    "US Treasury 2-year": "2 Yr",
    "US Treasury 10-year": "10 Yr",
    "US Treasury 30-year": "30 Yr",
}


class MarketDataError(RuntimeError):
    """Raised when a market-data source cannot provide a usable snapshot."""


def _ensure_env() -> None:
    if os.environ.get("TWELVEDATA_API_KEY"):
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


def _number(value: Any, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise MarketDataError(f"Invalid {label}: {value!r}") from error


def _daily_change(level: float, previous_close: float) -> float:
    if previous_close == 0:
        raise MarketDataError("Previous close is zero; cannot calculate daily change.")
    return (level - previous_close) / previous_close * 100


def _gold_previous_close(api_key: str) -> float:
    """Get gold's previous completed daily close, absent from its quote response."""
    payload = _get_json(
        TWELVE_DATA_TIME_SERIES_URL,
        {
            "symbol": MARKET_SYMBOLS["Gold"],
            "interval": "1day",
            "outputsize": 2,
            "apikey": api_key,
        },
    )
    values = payload.get("values") if isinstance(payload, dict) else None
    if not isinstance(values, list) or len(values) < 2:
        raise MarketDataError(f"Twelve Data did not return two gold daily bars: {payload!r}")
    return _number(values[1].get("close"), "XAU/USD previous daily close")


def _get_json(url: str, params: dict) -> dict:
    """GET with retries - Twelve Data's free tier throws transient 5xx / 'Quota
    API is not available' errors that clear within a few seconds."""
    last = ""
    for attempt in range(4):
        if attempt:
            time.sleep(3 * attempt)
        try:
            response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as error:
            last = str(error)
            continue
        if response.status_code >= 500 or "Quota API is not available" in response.text:
            last = f"HTTP {response.status_code}: {response.text[:160]}"
            continue
        if not response.ok:
            raise MarketDataError(f"Twelve Data HTTP {response.status_code}: {response.text}")
        return response.json()
    raise MarketDataError(f"Twelve Data unavailable after retries ({last})")


def _market_quotes(api_key: str) -> dict[str, dict[str, float | str]]:
    payload = _get_json(
        TWELVE_DATA_QUOTE_URL,
        {"symbol": ",".join(MARKET_SYMBOLS.values()), "apikey": api_key},
    )
    if not isinstance(payload, dict):
        raise MarketDataError(f"Unexpected Twelve Data response: {payload!r}")

    quotes: dict[str, dict[str, float | str]] = {}
    for label, symbol in MARKET_SYMBOLS.items():
        quote = payload.get(symbol)
        if not isinstance(quote, dict) or quote.get("status") == "error":
            raise MarketDataError(f"Twelve Data did not return {symbol}: {quote!r}")
        level = _number(quote.get("close"), f"{symbol} close")
        previous_close = (
            _gold_previous_close(api_key)
            if label == "Gold"
            else _number(quote.get("previous_close"), f"{symbol} previous close")
        )
        quotes[label] = {
            "symbol": symbol,
            "level": level,
            "previous_close": previous_close,
            "daily_change_pct": _daily_change(level, previous_close),
            "source": "Twelve Data /quote",
        }
    return quotes


def _treasury_yields() -> dict[str, dict[str, float | str]]:
    year = datetime.now(timezone.utc).year
    response = requests.get(
        TREASURY_CSV_URL.format(year=year),
        params={
            "type": "daily_treasury_yield_curve",
            "field_tdr_date_value": year,
            "page": "",
            "_format": "csv",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise MarketDataError(f"U.S. Treasury HTTP {response.status_code}: {response.text}")
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8"))))
    usable_rows = [
        row for row in rows
        if all(row.get(column) not in (None, "", "N/A") for column in TREASURY_MATURITIES.values())
    ]
    if len(usable_rows) < 2:
        raise MarketDataError("U.S. Treasury yield-curve feed did not contain two usable rows.")

    latest, previous = usable_rows[-1], usable_rows[-2]
    yields: dict[str, dict[str, float | str]] = {}
    for label, column in TREASURY_MATURITIES.items():
        level = _number(latest[column], f"{label} current yield")
        previous_close = _number(previous[column], f"{label} previous yield")
        yields[label] = {
            "symbol": column,
            "level": level,
            "previous_close": previous_close,
            "daily_change_pct": _daily_change(level, previous_close),
            "source": "U.S. Treasury Daily Par Yield Curve",
            "as_of": latest.get("Date", ""),
        }
    return yields


def get_snapshot() -> dict[str, dict[str, float | str]]:
    """Return current level, prior close, and daily percentage change for each item."""
    _ensure_env()
    api_key = os.environ.get("TWELVEDATA_API_KEY")
    if not api_key:
        raise MarketDataError("TWELVEDATA_API_KEY is not set (expected in .env or environment).")
    return _market_quotes(api_key) | _treasury_yields()
