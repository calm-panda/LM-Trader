import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.database import async_session, init_db
from app.routers import ai, auth, backtest, chart, dashboard, portfolio, settings, signals, strategies

logger = logging.getLogger(__name__)


async def _start_data_pipeline() -> None:
    """Download instrument master, seed OHLCV, and start WebSocket feed.

    Runs as a background task so the server stays responsive even while
    the potentially slow OHLCV bootstrap is in progress.
    """
    import asyncio

    from sqlalchemy import select

    from app.models.auth_token import AuthToken
    from app.services.upstox.feed import start_feed
    from app.services.upstox.history import bootstrap_ohlcv
    from app.services.upstox.instruments import (
        download_instrument_master,
        get_instrument_key,
        is_master_loaded,
        load_instrument_master,
    )

    cfg = get_settings()

    # ── 1. Instrument master ─────────────────────────────────────────────────
    if not is_master_loaded():
        loaded = load_instrument_master()          # fast path: from disk cache
        if not loaded:
            await download_instrument_master()     # slower: download from Upstox CDN

    # ── 2. Resolve watchlist → instrument keys ───────────────────────────────
    symbols = cfg.watchlist_symbols
    sym_key_pairs: list[tuple[str, str]] = []
    missing: list[str] = []

    for sym in symbols:
        key = get_instrument_key(sym)
        if key:
            sym_key_pairs.append((sym, key))
        else:
            missing.append(sym)

    if missing:
        logger.warning(
            "Could not resolve instrument keys for: %s — "
            "they may be absent from the instrument master or in a different exchange.",
            ", ".join(missing),
        )

    if not sym_key_pairs:
        logger.warning("No resolvable symbols in watchlist — skipping OHLCV bootstrap and feed.")
        return

    # ── 3. Retrieve stored access token ─────────────────────────────────────
    access_token: str | None = None
    async with async_session() as db:
        result = await db.execute(
            select(AuthToken).order_by(AuthToken.updated_at.desc()).limit(1)
        )
        token_row = result.scalar_one_or_none()
        if token_row:
            access_token = token_row.access_token

    if not access_token:
        logger.warning(
            "No Upstox access token found — OHLCV bootstrap and live feed skipped. "
            "Log in via /auth/login first."
        )
        return

    # ── 4. OHLCV bootstrap (runs in background — server stays up during this) ─
    logger.info("Starting OHLCV bootstrap for %d symbols …", len(sym_key_pairs))
    asyncio.create_task(bootstrap_ohlcv(access_token, sym_key_pairs))

    # ── 5. WebSocket live feed ───────────────────────────────────────────────
    instrument_keys = [key for _, key in sym_key_pairs]
    start_feed(access_token, instrument_keys)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    import asyncio
    asyncio.create_task(_start_data_pipeline())

    # Strategy scheduler + paper engine initialisation
    from app.services.paper_engine import paper_engine
    from app.services.scheduler import start_scheduler
    await paper_engine.restore_from_db()
    start_scheduler()

    yield

    # Shutdown: stop the WebSocket feed and scheduler cleanly
    from app.services.upstox.feed import stop_feed
    from app.services.scheduler import stop_scheduler
    stop_feed()
    stop_scheduler()


def create_app() -> FastAPI:
    settings_obj = get_settings()
    app = FastAPI(title="LM Trader", version="0.1.0", lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=settings_obj.secret_key)
    app.mount("/static", StaticFiles(directory="static"), name="static")

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(chart.router)
    app.include_router(strategies.router)
    app.include_router(backtest.router)
    app.include_router(signals.router)
    app.include_router(portfolio.router)
    app.include_router(ai.router)
    app.include_router(settings.router)

    @app.get("/")
    async def root():
        return RedirectResponse(url="/dashboard", status_code=302)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
