"""
catalyst.py
===========
OpenClaw catalyst scoring router for think-tank-relay.
Sources: Finnhub company news (primary) → NewsAPI headlines (secondary) → SEC EDGAR 8-K (fallback)

ENDPOINTS:
  POST /score            — score a list of tickers (body: {tickers: [...], secret: "..."})
  GET  /catalyst/health  — validates provider auth (not just env var presence)

HYGIENE PATCH (2026-05-27):
  - Added logging throughout — errors are now visible in Railway logs
  - Finnhub: detects 200-with-error-dict (rate-limit / auth failure)
  - NewsAPI: detects 200-with-error-body (developer plan CORS / auth failure)
  - /catalyst/health: makes lightweight live calls to validate keys, not just check presence
  Bug existed since commit 03eb89b. Not introduced by refactor 7cb34f9 (whitespace only).
"""

import logging
import os
import json
import requests
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone, timedelta
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
NEWS_API_KEY    = os.environ.get("NEWS_API_KEY", "")
BRIDGE_SECRET   = os.environ.get("BRIDGE_SECRET", "")

DAMPENING_FINNHUB = 0.85
DAMPENING_NEWS    = 0.75
DAMPENING_EDGAR   = 0.85

SEC_HEADERS = {"User-Agent": "BonsignoreTradingBot research@bonsignore.trading"}


# ──────────────────────────────────────────────────────────────
# Request / Response models
# ──────────────────────────────────────────────────────────────

class ScoreRequest(BaseModel):
    tickers: list[str]
    secret: str


# ──────────────────────────────────────────────────────────────
# Finnhub — Company News
# ──────────────────────────────────────────────────────────────

FINNHUB_POSITIVE = [
    ("partnership", 6), ("agreement", 5), ("contract", 5), ("acquisition", 7),
    ("fda approved", 9), ("fda cleared", 8), ("guidance raised", 7),
    ("earnings beat", 7), ("record revenue", 6), ("buyback", 4),
    ("merger", 6), ("joint venture", 5), ("upgrade", 4), ("outperform", 4),
    ("buy rating", 4), ("price target raised", 5), ("beat estimates", 6),
]

FINNHUB_NEGATIVE = [
    ("restatement", -8), ("investigation", -8), ("sec subpoena", -10),
    ("class action", -7), ("going concern", -9), ("bankruptcy", -10),
    ("delisted", -10), ("guidance cut", -6), ("missed estimates", -5),
    ("downgrade", -5), ("sell rating", -4), ("price target cut", -4),
    ("layoffs", -3), ("ceo resign", -4),
]


def fetch_finnhub_news(ticker: str, days_back: int = 2) -> list[dict]:
    """
    Pull recent company news for a ticker from Finnhub.

    FIXED BUGS:
    - Now detects 200 responses with error dicts (rate-limit, auth failure)
    - Logs all failures explicitly instead of silently returning []
    """
    if not FINNHUB_API_KEY:
        logger.warning("fetch_finnhub_news: FINNHUB_API_KEY not set")
        return []

    today     = date.today()
    from_date = (today - timedelta(days=days_back)).isoformat()
    to_date   = today.isoformat()

    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": ticker.upper(),
                "from":   from_date,
                "to":     to_date,
                "token":  FINNHUB_API_KEY,
            },
            timeout=10,
        )

        if resp.status_code != 200:
            logger.warning("fetch_finnhub_news: HTTP %d for %s", resp.status_code, ticker)
            return []

        data = resp.json()

        # FIX: Finnhub returns a dict (not list) when rate-limited or auth fails.
        # e.g. {"error": "You don't have access to this resource."}
        # The old code passed isinstance(data, list) check only when data IS a list,
        # but never logged the dict case — it just returned [].
        if not isinstance(data, list):
            logger.warning(
                "fetch_finnhub_news: expected list, got %s for ticker %s. "
                "Possible rate-limit or auth failure. Response: %s",
                type(data).__name__, ticker, str(data)[:200]
            )
            return []

        if not data:
            logger.info("fetch_finnhub_news: no articles for %s in date range %s to %s",
                        ticker, from_date, to_date)

        return [
            {
                "headline": a.get("headline", ""),
                "summary":  a.get("summary", ""),
                "url":      a.get("url", ""),
                "datetime": a.get("datetime", 0),
                "source":   a.get("source", "Finnhub"),
            }
            for a in data
        ]

    except requests.exceptions.Timeout:
        logger.error("fetch_finnhub_news: timeout for ticker %s", ticker)
        return []
    except requests.exceptions.RequestException as exc:
        logger.error("fetch_finnhub_news: request error for %s: %s", ticker, exc)
        return []
    except Exception as exc:
        logger.error("fetch_finnhub_news: unexpected error for %s: %s", ticker, exc, exc_info=True)
        return []


def score_finnhub_news(ticker: str, articles: list[dict]) -> dict | None:
    if not articles:
        return None

    total_score   = 0
    match_count   = 0
    best_headline = ""
    best_catalyst = "finnhub_news"

    for article in articles:
        text = (article.get("headline", "") + " " + article.get("summary", "")).lower()
        match_count += 1
        if not best_headline:
            best_headline = article.get("headline", "")[:120]

        for keyword, points in FINNHUB_POSITIVE:
            if keyword in text:
                total_score  += points
                best_catalyst = f"finnhub_{keyword.replace(' ', '_')}"
                break

        for keyword, points in FINNHUB_NEGATIVE:
            if keyword in text:
                total_score  += points
                best_catalyst = f"finnhub_negative_{keyword.replace(' ', '_')}"
                break

    if match_count > 1:
        total_score += min(2 * (match_count - 1), 4)

    if total_score == 0:
        return None

    return {
        "catalyst_type": best_catalyst,
        "headline":      best_headline,
        "source":        "Finnhub",
        "raw_score":     total_score,
        "score":         round(total_score * DAMPENING_FINNHUB, 1),
        "dampening":     DAMPENING_FINNHUB,
        "confidence":    "high" if abs(total_score) >= 7 else "medium",
        "url":           articles[0].get("url", ""),
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────────────────────
# NewsAPI — Headline Scoring
# ──────────────────────────────────────────────────────────────

NEWS_POSITIVE = [
    ("partnership", 5), ("acquisition", 6), ("earnings beat", 6),
    ("raised guidance", 5), ("record", 4), ("upgrade", 4), ("buy", 3),
    ("deal", 4), ("contract", 4), ("approved", 5),
]

NEWS_NEGATIVE = [
    ("downgrade", -4), ("sell", -3), ("miss", -3), ("investigation", -6),
    ("lawsuit", -5), ("recall", -5), ("bankruptcy", -9), ("cut guidance", -5),
]


def fetch_newsapi_headlines(ticker: str) -> list[dict]:
    """
    Pull news headlines from NewsAPI for a ticker.

    FIXED BUGS:
    - Now detects 200-with-error-body (NewsAPI developer plan returns status:"error" with HTTP 200)
    - Logs all failure paths explicitly
    """
    if not NEWS_API_KEY:
        logger.warning("fetch_newsapi_headlines: NEWS_API_KEY not set")
        return []

    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q":        f"{ticker} stock",
                "language": "en",
                "sortBy":   "publishedAt",
                "pageSize": 10,
                "from":     (date.today() - timedelta(days=2)).isoformat(),
                "apiKey":   NEWS_API_KEY,
            },
            timeout=10,
        )

        if resp.status_code != 200:
            logger.warning("fetch_newsapi_headlines: HTTP %d for %s", resp.status_code, ticker)
            return []

        data = resp.json()

        # FIX: NewsAPI developer plan returns HTTP 200 with {"status": "error", "code": "...", "message": "..."}
        # when the plan doesn't support the query (CORS restrictions, domain limits).
        # The old code called .get("articles", []) which returned [] silently with no log.
        if data.get("status") != "ok":
            logger.warning(
                "fetch_newsapi_headlines: NewsAPI returned status=%s for ticker %s. "
                "Code: %s. Message: %s",
                data.get("status"), ticker,
                data.get("code", "unknown"),
                data.get("message", "no message")[:200]
            )
            return []

        articles = data.get("articles", [])
        if not articles:
            logger.info("fetch_newsapi_headlines: no articles for %s", ticker)

        return [
            {
                "headline": a.get("title", ""),
                "url":      a.get("url", ""),
                "source":   a.get("source", {}).get("name", "NewsAPI"),
            }
            for a in articles
        ]

    except requests.exceptions.Timeout:
        logger.error("fetch_newsapi_headlines: timeout for ticker %s", ticker)
        return []
    except requests.exceptions.RequestException as exc:
        logger.error("fetch_newsapi_headlines: request error for %s: %s", ticker, exc)
        return []
    except Exception as exc:
        logger.error("fetch_newsapi_headlines: unexpected error for %s: %s", ticker, exc, exc_info=True)
        return []


def score_newsapi_headlines(ticker: str, articles: list[dict]) -> dict | None:
    if not articles:
        return None

    total_score   = 0
    best_headline = ""
    best_catalyst = "newsapi_headline"

    for article in articles:
        text = article.get("headline", "").lower()
        if not best_headline:
            best_headline = article.get("headline", "")[:120]

        for keyword, points in NEWS_POSITIVE:
            if keyword in text:
                total_score  += points
                best_catalyst = f"news_{keyword.replace(' ', '_')}"
                break

        for keyword, points in NEWS_NEGATIVE:
            if keyword in text:
                total_score  += points
                break

    if total_score == 0:
        return None

    return {
        "catalyst_type": best_catalyst,
        "headline":      best_headline,
        "source":        "NewsAPI",
        "raw_score":     total_score,
        "score":         round(total_score * DAMPENING_NEWS, 1),
        "dampening":     DAMPENING_NEWS,
        "confidence":    "medium",
        "url":           articles[0].get("url", "") if articles else "",
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────────────────────
# SEC EDGAR — 8-K Fallback
# ──────────────────────────────────────────────────────────────

SEC_RSS_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany&CIK={ticker}&type=8-K"
    "&dateb=&owner=include&count=5&search_text=&output=atom"
)

SEC_POSITIVE = [
    ("partnership", 5), ("agreement", 4), ("acquisition", 6),
    ("guidance raised", 6), ("earnings beat", 6),
]
SEC_NEGATIVE = [
    ("restatement", -8), ("investigation", -8), ("sec inquiry", -10),
    ("class action", -7), ("going concern", -9),
]


def fetch_edgar_8k(ticker: str) -> list[dict]:
    try:
        resp = requests.get(
            SEC_RSS_URL.format(ticker=ticker.upper()),
            headers=SEC_HEADERS, timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("fetch_edgar_8k: HTTP %d for %s", resp.status_code, ticker)
            return []

        root    = ET.fromstring(resp.content)
        ns      = {"atom": "http://www.w3.org/2005/Atom"}
        entries = []
        today   = date.today().isoformat()

        for entry in root.findall("atom:entry", ns):
            updated = entry.findtext("atom:updated", "", ns)[:10]
            if updated < today:
                continue
            entries.append({
                "title":   entry.findtext("atom:title", "", ns),
                "summary": entry.findtext("atom:summary", "", ns),
                "updated": updated,
                "url":     (entry.find("atom:link", ns) or {}).get("href", ""),
            })
        return entries

    except ET.ParseError as exc:
        logger.error("fetch_edgar_8k: XML parse error for %s: %s", ticker, exc)
        return []
    except requests.exceptions.RequestException as exc:
        logger.error("fetch_edgar_8k: request error for %s: %s", ticker, exc)
        return []
    except Exception as exc:
        logger.error("fetch_edgar_8k: unexpected error for %s: %s", ticker, exc, exc_info=True)
        return []


def score_edgar_filing(filing: dict) -> dict | None:
    text  = (filing.get("title", "") + " " + filing.get("summary", "")).lower()
    score = 3
    cat   = "sec_8k"

    for kw, pts in SEC_POSITIVE:
        if kw in text:
            score += pts
            cat = f"sec_8k_{kw.replace(' ', '_')}"
            break

    for kw, pts in SEC_NEGATIVE:
        if kw in text:
            score += pts
            cat = "sec_8k_negative"
            break

    final = round(score * DAMPENING_EDGAR, 1)
    return {
        "catalyst_type": cat,
        "headline":      filing.get("title", "SEC 8-K filing today"),
        "source":        "SEC EDGAR",
        "raw_score":     score,
        "score":         final,
        "dampening":     DAMPENING_EDGAR,
        "confidence":    "high" if abs(score) >= 6 else "medium",
        "url":           filing.get("url", ""),
        "timestamp":     filing.get("updated", datetime.now(timezone.utc).isoformat()),
    }


# ──────────────────────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────────────────────

def score_ticker(ticker: str) -> dict | None:
    """Run all sources for one ticker, return best signal or None."""
    logger.info("score_ticker: scoring %s", ticker)
    candidates = []

    articles = fetch_finnhub_news(ticker)
    result   = score_finnhub_news(ticker, articles)
    if result:
        candidates.append(result)

    news_articles = fetch_newsapi_headlines(ticker)
    news_result   = score_newsapi_headlines(ticker, news_articles)
    if news_result:
        candidates.append(news_result)

    if not candidates:
        filings = fetch_edgar_8k(ticker)
        for f in filings:
            r = score_edgar_filing(f)
            if r:
                candidates.append(r)

    if not candidates:
        logger.info("score_ticker: no signal for %s", ticker)
        return None

    best = max(candidates, key=lambda c: abs(c["score"]))
    logger.info("score_ticker: %s → %s score=%.1f", ticker, best["catalyst_type"], best["score"])
    return {"symbol": ticker, **best}


def run_pipeline(tickers: list[str]) -> dict:
    logger.info("run_pipeline: scoring %d tickers: %s", len(tickers), tickers)
    now       = datetime.now(timezone.utc).isoformat()
    signals   = []
    no_signal = []

    for ticker in tickers:
        result = score_ticker(ticker)
        if result:
            signals.append(result)
        else:
            no_signal.append(ticker)

    output = {
        "date":         date.today().isoformat(),
        "generated_at": now,
        "signals":      signals,
        "no_signal":    no_signal,
        "sources_used": [
            s for s in [
                "Finnhub" if FINNHUB_API_KEY else None,
                "NewsAPI" if NEWS_API_KEY else None,
                "SEC EDGAR (fallback)",
            ] if s
        ],
    }

    logger.info("run_pipeline: complete — %d signals, %d no-signal", len(signals), len(no_signal))
    return output


# ──────────────────────────────────────────────────────────────
# FastAPI routes
# ──────────────────────────────────────────────────────────────

@router.post("/score")
async def score_endpoint(req: ScoreRequest):
    if req.secret != BRIDGE_SECRET:
        logger.warning("score_endpoint: unauthorized request (bad secret)")
        raise HTTPException(status_code=401, detail="unauthorized")

    tickers = [t.strip().upper() for t in req.tickers if t.strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="tickers required")

    return run_pipeline(tickers)


@router.get("/catalyst/health")
async def catalyst_health():
    """
    UPGRADED: Now makes lightweight live API calls to validate provider auth.
    Old behavior: only checked env var presence (bool(FINNHUB_API_KEY)).
    New behavior: actually calls providers with a minimal request.

    Returns status per provider: "ok", "no_key", or "auth_error: <message>"
    """
    result = {}

    # --- Finnhub ---
    if not FINNHUB_API_KEY:
        result["finnhub"] = "no_key"
    else:
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": "SPY", "token": FINNHUB_API_KEY},
                timeout=5,
            )
            data = resp.json()
            if resp.status_code == 200 and isinstance(data, dict) and "c" in data:
                result["finnhub"] = "ok"
            elif isinstance(data, dict) and "error" in data:
                result["finnhub"] = f"auth_error: {data['error']}"
                logger.warning("catalyst_health: Finnhub auth error: %s", data["error"])
            else:
                result["finnhub"] = f"unexpected_response: HTTP {resp.status_code}"
        except Exception as exc:
            result["finnhub"] = f"error: {exc}"
            logger.error("catalyst_health: Finnhub check failed: %s", exc)

    # --- NewsAPI ---
    if not NEWS_API_KEY:
        result["newsapi"] = "no_key"
    else:
        try:
            resp = requests.get(
                "https://newsapi.org/v2/top-headlines",
                params={"country": "us", "pageSize": 1, "apiKey": NEWS_API_KEY},
                timeout=5,
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("status") == "ok":
                result["newsapi"] = "ok"
            else:
                msg = data.get("message", f"HTTP {resp.status_code}")
                result["newsapi"] = f"auth_error: {msg}"
                logger.warning("catalyst_health: NewsAPI auth error: %s", msg)
        except Exception as exc:
            result["newsapi"] = f"error: {exc}"
            logger.error("catalyst_health: NewsAPI check failed: %s", exc)

    overall = "ok" if all(v == "ok" for v in result.values()) else "degraded"
    if any("no_key" in str(v) for v in result.values()):
        overall = "missing_keys"

    return {"status": overall, **result}
