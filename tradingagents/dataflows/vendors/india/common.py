"""Shared plumbing for the Indian-market vendors (BSE filings, Indian news).

Only NSE (``.NS``) and BSE (``.BO``) listings are served; any other symbol
raises ``NoMarketDataError`` so a vendor chain such as ``"bse,yfinance"`` falls
through to the next vendor for US and other listings.
"""

from __future__ import annotations

import logging
import re

import requests

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.symbols import normalize_symbol

logger = logging.getLogger(__name__)

TIMEOUT = 20.0
# BSE's API answers only browser-like requests that come from its own site.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
}

_INDIAN = re.compile(r"^(?P<root>[A-Z0-9&\-_]+)\.(?P<exchange>NS|BO)$")
_SUFFIXES = re.compile(
    r"[\s,.]+(ltd|limited|ltd\.|pvt|private|corporation|corp|co|company|india|\(india\))\.?$",
    re.IGNORECASE,
)


def indian_listing(ticker: str) -> tuple[str, str] | None:
    """``(root, exchange)`` for an NSE/BSE symbol, e.g. ``("RELIANCE", "NS")``; else None.

    A BSE numeric code works too (``500325.BO``).
    """
    m = _INDIAN.match(normalize_symbol(ticker) or "")
    return (m.group("root"), m.group("exchange")) if m else None


def require_indian(ticker: str) -> tuple[str, str]:
    """The listing, or ``NoMarketDataError`` so the router tries the next vendor."""
    listing = indian_listing(ticker)
    if listing is None:
        raise NoMarketDataError(ticker, detail="not an NSE (.NS) or BSE (.BO) listing")
    return listing


def search_name(name: str) -> str:
    """A company name trimmed for news search: 'Reliance Industries Ltd.' -> 'Reliance Industries'."""
    name = re.sub(r"\s+", " ", name or "").strip(" .,-")
    while True:
        trimmed = _SUFFIXES.sub("", name).strip(" .,-")
        if trimmed == name or not trimmed:
            return name
        name = trimmed


def http_get(url: str, *, params: dict | None = None, headers: dict | None = None,
             source: str) -> requests.Response:
    """GET with a timeout; any transport failure or bad status is ``VendorUnavailableError``."""
    try:
        response = requests.get(url, params=params, headers=headers or BROWSER_HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise VendorUnavailableError(f"{source} request failed: {exc}") from None


def http_json(url: str, *, params: dict | None = None, source: str):
    """Decoded JSON, or ``VendorUnavailableError`` when the body is not JSON.

    BSE serves an HTML error page with status 200 when it rejects a request.
    """
    response = http_get(url, params=params, source=source)
    try:
        return response.json()
    except ValueError:
        raise VendorUnavailableError(
            f"{source} returned a non-JSON response ({response.text[:120]!r})"
        ) from None
