from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Signal(Base):
    """AI-generated trading recommendation awaiting human confirm/reject."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="5m")
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)  # BUY | SELL
    entry: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    target: Mapped[float] = mapped_column(Float, nullable=False)
    risk_reward_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)  # 0-1
    indicators_snapshot: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    indicators_triggered: Mapped[Optional[str]] = mapped_column(Text)  # JSON list
    reasoning: Mapped[str] = mapped_column(Text, nullable=False, default="")
    invalidation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )  # pending | confirmed | rejected | expired
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model_used: Mapped[str] = mapped_column(String(64), nullable=False, default="deepseek-chat")
