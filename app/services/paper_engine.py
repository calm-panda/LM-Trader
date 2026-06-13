"""Paper Trading Engine.

Responsibilities
----------------
* Maintain in-memory cash + position state.
* Enforce risk gates before every trade.
* Persist every fill as a ``PaperOrder`` row.
* Expose P&L helpers (unrealised, realised, day P&L).
* Auto-restore open positions from the DB on startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select

from app.config import get_settings
from app.database import async_session
from app.models.paper_order import OrderSide, OrderStatus, PaperOrder
from app.services.strategies.base import Signal

logger = logging.getLogger(__name__)

# Slippage applied to every simulated fill (5 bps each side).
_SLIPPAGE_BPS = 5


@dataclass
class Position:
    symbol: str
    qty: int
    avg_price: float
    strategy_id: Optional[int] = None
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PaperEngine:
    """Singleton paper trading engine for a single-process deployment."""

    def __init__(self) -> None:
        cfg = get_settings()
        self.starting_capital: float = cfg.paper_starting_capital
        self.cash: float = cfg.paper_starting_capital
        self.positions: dict[str, Position] = {}  # symbol → open position
        self.realised_pnl: float = 0.0
        self.day_start_value: float = cfg.paper_starting_capital
        self._initialised: bool = False

        # Risk limits (from settings)
        self._max_pct: float = cfg.max_capital_per_trade_pct
        self._max_pos: int = cfg.max_open_positions
        self._loss_limit_pct: float = cfg.daily_loss_limit_pct

    # ── Startup ───────────────────────────────────────────────────────────────

    async def restore_from_db(self) -> None:
        """Reconstruct cash + positions by replaying paper_orders from the DB."""
        if self._initialised:
            return
        self._initialised = True

        async with async_session() as db:
            result = await db.execute(
                select(PaperOrder)
                .where(PaperOrder.status == OrderStatus.FILLED.value)
                .order_by(PaperOrder.filled_at)
            )
            orders: list[PaperOrder] = list(result.scalars().all())

        cash = self.starting_capital
        positions: dict[str, Position] = {}
        realised = 0.0

        for o in orders:
            fill = o.fill_price or o.price
            cost = fill * o.quantity
            if o.side == OrderSide.BUY.value:
                cash -= cost
                pos = positions.get(o.symbol)
                if pos:
                    total_qty = pos.qty + o.quantity
                    pos.avg_price = (pos.avg_price * pos.qty + fill * o.quantity) / total_qty
                    pos.qty = total_qty
                else:
                    positions[o.symbol] = Position(
                        symbol=o.symbol,
                        qty=o.quantity,
                        avg_price=fill,
                        strategy_id=o.strategy_id,
                    )
            elif o.side == OrderSide.SELL.value:
                cash += cost
                pos = positions.get(o.symbol)
                if pos:
                    realised += (fill - pos.avg_price) * o.quantity
                    remaining = pos.qty - o.quantity
                    if remaining <= 0:
                        del positions[o.symbol]
                    else:
                        pos.qty = remaining

        self.cash = cash
        self.positions = positions
        self.realised_pnl = realised
        logger.info(
            "PaperEngine restored: cash=%.2f positions=%s realised_pnl=%.2f",
            self.cash,
            list(self.positions.keys()),
            self.realised_pnl,
        )

    # ── Signal processing ─────────────────────────────────────────────────────

    async def process_signal(
        self,
        symbol: str,
        signal: Signal,
        ltp: float,
        strategy_id: Optional[int] = None,
        notes: str = "",
    ) -> Optional[PaperOrder]:
        """Route a BUY/SELL signal through risk checks and record the fill.

        Returns the created PaperOrder on a fill, or None if vetoed by risk.
        """
        if signal == Signal.HOLD:
            return None
        if ltp <= 0:
            logger.warning("process_signal(%s): ltp=%.2f invalid — skipped", symbol, ltp)
            return None

        if not self._initialised:
            await self.restore_from_db()

        # ── Daily loss gate ───────────────────────────────────────────────────
        total_value = self._total_value(ltp, symbol)
        day_pnl_pct = (total_value - self.day_start_value) / self.day_start_value * 100
        if day_pnl_pct < -self._loss_limit_pct:
            logger.warning(
                "Daily loss limit hit (%.2f%% < -%.2f%%) — no new trades today",
                day_pnl_pct,
                self._loss_limit_pct,
            )
            return None

        if signal == Signal.BUY:
            return await self._execute_buy(symbol, ltp, strategy_id, notes)
        else:
            return await self._execute_sell(symbol, ltp, strategy_id, notes)

    # ── Buy / Sell execution ──────────────────────────────────────────────────

    async def _execute_buy(
        self,
        symbol: str,
        ltp: float,
        strategy_id: Optional[int],
        notes: str,
    ) -> Optional[PaperOrder]:
        if symbol in self.positions:
            logger.debug("Already have an open position in %s — skip BUY", symbol)
            return None

        if len(self.positions) >= self._max_pos:
            logger.warning(
                "Max open positions (%d) reached — skip BUY %s", self._max_pos, symbol
            )
            return None

        max_spend = self.cash * (self._max_pct / 100)
        slippage_factor = 1 + _SLIPPAGE_BPS / 10_000
        fill_price = ltp * slippage_factor
        qty = int(max_spend / fill_price)

        if qty <= 0:
            logger.warning("BUY %s: qty=0 (cash=%.2f ltp=%.2f)", symbol, self.cash, ltp)
            return None

        cost = fill_price * qty
        self.cash -= cost
        self.positions[symbol] = Position(
            symbol=symbol, qty=qty, avg_price=fill_price, strategy_id=strategy_id
        )

        order = await self._persist_order(
            symbol=symbol,
            side=OrderSide.BUY,
            qty=qty,
            price=ltp,
            fill_price=fill_price,
            strategy_id=strategy_id,
            slippage=fill_price - ltp,
            notes=notes,
        )
        logger.info(
            "PAPER BUY %s qty=%d @%.2f (fill=%.2f) cash_left=%.2f",
            symbol, qty, ltp, fill_price, self.cash,
        )
        return order

    async def _execute_sell(
        self,
        symbol: str,
        ltp: float,
        strategy_id: Optional[int],
        notes: str,
    ) -> Optional[PaperOrder]:
        pos = self.positions.get(symbol)
        if pos is None:
            logger.debug("No open position in %s — skip SELL", symbol)
            return None

        slippage_factor = 1 - _SLIPPAGE_BPS / 10_000
        fill_price = ltp * slippage_factor
        proceeds = fill_price * pos.qty
        pnl = (fill_price - pos.avg_price) * pos.qty

        self.cash += proceeds
        self.realised_pnl += pnl
        del self.positions[symbol]

        order = await self._persist_order(
            symbol=symbol,
            side=OrderSide.SELL,
            qty=pos.qty,
            price=ltp,
            fill_price=fill_price,
            strategy_id=strategy_id,
            slippage=ltp - fill_price,
            notes=f"{notes} pnl={pnl:+.2f}",
        )
        logger.info(
            "PAPER SELL %s qty=%d @%.2f (fill=%.2f) pnl=%+.2f cash=%.2f",
            symbol, pos.qty, ltp, fill_price, pnl, self.cash,
        )
        return order

    # ── DB persistence ────────────────────────────────────────────────────────

    @staticmethod
    async def _persist_order(
        *,
        symbol: str,
        side: OrderSide,
        qty: int,
        price: float,
        fill_price: float,
        strategy_id: Optional[int],
        slippage: float,
        notes: str,
    ) -> PaperOrder:
        now = datetime.now(timezone.utc)
        order = PaperOrder(
            symbol=symbol,
            side=side.value,
            quantity=qty,
            price=price,
            fill_price=fill_price,
            status=OrderStatus.FILLED.value,
            strategy_id=strategy_id,
            slippage=abs(slippage),
            notes=notes or None,
            filled_at=now,
        )
        async with async_session() as db:
            db.add(order)
            await db.commit()
            await db.refresh(order)
        return order

    # ── Square-off ────────────────────────────────────────────────────────────

    async def square_off_all(self, get_ltp_fn) -> None:
        """Close all open positions at market (used for EOD auto square-off).

        ``get_ltp_fn(symbol) → float`` should return the current price.
        """
        for symbol in list(self.positions.keys()):
            ltp = get_ltp_fn(symbol)
            if ltp and ltp > 0:
                await self._execute_sell(symbol, ltp, None, "EOD_AUTO_SQUAREOFF")
            else:
                logger.warning("square_off_all: no LTP for %s — position left open", symbol)

    # ── P&L helpers ───────────────────────────────────────────────────────────

    def unrealised_pnl(self, ltp_map: dict[str, float]) -> float:
        total = 0.0
        for sym, pos in self.positions.items():
            ltp = ltp_map.get(sym, pos.avg_price)
            total += (ltp - pos.avg_price) * pos.qty
        return total

    def _total_value(self, ltp: float, hint_symbol: str = "") -> float:
        """Quick estimate of portfolio total value."""
        ltp_map = {hint_symbol: ltp} if hint_symbol else {}
        return self.cash + self.unrealised_pnl(ltp_map)

    def snapshot(self, ltp_map: dict[str, float] | None = None) -> dict:
        ltp_map = ltp_map or {}
        unreal = self.unrealised_pnl(ltp_map)
        total = self.cash + unreal
        return {
            "cash": round(self.cash, 2),
            "unrealised_pnl": round(unreal, 2),
            "realised_pnl": round(self.realised_pnl, 2),
            "total_value": round(total, 2),
            "day_pnl": round(total - self.day_start_value, 2),
            "open_positions": len(self.positions),
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": p.qty,
                    "avg_price": round(p.avg_price, 2),
                    "ltp": ltp_map.get(p.symbol, p.avg_price),
                    "pnl": round((ltp_map.get(p.symbol, p.avg_price) - p.avg_price) * p.qty, 2),
                }
                for p in self.positions.values()
            ],
        }


# Module-level singleton — shared across the application
paper_engine = PaperEngine()
