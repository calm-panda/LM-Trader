import logging
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session


async def _migrate_trades_columns(conn) -> None:
    """Add signal_id / upstox_order_id to trades if missing (no Alembic)."""
    result = await conn.execute(text("PRAGMA table_info(trades)"))
    cols = {row[1] for row in result.fetchall()}
    if "signal_id" not in cols:
        await conn.execute(text("ALTER TABLE trades ADD COLUMN signal_id INTEGER"))
        logger.info("Migrated trades: added signal_id")
    if "upstox_order_id" not in cols:
        await conn.execute(text("ALTER TABLE trades ADD COLUMN upstox_order_id VARCHAR(64)"))
        logger.info("Migrated trades: added upstox_order_id")


async def init_db() -> None:
    from app.models import (  # noqa: F401 — register models
        auth_token,
        ohlcv,
        paper_order,
        portfolio,
        signal,
        strategy,
        trade,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Lightweight column migrations for existing SQLite DBs
        try:
            await _migrate_trades_columns(conn)
        except Exception:
            logger.exception("trades column migration skipped/failed")
