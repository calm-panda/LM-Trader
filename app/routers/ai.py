from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/ai/chat", response_class=HTMLResponse, name="ai_chat")
async def ai_chat_page(request: Request):
    return templates.TemplateResponse(
        request,
        "ai_chat.html",
        {"title": "AI Chat"},
    )
