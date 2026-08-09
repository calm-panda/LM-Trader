from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TradeJournal(Base):
    """Audit log for every AI signal outcome (confirmed / rejected / expired).

    Confirmed rows may later receive outcome_pnl / achieved_rr when the
    linked live Trade closes.
    """

    __tablename__ = "trade_journal"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    signal_id: Mapped[Optional[int]] = mapped_column(Integer)
    trade_id: Mapped[Optional[int]] = mapped_column(Integer)
    order_id: Mapped[Optional[str]] = mapped_column(String(64))  # Upstox order id
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # confirmed|rejected|expired
    symbol: Mapped[Optional[str]] = mapped_column(String(32))
    direction: Mapped[Optional[str]] = mapped_column(String(8))
    executed_price: Mapped[Optional[float]] = mapped_column(Float)
    quantity: Mapped[Optional[int]] = mapped_column(Integer)
    planned_rr: Mapped[Optional[float]] = mapped_column(Float)
    achieved_rr: Mapped[Optional[float]] = mapped_column(Float)
    outcome_pnl: Mapped[Optional[float]] = mapped_column(Float)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
