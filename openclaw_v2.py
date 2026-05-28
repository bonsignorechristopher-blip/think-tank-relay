"""
openclaw_v2.py
==============
OpenClaw Pipeline v2 — FastAPI router for think-tank-relay (Path B integration).

Mounts as /score/v2 alongside the existing /score endpoint.
Uses the v2.3 audit-hardened scoring logic from openclaw_pipeline_v2.py:
  - Finnhub (primary) with 200-error-body detection
  - NewsAPI (secondary) with error-status detection
  - SEC EDGAR (fallback)
  - Full logging throughout

WIRING (add to main.py):
  from openclaw_v2 import router as openclaw_v2_router
  app.include_router(openclaw_v2_router)

ENDPOINTS:
  POST /score/v2  — same auth + body as /score, uses v2 scoring logic
  GET  /v2/health — provider auth test (same as /catalyst/health but versioned)

Path B rationale: runs in parallel with /score so A/B comparison is possible.
Clean up /score (v1) once /score/v2 is validated over 1–2 weeks.
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

router = APIRouter(prefix="/score", tags=["openclaw-v2"])

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
NEWS_API_KEY    = os.environ.get("NEWS_API_KEY", "")
BRIDGE_SECRET   = os.environ.get("BRIDGE_SECRET", "")

# v2 dampening factors
DAMPENING_FINNHUB = 0.85
DAMPENING_NEWS    = 0.75
DAMPENING_EDGAR   = 0.85

SEC_HEADERS = {"User-Agent": "BonsignoreTradingBot research@bonsignore.trading"}


# ──────────────────────────────────────────────────────────────
# Request model
# ──────────────────────────────────────────────────────────────

class ScoreRequest(BaseModel):
    tickers: list[str]
    secret: str


# ──────────────────────────────────────────────────────────────
# Finnhub
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


def _fetch_finnhub(ticker: str, days_back: int = 2) -> list[dict]:
    if not FINNHUB_API_KEY:
        logger.warning("_fetch_finnhub: FINNHUB_API_KEY not set")
        return []
    today     = date.today()
    from_date = (today - timedelta(days=days_back)).isoformat()
    try:
        resp = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": ticker.upper(), "from": from_date,
                    "to": today.isoformat(), "token": FINNHUB_API_KEY},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("_fetch_finnhub: HTTP %d for %s", resp.status_code, ticker)
            return []
        data = resp.json()
        if not isinstance(data, list):
            logger.warning("_fetch_finnhub: non-list response for %s: %s",
                           ticker, str(data)[:200])
            return []
        return [{"headline": a.get("headline",""), "summary": a.get("summary",""),
                 "url": a.get("url",""), "source": a.get("source","Finnhub")}
                for a in data]
    except Exception as exc:
        logger.error("_fetch_finnhub: error for %s: %s", ticker, exc, exc_info=True)
        return []


def _score_finnhub(ticker: str, articles: list[dict]) -> dict | None:
    if not articles:
        return None
    total, count, headline, catalyst = 0, 0, "", "finnhub_news"
    for a in articles:
        text = (a.get("headline","") + " " + a.get("summary","")).lower()
        count += 1
        if not headline:
            headline = a.get("headline","")[:120]
        for kw, pts in FINNHUB_POSITIVE:
            if kw in text:
                total += pts; catalyst = f"finnhub_{kw.replace(' ','_')}"; break
        for kw, pts in FINNHUB_NEGATIVE:
            if kw in text:
                total += pts; catalyst = f"finnhub_neg_{kw.replace(' ','_')}"; break
    if count > 1:
        total += min(2 * (count - 1), 4)
    if total == 0:
        return None
    return {"catalyst_type": catalyst, "headline": headline, "source": "Finnhub",
            "raw_score": total, "score": round(total * DAMPENING_FINNHUB, 1),
            "dampening": DAMPENING_FINNHUB,
            "confidence": "high" if abs(total) >= 7 else "medium",
            "url": articles[0].get("url",""),
            "timestamp": datetime.now(timezone.utc).isoformat()}


# ──────────────────────────────────────────────────────────────
# NewsAPI
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


def _fetch_newsapi(ticker: str) -> list[dict]:
    if not NEWS_API_KEY:
        logger.warning("_fetch_newsapi: NEWS_API_KEY not set")
        return []
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={"q": f"{ticker} stock", "language": "en", "sortBy": "publishedAt",
                    "pageSize": 10, "from": (date.today() - timedelta(days=2)).isoformat(),
                    "apiKey": NEWS_API_KEY},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("_fetch_newsapi: HTTP %d for %s", resp.status_code, ticker)
            return []
        data = resp.json()
        if data.get("status") != "ok":
            logger.warning("_fetch_newsapi: status=%s for %s — %s",
                           data.get("status"), ticker, data.get("message","")[:200])
            return []
        return [{"headline": a.get("title",""), "url": a.get("url",""),
                 "source": a.get("source",{}).get("name","NewsAPI")}
                for a in data.get("articles", [])]
    except Exception as exc:
        logger.error("_fetch_newsapi: error for %s: %s", ticker, exc, exc_info=True)
        return []


def _score_newsapi(ticker: str, articles: list[dict]) -> dict | None:
    if not articles:
        return None
    total, headline, catalyst = 0, "", "newsapi_headline"
    for a in articles:
        text = a.get("headline","").lower()
        if not headline:
            headline = a.get("headline","")[:120]
        for kw, pts in NEWS_POSITIVE:
            if kw in text:
                total += pts; catalyst = f"news_{kw.replace(' ','_')}"; break
        for kw, pts in NEWS_NEGATIVE:
            if kw in text:
                total += pts; break
    if total == 0:
        return None
    return {"catalyst_type": catalyst, "headline": headline, "source": "NewsAPI",
            "raw_score": total, "score": round(total * DAMPENING_NEWS, 1),
            "dampening": DAMPENING_NEWS, "confidence": "medium",
            "url": articles[0].get("url","") if articles else "",
            "timestamp": datetime.now(timezone.utc).isoformat()}


# ──────────────────────────────────────────────────────────────
# SEC EDGAR
# ──────────────────────────────────────────────────────────────

SEC_RSS = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany&CIK={ticker}&type=8-K"
    "&dateb=&owner=include&count=5&search_text=&output=atom"
)

SEC_POSITIVE = [("partnership",5),("agreement",4),("acquisition",6),
                ("guidance raised",6),("earnings beat",6)]
SEC_NEGATIVE = [("restatement",-8),("investigation",-8),("sec inquiry",-10),
                ("class action",-7),("going concern",-9)]


def _fetch_edgar(ticker: str) -> list[dict]:
    try:
        resp = requests.get(SEC_RSS.format(ticker=ticker.upper()),
                            headers=SEC_HEADERS, timeout=10)
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        today = date.today().isoformat()
        entries = []
        for entry in root.findall("atom:entry", ns):
            updated = entry.findtext("atom:updated","",ns)[:10]
            if updated < today:
                continue
            link = entry.find("atom:link", ns)
            entries.append({
                "title":   entry.findtext("atom:title","",ns),
                "summary": entry.findtext("atom:summary","",ns),
                "updated": updated,
                "url":     link.get("href","") if link is not None else "",
            })
        return entries
    except Exception as exc:
        logger.error("_fetch_edgar: error for %s: %s", ticker, exc, exc_info=True)
        return []


def _score_edgar(filing: dict) -> dict | None:
    text  = (filing.get("title","") + " " + filing.get("summary","")).lower()
    score, cat = 3, "sec_8k"
    for kw, pts in SEC_POSITIVE:
        if kw in text:
            score += pts; cat = f"sec_8k_{kw.replace(' ','_')}"; break
    for kw, pts in SEC_NEGATIVE:
        if kw in text:
            score += pts; cat = "sec_8k_negative"; break
    final = round(score * DAMPENING_EDGAR, 1)
    return {"catalyst_type": cat, "headline": filing.get("title","SEC 8-K today"),
            "source": "SEC EDGAR", "raw_score": score, "score": final,
            "dampening": DAMPENING_EDGAR,
            "confidence": "high" if abs(score) >= 6 else "medium",
            "url": filing.get("url",""),
            "timestamp": filing.get("updated", datetime.now(timezone.utc).isoformat())}


# ──────────────────────────────────────────────────────────────
# Pipeline
# ──────────────────────────────────────────────────────────────

def _score_ticker_v2(ticker: str) -> dict | None:
    logger.info("v2 scoring: %s", ticker)
    candidates = []

    result = _score_finnhub(ticker, _fetch_finnhub(ticker))
    if result:
        candidates.append(result)

    result = _score_newsapi(ticker, _fetch_newsapi(ticker))
    if result:
        candidates.append(result)

    if not candidates:
        for f in _fetch_edgar(ticker):
            r = _score_edgar(f)
            if r:
                candidates.append(r)

    if not candidates:
        logger.info("v2 no signal: %s", ticker)
        return None

    best = max(candidates, key=lambda c: abs(c["score"]))
    logger.info("v2 signal: %s → %s score=%.1f", ticker, best["catalyst_type"], best["score"])
    return {"symbol": ticker, "pipeline": "v2", **best}


def _run_pipeline_v2(tickers: list[str]) -> dict:
    logger.info("v2 pipeline: %d tickers %s", len(tickers), tickers)
    now = datetime.now(timezone.utc).isoformat()
    signals, no_signal = [], []
    for t in tickers:
        r = _score_ticker_v2(t)
        (signals if r else no_signal).append(r if r else t)
    return {
        "date": date.today().isoformat(),
        "generated_at": now,
        "pipeline": "v2",
        "signals": signals,
        "no_signal": no_signal,
        "sources_used": [s for s in [
            "Finnhub" if FINNHUB_API_KEY else None,
            "NewsAPI" if NEWS_API_KEY else None,
            "SEC EDGAR (fallback)",
        ] if s],
    }


# ──────────────────────────────────────────────────────────────
# FastAPI routes  (mounted at /score/v2 and /v2/health)
# ──────────────────────────────────────────────────────────────

@router.post("/v2")
async def score_v2(req: ScoreRequest):
    """Path B parallel endpoint — same auth, v2.3 scoring logic."""
    if req.secret != BRIDGE_SECRET:
        logger.warning("score/v2: unauthorized")
        raise HTTPException(status_code=401, detail="unauthorized")
    tickers = [t.strip().upper() for t in req.tickers if t.strip()]
    if not tickers:
        raise HTTPException(status_code=400, detail="tickers required")
    return _run_pipeline_v2(tickers)


@router.get("/v2/health")
async def v2_health():
    """Provider auth test for v2 pipeline."""
    result: dict = {}

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
            else:
                result["finnhub"] = f"error: {data.get('error', resp.status_code)}"
        except Exception as exc:
            result["finnhub"] = f"exception: {exc}"

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
            result["newsapi"] = "ok" if data.get("status") == "ok" else f"error: {data.get('message','')}"
        except Exception as exc:
            result["newsapi"] = f"exception: {exc}"

    overall = "ok" if all(v == "ok" for v in result.values()) else "degraded"
    return {"status": overall, "pipeline": "v2", **result}
