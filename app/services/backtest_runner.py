"""Backtest runner — adapts BaseStrategy into backtesting.py and runs on stored OHLCV.

Gate thresholds (configurable):
  SHARPE_MIN     = 0.8
  MAX_DRAWDOWN   = 20 %  (abs)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from backtesting import Backtest
from backtesting import Strategy as BTStrategy
from sqlalchemy import select

from app.database import async_session
from app.models.ohlcv import OHLCV
from app.services.strategies import REGISTRY, Signal
from app.services.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)

# ── Backtest-quality gates ─────────────────────────────────────────────────────
SHARPE_MIN: float = 0.8
MAX_DRAWDOWN_PCT: float = 20.0   # absolute %


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if f != f else f   # NaN guard
    except (TypeError, ValueError):
        return default


def _check_gate(sharpe: float, max_dd_pct: float) -> tuple[bool, str]:
    """Return (passed, human-readable reason)."""
    if sharpe < SHARPE_MIN:
        return False, f"Sharpe {sharpe:.2f} < minimum {SHARPE_MIN}"
    if abs(max_dd_pct) > MAX_DRAWDOWN_PCT:
        return False, f"Max drawdown {abs(max_dd_pct):.1f}% > limit {MAX_DRAWDOWN_PCT}%"
    return True, "All gates passed ✓"


# ── backtesting.py strategy adapter ───────────────────────────────────────────

def _make_bt_class(strategy_instance: BaseStrategy) -> type:
    """Dynamically build a backtesting.Strategy subclass wrapping our BaseStrategy.

    We cache a sliced view of the data DataFrame so each next() call is O(1).
    """
    class _Adapter(BTStrategy):
        _inner = strategy_instance

        def init(self):
            # Store full data as a lowercase DataFrame; next() slices into it.
            raw = self.data.df.copy()
            raw.columns = [c.lower() for c in raw.columns]
            self._df = raw

        def next(self):
            n = len(self.data)
            df = self._df.iloc[:n].copy()
            df.index = range(len(df))          # reset to int index for pandas-ta
            signal = self._inner.on_tick("BACKTEST", df)
            if signal == Signal.BUY and not self.position:
                self.buy()
            elif signal == Signal.SELL and self.position:
                self.sell()

    _Adapter.__name__ = f"BT_{type(strategy_instance).__name__}"
    return _Adapter


# ── OHLCV fetch from DB ────────────────────────────────────────────────────────

async def _fetch_ohlcv(
    symbol: str,
    timeframe: str,
    from_dt: datetime,
    to_dt: datetime,
) -> pd.DataFrame:
    """Return backtesting.py-compatible DataFrame (Open/High/Low/Close/Volume, DatetimeIndex)."""
    async with async_session() as db:
        result = await db.execute(
            select(OHLCV)
            .where(
                OHLCV.symbol == symbol,
                OHLCV.timeframe == timeframe,
                OHLCV.timestamp >= from_dt,
                OHLCV.timestamp <= to_dt,
            )
            .order_by(OHLCV.timestamp.asc())
        )
        rows = list(result.scalars())

    if not rows:
        return pd.DataFrame()

    data = [
        {
            "Open":   r.open,
            "High":   r.high,
            "Low":    r.low,
            "Close":  r.close,
            "Volume": r.volume or 0.0,
        }
        for r in rows
    ]
    index = pd.DatetimeIndex([r.timestamp for r in rows])
    return pd.DataFrame(data, index=index)


# ── Main entry point ───────────────────────────────────────────────────────────

async def run_backtest(
    strategy_class_name: str,
    symbol: str,
    timeframe: str,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    params: dict | None = None,
    cash: float = 100_000.0,
    commission: float = 0.001,
) -> dict[str, Any]:
    """Run a backtest and return a result dict.

    Keys: return_pct, sharpe, max_drawdown_pct, win_rate_pct, num_trades,
          passed_gate, gate_reason, equity_curve, trades, rows_used, error
    """
    if to_date is None:
        to_date = datetime.now(timezone.utc)
    if from_date is None:
        from_date = to_date - timedelta(days=60)

    cls = REGISTRY.get(strategy_class_name)
    if cls is None:
        return {"error": f"Unknown strategy class: '{strategy_class_name}'"}

    strategy_instance = cls(params)
    ohlcv_df = await _fetch_ohlcv(symbol, timeframe, from_date, to_date)

    if len(ohlcv_df) < 50:
        return {
            "error": (
                f"Not enough OHLCV data — found {len(ohlcv_df)} rows for "
                f"{symbol}/{timeframe} between "
                f"{from_date.date()} and {to_date.date()}. Need ≥ 50."
            )
        }

    bt_cls = _make_bt_class(strategy_instance)
    bt = Backtest(
        ohlcv_df,
        bt_cls,
        cash=cash,
        commission=commission,
        trade_on_close=True,
        exclusive_orders=True,
        finalize_trades=True,
    )

    try:
        stats = await asyncio.get_event_loop().run_in_executor(None, bt.run)
    except Exception as exc:
        logger.exception("backtesting.py raised during run")
        return {"error": f"Backtest failed: {exc}"}

    return_pct = _safe_float(stats.get("Return [%]"))
    sharpe     = _safe_float(stats.get("Sharpe Ratio"))
    max_dd     = _safe_float(stats.get("Max. Drawdown [%]"))
    win_rate   = _safe_float(stats.get("Win Rate [%]"))
    num_trades = int(_safe_float(stats.get("# Trades"), 0))
    passed, gate_reason = _check_gate(sharpe, max_dd)

    # ── Equity curve for Lightweight Charts ───────────────────────────────────
    equity_curve: list[dict] = []
    try:
        ec = stats._equity_curve["Equity"]
        step = max(1, len(ec) // 400)
        for ts, val in ec.iloc[::step].items():
            equity_curve.append({"time": int(ts.timestamp()), "value": round(float(val), 2)})
    except Exception:
        logger.debug("Could not extract equity curve", exc_info=True)

    # ── Trade log ─────────────────────────────────────────────────────────────
    trades_list: list[dict] = []
    try:
        for _, t in stats._trades.tail(100).iterrows():
            trades_list.append({
                "entry_time":  str(t.get("EntryTime", ""))[:19],
                "exit_time":   str(t.get("ExitTime", ""))[:19],
                "size":        int(_safe_float(t.get("Size"))),
                "entry_price": round(_safe_float(t.get("EntryPrice")), 2),
                "exit_price":  round(_safe_float(t.get("ExitPrice")), 2),
                "pnl":         round(_safe_float(t.get("PnL")), 2),
                "return_pct":  round(_safe_float(t.get("ReturnPct")) * 100, 2),
            })
    except Exception:
        logger.debug("Could not extract trade log", exc_info=True)

    return {
        "strategy_class":    strategy_class_name,
        "symbol":            symbol,
        "timeframe":         timeframe,
        "from_date":         str(from_date.date()),
        "to_date":           str(to_date.date()),
        "rows_used":         len(ohlcv_df),
        "return_pct":        round(return_pct, 2),
        "sharpe":            round(sharpe, 3),
        "max_drawdown_pct":  round(max_dd, 2),
        "win_rate_pct":      round(win_rate, 1),
        "num_trades":        num_trades,
        "profit_factor":     round(_safe_float(stats.get("Profit Factor")), 3),
        "expectancy_pct":    round(_safe_float(stats.get("Expectancy [%]")), 2),
        "passed_gate":       passed,
        "gate_reason":       gate_reason,
        "equity_curve":      equity_curve,
        "trades":            trades_list,
    }
