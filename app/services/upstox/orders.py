"""Upstox order & portfolio REST helpers (live trading).

All calls require a valid access_token. Order placement is only invoked from
the human confirmation gate — never from the automated paper pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.upstox.com/v2"
_ORDER_URL = f"{_BASE}/order/place"
_FUNDS_URL = f"{_BASE}/user/get-funds-and-margin"
_POSITIONS_URL = f"{_BASE}/portfolio/short-term-positions"
_HOLDINGS_URL = f"{_BASE}/portfolio/long-term-holdings"


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def place_market_order(
    access_token: str,
    instrument_key: str,
    side: str,
    quantity: int,
    product: str = "I",
) -> str:
    """Place a market order. Returns Upstox order_id.

    product: "I" = intraday (MIS), "D" = delivery (CNC).
    side: "BUY" or "SELL".
    """
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"Invalid side: {side}")
    if quantity < 1:
        raise ValueError("quantity must be >= 1")

    payload = {
        "quantity": quantity,
        "product": product,
        "validity": "DAY",
        "price": 0,
        "tag": "lm-trader",
        "instrument_token": instrument_key,
        "order_type": "MARKET",
        "transaction_type": side,
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(_ORDER_URL, headers=_headers(access_token), json=payload)
        if resp.status_code >= 400:
            logger.error("place_market_order failed: %s %s", resp.status_code, resp.text)
            resp.raise_for_status()
        data = resp.json()

    order_id = (data.get("data") or {}).get("order_id")
    if not order_id:
        raise RuntimeError(f"Upstox order response missing order_id: {data}")
    logger.info("Placed %s %s qty=%d → order_id=%s", side, instrument_key, quantity, order_id)
    return str(order_id)


async def get_funds(access_token: str) -> float:
    """Return available equity cash (₹) from Upstox funds-and-margin."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            _FUNDS_URL,
            headers=_headers(access_token),
            params={"segment": "SEC"},
        )
        resp.raise_for_status()
        data = resp.json().get("data") or {}

    equity = data.get("equity") or data
    # Prefer available_margin / available_cash depending on API shape
    for key in ("available_margin", "available_cash", "cash_balance"):
        val = equity.get(key) if isinstance(equity, dict) else None
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass

    # Nested commodity/equity fallback
    if isinstance(equity, dict):
        for nested_key in ("available_margin", "available_cash"):
            for section in equity.values():
                if isinstance(section, dict) and section.get(nested_key) is not None:
                    try:
                        return float(section[nested_key])
                    except (TypeError, ValueError):
                        continue

    logger.warning("get_funds: could not parse available cash from %s", data)
    return 0.0


async def get_positions(access_token: str) -> list[dict[str, Any]]:
    """Return short-term (intraday) positions."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(_POSITIONS_URL, headers=_headers(access_token))
        resp.raise_for_status()
        data = resp.json().get("data") or []
    return list(data) if isinstance(data, list) else []


async def get_holdings(access_token: str) -> list[dict[str, Any]]:
    """Return long-term holdings."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(_HOLDINGS_URL, headers=_headers(access_token))
        resp.raise_for_status()
        data = resp.json().get("data") or []
    return list(data) if isinstance(data, list) else []


def estimate_live_total_value(
    funds: float,
    positions: list[dict[str, Any]],
    holdings: list[dict[str, Any]],
) -> float:
    """Rough portfolio total = cash + mark-to-market of positions/holdings."""
    total = funds
    for p in positions:
        try:
            qty = float(p.get("quantity") or p.get("net_quantity") or 0)
            ltp = float(p.get("last_price") or p.get("ltp") or 0)
            total += qty * ltp
        except (TypeError, ValueError):
            continue
    for h in holdings:
        try:
            qty = float(h.get("quantity") or 0)
            ltp = float(h.get("last_price") or h.get("ltp") or 0)
            total += qty * ltp
        except (TypeError, ValueError):
            continue
    return total
