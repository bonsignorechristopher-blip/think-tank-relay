"""
  catalyst.py — OpenClaw Catalyst Scoring Router
FastAPI router mounted to think-tank-relay.
  Adds POST /score and GET /catalyst/health endpoints.

  Sources: Finnhub (primary) -> NewsAPI (secondary) -> SEC EDGAR (fallback)
Output: catalyst_score.json schema per OPENCLAW_CATALYST_DESIGN.md
"""

  import os
import requests
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone, timedelta
  from fastapi import APIRouter, HTTPException
  from pydantic import BaseModel

router = APIRouter()

  FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
  NEWS_API_KEY    = os.environ.get("NEWS_API_KEY", "")
  BRIDGE_SECRET   = os.environ.get("BRIDGE_SECRET", "")

  DAMPENING_FINNHUB = 0.85
  DAMPENING_NEWS    = 0.75
  DAMPENING_EDGAR   = 0.85

  SEC_HEADERS = {"User-Agent": "BonsignoreTradingBot research@bonsignore.trading"}

FINNHUB_POS = [
      ("partnership", 6), ("agreement", 5), ("contract", 5), ("acquisition", 7),
      ("fda approved", 9), ("fda cleared", 8), ("guidance raised", 7),
      ("earnings beat", 7), ("record revenue", 6), ("buyback", 4),
      ("merger", 6), ("upgrade", 4), ("outperform", 4), ("beat estimates", 6),
  ]
  FINNHUB_NEG = [
      ("restatement", -8), ("investigation", -8), ("sec subpoena", -10),
      ("class action", -7), ("going concern", -9), ("bankruptcy", -10),
      ("guidance cut", -6), ("missed estimates", -5), ("downgrade", -5),
  ]
  NEWS_POS = [
      ("partnership", 5), ("acquisition", 6), ("earnings beat", 6),
      ("raised guidance", 5), ("record", 4), ("upgrade", 4), ("deal", 4),
  ]
  NEWS_NEG = [("downgrade", -4), ("miss", -3), ("investigation", -6), ("bankruptcy", -9)]
  SEC_POS = [("partnership", 5), ("agreement", 4), ("acquisition", 6)]
  SEC_NEG = [("restatement", -8), ("investigation", -8), ("class action", -7)]


  def _fetch_finnhub(ticker):
      if not FINNHUB_API_KEY:
          return []
      try:
          r = requests.get("https://finnhub.io/api/v1/company-news",
              params={"symbol": ticker, "from": (date.today()-timedelta(days=2)).isoformat(),
                      "to": date.today().isoformat(), "token": FINNHUB_API_KEY}, timeout=10)
          return r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
      except Exception:
          return []


  def _score_finnhub(articles):
      if not articles:
          return None
      total, headline, cat = 0, "", "finnhub_news"
      for a in articles:
          text = (a.get("headline","") + " " + a.get("summary","")).lower()
          if not headline:
              headline = a.get("headline","")[:120]
          for kw, pts in FINNHUB_POS:
              if kw in text:
                  total += pts; cat = f"finnhub_{kw.replace(' ','_')}"; break
          for kw, pts in FINNHUB_NEG:
              if kw in text:
                  total += pts; break
      if total == 0:
          return None
      return {"catalyst_type": cat, "headline": headline, "source": "Finnhub",
              "raw_score": total, "score": round(total*DAMPENING_FINNHUB,1),
              "confidence": "high" if abs(total)>=7 else "medium",
              "url": articles[0].get("url",""),
              "timestamp": datetime.now(timezone.utc).isoformat()}


  def _fetch_newsapi(ticker):
      if not NEWS_API_KEY:
          return []
      try:
          r = requests.get("https://newsapi.org/v2/everything",
              params={"q": f"{ticker} stock", "language":"en", "sortBy":"publishedAt",
                      "pageSize":10, "from":(date.today()-timedelta(days=2)).isoformat(),
                      "apiKey": NEWS_API_KEY}, timeout=10)
          return r.json().get("articles",[]) if r.status_code == 200 else []
      except Exception:
          return []


  def _score_newsapi(ticker, articles):
      if not articles:
          return None
      total, headline, cat = 0, "", "newsapi_headline"
      for a in articles:
          text = a.get("title","").lower()
          if not headline:
              headline = a.get("title","")[:120]
          for kw, pts in NEWS_POS:
              if kw in text:
                  total += pts; cat = f"news_{kw.replace(' ','_')}"; break
          for kw, pts in NEWS_NEG:
              if kw in text:
                  total += pts; break
      if total == 0:
          return None
      return {"catalyst_type": cat, "headline": headline, "source": "NewsAPI",
              "raw_score": total, "score": round(total*DAMPENING_NEWS,1),
              "confidence": "medium", "url": articles[0].get("url","") if articles else "",
              "timestamp": datetime.now(timezone.utc).isoformat()}


  def _fetch_edgar(ticker):
      url = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ticker}"
             f"&type=8-K&dateb=&owner=include&count=5&search_text=&output=atom")
      try:
          r = requests.get(url, headers=SEC_HEADERS, timeout=10)
          if r.status_code != 200:
              return []
          root = ET.fromstring(r.content)
          ns = {"atom": "http://www.w3.org/2005/Atom"}
          today, out = date.today().isoformat(), []
          for entry in root.findall("atom:entry", ns):
              if entry.findtext("atom:updated","",ns)[:10] < today:
                  continue
              link = entry.find("atom:link", ns)
              out.append({"title": entry.findtext("atom:title","",ns),
                          "summary": entry.findtext("atom:summary","",ns),
                          "url": link.get("href","") if link is not None else ""})
          return out
      except Exception:
          return []


  def _score_edgar(filings):
      if not filings:
          return None
      f = filings[0]
      text = (f.get("title","") + " " + f.get("summary","")).lower()
      score, cat = 3, "sec_8k"
      for kw, pts in SEC_POS:
          if kw in text:
              score += pts; cat = f"sec_8k_{kw.replace(' ','_')}"; break
      for kw, pts in SEC_NEG:
          if kw in text:
              score += pts; break
      return {"catalyst_type": cat, "headline": f.get("title","SEC 8-K today"),
              "source": "SEC EDGAR", "raw_score": score,
              "score": round(score*DAMPENING_EDGAR,1), "confidence": "high",
              "url": f.get("url",""), "timestamp": datetime.now(timezone.utc).isoformat()}


  def score_ticker(ticker):
      candidates = []
      r = _score_finnhub(_fetch_finnhub(ticker))
      if r: candidates.append(r)
      r = _score_newsapi(ticker, _fetch_newsapi(ticker))
      if r: candidates.append(r)
      if not candidates:
          r = _score_edgar(_fetch_edgar(ticker))
          if r: candidates.append(r)
      if not candidates:
          return None
      return {"symbol": ticker, **max(candidates, key=lambda c: abs(c["score"]))}


  class ScoreRequest(BaseModel):
      tickers: list[str]
      secret: str = ""


  @router.get("/catalyst/health")
  def catalyst_health():
      return {"status": "ok", "finnhub": bool(FINNHUB_API_KEY), "newsapi": bool(NEWS_API_KEY)}


  @router.post("/score")
  def score_endpoint(req: ScoreRequest):
      if BRIDGE_SECRET and req.secret != BRIDGE_SECRET:
          raise HTTPException(status_code=401, detail="unauthorized")
      tickers = [t.strip().upper() for t in req.tickers if t.strip()]
      if not tickers:
              raise HTTPException(status_code=400, detail="tickers required")
    signals, no_signal = [], []
            for ticker in tickers:
              result = score_ticker(ticker)
                        if result: signals.append(result)
                else: no_signal.append(ticker)
            return {"date": date.today().isoformat(),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "signals": signals, "no_signal": no_signal}
