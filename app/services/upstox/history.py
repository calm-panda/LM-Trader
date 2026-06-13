"""Fetch historical and intraday OHLCV candles from Upstox and persist to SQLite."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import upstox_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.ohlcv import OHLCV

logger = logging.getLogger(__name__)

# Upstox API version header required by all history endpoints
_API_VERSION = "2.0"

# Upstox historical API interval names (these are the only accepted values)
# "1minute" | "30minute" | "day" | "week" | "month"
# Note: 5m and 15m candles do NOT exist in the historical API — we resample from 1m.
_INTERVAL_HISTORICAL = "1minute"
_INTERVAL_DAILY = "day"

# How far back to seed each timeframe on first run
_SEED_DAYS_1M = 25       # 1-minute: API returns up to ~1 month
_SEED_DAYS_1D = 365      # Daily: up to ~1 year


def _make_api_client(access_token: str) -> upstox_client.ApiClient:
    config = upstox_client.Configuration()
    config.access_token = access_token
    return upstox_client.ApiClient(config)


def _candle_row_to_ohlcv(
    row: list,
    symbol: str,
    instrument_key: str,
    timeframe: str,
) -> Optional[OHLCV]:
    """Convert a raw candle list [timestamp, o, h, l, c, volume, oi] to an OHLCV model."""
    try:
        ts_raw = row[0]
        # Upstox returns ISO-8601 strings like "2024-01-15T09:15:00+05:30"
        if isinstance(ts_raw, str):
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = datetime.fromtimestamp(ts_raw / 1000, tz=timezone.utc)

        return OHLCV(
            symbol=symbol,
            instrument_token=instrument_key,
            timeframe=timeframe,
            timestamp=ts,
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]) if len(row) > 5 else 0.0,
        )
    except Exception as exc:
        logger.debug("Skipping malformed candle row %s: %s", row, exc)
        return None


async def _upsert_candles(db: AsyncSession, candles: list[OHLCV]) -> int:
    """Insert candles, ignoring duplicates (same symbol+timeframe+timestamp)."""
    if not candles:
        return 0

    inserted = 0
    for candle in candles:
        result = await db.execute(
            select(OHLCV).where(
                OHLCV.symbol == candle.symbol,
                OHLCV.timeframe == candle.timeframe,
                OHLCV.timestamp == candle.timestamp,
            ).limit(1)
        )
        if result.scalar_one_or_none() is None:
            db.add(candle)
            inserted += 1

    await db.commit()
    return inserted


def _fetch_historical_sync(
    api: upstox_client.HistoryApi,
    instrument_key: str,
    interval: str,
    from_date: str,
    to_date: str,
    retries: int = 3,
) -> list[list]:
    """Synchronous call to Upstox HistoryApi — run in a thread pool."""
    import time
    for attempt in range(1, retries + 1):
        try:
            resp = api.get_historical_candle_data1(
                instrument_key=instrument_key,
                interval=interval,
                to_date=to_date,
                from_date=from_date,
                api_version=_API_VERSION,
            )
            if resp and resp.data and resp.data.candles:
                return resp.data.candles
            return []
        except Exception as exc:
            logger.warning(
                "Historical fetch attempt %d/%d failed for %s [%s %s→%s]: %s",
                attempt, retries, instrument_key, interval, from_date, to_date, exc,
            )
            if attempt < retries:
                time.sleep(2 ** attempt)   # back off: 2s, 4s
    return []


def _fetch_intraday_sync(
    api: upstox_client.HistoryApi,
    instrument_key: str,
    interval: str,
    retries: int = 3,
) -> list[list]:
    """Synchronous call to Upstox intraday API — run in a thread pool."""
    import time
    for attempt in range(1, retries + 1):
        try:
            resp = api.get_intra_day_candle_data(
                instrument_key=instrument_key,
                interval=interval,
                api_version=_API_VERSION,
            )
            if resp and resp.data and resp.data.candles:
                return resp.data.candles
            return []
        except Exception as exc:
            logger.warning(
                "Intraday fetch attempt %d/%d failed for %s [%s]: %s",
                attempt, retries, instrument_key, interval, exc,
            )
            if attempt < retries:
                time.sleep(2 ** attempt)
    return []


async def fetch_and_store_symbol(
    access_token: str,
    symbol: str,
    instrument_key: str,
    seed_days_1m: int = _SEED_DAYS_1M,
    seed_days_1d: int = _SEED_DAYS_1D,
) -> dict[str, int]:
    """Fetch historical + intraday 1m and daily candles for one symbol.

    Returns a dict of {timeframe: rows_inserted}.
    """
    loop = asyncio.get_event_loop()
    api_client = _make_api_client(access_token)
    history_api = upstox_client.HistoryApi(api_client)

    today = date.today()
    counts: dict[str, int] = {}

    # ── Daily candles (last N days) ──────────────────────────────────────────
    from_1d = (today - timedelta(days=seed_days_1d)).isoformat()
    to_1d = today.isoformat()

    daily_raw = await loop.run_in_executor(
        None,
        _fetch_historical_sync,
        history_api, instrument_key, _INTERVAL_DAILY, from_1d, to_1d,
    )
    daily_candles = [
        c for row in daily_raw
        if (c := _candle_row_to_ohlcv(row, symbol, instrument_key, "1d")) is not None
    ]
    async with async_session() as db:
        counts["1d"] = await _upsert_candles(db, daily_candles)
    logger.info("%s daily: fetched %d, inserted %d", symbol, len(daily_raw), counts["1d"])

    # ── 1-minute candles (last N days, chunked monthly because API limit) ────
    # Upstox returns at most ~1 month per request for 1m interval.
    # We split into 25-day chunks.
    chunk_days = 25
    all_1m: list[OHLCV] = []
    chunk_end = today
    chunk_start = max(today - timedelta(days=seed_days_1m), chunk_end - timedelta(days=chunk_days))

    while chunk_start < chunk_end:
        raw = await loop.run_in_executor(
            None,
            _fetch_historical_sync,
            history_api, instrument_key, _INTERVAL_HISTORICAL,
            chunk_start.isoformat(), chunk_end.isoformat(),
        )
        all_1m.extend(
            c for row in raw
            if (c := _candle_row_to_ohlcv(row, symbol, instrument_key, "1m")) is not None
        )
        chunk_end = chunk_start - timedelta(days=1)
        chunk_start = max(
            today - timedelta(days=seed_days_1m),
            chunk_end - timedelta(days=chunk_days),
        )
        if chunk_end <= today - timedelta(days=seed_days_1m):
            break

    async with async_session() as db:
        counts["1m"] = await _upsert_candles(db, all_1m)
    logger.info("%s 1m: fetched %d, inserted %d", symbol, len(all_1m), counts["1m"])

    # ── Today's intraday 1-minute candles ────────────────────────────────────
    intraday_raw = await loop.run_in_executor(
        None,
        _fetch_intraday_sync,
        history_api, instrument_key, _INTERVAL_HISTORICAL,
    )
    intraday_candles = [
        c for row in intraday_raw
        if (c := _candle_row_to_ohlcv(row, symbol, instrument_key, "1m")) is not None
    ]
    async with async_session() as db:
        intraday_count = await _upsert_candles(db, intraday_candles)
    counts["1m"] = counts.get("1m", 0) + intraday_count
    logger.info("%s intraday 1m: fetched %d, inserted %d", symbol, len(intraday_raw), intraday_count)

    return counts


async def bootstrap_ohlcv(
    access_token: str,
    symbols_with_keys: list[tuple[str, str]],
    concurrency: int = 2,
) -> None:
    """Seed OHLCV data for a list of (symbol, instrument_key) pairs.

    Processes `concurrency` symbols at a time to stay within Upstox rate limits
    (1000 calls/min — each symbol uses ~3 calls, so 2 concurrent is safe).
    """
    sem = asyncio.Semaphore(concurrency)

    async def _guarded(symbol: str, key: str) -> None:
        async with sem:
            try:
                counts = await fetch_and_store_symbol(access_token, symbol, key)
                logger.info("Bootstrapped %s: %s", symbol, counts)
            except Exception as exc:
                logger.error("Failed to bootstrap %s: %s", symbol, exc)

    tasks = [asyncio.create_task(_guarded(sym, key)) for sym, key in symbols_with_keys]
    await asyncio.gather(*tasks)
    logger.info("OHLCV bootstrap complete for %d symbols.", len(symbols_with_keys))


async def refresh_intraday(
    access_token: str,
    symbols_with_keys: list[tuple[str, str]],
) -> None:
    """Refresh today's intraday 1m candles. Call this every 5–15 min during market hours."""
    loop = asyncio.get_event_loop()

    for symbol, instrument_key in symbols_with_keys:
        api_client = _make_api_client(access_token)
        history_api = upstox_client.HistoryApi(api_client)

        raw = await loop.run_in_executor(
            None,
            _fetch_intraday_sync,
            history_api, instrument_key, _INTERVAL_HISTORICAL,
        )
        candles = [
            c for row in raw
            if (c := _candle_row_to_ohlcv(row, symbol, instrument_key, "1m")) is not None
        ]
        async with async_session() as db:
            n = await _upsert_candles(db, candles)
        if n:
            logger.debug("Intraday refresh %s: +%d candles", symbol, n)
