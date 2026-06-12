from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.database import init_db
from app.routers import ai, auth, backtest, chart, dashboard, portfolio, settings, strategies


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


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
