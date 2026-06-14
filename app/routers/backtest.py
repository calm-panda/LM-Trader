"""Backtest router — run backtests, approve strategies."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select

from app.database import async_session
from app.models.strategy import Strategy
from app.services.backtest_runner import SHARPE_MIN, MAX_DRAWDOWN_PCT, run_backtest
from app.services.strategies import REGISTRY

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ── Page ───────────────────────────────────────────────────────────────────────

@router.get("/backtest", response_class=HTMLResponse, name="backtest")
async def backtest_page(request: Request):
    async with async_session() as db:
        result = await db.execute(select(Strategy).order_by(Strategy.id))
        strategies = list(result.scalars())

    from app.services.chart import get_available_symbols
    async with async_session() as db2:
        symbols = await get_available_symbols(db2)

    return templates.TemplateResponse(
        request,
        "backtest.html",
        {
            "title":        "Backtest",
            "strategies":   strategies,
            "symbols":      symbols,
            "registry":     list(REGISTRY.keys()),
            "sharpe_min":   SHARPE_MIN,
            "max_dd":       MAX_DRAWDOWN_PCT,
        },
    )


# ── Run endpoint ───────────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    strategy_class:  str
    symbol:          str
    timeframe:       str = "1d"
    from_date:       str | None = None  # ISO date string e.g. "2024-01-01"
    to_date:         str | None = None
    params:          dict | None = None
    cash:            float = 100_000.0
    commission:      float = 0.001
    strategy_id:     int | None = None  # if provided, link result to DB row


@router.post("/backtest/run")
async def run_backtest_endpoint(req: BacktestRequest):
    now = datetime.now(timezone.utc)

    from_dt = (
        datetime.fromisoformat(req.from_date).replace(tzinfo=timezone.utc)
        if req.from_date
        else now - timedelta(days=90)
    )
    to_dt = (
        datetime.fromisoformat(req.to_date).replace(tzinfo=timezone.utc)
        if req.to_date
        else now
    )

    result = await run_backtest(
        strategy_class_name=req.strategy_class,
        symbol=req.symbol,
        timeframe=req.timeframe,
        from_date=from_dt,
        to_date=to_dt,
        params=req.params,
        cash=req.cash,
        commission=req.commission,
    )

    if "error" not in result and req.strategy_id:
        result["strategy_id"] = req.strategy_id

    return JSONResponse(result)


# ── Approve (set backtest_passed + optionally enable) ─────────────────────────

class ApproveRequest(BaseModel):
    sharpe:          float
    max_drawdown_pct: float
    auto_enable:     bool = False


@router.post("/backtest/{strategy_id}/approve")
async def approve_strategy(strategy_id: int, body: ApproveRequest):
    """Mark a strategy as backtest-passed (and optionally enable it)."""
    if body.sharpe < SHARPE_MIN:
        raise HTTPException(
            status_code=422,
            detail=f"Sharpe {body.sharpe:.2f} < minimum {SHARPE_MIN}",
        )
    if abs(body.max_drawdown_pct) > MAX_DRAWDOWN_PCT:
        raise HTTPException(
            status_code=422,
            detail=f"Drawdown {abs(body.max_drawdown_pct):.1f}% > limit {MAX_DRAWDOWN_PCT}%",
        )

    async with async_session() as db:
        result = await db.execute(
            select(Strategy).where(Strategy.id == strategy_id)
        )
        strat = result.scalar_one_or_none()
        if strat is None:
            raise HTTPException(status_code=404, detail="Strategy not found")
        strat.backtest_passed = True
        if body.auto_enable:
            strat.enabled = True
        await db.commit()
        await db.refresh(strat)

    return {
        "id":               strat.id,
        "name":             strat.name,
        "backtest_passed":  strat.backtest_passed,
        "enabled":          strat.enabled,
    }
