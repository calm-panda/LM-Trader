from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/chart/{symbol}", response_class=HTMLResponse, name="chart")
async def chart_page(request: Request, symbol: str):
    return templates.TemplateResponse(
        request,
        "chart.html",
        {"title": f"Chart — {symbol.upper()}", "symbol": symbol.upper()},
    )
