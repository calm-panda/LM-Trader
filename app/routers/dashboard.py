from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.paper_order import PaperOrder
from app.models.signal import Signal
from app.models.strategy import Strategy
from app.services.paper_engine import paper_engine
from app.services.tick_store import tick_store
from app.services.upstox.auth import UpstoxAuthService
from app.templating import risk_status_from_paper, templates, ui_context

router = APIRouter()


@router.get("/dashboard", response_class=HTMLResponse, name="dashboard")
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    auth = UpstoxAuthService(db)
    is_authenticated = await auth.is_authenticated()

    strat_result = await db.execute(
        select(Strategy).where(Strategy.enabled.is_(True)).order_by(Strategy.name)
    )
    active_strats = [
        {
            "id": s.id,
            "name": s.name,
            "symbols": s.symbols,
            "timeframe": s.timeframe,
            "backtest_passed": s.backtest_passed,
        }
        for s in strat_result.scalars()
    ]

    ltp_map = {t.symbol: t.ltp for t in tick_store.all_ticks() if t.ltp > 0}
    snap = paper_engine.snapshot(ltp_map)

    watchlist_quotes = []
    for sym in settings.watchlist_symbols[:8]:
        tick = tick_store.get_by_symbol(sym)
        watchlist_quotes.append({
            "symbol": sym,
            "ltp": round(tick.ltp, 2) if tick and tick.ltp else None,
        })

    now = datetime.now(timezone.utc)
    pending_result = await db.execute(
        select(Signal)
        .where(Signal.status == "pending", Signal.expires_at > now)
        .order_by(Signal.generated_at.desc())
        .limit(3)
    )
    pending = []
    for s in pending_result.scalars():
        pending.append({
            "id": s.id,
            "symbol": s.symbol,
            "direction": s.direction,
            "confidence": s.confidence,
            "entry": s.entry,
            "expires_at": s.expires_at,
        })

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
        ui_context(
            title="Dashboard",
            is_authenticated=is_authenticated,
            active_strategies=len(active_strats),
            active_strats=active_strats,
            snap=snap,
            watchlist_quotes=watchlist_quotes,
            pending_signals=pending,
            recent_trades=recent_trades,
            risk=risk_status_from_paper(),
        ),
    )
