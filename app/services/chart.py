"""Chart data service — OHLCV queries, resampling, and indicator computation."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd
import pandas_ta as ta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ohlcv import OHLCV

logger = logging.getLogger(__name__)

# IST offset — all DB timestamps are stored as naive IST local times
_IST = timezone(timedelta(hours=5, minutes=30))

# How many raw 1m candles to pull before resampling (ensures enough for indicators)
_RESAMPLE_FETCH = {
    "5m":  3000,
    "15m": 9000,
    "30m": 18000,
}

# Resample rule strings
_RESAMPLE_RULE = {
    "5m":  "5min",
    "15m": "15min",
    "30m": "30min",
}

VALID_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1d"}


# ─── Timestamp helpers ────────────────────────────────────────────────────────

def _to_unix(dt: datetime) -> int:
    """Convert a naive IST datetime (from SQLite) to UTC Unix seconds."""
    return int(dt.replace(tzinfo=_IST).timestamp())


def _to_date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


# ─── DB query ─────────────────────────────────────────────────────────────────

async def _fetch_rows(
    db: AsyncSession,
    symbol: str,
    db_timeframe: str,
    limit: int,
) -> list[OHLCV]:
    result = await db.execute(
        select(OHLCV)
        .where(OHLCV.symbol == symbol, OHLCV.timeframe == db_timeframe)
        .order_by(OHLCV.timestamp.asc())
        .limit(limit)
    )
    return list(result.scalars())


# ─── Resampling ───────────────────────────────────────────────────────────────

def _rows_to_df(rows: list[OHLCV]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
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
    return df.sort_values("timestamp").reset_index(drop=True)


def _resample_df(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.set_index("timestamp")
    resampled = df.resample(rule, label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna(subset=["open"])
    return resampled.reset_index()


# ─── Indicator computation (runs in thread pool) ──────────────────────────────

def _compute_indicators_sync(df: pd.DataFrame, is_intraday: bool) -> dict[str, list[dict]]:
    if df.empty or len(df) < 5:
        return {}

    close = df["close"]
    times = (
        [_to_unix(t.to_pydatetime()) for t in df["timestamp"]]
        if is_intraday
        else [_to_date_str(t.to_pydatetime()) for t in df["timestamp"]]
    )

    def to_lw_line(series: pd.Series) -> list[dict]:
        """Convert a Series to [{time, value}, ...] dropping NaN."""
        out = []
        for t, v in zip(times, series):
            if pd.notna(v):
                out.append({"time": t, "value": round(float(v), 4)})
        return out

    result: dict[str, list] = {}

    # ── EMAs ───────────────────────────────────────────────────────────────
    for length, key in ((9, "ema9"), (21, "ema21"), (50, "ema50")):
        try:
            s = ta.ema(close, length=length)
            if s is not None:
                result[key] = to_lw_line(s)
        except Exception as exc:
            logger.debug("EMA%d failed: %s", length, exc)

    # ── RSI 14 ─────────────────────────────────────────────────────────────
    try:
        s = ta.rsi(close, length=14)
        if s is not None:
            result["rsi"] = to_lw_line(s)
    except Exception as exc:
        logger.debug("RSI failed: %s", exc)

    # ── MACD (12, 26, 9) ───────────────────────────────────────────────────
    try:
        m = ta.macd(close, fast=12, slow=26, signal=9)
        if m is not None and not m.empty:
            result["macd_line"]   = to_lw_line(m.iloc[:, 0])  # MACD_12_26_9
            result["macd_signal"] = to_lw_line(m.iloc[:, 2])  # MACDs_12_26_9
            # Histogram — include sign in value; renderer colours positive/negative
            result["macd_hist"] = to_lw_line(m.iloc[:, 1])    # MACDh_12_26_9
    except Exception as exc:
        logger.debug("MACD failed: %s", exc)

    # ── Bollinger Bands (20, 2) ────────────────────────────────────────────
    try:
        b = ta.bbands(close, length=20, std=2)
        if b is not None and not b.empty:
            result["bb_upper"] = to_lw_line(b.iloc[:, 2])  # BBU
            result["bb_lower"] = to_lw_line(b.iloc[:, 0])  # BBL
            result["bb_mid"]   = to_lw_line(b.iloc[:, 1])  # BBM
    except Exception as exc:
        logger.debug("BBands failed: %s", exc)

    return result


# ─── Public API ───────────────────────────────────────────────────────────────

async def get_chart_data(
    db: AsyncSession,
    symbol: str,
    timeframe: str = "5m",
    limit: int = 500,
) -> dict[str, Any]:
    """Return OHLCV candles + indicator arrays in Lightweight Charts-ready format."""
    symbol = symbol.upper()
    timeframe = timeframe.lower()
    if timeframe not in VALID_TIMEFRAMES:
        timeframe = "5m"

    is_intraday = timeframe != "1d"

    # ── Fetch from DB ─────────────────────────────────────────────────────
    if timeframe in _RESAMPLE_RULE:
        fetch_limit = _RESAMPLE_FETCH.get(timeframe, limit * 6)
        raw_rows = await _fetch_rows(db, symbol, "1m", fetch_limit)
    else:
        raw_rows = await _fetch_rows(db, symbol, timeframe, limit)

    df = _rows_to_df(raw_rows)

    # ── Resample if needed ────────────────────────────────────────────────
    if timeframe in _RESAMPLE_RULE:
        df = _resample_df(df, _RESAMPLE_RULE[timeframe])

    # Trim to requested limit AFTER resampling (keep most recent)
    if len(df) > limit:
        df = df.iloc[-limit:].reset_index(drop=True)

    if df.empty:
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "candles": [],
            "indicators": {},
            "ltp": None,
            "market_open": _is_market_open(),
            "count": 0,
        }

    # ── Build candle list ─────────────────────────────────────────────────
    candles: list[dict] = []
    for row in df.itertuples(index=False):
        ts = row.timestamp.to_pydatetime()
        t = _to_unix(ts) if is_intraday else _to_date_str(ts)
        candles.append({
            "time":   t,
            "open":   round(row.open, 4),
            "high":   round(row.high, 4),
            "low":    round(row.low, 4),
            "close":  round(row.close, 4),
            "volume": round(row.volume, 0),
        })

    # ── Compute indicators in thread pool (CPU-bound pandas work) ─────────
    loop = asyncio.get_event_loop()
    indicators = await loop.run_in_executor(
        None, _compute_indicators_sync, df, is_intraday
    )

    # ── Live LTP from tick store ──────────────────────────────────────────
    ltp: Optional[float] = None
    try:
        from app.services.tick_store import tick_store  # noqa: PLC0415
        tick = tick_store.get_by_symbol(symbol)
        if tick:
            ltp = tick.ltp
    except Exception:
        pass

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": candles,
        "indicators": indicators,
        "ltp": ltp,
        "market_open": _is_market_open(),
        "count": len(candles),
    }


def _is_market_open() -> bool:
    """Check if NSE market is currently open (IST 9:15–15:30, Mon–Fri)."""
    now_ist = datetime.now(_IST)
    if now_ist.weekday() >= 5:          # Saturday, Sunday
        return False
    market_start = now_ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_end   = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_start <= now_ist <= market_end


async def get_available_symbols(db: AsyncSession) -> list[str]:
    """Return all symbols that have OHLCV data in the DB."""
    from sqlalchemy import distinct, text  # noqa: PLC0415
    result = await db.execute(
        select(OHLCV.symbol).distinct().order_by(OHLCV.symbol)
    )
    return [row[0] for row in result]
