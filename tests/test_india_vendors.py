"""Indian-market vendors: listing detection, BSE filings, Indian news, routing."""

from __future__ import annotations

import pytest

from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.vendors.india import bse, news
from tradingagents.dataflows.vendors.india.common import indian_listing, search_name

MASTER = [
    {"SCRIP_CD": "500325", "Scrip_Name": "Reliance Industries Ltd", "scrip_id": "RELIANCE",
     "Issuer_Name": "Reliance Industries Ltd", "ISIN_NUMBER": "INE002A01018",
     "INDUSTRY": "Refineries & Marketing", "Status": "Active"},
    {"SCRIP_CD": "532540", "Scrip_Name": "TATA CONSULTANCY SERVICES LTD.", "scrip_id": "TCS",
     "Issuer_Name": "Tata Consultancy Services Limited", "ISIN_NUMBER": "INE467B01029",
     "INDUSTRY": "IT", "Status": "Active"},
]


def _ann(dt, cat, sub, headline="", attach=""):
    return {"NEWSSUB": sub, "HEADLINE": headline, "CATEGORYNAME": cat, "SUBCATNAME": "-1",
            "DissemDT": dt, "NEWS_DT": dt, "ATTACHMENTNAME": attach}


ANNOUNCEMENTS = {
    "Table": [
        _ann("2026-11-14T09:05:00.13", "Company Update", "Order win (after the run date)"),
        _ann("2026-11-13T18:30:12.48", "Result", "Financial Results for Q2 FY27", "Net profit up 9%", "abc.pdf"),
        _ann("2026-11-10T11:00:00", "Insider Trading / SAST",
             "Disclosure under Regulation 31(1) of SEBI (SAST) - creation of pledge"),
        _ann("2026-10-01T10:00:00", "Board Meeting", "Board meeting on 13 Nov (before window)"),
    ],
    "Table1": [{"ROWCNT": 4}],
}

GOOGLE_RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Reliance Q2 profit beats estimates - The Economic Times</title>
<link>https://news.google.com/rss/articles/a</link>
<pubDate>Fri, 13 Nov 2026 14:00:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Reliance Q2 profit beats estimates&lt;/a&gt;</description>
<source url="https://economictimes.indiatimes.com">The Economic Times</source></item>
<item><title>Reliance Q2 profit beats estimates - Moneycontrol</title>
<link>https://news.google.com/rss/articles/b</link>
<pubDate>Fri, 13 Nov 2026 15:00:00 GMT</pubDate>
<source url="https://www.moneycontrol.com">Moneycontrol</source></item>
<item><title>Reliance Jio tariff hike - Mint</title>
<link>https://news.google.com/rss/articles/c</link>
<pubDate>Sat, 14 Nov 2026 03:00:00 GMT</pubDate>
<source url="https://www.livemint.com">Mint</source></item>
</channel></rss>"""


def _configure(**values):
    """Set config keys for one test (get_config() hands out copies); _restore_config undoes it."""
    from tradingagents.dataflows.config import set_config

    set_config(values)


@pytest.fixture(autouse=True)
def _restore_config():
    from tradingagents.dataflows.config import get_config, set_config

    keys = ("data_cache_dir", "india_bse_enabled")
    saved = {k: get_config().get(k) for k in keys}
    yield
    set_config(saved)


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """BSE and RSS answered from fixtures, with the scrip master cache in tmp."""

    calls = []
    _configure(data_cache_dir=str(tmp_path), india_bse_enabled=True)

    def fake_json(url, params=None, source=""):
        calls.append((url, params))
        return MASTER if "ListofScripData" in url else ANNOUNCEMENTS

    class _Resp:
        def __init__(self, text):
            self.text = text

    def fake_get(url, params=None, headers=None, source=""):
        calls.append((url, params))
        return _Resp(GOOGLE_RSS)

    monkeypatch.setattr(bse, "http_json", fake_json)
    monkeypatch.setattr(news, "http_get", fake_get)
    import tradingagents.dataflows.vendors.yahoo.news as yahoo_news
    monkeypatch.setattr(yahoo_news, "get_news_yfinance", lambda *a: "No news found")
    return calls


@pytest.mark.unit
@pytest.mark.parametrize("ticker, expected", [
    ("RELIANCE.NS", ("RELIANCE", "NS")),
    ("reliance.bo", ("RELIANCE", "BO")),
    ("500325.BO", ("500325", "BO")),
    ("M&M.NS", ("M&M", "NS")),
    ("NVDA", None),
    ("0700.HK", None),
])
def test_indian_listing(ticker, expected):
    assert indian_listing(ticker) == expected


@pytest.mark.unit
@pytest.mark.parametrize("raw, expected", [
    ("Reliance Industries Ltd", "Reliance Industries"),
    ("Tata Consultancy Services Limited", "Tata Consultancy Services"),
    ("Bharti Airtel Ltd.", "Bharti Airtel"),
])
def test_search_name(raw, expected):
    assert search_name(raw) == expected


@pytest.mark.unit
def test_scrip_resolves_by_nse_symbol_and_bse_code(offline):
    assert bse.resolve_scrip("RELIANCE.NS")["code"] == "500325"
    assert bse.resolve_scrip("532540.BO")["symbol"] == "TCS"
    with pytest.raises(NoMarketDataError):
        bse.resolve_scrip("NOSUCH.NS")


@pytest.mark.unit
def test_non_indian_ticker_falls_through(offline):
    with pytest.raises(NoMarketDataError):
        bse.get_announcements_bse("NVDA", "2026-11-01", "2026-11-13")
    with pytest.raises(NoMarketDataError):
        news.get_news_india("NVDA", "2026-11-01", "2026-11-13")
    assert offline == []  # no request was made for a US ticker


@pytest.mark.unit
def test_announcements_are_point_in_time(offline):
    out = bse.get_announcements_bse("RELIANCE.NS", "2026-11-06", "2026-11-13")
    assert "Financial Results for Q2 FY27" in out
    assert "Net profit up 9%" in out
    assert "AttachLive/abc.pdf" in out
    assert "after the run date" not in out      # disseminated 14 Nov
    assert "before window" not in out
    params = [p for u, p in offline if "AnnSubCategoryGetData" in u][0]
    assert params["strScrip"] == "500325"
    assert (params["strPrevDate"], params["strToDate"]) == ("20261106", "20261113")


@pytest.mark.unit
def test_insider_view_keeps_only_pit_and_sast(offline):
    out = bse.get_insider_transactions_bse("RELIANCE.NS", "2026-11-13")
    assert "creation of pledge" in out
    assert "Financial Results" not in out


@pytest.mark.unit
def test_google_news_parsing_dedupes_and_respects_window(offline):
    out = news.get_news_india("RELIANCE.NS", "2026-11-06", "2026-11-13")
    assert out.count("Reliance Q2 profit beats estimates") == 1   # same story, two outlets
    assert "(source: The Economic Times)" in out or "(source: Moneycontrol)" in out
    assert "tariff hike" not in out                              # published 14 Nov
    q = [p for u, p in offline if u == news.GOOGLE_NEWS][0]
    assert q["q"] == '"Reliance Industries" after:2026-11-06 before:2026-11-14'
    assert q["gl"] == "IN"


@pytest.mark.unit
@pytest.mark.parametrize("bse_on", [True, False])
def test_india_tools_offered_only_for_indian_listings(monkeypatch, bse_on):
    from tradingagents.agents.analysts import fundamentals_analyst as fa, news_analyst as na
    _configure(india_bse_enabled=bse_on)
    for mod in (na, fa):
        offered = mod.tools_for("TCS.NS")
        assert all(t in offered for t in mod.BASE_TOOLS)
        assert all((t in offered) == bse_on for t in mod.BSE_TOOLS)
        assert mod.tools_for("NVDA") == mod.BASE_TOOLS
        assert mod.india_brief("NVDA") == ""
        assert ("get_corporate_announcements" in mod.india_brief("TCS.NS")) == bse_on
    assert na.get_india_market_news in na.tools_for("TCS.NS")


@pytest.mark.unit
def test_with_bse_off_insider_falls_back_to_yahoo(monkeypatch):
    from tradingagents.dataflows import router

    _configure(india_bse_enabled=False)
    monkeypatch.setattr(bse, "http_json", lambda *a, **k: pytest.fail("BSE was called"))
    monkeypatch.setitem(router.VENDOR_METHODS["get_insider_transactions"], "yfinance",
                        lambda *a: "yahoo insiders")
    assert router.route_to_vendor("get_insider_transactions", "RELIANCE.NS", None) == "yahoo insiders"
    out = router.route_to_vendor("get_corporate_announcements", "RELIANCE.NS", "2026-11-01", "2026-11-13")
    assert out.startswith("DATA_UNAVAILABLE") and "TRADINGAGENTS_INDIA_BSE" in out


@pytest.mark.unit
def test_router_sends_us_tickers_to_yahoo(monkeypatch):
    from tradingagents.dataflows import router

    monkeypatch.setitem(router.VENDOR_METHODS["get_news"], "yfinance", lambda *a: "yahoo news")
    assert router.route_to_vendor("get_news", "NVDA", "2026-11-01", "2026-11-13") == "yahoo news"
