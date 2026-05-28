"""
think-tank-relay — Claude + GPT-4o Architecture Think Tank
Asks both AIs the same question in parallel, synthesizes responses.
Deploy on Railway. Env vars: ANTHROPIC_API_KEY, OPENAI_API_KEY
"""

import os
import asyncio
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from catalyst import router as catalyst_router
from openclaw_v2 import router as openclaw_v2_router

app = FastAPI(title="Bonsignore Think Tank Relay", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(catalyst_router)
app.include_router(openclaw_v2_router)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")

SYSTEM_PROMPT = """You are an expert trading system architect for Christopher Bonsignore,
a neurological surgeon building an automated day trading system.
System: ZenScans + TradingView + Telegram alert pipeline. Bonsignore Trading Bot on Railway
(Python/FastAPI v9.2.7). Massive.com REST API for OHLCV data. 783 ticker universe, BBT scoring.
Account: $98,844 | Max risk/trade: $500 | Strategy: Day-2 institutional continuation.
Be specific, direct, and opinionated. Reference his actual system when relevant."""

class RelayRequest(BaseModel):
    question: str
    context: Optional[str] = ""

class RelayResponse(BaseModel):
    claude_response: str
    openai_response: str
    synthesis: str
    agreement_score: int

async def ask_claude(question: str, context: str) -> str:
    if not ANTHROPIC_KEY:
        return "ANTHROPIC_API_KEY not set"
    prompt = f"{context}\n\n{question}" if context else question
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                "content-type": "application/json"},
            json={"model": "claude-opus-4-20250514", "max_tokens": 1500,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": prompt}]})
        return r.json().get("content", [{}])[0].get("text", "No response")

async def ask_openai(question: str, context: str) -> str:
    if not OPENAI_KEY:
        return "OPENAI_API_KEY not set"
    prompt = f"{context}\n\n{question}" if context else question
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o", "max_tokens": 1500,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}]})
        return r.json().get("choices",[{}])[0].get("message",{}).get("content","No response")

async def synthesize(question: str, claude: str, openai: str) -> tuple:
    if not ANTHROPIC_KEY:
        return "Cannot synthesize", 5
    prompt = f"""Two AIs answered: {question}
CLAUDE: {claude}
GPT-4o: {openai}
Synthesize with: 1) AGREEMENT bullets 2) DISAGREEMENT bullets 3) RECOMMENDATION for Christopher's system 4) AGREEMENT SCORE: N/10"""
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}]})
        text = r.json().get("content",[{}])[0].get("text","")
        import re
        nums = re.findall(r"AGREEMENT SCORE.*?(\d+)", text)
        score = int(nums[0]) if nums else 5
        return text, min(10, max(0, score))

@app.get("/health")
def health():
    return {"status":"ok","claude_ready":bool(ANTHROPIC_KEY),"openai_ready":bool(OPENAI_KEY),"version":"1.0.0"}

@app.post("/relay", response_model=RelayResponse)
async def relay(req: RelayRequest):
    claude_t = asyncio.create_task(ask_claude(req.question, req.context or ""))
    openai_t = asyncio.create_task(ask_openai(req.question, req.context or ""))
    claude_r, openai_r = await asyncio.gather(claude_t, openai_t)
    synthesis, score = await synthesize(req.question, claude_r, openai_r)
    return RelayResponse(claude_response=claude_r, openai_response=openai_r,
        synthesis=synthesis, agreement_score=score)

@app.post("/quick")
async def quick(req: RelayRequest):
    full = await relay(req)
    return {"synthesis": full.synthesis, "agreement_score": full.agreement_score}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
