from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/backtest", response_class=HTMLResponse, name="backtest")
async def backtest_page(request: Request):
    return templates.TemplateResponse(
        request,
        "backtest.html",
        {"title": "Backtest"},
    )
