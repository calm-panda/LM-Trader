from sqlalchemy import func, select

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.paper_order import PaperOrder
from app.models.strategy import Strategy
from app.services.paper_engine import paper_engine
from app.services.tick_store import tick_store
from app.services.upstox.auth import UpstoxAuthService

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard", response_class=HTMLResponse, name="dashboard")
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    auth = UpstoxAuthService(db)
    is_authenticated = await auth.is_authenticated()

    # Active strategies count
    result = await db.execute(
        select(func.count()).select_from(Strategy).where(Strategy.enabled.is_(True))
    )
    active_count = result.scalar_one() or 0

    # Paper portfolio snapshot
    ltp_map = {t.symbol: t.ltp for t in tick_store.all_ticks() if t.ltp > 0}
    snap = paper_engine.snapshot(ltp_map)

    # Last 5 trades
    trades_result = await db.execute(
        select(PaperOrder).order_by(PaperOrder.filled_at.desc()).limit(5)
    )
    recent_trades = [
        {
            "symbol": o.symbol,
            "side": o.side,
            "qty": o.quantity,
            "fill_price": round(o.fill_price or 0, 2),
            "notes": o.notes,
            "filled_at": o.filled_at.isoformat() if o.filled_at else None,
        }
        for o in trades_result.scalars()
    ]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "title": "Dashboard",
            "is_authenticated": is_authenticated,
            "live_trading_enabled": False,
            "active_strategies": active_count,
            "snap": snap,
            "recent_trades": recent_trades,
        },
    )
