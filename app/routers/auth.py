from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.upstox.auth import UpstoxAuthService

router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="app/templates")

# In-memory OAuth state store (single-user Pi deployment)
_oauth_states: set[str] = set()


@router.get("/login")
async def login(request: Request, db: AsyncSession = Depends(get_db)):
    auth = UpstoxAuthService(db)
    if not auth.settings.upstox_api_key:
        raise HTTPException(
            status_code=503,
            detail="Upstox API key not configured. Set UPSTOX_API_KEY in .env",
        )
    url, state = auth.get_login_url()
    _oauth_states.add(state)
    request.session["oauth_state"] = state  # type: ignore[attr-defined]
    return RedirectResponse(url)


@router.get("/callback")
async def callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    if error:
        return templates.TemplateResponse(
            request,
            "auth_error.html",
            {"title": "Auth Error", "error": error},
            status_code=400,
        )

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    stored_state = request.session.get("oauth_state")  # type: ignore[attr-defined]
    if state and stored_state and state != stored_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    auth = UpstoxAuthService(db)
    try:
        token = await auth.exchange_code(code)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "auth_error.html",
            {"title": "Auth Error", "error": str(exc)},
            status_code=400,
        )

    _oauth_states.discard(state or "")
    return templates.TemplateResponse(
        request,
        "auth_success.html",
        {
            "title": "Connected",
            "user_name": token.user_name or "User",
        },
    )


@router.get("/status")
async def auth_status(db: AsyncSession = Depends(get_db)):
    auth = UpstoxAuthService(db)
    token = await auth.get_token()
    return {
        "authenticated": await auth.is_authenticated(),
        "user_name": token.user_name if token else None,
        "expires_at": token.expires_at.isoformat() if token and token.expires_at else None,
    }
