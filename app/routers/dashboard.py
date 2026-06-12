from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.upstox.auth import UpstoxAuthService

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard", response_class=HTMLResponse, name="dashboard")
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    auth = UpstoxAuthService(db)
    is_authenticated = await auth.is_authenticated()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "title": "Dashboard",
            "is_authenticated": is_authenticated,
            "live_trading_enabled": False,
        },
    )
