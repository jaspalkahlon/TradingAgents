"""Indian business news: Google News (India edition) and Economic Times RSS.

Yahoo's news for NSE/BSE listings is mostly wire copy. Google News' India
edition searches the Indian business press (Economic Times, Moneycontrol,
Business Standard, Mint, CNBC-TV18...) by company name and accepts a date range,
so a backtest can ask for the week before its run date. Economic Times' market
and economy feeds give the India-wide picture (RBI, FII flows, Nifty, rupee).
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.date_window import coverage_gap, in_window
from tradingagents.dataflows.errors import VendorError, VendorUnavailableError
from tradingagents.dataflows.vendors.india.common import http_get, require_indian, search_name

logger = logging.getLogger(__name__)

GOOGLE_NEWS = "https://news.google.com/rss/search"
ET_FEEDS = {
    "ET Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "ET Economy": "https://economictimes.indiatimes.com/news/economy/rssfeeds/1373380680.cms",
}
_RSS_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; tradingagents-india/1.0)"}
_TAGS = re.compile(r"<[^>]+>")


def _text(node, tag: str) -> str:
    found = node.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_rss(xml_text: str, default_source: str) -> list[dict]:
    """RSS ``<item>``s as ``{"title", "summary", "publisher", "link", "pub_date"}``."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items = []
    for item in root.iter("item"):
        title = html.unescape(_text(item, "title"))
        source = item.find("source")
        publisher = (source.text or "").strip() if source is not None and source.text else default_source
        # Google News appends " - Publisher" to every title.
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3]
        summary = html.unescape(_TAGS.sub(" ", html.unescape(_text(item, "description"))))
        summary = re.sub(r"\s+", " ", summary).strip()
        if summary.startswith(title):  # Google's description repeats the title
            summary = ""
        pub_date = None
        if raw := _text(item, "pubDate"):
            try:
                pub_date = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                pub_date = None
        items.append({"title": title, "summary": summary[:400], "publisher": publisher,
                      "link": _text(item, "link"), "pub_date": pub_date})
    return items


def _fetch_rss(url: str, params: dict | None, source: str) -> list[dict]:
    response = http_get(url, params=params, headers=_RSS_HEADERS, source=source)
    return parse_rss(response.text, source)


def _company_name(ticker: str) -> str:
    """The issuer's name from BSE, else the NSE/BSE symbol itself."""
    from tradingagents.dataflows.vendors.india.bse import resolve_scrip

    try:
        return search_name(resolve_scrip(ticker)["name"])
    except VendorError as exc:
        logger.info("BSE name lookup failed for %s (%s); searching by symbol", ticker, exc)
        return require_indian(ticker)[0]


def _format(articles: list[dict]) -> str:
    out = []
    for a in articles:
        when = f"{a['pub_date']:%Y-%m-%d} " if a["pub_date"] else ""
        line = f"### {when}{a['title']} (source: {a['publisher']})"
        if a["summary"]:
            line += f"\n{a['summary']}"
        if a["link"]:
            line += f"\nLink: {a['link']}"
        out.append(line)
    return "\n\n".join(out)


def _dedupe(articles: list[dict]) -> list[dict]:
    seen, kept = set(), []
    for a in articles:
        key = re.sub(r"\W+", "", a["title"].lower())[:80]
        if key and key not in seen:
            seen.add(key)
            kept.append(a)
    return kept


def get_news_india(ticker: str, start_date: str, end_date: str) -> str:
    """Indian press coverage of an NSE/BSE company in the window, plus Yahoo's feed.

    Any other listing raises ``NoMarketDataError`` so the chain moves to the next
    vendor. Google News is searched with the window's own dates, then each item
    is checked against the window, so a past run reads only what was published
    by its date.
    """
    require_indian(ticker)
    name = _company_name(ticker)
    limit = get_config()["news_article_limit"]
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    before = (end_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    query = f'"{name}" after:{start_date} before:{before}'
    articles = _fetch_rss(GOOGLE_NEWS, {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"},
                          "Google News India")
    articles = [a for a in articles if in_window(a["pub_date"], start_dt, end_dt)]
    articles.sort(key=lambda a: a["pub_date"] or start_dt, reverse=True)
    articles = _dedupe(articles)[: max(limit, 15)]

    # Yahoo's feed is a second opinion, not a dependency.
    from tradingagents.dataflows.vendors.yahoo.news import get_news_yfinance
    try:
        yahoo = get_news_yfinance(ticker, start_date, end_date)
    except (VendorError, Exception) as exc:  # noqa: BLE001 — never fail Indian news on Yahoo
        logger.info("Yahoo news unavailable for %s: %s", ticker, exc)
        yahoo = ""

    out = f"## {ticker} ({name}) news from the Indian press, {start_date} to {end_date}:\n\n"
    out += _format(articles) if articles else (
        f"Google News India found no articles naming \"{name}\" in this window.")
    if yahoo and not yahoo.startswith("<") and not yahoo.startswith("No news"):
        out += "\n\n" + yahoo.replace("## ", "## Yahoo Finance: ", 1)
    return out


def get_india_market_news(as_of_date: str, look_back_days: int = 3, limit: int = 20) -> str:
    """India-wide market and economy headlines from Economic Times up to ``as_of_date``.

    The feeds hold only the latest stories, so a run dated in the past gets a
    coverage notice instead of today's headlines.
    """
    end_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=look_back_days)
    start_date = f"{start_dt:%Y-%m-%d}"
    articles: list[dict] = []
    failures = []
    for source, url in ET_FEEDS.items():
        try:
            articles.extend(_fetch_rss(url, None, source))
        except VendorError as exc:
            failures.append(f"{source}: {exc}")
    if failures and not articles:
        raise VendorUnavailableError("; ".join(failures))
    dates = [a["pub_date"] for a in articles]
    in_range = [a for a in articles if in_window(a["pub_date"], start_dt, end_dt)]
    in_range.sort(key=lambda a: a["pub_date"] or end_dt, reverse=True)
    in_range = _dedupe(in_range)[:limit]
    if not in_range:
        gap = coverage_gap(dates, start_date, as_of_date, "Economic Times feeds", "Indian market news")
        return gap or f"No Indian market headlines between {start_date} and {as_of_date}."
    return (f"## Indian market and economy headlines (Economic Times), {start_date} to {as_of_date}:\n\n"
            + _format(in_range))
