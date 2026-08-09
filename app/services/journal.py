"""Trade journal helpers + portfolio analytics (win rate, planned vs achieved R:R)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.signal import Signal
from app.models.trade import Trade
from app.models.trade_journal import TradeJournal

logger = logging.getLogger(__name__)


async def record_journal(
    *,
    action: str,
    signal: Signal,
    order_id: str | None = None,
    trade_id: int | None = None,
    executed_price: float | None = None,
    quantity: int | None = None,
    notes: str | None = None,
    db: AsyncSession | None = None,
) -> TradeJournal:
    """Insert a trade_journal row for confirmed / rejected / expired."""
    row = TradeJournal(
        signal_id=signal.id,
        trade_id=trade_id,
        order_id=order_id,
        action=action,
        symbol=signal.symbol,
        direction=signal.direction,
        executed_price=executed_price,
        quantity=quantity,
        planned_rr=signal.risk_reward_ratio,
        notes=notes,
    )

    if db is not None:
        db.add(row)
        await db.flush()
        return row

    async with async_session() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def record_expired_signals(signal_ids: list[int]) -> int:
    """Write journal rows for newly expired signals (skip if already journaled)."""
    if not signal_ids:
        return 0

    async with async_session() as db:
        existing = await db.execute(
            select(TradeJournal.signal_id).where(
                TradeJournal.signal_id.in_(signal_ids),
                TradeJournal.action == "expired",
            )
        )
        already = {sid for sid in existing.scalars().all() if sid is not None}
        to_write = [sid for sid in signal_ids if sid not in already]
        if not to_write:
            return 0

        result = await db.execute(select(Signal).where(Signal.id.in_(to_write)))
        count = 0
        for sig in result.scalars():
            db.add(
                TradeJournal(
                    signal_id=sig.id,
                    action="expired",
                    symbol=sig.symbol,
                    direction=sig.direction,
                    planned_rr=sig.risk_reward_ratio,
                    notes="validity window elapsed without confirm",
                )
            )
            count += 1
        await db.commit()
        return count


def _achieved_rr(trade: Trade, signal: Signal | None, exit_price: float) -> float | None:
    if not signal or not signal.entry or not signal.stop_loss:
        return None
    risk = abs(signal.entry - signal.stop_loss)
    if risk <= 0:
        return None
    if trade.side == "BUY":
        return round((exit_price - trade.entry_price) / risk, 3)
    return round((trade.entry_price - exit_price) / risk, 3)


async def close_journal_for_trade(
    db: AsyncSession,
    trade: Trade,
    *,
    exit_price: float,
    pnl: float,
) -> None:
    """Fill outcome_pnl / achieved_rr on the confirmed journal row for this trade."""
    signal: Signal | None = None
    if trade.signal_id:
        signal = await db.get(Signal, trade.signal_id)

    row: TradeJournal | None = None
    if trade.id:
        result = await db.execute(
            select(TradeJournal)
            .where(TradeJournal.trade_id == trade.id, TradeJournal.action == "confirmed")
            .limit(1)
        )
        row = result.scalar_one_or_none()
    if row is None and trade.signal_id:
        result = await db.execute(
            select(TradeJournal)
            .where(
                TradeJournal.signal_id == trade.signal_id,
                TradeJournal.action == "confirmed",
            )
            .limit(1)
        )
        row = result.scalar_one_or_none()

    if not row:
        return

    row.outcome_pnl = pnl
    row.achieved_rr = _achieved_rr(trade, signal, exit_price)
    row.closed_at = datetime.now(timezone.utc)
    if trade.id and not row.trade_id:
        row.trade_id = trade.id


async def compute_analytics(db: AsyncSession) -> dict[str, Any]:
    """Win rate, planned vs achieved R:R, action breakdown — from trade_journal."""
    result = await db.execute(
        select(TradeJournal).order_by(TradeJournal.created_at.desc()).limit(500)
    )
    rows = list(result.scalars())

    confirmed = [r for r in rows if r.action == "confirmed"]
    rejected = [r for r in rows if r.action == "rejected"]
    expired = [r for r in rows if r.action == "expired"]

    closed = [r for r in confirmed if r.outcome_pnl is not None]
    wins = [r for r in closed if (r.outcome_pnl or 0) > 0]
    losses = [r for r in closed if (r.outcome_pnl or 0) < 0]

    win_rate = (len(wins) / len(closed) * 100) if closed else None
    planned_vals = [r.planned_rr for r in confirmed if r.planned_rr is not None]
    avg_planned = sum(planned_vals) / len(planned_vals) if planned_vals else None
    achieved_vals = [r.achieved_rr for r in closed if r.achieved_rr is not None]
    avg_achieved = sum(achieved_vals) / len(achieved_vals) if achieved_vals else None
    total_pnl = sum(r.outcome_pnl or 0 for r in closed) if closed else None

    from app.models.portfolio import PortfolioSnapshot

    snap_result = await db.execute(
        select(PortfolioSnapshot)
        .where(PortfolioSnapshot.mode == "live")
        .order_by(PortfolioSnapshot.snapshot_at.desc())
        .limit(50)
    )
    snaps = list(snap_result.scalars())
    day_max_dd_pct: Optional[float] = None
    if snaps:
        values = [s.total_value for s in reversed(snaps) if s.total_value]
        if values:
            peak = values[0]
            max_dd = 0.0
            for v in values:
                peak = max(peak, v)
                if peak > 0:
                    dd = (peak - v) / peak * 100
                    max_dd = max(max_dd, dd)
            day_max_dd_pct = round(max_dd, 2)

    return {
        "total_signals_journaled": len(rows),
        "confirmed": len(confirmed),
        "rejected": len(rejected),
        "expired": len(expired),
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
        "avg_planned_rr": round(avg_planned, 2) if avg_planned is not None else None,
        "avg_achieved_rr": round(avg_achieved, 2) if avg_achieved is not None else None,
        "total_outcome_pnl": round(total_pnl, 2) if total_pnl is not None else None,
        "day_max_drawdown_pct": day_max_dd_pct,
        "override_rate_pct": (
            round(len(rejected) / max(1, len(confirmed) + len(rejected)) * 100, 1)
            if (confirmed or rejected)
            else None
        ),
    }
