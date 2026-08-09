from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.chart import VALID_TIMEFRAMES, _is_market_open, get_available_symbols, get_chart_data
from app.templating import templates, ui_context

router = APIRouter()


# NOTE: /chart/data and /chart/ltp/{symbol} must be declared BEFORE /chart/{symbol}
# so FastAPI matches them as exact paths before treating the segment as a {symbol} parameter.


@router.get("/chart/data", response_class=JSONResponse)
async def chart_data(
    symbol: str = Query("RELIANCE"),
    tf: str = Query("5m"),
    limit: int = Query(500, ge=50, le=2000),
    db: AsyncSession = Depends(get_db),
):
    """Return OHLCV candles + indicator arrays in Lightweight Charts format."""
    data = await get_chart_data(db, symbol, tf, limit)
    return JSONResponse(content=data)


@router.get("/chart/ltp/{symbol}", response_class=HTMLResponse)
async def chart_ltp(symbol: str):
    """HTMX fragment: current LTP for symbol from the in-memory tick store.

    Returns a tiny HTML snippet — HTMX replaces the LTP badge innerHTML with it.
    """
    from app.services.tick_store import tick_store  # noqa: PLC0415

    symbol = symbol.upper()
    tick = tick_store.get_by_symbol(symbol)

    if tick:
        ltp = f"₹{tick.ltp:,.2f}"
        ts = tick.timestamp.strftime("%H:%M:%S")
        html = f'<span class="ltp-value">{ltp}</span> <span class="ltp-time">{ts}</span>'
    else:
        html = '<span class="ltp-value">—</span>'

    return HTMLResponse(content=html)


@router.get("/chart/symbols", response_class=JSONResponse)
async def chart_symbols(db: AsyncSession = Depends(get_db)):
    """Return list of symbols that have OHLCV data."""
    symbols = await get_available_symbols(db)
    return JSONResponse(content={"symbols": symbols})


@router.get("/chart/{symbol}", response_class=HTMLResponse, name="chart")
async def chart_page(
    request: Request,
    symbol: str,
    tf: str = Query("5m"),
    db: AsyncSession = Depends(get_db),
):
    symbol = symbol.upper()
    symbols = await get_available_symbols(db)
    return templates.TemplateResponse(
        request,
        "chart.html",
        ui_context(
            title=f"{symbol} — Chart",
            symbol=symbol,
            timeframe=tf,
            symbols=symbols,
            valid_timeframes=sorted(VALID_TIMEFRAMES),
            market_open=_is_market_open(),
        ),
    )
