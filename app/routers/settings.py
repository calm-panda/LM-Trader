from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/settings", response_class=HTMLResponse, name="settings")
async def settings_page(request: Request):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "title": "Settings",
            "live_trading_enabled": settings.live_trading_enabled,
            "paper_starting_capital": settings.paper_starting_capital,
            "max_capital_per_trade_pct": settings.max_capital_per_trade_pct,
            "max_open_positions": settings.max_open_positions,
            "daily_loss_limit_pct": settings.daily_loss_limit_pct,
            "signal_validity_minutes": settings.signal_validity_minutes,
            "signal_generation_interval_minutes": settings.signal_generation_interval_minutes,
            "signal_confidence_threshold": settings.signal_confidence_threshold,
        },
    )
