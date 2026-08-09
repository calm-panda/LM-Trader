from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.templating import templates, ui_context

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse, name="settings")
async def settings_page(request: Request):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        ui_context(
            title="Settings",
            signal_validity_minutes=settings.signal_validity_minutes,
            signal_generation_interval_minutes=settings.signal_generation_interval_minutes,
            signal_confidence_threshold=settings.signal_confidence_threshold,
            upstox_redirect_uri=settings.upstox_redirect_uri,
        ),
    )
