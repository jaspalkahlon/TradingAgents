"""BSE corporate filings: announcements, insider/SAST disclosures, scrip lookup.

Every BSE announcement carries the time BSE disseminated it, which is when the
market learned of it, so filtering on that time is point-in-time safe for
backtests: a run dated D never reads a filing made public after D.

NSE symbols are mapped to BSE scrip codes through BSE's own scrip master
(``scrip_id`` is the NSE-style symbol for almost every dual-listed company).
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.vendors.india.common import http_json, require_bse, require_indian

logger = logging.getLogger(__name__)

API = "https://api.bseindia.com/BseIndiaAPI/api"
ATTACHMENT_URL = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{name}"
_MASTER_TTL_SECONDS = 7 * 24 * 3600
_MAX_PAGES = 5           # 50 rows a page; a busy company files ~20 a month
_INSIDER_CATEGORIES = ("insider trading", "sast")
_ROUTINE_SUBCATEGORIES = ("closure of trading window",)  # a calendar notice, not a trade


# --------------------------------------------------------------------------- scrips

def _master_path() -> Path:
    return Path(get_config()["data_cache_dir"]) / "bse" / "scrip_master.json"


def _scrip_master() -> list[dict]:
    """BSE's list of active equity scrips, cached for a week."""
    require_bse()
    path = _master_path()
    if path.exists() and time.time() - path.stat().st_mtime < _MASTER_TTL_SECONDS:
        with contextlib.suppress(ValueError):
            return json.loads(path.read_text(encoding="utf-8"))
    rows = http_json(
        f"{API}/ListofScripData/w",
        params={"Group": "", "Scripcode": "", "industry": "", "segment": "Equity", "status": "Active"},
        source="BSE scrip master",
    )
    if not isinstance(rows, list) or not rows:
        raise VendorUnavailableError("BSE scrip master came back empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    replace_file(path, lambda temp: Path(temp).write_text(json.dumps(rows), encoding="utf-8"))
    return rows


def resolve_scrip(ticker: str) -> dict:
    """``{"code", "symbol", "name", "isin", "industry"}`` for an NSE/BSE ticker.

    ``RELIANCE.NS`` and ``RELIANCE.BO`` match on BSE's ``scrip_id``; a numeric
    ``500325.BO`` matches on the scrip code.
    """
    root, _ = require_indian(ticker)
    for row in _scrip_master():
        code = str(row.get("SCRIP_CD") or "").strip()
        symbol = str(row.get("scrip_id") or "").strip().upper()
        if root in (code, symbol):
            return {
                "code": code,
                "symbol": symbol,
                "name": (row.get("Issuer_Name") or row.get("Scrip_Name") or symbol).strip(),
                "isin": (row.get("ISIN_NUMBER") or "").strip(),
                "industry": (row.get("INDUSTRY") or "").strip(),
            }
    raise NoMarketDataError(ticker, detail="no active BSE equity listing with that symbol")


# --------------------------------------------------------------------- announcements

def _parse_time(raw) -> datetime | None:
    """BSE times are IST without an offset, e.g. ``2026-11-13T18:30:12.48``."""
    if not raw:
        return None
    text = str(raw).strip().replace(" ", "T")
    with contextlib.suppress(ValueError):
        return datetime.fromisoformat(text.split(".")[0])
    return None


def _disseminated(row: dict) -> datetime | None:
    """When the market saw the filing (IST, naive)."""
    return _parse_time(row.get("DissemDT")) or _parse_time(row.get("NEWS_DT")) or _parse_time(row.get("DT_TM"))


def fetch_announcements(code: str, start_date: str, end_date: str) -> list[dict]:
    """All announcements a scrip filed between the dates (inclusive), newest first."""
    require_bse()
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    rows: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        data = http_json(
            f"{API}/AnnSubCategoryGetData/w",
            params={
                "pageno": page, "strCat": "-1", "subcategory": "-1",
                "strPrevDate": f"{start:%Y%m%d}", "strToDate": f"{end:%Y%m%d}",
                "strScrip": code, "strSearch": "P", "strType": "C",
            },
            source="BSE announcements",
        )
        if not isinstance(data, dict):
            raise VendorUnavailableError("BSE announcements returned an unexpected payload")
        table = data.get("Table") or []
        rows.extend(table)
        total = int(((data.get("Table1") or [{}])[0] or {}).get("ROWCNT") or 0)
        if not table or len(rows) >= total:
            break
    # The window is inclusive of end_date, in IST: keep up to midnight after it.
    limit = end + timedelta(days=1)
    kept = [r for r in rows if (t := _disseminated(r)) is not None and start <= t < limit]
    kept.sort(key=_disseminated, reverse=True)
    return kept


def _clean(text) -> str:
    """BSE text with its SQL-escaped quotes ('') undone and whitespace collapsed."""
    return " ".join(str(text or "").replace("''", "'").split())


def _format_row(row: dict) -> str:
    when = _disseminated(row)
    category = " / ".join(p for p in (row.get("CATEGORYNAME"), row.get("SUBCATNAME")) if p and p.strip() != "-1")
    subject = _clean(row.get("NEWSSUB"))
    # MORE holds the full text; HEADLINE is it cut off with "....".
    headline = _clean(row.get("MORE")) or _clean(row.get("HEADLINE"))
    line = f"- **{when:%Y-%m-%d %H:%M} IST** [{category or 'Uncategorised'}] {subject}"
    if headline and headline.lower() not in subject.lower():
        line += f"\n  {headline[:600]}"
    if row.get("ATTACHMENTNAME"):
        line += f"\n  Filing: {ATTACHMENT_URL.format(name=row['ATTACHMENTNAME'])}"
    return line


def get_announcements_bse(ticker: str, start_date: str, end_date: str) -> str:
    """Corporate announcements filed with BSE in the window, as disseminated."""
    scrip = resolve_scrip(ticker)
    rows = fetch_announcements(scrip["code"], start_date, end_date)
    head = (f"## BSE corporate announcements for {scrip['name']} "
            f"(BSE {scrip['code']}, {scrip['symbol']}), {start_date} to {end_date}\n")
    if not rows:
        return head + "\nNo announcements were filed with BSE in this window."
    return head + f"\n{len(rows)} filings, newest first:\n\n" + "\n".join(_format_row(r) for r in rows)


def get_insider_transactions_bse(ticker: str, as_of_date: str | None = None) -> str:
    """Insider-trading (SEBI PIT) and takeover-code (SAST) disclosures, including
    promoter pledges and their release, filed in the 90 days to ``as_of_date``."""
    end = as_of_date or datetime.now().strftime("%Y-%m-%d")
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=90)).strftime("%Y-%m-%d")
    scrip = resolve_scrip(ticker)
    rows = [
        r for r in fetch_announcements(scrip["code"], start, end)
        if any(c in (r.get("CATEGORYNAME") or "").lower() for c in _INSIDER_CATEGORIES)
        and not any(s in (r.get("SUBCATNAME") or "").lower() for s in _ROUTINE_SUBCATEGORIES)
    ]
    head = (f"# Insider and SAST disclosures for {scrip['name']} (BSE {scrip['code']})\n"
            f"# Filed {start} to {end}, as disseminated by BSE"
            f"{' (point-in-time)' if is_historical(as_of_date) else ''}\n\n"
            "Covers SEBI PIT Regulation 7 trades by promoters, directors and designated persons, "
            "and SAST Regulation 29/31 filings: substantial acquisitions and the creation, "
            "invocation or release of promoter pledges. Quantities are in the linked filings.\n\n")
    if not rows:
        return head + "No insider or SAST disclosures were filed with BSE in this window."
    return head + "\n".join(_format_row(r) for r in rows)


# ----------------------------------------------------------------------- shareholding

def get_shareholding_bse(ticker: str, as_of_date: str | None = None) -> str:
    """Quarterly shareholding pattern filed with BSE (promoter, FPI, DII, public, pledge)."""
    resolve_scrip(ticker)
    raise VendorUnavailableError("BSE shareholding pattern is not connected yet")
