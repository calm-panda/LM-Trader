from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_redirect_uri: str = "http://localhost:8000/auth/callback"

    deepseek_api_key: str = ""
    newsapi_key: str = ""

    database_url: str = "sqlite+aiosqlite:///./data/trading.db"
    paper_starting_capital: float = 100_000.0
    live_trading_enabled: bool = False
    secret_key: str = "change-me-in-production"

    # Risk defaults
    max_capital_per_trade_pct: float = 10.0
    max_open_positions: int = 5
    daily_loss_limit_pct: float = 2.0

    @property
    def data_dir(self) -> Path:
        return Path("data")

    @property
    def db_path(self) -> Path:
        url = self.database_url
        if url.startswith("sqlite+aiosqlite:///"):
            return Path(url.replace("sqlite+aiosqlite:///", ""))
        return Path("data/trading.db")


@lru_cache
def get_settings() -> Settings:
    return Settings()
