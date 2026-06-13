"""Upstox Market Data WebSocket feed — wires live ticks into TickStore.

The Upstox SDK uses websocket-client (synchronous, thread-based).
This module runs the streamer in a daemon thread and pushes decoded ticks into
the shared tick_store singleton — safe for the single-process Pi deployment.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import upstox_client

from app.services.tick_store import Tick, tick_store

logger = logging.getLogger(__name__)

# Module-level streamer instance (one per process)
_streamer: Optional[upstox_client.MarketDataStreamerV3] = None
_lock = threading.Lock()


def _make_api_client(access_token: str) -> upstox_client.ApiClient:
    config = upstox_client.Configuration()
    config.access_token = access_token
    return upstox_client.ApiClient(config)


def _on_message(data: dict) -> None:
    """Handle a decoded protobuf message from the WebSocket feed."""
    feeds: dict = data.get("feeds", {})
    for instrument_key, feed_data in feeds.items():
        _process_feed(instrument_key, feed_data)


def _process_feed(instrument_key: str, feed_data: dict) -> None:
    """Extract LTP and other fields from a single instrument feed and update TickStore."""
    try:
        # ltpc mode provides: ltp, cp (close price), volume, etc.
        ltpc = feed_data.get("ltpc") or {}
        full = feed_data.get("fullFeed") or feed_data.get("marketFF") or {}

        ltp: Optional[float] = None
        volume: float = 0.0
        open_interest: float = 0.0

        if ltpc:
            ltp = float(ltpc.get("ltp") or 0) or None

        if full:
            # fullFeed → marketFullFeed → ltpc
            mff = full.get("marketFullFeed") or {}
            ltpc2 = mff.get("ltpc") or {}
            if not ltp:
                ltp = float(ltpc2.get("ltp") or 0) or None
            volume = float(mff.get("vtt") or 0)           # volume traded today
            open_interest = float(mff.get("oi") or 0)

        if ltp is None or ltp == 0.0:
            return

        # Derive readable symbol from instrument_key (e.g. "NSE_EQ|INE040A01034" → key as-is;
        # the TickStore also indexes by symbol string, but we may not know it here).
        # The caller (or strategy engine) can resolve symbol from the instrument master.
        symbol = instrument_key  # fallback; overridden if we can resolve

        # Try to resolve a human-readable symbol from the instrument master
        try:
            from app.services.upstox.instruments import _symbol_index  # noqa: PLC0415
            for sym, key in _symbol_index.items():
                if key == instrument_key:
                    symbol = sym
                    break
        except Exception:
            pass

        tick = Tick(
            instrument_token=instrument_key,
            symbol=symbol,
            ltp=ltp,
            volume=volume,
            timestamp=datetime.now(timezone.utc),
        )
        tick_store.update(tick)

    except Exception as exc:
        logger.debug("Error processing feed for %s: %s", instrument_key, exc)


def _on_open() -> None:
    logger.info("Upstox WebSocket feed connected.")


def _on_error(error: object) -> None:
    logger.warning("Upstox WebSocket error: %s", error)


def _on_close(code: object, msg: object) -> None:
    logger.info("Upstox WebSocket closed (code=%s): %s", code, msg)


def start_feed(access_token: str, instrument_keys: list[str]) -> None:
    """Start the market data WebSocket feed in a background daemon thread.

    Safe to call multiple times — stops the existing streamer first.
    Instrument keys are in Upstox format, e.g. ``"NSE_EQ|INE040A01034"``.
    """
    global _streamer

    with _lock:
        _stop_feed_inner()

        if not instrument_keys:
            logger.warning("start_feed called with empty instrument_keys — feed not started.")
            return

        api_client = _make_api_client(access_token)
        streamer = upstox_client.MarketDataStreamerV3(
            api_client=api_client,
            instrumentKeys=instrument_keys,
            mode="ltpc",          # lightweight; upgrade to "full" if you need depth/OI
        )

        streamer.on("open", lambda: _on_open())
        streamer.on("message", _on_message)
        streamer.on("error", _on_error)
        streamer.on("close", lambda code, msg: _on_close(code, msg))

        # The SDK starts its own daemon thread inside connect()
        streamer.connect()
        _streamer = streamer
        logger.info("Market data feed started for %d instruments.", len(instrument_keys))


def stop_feed() -> None:
    with _lock:
        _stop_feed_inner()


def _stop_feed_inner() -> None:
    global _streamer
    if _streamer is not None:
        try:
            _streamer.disconnect()
        except Exception:
            pass
        _streamer = None
        logger.info("Market data feed stopped.")


def is_feed_running() -> bool:
    return _streamer is not None


def update_subscriptions(
    instrument_keys: list[str],
    mode: str = "ltpc",
) -> None:
    """Subscribe additional instrument keys on an already-running streamer."""
    if _streamer is None:
        logger.warning("update_subscriptions called but feed is not running.")
        return
    try:
        _streamer.subscribe(instrument_keys, mode)
        logger.info("Subscribed %d additional instruments.", len(instrument_keys))
    except Exception as exc:
        logger.error("Failed to subscribe instruments: %s", exc)
