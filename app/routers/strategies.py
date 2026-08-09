"""Strategies router — list, seed, toggle enable/disable, backtest-approve."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.database import async_session
from app.models.paper_order import PaperOrder
from app.models.strategy import Strategy
from app.services.paper_engine import paper_engine
from app.services.strategies import DEFAULT_STRATEGIES, REGISTRY
from app.services.tick_store import tick_store
from app.templating import templates, ui_context

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Helper ─────────────────────────────────────────────────────────────────────

def _strat_to_dict(s: Strategy) -> dict[str, Any]:
    try:
        params = json.loads(s.params or "{}")
    except json.JSONDecodeError:
        params = {}
    return {
        "id": s.id,
        "name": s.name,
        "class_name": s.class_name,
        "symbols": s.symbols,
        "timeframe": s.timeframe,
        "params": params,
        "params_json": s.params,
        "enabled": s.enabled,
        "backtest_passed": s.backtest_passed,
    }


# ── Pages ──────────────────────────────────────────────────────────────────────

@router.get("/strategies", response_class=HTMLResponse, name="strategies")
async def strategies_page(request: Request):
    async with async_session() as db:
        result = await db.execute(select(Strategy).order_by(Strategy.id))
        strats = [_strat_to_dict(s) for s in result.scalars()]

    # Paper portfolio snapshot
    ltp_map = {
        tick.symbol: tick.ltp
        for tick in tick_store.all_ticks()
        if tick.ltp > 0
    }
    snap = paper_engine.snapshot(ltp_map)

    return templates.TemplateResponse(
        request,
        "strategies.html",
        ui_context(
            title="Strategies",
            strategies=strats,
            snap=snap,
            registry_names=list(REGISTRY.keys()),
        ),
    )


# ── Seed default strategies ────────────────────────────────────────────────────

@router.post("/strategies/seed")
async def seed_strategies():
    """Insert the 3 default strategies if they don't exist yet."""
    inserted = []
    async with async_session() as db:
        for row in DEFAULT_STRATEGIES:
            existing = await db.execute(
                select(Strategy).where(Strategy.name == row["name"])
            )
            if existing.scalar_one_or_none() is None:
                strat = Strategy(
                    name=row["name"],
                    class_name=row["class_name"],
                    symbols=row["symbols"],
                    timeframe=row["timeframe"],
                    params=row["params"],
                    enabled=False,
                )
                db.add(strat)
                inserted.append(row["name"])
        await db.commit()
    return {"inserted": inserted}


# ── Toggle enable/disable (HTMX-friendly) ─────────────────────────────────────

@router.post("/strategies/{strategy_id}/toggle", response_class=HTMLResponse)
async def toggle_strategy(strategy_id: int, request: Request):
    async with async_session() as db:
        result = await db.execute(
            select(Strategy).where(Strategy.id == strategy_id)
        )
        strat = result.scalar_one_or_none()
        if strat is None:
            raise HTTPException(status_code=404, detail="Strategy not found")
        strat.enabled = not strat.enabled
        await db.commit()
        await db.refresh(strat)
        d = _strat_to_dict(strat)

    return templates.TemplateResponse(
        request,
        "partials/strategy_row.html",
        {"strategy": d},
    )


# ── Recent signals (trades) for a strategy ────────────────────────────────────

@router.get("/strategies/{strategy_id}/signals")
async def strategy_signals(strategy_id: int, limit: int = 20):
    async with async_session() as db:
        result = await db.execute(
            select(PaperOrder)
            .where(PaperOrder.strategy_id == strategy_id)
            .order_by(PaperOrder.filled_at.desc())
            .limit(limit)
        )
        orders = result.scalars().all()

    return [
        {
            "id": o.id,
            "symbol": o.symbol,
            "side": o.side,
            "qty": o.quantity,
            "fill_price": o.fill_price,
            "pnl_note": o.notes,
            "filled_at": o.filled_at.isoformat() if o.filled_at else None,
        }
        for o in orders
    ]


# ── Paper portfolio snapshot (JSON) ───────────────────────────────────────────

@router.get("/strategies/paper/snapshot")
async def paper_snapshot():
    ltp_map = {
        tick.symbol: tick.ltp
        for tick in tick_store.all_ticks()
        if tick.ltp > 0
    }
    return paper_engine.snapshot(ltp_map)


# ── Recent trades (all strategies) ────────────────────────────────────────────

@router.get("/strategies/paper/trades")
async def recent_trades(limit: int = 50):
    async with async_session() as db:
        result = await db.execute(
            select(PaperOrder)
            .order_by(PaperOrder.filled_at.desc())
            .limit(limit)
        )
        orders = result.scalars().all()

    return [
        {
            "id": o.id,
            "symbol": o.symbol,
            "side": o.side,
            "qty": o.quantity,
            "fill_price": round(o.fill_price or 0, 2),
            "notes": o.notes,
            "filled_at": o.filled_at.isoformat() if o.filled_at else None,
        }
        for o in orders
    ]


# ── Backtest approval gate ─────────────────────────────────────────────────────

class BacktestApproveBody(BaseModel):
    auto_enable: bool = False


@router.post("/strategies/{strategy_id}/approve-backtest")
async def approve_backtest(strategy_id: int, body: BacktestApproveBody):
    """Mark strategy as backtest_passed=True (and optionally enable it)."""
    async with async_session() as db:
        result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
        strat = result.scalar_one_or_none()
        if strat is None:
            raise HTTPException(status_code=404, detail="Strategy not found")
        strat.backtest_passed = True
        if body.auto_enable:
            strat.enabled = True
        await db.commit()
        await db.refresh(strat)
    return {
        "id": strat.id,
        "name": strat.name,
        "backtest_passed": strat.backtest_passed,
        "enabled": strat.enabled,
    }
