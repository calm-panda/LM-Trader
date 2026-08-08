"""APScheduler runner — strategy execution, AI signal generation, EOD square-off,
daily token refresh.

Scheduled jobs (all times IST / Asia/Kolkata)
----------------------------------------------
token_refresh      — 08:30  Mon-Fri  Headless TOTP login; restart feed with new token.
day_reset          — 09:00  Mon-Fri  Snapshot paper (+ live if enabled) for day P&L baseline.
pre_market_check   — 09:10  Mon-Fri  Verify token valid + instrument master loaded.
strategy_runner    — every 1 min     No-ops outside 09:15–15:30; evaluates enabled strategies.
generate_signals   — every 5 min     AI recommendations for watchlist (confirm-gate only).
expire_signals     — every 1 min     Flip pending signals past expires_at → expired.
auto_squareoff     — 15:15  Mon-Fri  Close all open paper (+ live) positions.
intraday_refresh   — 15:45  Mon-Fri  Persist today's 1m candles that arrived via WebSocket.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update

from app.config import get_settings
from app.database import async_session
from app.models.ohlcv import OHLCV
from app.models.portfolio import PortfolioSnapshot
from app.models.signal import Signal as SignalRow
from app.models.strategy import Strategy
from app.models.trade import Trade
from app.services.paper_engine import paper_engine
from app.services.strategies import REGISTRY, Signal
from app.services.tick_store import tick_store

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# ── Candle-fetch limits (rows before resampling) ──────────────────────────────
_FETCH_LIMIT: dict[str, int] = {
    "1m": 200,
    "5m": 1000,
    "15m": 3000,
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
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_ = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_ <= now <= close_


# ── OHLCV fetch + resample ─────────────────────────────────────────────────────

async def _get_ohlcv_df(symbol: str, timeframe: str) -> pd.DataFrame:
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

    df = pd.DataFrame([
        {"timestamp": r.timestamp, "open": r.open, "high": r.high,
         "low": r.low, "close": r.close, "volume": r.volume}
        for r in rows
    ])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    rule = _RESAMPLE_RULE.get(timeframe)
    if rule:
        df = df.set_index("timestamp")
        df = (
            df.resample(rule, label="left", closed="left")
            .agg(open=("open","first"), high=("high","max"),
                 low=("low","min"), close=("close","last"), volume=("volume","sum"))
            .dropna(subset=["open"])
            .reset_index()
        )
    return df


def _get_ltp(symbol: str) -> float:
    tick = tick_store.get_by_symbol(symbol)
    return tick.ltp if (tick and tick.ltp > 0) else 0.0


async def _latest_access_token() -> str | None:
    from sqlalchemy import select as sa_select
    from app.models.auth_token import AuthToken

    async with async_session() as db:
        result = await db.execute(
            sa_select(AuthToken).order_by(AuthToken.updated_at.desc()).limit(1)
        )
        token_row = result.scalar_one_or_none()
    return token_row.access_token if token_row else None


# ── 08:30 — Daily token auto-refresh ─────────────────────────────────────────

async def token_refresh() -> None:
    """Attempt headless TOTP re-login; restart feed with fresh token on success."""
    logger.info("=== Daily token refresh starting ===")
    try:
        from app.services.upstox.auto_login import auto_refresh_token
        success = await auto_refresh_token()
    except Exception:
        logger.exception("token_refresh: unhandled error")
        success = False

    if not success:
        logger.warning(
            "Token refresh failed or skipped.  "
            "If the current token expires before 09:15, strategies will halt.  "
            "Log in manually at /auth/login"
        )
        return

    # Restart the WebSocket feed with the new token
    try:
        from app.services.upstox.feed import stop_feed, start_feed, is_feed_running
        from app.services.upstox.instruments import get_instrument_key

        token = await _latest_access_token()
        if not token:
            logger.error("token_refresh: no token in DB after refresh — feed not restarted")
            return

        cfg = get_settings()
        instrument_keys = [
            k for sym in cfg.watchlist_symbols
            if (k := get_instrument_key(sym))
        ]

        if is_feed_running():
            stop_feed()
            await asyncio.sleep(2)

        start_feed(token, instrument_keys)
        logger.info("token_refresh: WebSocket feed restarted with %d instruments", len(instrument_keys))
    except Exception:
        logger.exception("token_refresh: failed to restart feed")


# ── 09:00 — Day reset ────────────────────────────────────────────────────────

async def day_reset() -> None:
    """Snapshot paper (+ live if enabled) portfolio value for day P&L tracking."""
    snap = paper_engine.snapshot()
    paper_engine.day_start_value = snap["total_value"]
    logger.info("Day reset: start_value=%.2f  positions=%d",
                paper_engine.day_start_value, snap["open_positions"])

    cfg = get_settings()
    if not cfg.live_trading_enabled:
        return

    token = await _latest_access_token()
    if not token:
        logger.warning("day_reset: live enabled but no token — skip live snapshot")
        return

    try:
        from app.services.upstox.orders import (
            estimate_live_total_value,
            get_funds,
            get_holdings,
            get_positions,
        )

        funds = await get_funds(token)
        positions = await get_positions(token)
        holdings = await get_holdings(token)
        total = estimate_live_total_value(funds, positions, holdings)
        open_pos = sum(
            1 for p in positions
            if float(p.get("quantity") or p.get("net_quantity") or 0) != 0
        )

        async with async_session() as db:
            db.add(PortfolioSnapshot(
                mode="live",
                cash=funds,
                total_value=total,
                day_pnl=0.0,
                total_pnl=0.0,
                open_positions=open_pos,
            ))
            await db.commit()
        logger.info("day_reset: live snapshot cash=%.2f total=%.2f open=%d",
                    funds, total, open_pos)
    except Exception:
        logger.exception("day_reset: live snapshot failed")


# ── 09:10 — Pre-market health check ──────────────────────────────────────────

async def pre_market_check() -> None:
    """Verify token validity + instrument master before strategies start at 09:15."""
    from app.services.upstox.instruments import is_master_loaded

    # Token check
    from sqlalchemy import select as sa_select
    from app.models.auth_token import AuthToken
    from app.services.upstox.auth import _as_utc, _utc_now

    async with async_session() as db:
        result = await db.execute(
            sa_select(AuthToken).order_by(AuthToken.updated_at.desc()).limit(1)
        )
        token_row = result.scalar_one_or_none()

    if not token_row:
        logger.critical(
            "pre_market_check: NO ACCESS TOKEN in DB — "
            "strategies will produce no signals.  Login at /auth/login"
        )
        return

    if _as_utc(token_row.expires_at) < _utc_now():
        logger.critical(
            "pre_market_check: Access token EXPIRED — "
            "strategies halted.  Login at /auth/login"
        )
        return

    # Instrument master
    if not is_master_loaded():
        logger.warning("pre_market_check: instrument master not loaded — attempting reload")
        try:
            from app.services.upstox.instruments import load_instrument_master, download_instrument_master
            if not load_instrument_master():
                await download_instrument_master()
        except Exception:
            logger.exception("pre_market_check: instrument master reload failed")

    # Feed status
    from app.services.upstox.feed import is_feed_running
    if not is_feed_running():
        logger.warning("pre_market_check: WebSocket feed is NOT running — live ticks unavailable")

    logger.info("pre_market_check: token valid, master loaded=%s, feed running=%s",
                is_master_loaded(), is_feed_running())


# ── Every 1 min — Strategy runner ────────────────────────────────────────────

async def run_strategies() -> None:
    """Evaluate all enabled strategies; no-op outside 09:15–15:30 IST Mon-Fri."""
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

                loop = asyncio.get_event_loop()
                signal: Signal = await loop.run_in_executor(
                    None, strategy.on_tick, symbol, df
                )

                if signal == Signal.HOLD:
                    continue

                ltp = _get_ltp(symbol)
                if ltp <= 0:
                    ltp = float(df["close"].iloc[-1])

                logger.info("Signal %s → %s/%s @%.2f",
                            signal.value, strat_row.name, symbol, ltp)

                await paper_engine.process_signal(
                    symbol=symbol,
                    signal=signal,
                    ltp=ltp,
                    strategy_id=strat_row.id,
                    notes=f"{strat_row.name}|{strat_row.timeframe}",
                )

            except Exception:
                logger.exception("Error running %s on %s", strat_row.name, symbol)


# ── Every 5 min — AI signal generation ───────────────────────────────────────

async def generate_signals() -> None:
    """Ask DeepSeek for structured recommendations on the watchlist (no auto-exec)."""
    if not _is_market_open():
        return

    cfg = get_settings()
    if not cfg.deepseek_api_key:
        return

    from app.services.deepseek import generate_signal

    symbols = cfg.watchlist_symbols
    logger.info("generate_signals: scanning %d symbols", len(symbols))

    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=cfg.signal_validity_minutes)

    for symbol in symbols:
        try:
            result = await generate_signal(symbol, timeframe="5m")
            if not result:
                continue

            # Skip if an unexpired pending signal already exists for this symbol
            async with async_session() as db:
                existing = await db.execute(
                    select(SignalRow).where(
                        SignalRow.symbol == symbol,
                        SignalRow.status == "pending",
                        SignalRow.expires_at > now,
                    ).limit(1)
                )
                if existing.scalar_one_or_none():
                    logger.debug("generate_signals: pending signal already exists for %s", symbol)
                    continue

                row = SignalRow(
                    symbol=result["symbol"],
                    timeframe=result.get("timeframe", "5m"),
                    generated_at=now,
                    direction=result["direction"],
                    entry=result["entry"],
                    stop_loss=result["stop_loss"],
                    target=result["target"],
                    risk_reward_ratio=result["risk_reward_ratio"],
                    confidence=result["confidence"],
                    indicators_snapshot=json.dumps(result.get("indicators_snapshot") or {}),
                    indicators_triggered=json.dumps(result.get("indicators_triggered") or []),
                    reasoning=result.get("reasoning") or "",
                    invalidation=result.get("invalidation") or "",
                    status="pending",
                    expires_at=expires,
                    model_used=result.get("model_used", "deepseek-chat"),
                )
                db.add(row)
                await db.commit()
                logger.info(
                    "generate_signals: stored %s %s conf=%.2f entry=%.2f",
                    result["direction"], symbol, result["confidence"], result["entry"],
                )
        except Exception:
            logger.exception("generate_signals: error for %s", symbol)


# ── Every 1 min — Expire stale pending signals ───────────────────────────────

async def expire_signals() -> None:
    """Flip pending signals past expires_at to expired."""
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        result = await db.execute(
            update(SignalRow)
            .where(SignalRow.status == "pending", SignalRow.expires_at <= now)
            .values(status="expired")
        )
        await db.commit()
        if result.rowcount:
            logger.info("expire_signals: expired %d signal(s)", result.rowcount)


# ── 15:15 — EOD auto square-off ──────────────────────────────────────────────

async def auto_squareoff() -> None:
    """Close all open paper (+ live) positions at 15:15 IST (NSE intraday cutoff)."""
    logger.info("=== EOD auto square-off at 15:15 IST ===")
    snap_before = paper_engine.snapshot()
    logger.info("Positions before square-off: %d", snap_before["open_positions"])
    await paper_engine.square_off_all(get_ltp_fn=_get_ltp)
    snap_after = paper_engine.snapshot()
    logger.info(
        "Square-off complete — cash=%.2f realised_pnl=%.2f",
        snap_after["cash"], snap_after["realised_pnl"],
    )

    cfg = get_settings()
    if not cfg.live_trading_enabled:
        return

    token = await _latest_access_token()
    if not token:
        logger.warning("auto_squareoff: live enabled but no token — skip live close")
        return

    try:
        from app.services.upstox.instruments import get_instrument_key
        from app.services.upstox.orders import get_positions, place_market_order

        positions = await get_positions(token)
        for p in positions:
            try:
                qty = int(float(p.get("quantity") or p.get("net_quantity") or 0))
            except (TypeError, ValueError):
                continue
            if qty == 0:
                continue

            symbol = (p.get("trading_symbol") or p.get("tradingsymbol") or "").upper()
            instrument_key = p.get("instrument_token") or p.get("instrument_key")
            if not instrument_key and symbol:
                instrument_key = get_instrument_key(symbol)
            if not instrument_key:
                logger.warning("auto_squareoff: no instrument key for %s — skip", symbol)
                continue

            side = "SELL" if qty > 0 else "BUY"
            abs_qty = abs(qty)
            try:
                order_id = await place_market_order(
                    token, instrument_key, side, abs_qty, product="I"
                )
                # Close matching open Trade rows
                async with async_session() as db:
                    result = await db.execute(
                        select(Trade).where(
                            Trade.mode == "live",
                            Trade.status == "open",
                            Trade.symbol == symbol,
                        )
                    )
                    for trade in result.scalars():
                        ltp = _get_ltp(symbol) or trade.entry_price
                        trade.exit_price = ltp
                        trade.pnl = (ltp - trade.entry_price) * trade.quantity * (
                            1 if trade.side == "BUY" else -1
                        )
                        trade.status = "closed"
                        trade.closed_at = datetime.now(timezone.utc)
                        trade.notes = (trade.notes or "") + f" | EOD_SQUAREOFF order={order_id}"
                    await db.commit()
                logger.info("auto_squareoff: closed live %s qty=%d via %s", symbol, abs_qty, order_id)
            except Exception:
                logger.exception("auto_squareoff: failed to close %s", symbol)
    except Exception:
        logger.exception("auto_squareoff: live square-off failed")


# ── 15:45 — Intraday candle refresh ──────────────────────────────────────────

async def intraday_refresh() -> None:
    """Persist today's intraday candles to DB after market close."""
    logger.info("intraday_refresh: persisting today's candles")
    try:
        from app.services.upstox.history import refresh_intraday
        from app.services.upstox.instruments import get_instrument_key

        token = await _latest_access_token()
        if not token:
            logger.warning("intraday_refresh: no token — skipped")
            return

        cfg = get_settings()
        pairs = [
            (sym, k)
            for sym in cfg.watchlist_symbols
            if (k := get_instrument_key(sym))
        ]
        await refresh_intraday(token, pairs)
        logger.info("intraday_refresh: done for %d symbols", len(pairs))
    except Exception:
        logger.exception("intraday_refresh failed")


# ── Scheduler lifecycle ───────────────────────────────────────────────────────

def start_scheduler() -> None:
    if scheduler.running:
        return

    cfg = get_settings()
    signal_interval = max(1, cfg.signal_generation_interval_minutes)

    scheduler.add_job(
        token_refresh,
        "cron",
        day_of_week="mon-fri",
        hour=8, minute=30,
        id="token_refresh",
        replace_existing=True,
    )
    scheduler.add_job(
        day_reset,
        "cron",
        day_of_week="mon-fri",
        hour=9, minute=0,
        id="day_reset",
        replace_existing=True,
    )
    scheduler.add_job(
        pre_market_check,
        "cron",
        day_of_week="mon-fri",
        hour=9, minute=10,
        id="pre_market_check",
        replace_existing=True,
    )
    scheduler.add_job(
        run_strategies,
        "interval",
        minutes=1,
        id="strategy_runner",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        generate_signals,
        "interval",
        minutes=signal_interval,
        id="generate_signals",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        expire_signals,
        "interval",
        minutes=1,
        id="expire_signals",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        auto_squareoff,
        "cron",
        day_of_week="mon-fri",
        hour=15, minute=15,
        id="auto_squareoff",
        replace_existing=True,
    )
    scheduler.add_job(
        intraday_refresh,
        "cron",
        day_of_week="mon-fri",
        hour=15, minute=45,
        id="intraday_refresh",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "APScheduler started — jobs: token_refresh@08:30, day_reset@09:00, "
        "pre_market_check@09:10, strategy_runner@1min, generate_signals@%dmin, "
        "expire_signals@1min, auto_squareoff@15:15, intraday_refresh@15:45",
        signal_interval,
    )


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
