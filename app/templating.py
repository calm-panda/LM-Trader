"""Shared Jinja2 templates + UI context helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.templating import Jinja2Templates

from app.config import get_settings

templates = Jinja2Templates(directory="app/templates")

_IST = timezone(timedelta(hours=5, minutes=30))


def ui_context(**extra: Any) -> dict[str, Any]:
    """Common template vars: mode badge, watchlist, risk knobs."""
    s = get_settings()
    ctx = {
        "live_trading_enabled": s.live_trading_enabled,
        "live_auto_squareoff": s.live_auto_squareoff,
        "watchlist": s.watchlist_symbols,
        "daily_loss_limit_pct": s.daily_loss_limit_pct,
        "max_open_positions": s.max_open_positions,
        "max_capital_per_trade_pct": s.max_capital_per_trade_pct,
        "paper_starting_capital": s.paper_starting_capital,
        "deepseek_configured": bool(s.deepseek_api_key.strip()),
        "newsapi_configured": bool(s.newsapi_key.strip()),
        "upstox_configured": bool(s.upstox_api_key.strip() and s.upstox_api_secret.strip()),
        "alert_webhook_configured": bool(s.alert_webhook_url.strip()),
    }
    ctx.update(extra)
    return ctx


def risk_status_from_paper() -> dict[str, Any]:
    """Build risk banner payload from paper engine day P&L + square-off clock."""
    from app.services.paper_engine import paper_engine

    s = get_settings()
    snap = paper_engine.snapshot()
    start = paper_engine.day_start_value or s.paper_starting_capital
    day_pnl_pct = 0.0
    if start > 0:
        day_pnl_pct = (snap["total_value"] - start) / start * 100

    limit = s.daily_loss_limit_pct
    used_pct = 0.0
    if limit > 0 and day_pnl_pct < 0:
        used_pct = min(100.0, abs(day_pnl_pct) / limit * 100)

    now = datetime.now(_IST)
    square_off = now.replace(hour=15, minute=15, second=0, microsecond=0)
    mins_to_so: int | None = None
    approaching_so = False
    if now.weekday() < 5 and now.hour >= 9:
        delta = (square_off - now).total_seconds() / 60
        if 0 < delta <= 60:
            mins_to_so = int(delta)
            approaching_so = delta <= 30

    show = used_pct >= 80 or approaching_so
    return {
        "show": show,
        "day_pnl_pct": round(day_pnl_pct, 2),
        "loss_used_pct": round(used_pct, 1),
        "daily_loss_limit_pct": limit,
        "mins_to_squareoff": mins_to_so,
        "approaching_squareoff": approaching_so,
        "open_positions": snap["open_positions"],
    }
