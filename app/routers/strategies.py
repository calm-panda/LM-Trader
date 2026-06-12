from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/strategies", response_class=HTMLResponse, name="strategies")
async def strategies_page(request: Request):
    return templates.TemplateResponse(
        request,
        "strategies.html",
        {"title": "Strategies", "strategies": []},
    )
