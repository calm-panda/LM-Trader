"""Shared UI partials — risk banner, More menu."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templating import risk_status_from_paper, templates, ui_context

router = APIRouter()


@router.get("/partials/risk-banner", response_class=HTMLResponse)
async def risk_banner_partial(request: Request):
    return templates.TemplateResponse(
        request,
        "partials/risk_banner.html",
        ui_context(risk=risk_status_from_paper()),
    )


@router.get("/more", response_class=HTMLResponse, name="more")
async def more_page(request: Request):
    return templates.TemplateResponse(
        request,
        "more.html",
        ui_context(title="More"),
    )
