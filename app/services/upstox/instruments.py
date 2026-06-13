"""Upstox instrument master — download NSE/BSE instrument list and resolve symbols."""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Upstox CDN hosts a fresh instrument JSON every trading day
_INSTRUMENT_URLS = {
    "NSE": "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
    "BSE": "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz",
}

_DATA_DIR = Path("data")
_CACHE_FILE = _DATA_DIR / "instruments.json"

# In-memory index: upper-case trading symbol → instrument_key (e.g. "NSE_EQ|INE040A01034")
_symbol_index: dict[str, str] = {}


def _cache_path() -> Path:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_FILE


async def download_instrument_master(exchanges: list[str] | None = None) -> int:
    """Download instrument JSON from Upstox CDN for the given exchanges.

    Returns the total number of equity instruments indexed.
    """
    exchanges = exchanges or ["NSE"]
    all_instruments: list[dict] = []

    async with httpx.AsyncClient(timeout=60) as client:
        for exchange in exchanges:
            url = _INSTRUMENT_URLS.get(exchange.upper())
            if not url:
                logger.warning("No instrument URL configured for exchange %s", exchange)
                continue
            try:
                logger.info("Downloading instrument master for %s …", exchange)
                resp = await client.get(url)
                resp.raise_for_status()
                instruments = json.loads(gzip.decompress(resp.content))
                all_instruments.extend(instruments)
                logger.info("Downloaded %d instruments for %s", len(instruments), exchange)
            except Exception as exc:
                logger.error("Failed to download instruments for %s: %s", exchange, exc)

    if not all_instruments:
        logger.warning("No instruments downloaded; keeping existing cache.")
        return 0

    # Persist raw list
    with open(_cache_path(), "w", encoding="utf-8") as fh:
        json.dump(all_instruments, fh)

    count = _build_index(all_instruments)
    logger.info("Instrument master indexed %d equity symbols.", count)
    return count


def load_instrument_master() -> int:
    """Load cached instrument master from disk into memory index.

    Returns the number of equity symbols indexed.  Call this on app startup
    to avoid re-downloading when the cache is fresh.
    """
    path = _cache_path()
    if not path.exists():
        logger.warning("Instrument cache not found at %s; call download_instrument_master().", path)
        return 0

    with open(path, encoding="utf-8") as fh:
        instruments = json.load(fh)

    count = _build_index(instruments)
    logger.info("Loaded %d equity symbols from instrument cache.", count)
    return count


def _build_index(instruments: list[dict]) -> int:
    """Populate _symbol_index from instrument list, keeping only equity instruments."""
    global _symbol_index
    _symbol_index = {}

    for inst in instruments:
        # Upstox JSON fields vary slightly; normalise
        segment = (inst.get("segment") or inst.get("exchange_segment") or "").upper()
        symbol = (inst.get("trading_symbol") or inst.get("tradingsymbol") or "").upper().strip()
        key = inst.get("instrument_key") or inst.get("instrument_token") or ""

        # Only index NSE/BSE cash equities (segment ends with _EQ or is NSE_EQ / BSE_EQ)
        if not symbol or not key:
            continue
        if "_EQ" not in segment and "EQ" not in (inst.get("instrument_type") or "").upper():
            continue

        # If a symbol already exists, prefer NSE over BSE
        if symbol in _symbol_index:
            existing_key: str = _symbol_index[symbol]
            if "NSE" in key:
                _symbol_index[symbol] = key
        else:
            _symbol_index[symbol] = key

    return len(_symbol_index)


def get_instrument_key(symbol: str) -> Optional[str]:
    """Resolve a trading symbol (e.g. 'RELIANCE') to an Upstox instrument key."""
    return _symbol_index.get(symbol.upper().strip())


def get_all_symbols() -> list[str]:
    """Return all indexed trading symbols."""
    return sorted(_symbol_index.keys())


def is_master_loaded() -> bool:
    return bool(_symbol_index)
