"""APScheduler runner — calls strategy on_tick() every minute during market hours.

Jobs
----
strategy_runner   — every 1 min, no-ops outside 9:15–15:30 IST Mon-Fri.
auto_squareoff    — fires at 15:16 IST Mon-Fri to close all paper positions.
day_reset         — fires at 09:00 IST Mon-Fri to snapshot day-start value.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.database import async_session
from app.models.ohlcv import OHLCV
from app.models.paper_order import PaperOrder
from app.models.strategy import Strategy
from app.services.paper_engine import paper_engine
from app.services.strategies import REGISTRY, Signal
from app.services.tick_store import tick_store

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# How many 1m candles to pull per symbol before resampling
_FETCH_LIMIT: dict[str, int] = {
    "1m": 200,
    "5m": 1000,   # ~200 × 5m bars
    "15m": 3000,  # ~200 × 15m bars
    "30m": 6000,
    "1d": 200,
}

_RESAMPLE_RULE: dict[str, str] = {
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
}

scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")


# ── Market-hours gate ─────────────────────────────────────────────────────────

def _is_market_open() -> bool:
    now = datetime.now(_IST)
    if now.weekday() >= 5:  # Saturday / Sunday
        return False
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now <= market_close


# ── OHLCV fetching + resampling (async) ───────────────────────────────────────

async def _get_ohlcv_df(symbol: str, timeframe: str) -> pd.DataFrame:
    """Fetch OHLCV rows from DB and resample to the requested timeframe."""
    db_tf = "1m" if timeframe in _RESAMPLE_RULE else timeframe
    limit = _FETCH_LIMIT.get(timeframe, 200)

    async with async_session() as db:
        result = await db.execute(
            select(OHLCV)
            .where(OHLCV.symbol == symbol, OHLCV.timeframe == db_tf)
            .order_by(OHLCV.timestamp.desc())
            .limit(limit)
        )
        rows = list(result.scalars())

    if not rows:
        return pd.DataFrame()

    data = [
        {
            "timestamp": r.timestamp,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
        }
        for r in rows
    ]
    df = pd.DataFrame(data)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    rule = _RESAMPLE_RULE.get(timeframe)
    if rule:
        df = df.set_index("timestamp")
        df = df.resample(rule, label="left", closed="left").agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        ).dropna(subset=["open"]).reset_index()

    return df


# ── LTP helper ────────────────────────────────────────────────────────────────

def _get_ltp(symbol: str) -> float:
    tick = tick_store.get_by_symbol(symbol)
    if tick and tick.ltp > 0:
        return tick.ltp
    return 0.0


# ── Core strategy runner ──────────────────────────────────────────────────────

async def run_strategies() -> None:
    """Called every minute. Evaluates all enabled strategies."""
    if not _is_market_open():
        return

    async with async_session() as db:
        result = await db.execute(
            select(Strategy).where(Strategy.enabled.is_(True))
        )
        enabled: list[Strategy] = list(result.scalars())

    if not enabled:
        return

    logger.info("strategy_runner: evaluating %d strategies", len(enabled))

    for strat_row in enabled:
        cls = REGISTRY.get(strat_row.class_name)
        if cls is None:
            logger.warning("Unknown strategy class: %s", strat_row.class_name)
            continue

        try:
            params = json.loads(strat_row.params or "{}")
        except json.JSONDecodeError:
            params = {}

        strategy = cls(params)
        symbols = [s.strip() for s in strat_row.symbols.split(",") if s.strip()]

        for symbol in symbols:
            try:
                df = await _get_ohlcv_df(symbol, strat_row.timeframe)

                if df.empty:
                    logger.debug("%s/%s: no OHLCV data", strat_row.name, symbol)
                    continue

                # Run strategy (CPU-bound — off-load to thread pool)
                loop = asyncio.get_event_loop()
                signal: Signal = await loop.run_in_executor(
                    None, strategy.on_tick, symbol, df
                )

                if signal == Signal.HOLD:
                    continue

                ltp = _get_ltp(symbol)
                if ltp <= 0:
                    # Fall back to last close price
                    ltp = float(df["close"].iloc[-1])

                logger.info(
                    "Signal %s → %s/%s @%.2f",
                    signal.value, strat_row.name, symbol, ltp,
                )

                await paper_engine.process_signal(
                    symbol=symbol,
                    signal=signal,
                    ltp=ltp,
                    strategy_id=strat_row.id,
                    notes=f"{strat_row.name}|{strat_row.timeframe}",
                )

            except Exception:
                logger.exception(
                    "Error running strategy %s on %s", strat_row.name, symbol
                )


# ── EOD auto square-off ───────────────────────────────────────────────────────

async def auto_squareoff() -> None:
    """Close all open paper positions at 15:16 IST."""
    logger.info("EOD auto square-off triggered")
    await paper_engine.square_off_all(get_ltp_fn=_get_ltp)


# ── Day-start reset ───────────────────────────────────────────────────────────

async def day_reset() -> None:
    """Snapshot day-start portfolio value at 09:00 IST for day P&L tracking."""
    snap = paper_engine.snapshot()
    paper_engine.day_start_value = snap["total_value"]
    logger.info("Day reset: start_value=%.2f", paper_engine.day_start_value)


# ── Scheduler lifecycle ───────────────────────────────────────────────────────

def start_scheduler() -> None:
    if scheduler.running:
        return

    scheduler.add_job(
        run_strategies,
        "interval",
        minutes=1,
        id="strategy_runner",
        replace_existing=True,
        max_instances=1,  # never overlap
    )
    scheduler.add_job(
        auto_squareoff,
        "cron",
        day_of_week="mon-fri",
        hour=15,
        minute=16,
        id="auto_squareoff",
        replace_existing=True,
    )
    scheduler.add_job(
        day_reset,
        "cron",
        day_of_week="mon-fri",
        hour=9,
        minute=0,
        id="day_reset",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("APScheduler started — strategy runner active")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
