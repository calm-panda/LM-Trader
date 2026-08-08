"""Portfolio page — paper snapshot, live Upstox positions/holdings, trade journal."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.auth_token import AuthToken
from app.models.signal import Signal
from app.models.trade import Trade
from app.services.paper_engine import paper_engine
from app.services.tick_store import tick_store
from app.services.upstox.auth import _as_utc, _utc_now

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/portfolio", response_class=HTMLResponse, name="portfolio")
async def portfolio_page(request: Request, db: AsyncSession = Depends(get_db)):
    settings = get_settings()

    ltp_map = {t.symbol: t.ltp for t in tick_store.all_ticks() if t.ltp > 0}
    paper_snap = paper_engine.snapshot(ltp_map)

    live = {
        "enabled": settings.live_trading_enabled,
        "funds": None,
        "positions": [],
        "holdings": [],
        "error": None,
    }

    if settings.live_trading_enabled:
        token_result = await db.execute(
            select(AuthToken).order_by(AuthToken.updated_at.desc()).limit(1)
        )
        token_row = token_result.scalar_one_or_none()
        if not token_row or _as_utc(token_row.expires_at) < _utc_now():
            live["error"] = "No valid Upstox token — login at /auth/login"
        else:
            try:
                from app.services.upstox.orders import get_funds, get_holdings, get_positions

                live["funds"] = await get_funds(token_row.access_token)
                live["positions"] = await get_positions(token_row.access_token)
                live["holdings"] = await get_holdings(token_row.access_token)
            except Exception as exc:
                logger.exception("portfolio: live fetch failed")
                live["error"] = str(exc)

    # Trade journal with optional signal reasoning
    trades_result = await db.execute(
        select(Trade).order_by(Trade.opened_at.desc()).limit(50)
    )
    trades = list(trades_result.scalars())
    signal_ids = [t.signal_id for t in trades if t.signal_id]
    signal_map: dict[int, Signal] = {}
    if signal_ids:
        sig_result = await db.execute(select(Signal).where(Signal.id.in_(signal_ids)))
        signal_map = {s.id: s for s in sig_result.scalars()}

    journal = []
    for t in trades:
        sig = signal_map.get(t.signal_id) if t.signal_id else None
        journal.append({
            "id": t.id,
            "mode": t.mode,
            "symbol": t.symbol,
            "side": t.side,
            "quantity": t.quantity,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "pnl": t.pnl,
            "status": t.status,
            "upstox_order_id": t.upstox_order_id,
            "opened_at": t.opened_at,
            "closed_at": t.closed_at,
            "notes": t.notes,
            "signal_confidence": sig.confidence if sig else None,
            "signal_reasoning": sig.reasoning if sig else None,
        })

    return templates.TemplateResponse(
        request,
        "portfolio.html",
        {
            "title": "Portfolio",
            "paper": paper_snap,
            "live": live,
            "journal": journal,
        },
    )
