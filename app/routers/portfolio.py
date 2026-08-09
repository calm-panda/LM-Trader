"""Portfolio page — Overview / Positions / Journal / Risk tabs."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.auth_token import AuthToken
from app.models.trade_journal import TradeJournal
from app.services.journal import compute_analytics
from app.services.paper_engine import paper_engine
from app.services.tick_store import tick_store
from app.services.upstox.auth import _as_utc, _utc_now
from app.templating import templates, ui_context

logger = logging.getLogger(__name__)
router = APIRouter()


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
        "auto_squareoff": settings.live_auto_squareoff,
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

    analytics = await compute_analytics(db)

    journal_result = await db.execute(
        select(TradeJournal).order_by(TradeJournal.created_at.desc()).limit(50)
    )
    journal = [
        {
            "id": j.id,
            "action": j.action,
            "symbol": j.symbol,
            "direction": j.direction,
            "executed_price": j.executed_price,
            "quantity": j.quantity,
            "planned_rr": j.planned_rr,
            "achieved_rr": j.achieved_rr,
            "outcome_pnl": j.outcome_pnl,
            "order_id": j.order_id,
            "notes": j.notes,
            "created_at": j.created_at,
            "closed_at": j.closed_at,
        }
        for j in journal_result.scalars()
    ]

    tab = request.query_params.get("tab", "overview")
    if tab not in ("overview", "positions", "journal", "risk"):
        tab = "overview"

    return templates.TemplateResponse(
        request,
        "portfolio.html",
        ui_context(
            title="Portfolio",
            paper=paper_snap,
            live=live,
            analytics=analytics,
            journal=journal,
            active_tab=tab,
        ),
    )
