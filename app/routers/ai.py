"""AI router — free-form chat, chart analysis, strategy generation, news sentiment."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ── Page ───────────────────────────────────────────────────────────────────────

@router.get("/ai/chat", response_class=HTMLResponse, name="ai_chat")
async def ai_chat_page(request: Request):
    from app.database import async_session
    from app.services.chart import get_available_symbols
    async with async_session() as db:
        symbols = await get_available_symbols(db)
    return templates.TemplateResponse(
        request,
        "ai_chat.html",
        {"title": "AI Assistant", "symbols": symbols},
    )


# ── Free-form chat ─────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    messages: list[dict[str, str]]  # [{role: "user", content: "..."}, ...]


@router.post("/ai/chat")
async def ai_chat(req: ChatRequest):
    from app.services.deepseek import chat
    try:
        reply = await chat(req.messages)
        return {"reply": reply}
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("DeepSeek chat error")
        raise HTTPException(status_code=502, detail=f"AI service error: {e}")


# ── Chart analysis ─────────────────────────────────────────────────────────────

class ChartAnalysisRequest(BaseModel):
    symbol: str
    timeframe: str = "5m"


@router.post("/ai/chart-analysis")
async def chart_analysis(req: ChartAnalysisRequest):
    from app.services.deepseek import analyze_chart
    try:
        analysis = await analyze_chart(req.symbol, req.timeframe)
        return {"symbol": req.symbol, "timeframe": req.timeframe, "analysis": analysis}
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("Chart analysis error")
        raise HTTPException(status_code=502, detail=f"AI service error: {e}")


# ── Strategy code generation ───────────────────────────────────────────────────

class StrategyGenRequest(BaseModel):
    description: str


@router.post("/ai/generate-strategy")
async def generate_strategy(req: StrategyGenRequest):
    if not req.description.strip():
        raise HTTPException(status_code=422, detail="Description cannot be empty")
    from app.services.deepseek import generate_strategy as gen
    try:
        code = await gen(req.description)
        return {"description": req.description, "code": code}
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("Strategy generation error")
        raise HTTPException(status_code=502, detail=f"AI service error: {e}")


# ── News sentiment ─────────────────────────────────────────────────────────────

@router.get("/ai/sentiment/{symbol}")
async def news_sentiment(symbol: str):
    from app.services.deepseek import analyze_sentiment
    try:
        result = await analyze_sentiment(symbol.upper())
        return result
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.exception("Sentiment analysis error for %s", symbol)
        raise HTTPException(status_code=502, detail=f"AI service error: {e}")
