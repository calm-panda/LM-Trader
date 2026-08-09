"""AI signal confirmation gate — list / confirm / reject recommendations."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.auth_token import AuthToken
from app.models.portfolio import PortfolioSnapshot
from app.models.signal import Signal
from app.models.trade import Trade
from app.models.trade_journal import TradeJournal
from app.services.upstox.auth import _as_utc, _utc_now
from app.templating import templates, ui_context

logger = logging.getLogger(__name__)
router = APIRouter()


def _parse_triggered(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else [str(data)]
    except json.JSONDecodeError:
        return [raw]


def _signal_ctx(sig: Signal) -> dict:
    return {
        "id": sig.id,
        "symbol": sig.symbol,
        "timeframe": sig.timeframe,
        "direction": sig.direction,
        "entry": sig.entry,
        "stop_loss": sig.stop_loss,
        "target": sig.target,
        "risk_reward_ratio": sig.risk_reward_ratio,
        "confidence": sig.confidence,
        "indicators_triggered": _parse_triggered(sig.indicators_triggered),
        "reasoning": sig.reasoning,
        "invalidation": sig.invalidation,
        "status": sig.status,
        "generated_at": sig.generated_at,
        "expires_at": sig.expires_at,
        "model_used": sig.model_used,
    }


async def _get_valid_token(db: AsyncSession) -> AuthToken | None:
    result = await db.execute(
        select(AuthToken).order_by(AuthToken.updated_at.desc()).limit(1)
    )
    token = result.scalar_one_or_none()
    if not token:
        return None
    if _as_utc(token.expires_at) < _utc_now():
        return None
    return token


@router.get("/signals", response_class=HTMLResponse, name="signals")
async def signals_page(request: Request, db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    now = datetime.now(timezone.utc)

    pending_result = await db.execute(
        select(Signal)
        .where(Signal.status == "pending", Signal.expires_at > now)
        .order_by(Signal.generated_at.desc())
    )
    pending = [_signal_ctx(s) for s in pending_result.scalars()]

    history_result = await db.execute(
        select(Signal)
        .where(Signal.status.in_(["confirmed", "rejected", "expired"]))
        .order_by(Signal.generated_at.desc())
        .limit(30)
    )
    history = [_signal_ctx(s) for s in history_result.scalars()]

    return templates.TemplateResponse(
        request,
        "signals.html",
        ui_context(
            title="Signals",
            pending=pending,
            history=history,
            signal_validity_minutes=settings.signal_validity_minutes,
        ),
    )


@router.get("/signals/{signal_id}", response_class=HTMLResponse, name="signal_detail")
async def signal_detail(signal_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Signal).where(Signal.id == signal_id))
    sig = result.scalar_one_or_none()
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")

    trade = None
    if sig.status == "confirmed":
        t_result = await db.execute(
            select(Trade).where(Trade.signal_id == sig.id).order_by(Trade.opened_at.desc()).limit(1)
        )
        trade = t_result.scalar_one_or_none()

    journal = None
    j_result = await db.execute(
        select(TradeJournal)
        .where(TradeJournal.signal_id == sig.id)
        .order_by(TradeJournal.created_at.desc())
        .limit(1)
    )
    journal = j_result.scalar_one_or_none()

    snapshot = {}
    snapshot_json = "{}"
    if sig.indicators_snapshot:
        try:
            snapshot = json.loads(sig.indicators_snapshot)
            snapshot_json = json.dumps(snapshot, indent=2)
        except json.JSONDecodeError:
            snapshot_json = sig.indicators_snapshot

    return templates.TemplateResponse(
        request,
        "signal_detail.html",
        ui_context(
            title=f"Signal · {sig.symbol}",
            sig=_signal_ctx(sig),
            trade=trade,
            journal=journal,
            snapshot_json=snapshot_json,
        ),
    )


@router.post("/signals/{signal_id}/reject", response_class=HTMLResponse)
async def reject_signal(
    signal_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    reason: str = Query(default=""),
):
    result = await db.execute(select(Signal).where(Signal.id == signal_id))
    sig = result.scalar_one_or_none()
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")
    if sig.status != "pending":
        raise HTTPException(status_code=409, detail=f"Signal already {sig.status}")

    notes = f"user rejected: {reason}" if reason.strip() else "user rejected"
    sig.status = "rejected"
    from app.services.journal import record_journal
    await record_journal(action="rejected", signal=sig, notes=notes, db=db)
    await db.commit()
    await db.refresh(sig)

    return templates.TemplateResponse(
        request,
        "partials/signal_card.html",
        ui_context(
            sig=_signal_ctx(sig),
            done=True,
            message=f"Rejected — logged to journal{' (' + reason + ')' if reason else ''}.",
        ),
    )


@router.post("/signals/{signal_id}/confirm", response_class=HTMLResponse)
async def confirm_signal(
    signal_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    settings = get_settings()

    if not settings.live_trading_enabled:
        raise HTTPException(
            status_code=403,
            detail="Live trading is disabled. Set LIVE_TRADING_ENABLED=true and restart.",
        )

    result = await db.execute(select(Signal).where(Signal.id == signal_id))
    sig = result.scalar_one_or_none()
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")

    now = datetime.now(timezone.utc)
    expires = sig.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    if sig.status != "pending":
        raise HTTPException(status_code=409, detail=f"Signal already {sig.status}")
    if expires <= now:
        sig.status = "expired"
        from app.services.journal import record_journal
        await record_journal(
            action="expired",
            signal=sig,
            notes="expired at confirm attempt",
            db=db,
        )
        await db.commit()
        raise HTTPException(status_code=409, detail="Signal has expired")

    # Max open positions gate
    open_count_result = await db.execute(
        select(func.count()).select_from(Trade).where(
            Trade.mode == "live", Trade.status == "open"
        )
    )
    open_count = open_count_result.scalar_one() or 0
    if open_count >= settings.max_open_positions:
        from app.services.alerts import send_alert
        await send_alert(
            "Confirm blocked — max open positions",
            f"{sig.symbol}: {open_count} >= {settings.max_open_positions}",
            level="WARNING",
        )
        raise HTTPException(
            status_code=400,
            detail=f"Max open positions ({settings.max_open_positions}) reached",
        )

    token_row = await _get_valid_token(db)
    if not token_row:
        raise HTTPException(status_code=401, detail="No valid Upstox token — login at /auth/login")

    from app.services.upstox.instruments import get_instrument_key
    from app.services.upstox.orders import (
        estimate_live_total_value,
        get_funds,
        get_holdings,
        get_positions,
        place_market_order,
    )

    access_token = token_row.access_token

    # Daily loss gate vs morning live snapshot
    try:
        funds = await get_funds(access_token)
        positions = await get_positions(access_token)
        holdings = await get_holdings(access_token)
        current_total = estimate_live_total_value(funds, positions, holdings)

        snap_result = await db.execute(
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.mode == "live")
            .order_by(PortfolioSnapshot.snapshot_at.desc())
            .limit(1)
        )
        day_snap = snap_result.scalar_one_or_none()
        if day_snap and day_snap.total_value > 0:
            day_pnl_pct = (current_total - day_snap.total_value) / day_snap.total_value * 100
            if day_pnl_pct < -settings.daily_loss_limit_pct:
                from app.services.alerts import send_alert
                await send_alert(
                    "Confirm blocked — daily loss limit",
                    f"{sig.symbol}: day P&L {day_pnl_pct:.2f}% breached "
                    f"-{settings.daily_loss_limit_pct}%",
                    level="CRITICAL",
                )
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Daily loss limit hit ({day_pnl_pct:.2f}% < "
                        f"-{settings.daily_loss_limit_pct}%) — no new live trades"
                    ),
                )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("confirm: risk check failed")
        raise HTTPException(status_code=502, detail=f"Risk check failed: {exc}") from exc

    # Position sizing
    max_spend = funds * (settings.max_capital_per_trade_pct / 100)
    if sig.entry <= 0:
        raise HTTPException(status_code=400, detail="Invalid entry price on signal")
    qty = int(math.floor(max_spend / sig.entry))
    if qty < 1:
        raise HTTPException(
            status_code=400,
            detail=f"Qty=0 — insufficient funds (₹{funds:.0f}) for entry ₹{sig.entry:.2f}",
        )

    instrument_key = get_instrument_key(sig.symbol)
    if not instrument_key:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve instrument key for {sig.symbol}",
        )

    try:
        order_id = await place_market_order(
            access_token,
            instrument_key,
            side=sig.direction,
            quantity=qty,
            product="I",
        )
    except Exception as exc:
        logger.exception("confirm: order placement failed for signal %d", signal_id)
        raise HTTPException(status_code=502, detail=f"Order placement failed: {exc}") from exc

    trade = Trade(
        mode="live",
        signal_id=sig.id,
        upstox_order_id=order_id,
        symbol=sig.symbol,
        side=sig.direction,
        quantity=qty,
        entry_price=sig.entry,
        status="open",
        notes=f"AI signal conf={sig.confidence:.2f} SL={sig.stop_loss} TGT={sig.target}",
    )
    sig.status = "confirmed"
    db.add(trade)
    await db.flush()
    from app.services.journal import record_journal
    await record_journal(
        action="confirmed",
        signal=sig,
        order_id=order_id,
        trade_id=trade.id,
        executed_price=sig.entry,
        quantity=qty,
        notes="user confirmed live order",
        db=db,
    )
    await db.commit()
    await db.refresh(sig)

    return templates.TemplateResponse(
        request,
        "partials/signal_card.html",
        ui_context(
            sig=_signal_ctx(sig),
            done=True,
            message=f"Confirmed — live order {order_id} placed (qty={qty}).",
        ),
    )
