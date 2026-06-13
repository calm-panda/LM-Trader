"""BaseStrategy abstract class and Signal enum for all trading strategies."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from enum import Enum

import pandas as pd

logger = logging.getLogger(__name__)


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class BaseStrategy(ABC):
    """All strategies inherit from this class.

    Subclasses must set ``NAME`` (unique slug matching ``Strategy.class_name``
    in the DB) and implement ``on_tick()``.
    """

    NAME: str = ""

    def __init__(self, params: dict | None = None) -> None:
        self.params: dict = params or self.default_params()

    # ── Subclass contract ──────────────────────────────────────────────────────

    @abstractmethod
    def on_tick(self, symbol: str, df: pd.DataFrame) -> Signal:
        """Receive the latest OHLCV candles and return a trading signal.

        Args:
            symbol: Trading symbol (e.g. "RELIANCE").
            df: DataFrame with columns [open, high, low, close, volume],
                sorted ascending by time.  The last row is the most recent
                completed candle.

        Returns:
            Signal.BUY, Signal.SELL, or Signal.HOLD.
        """

    @classmethod
    def default_params(cls) -> dict:
        """Return default hyper-parameters for this strategy."""
        return {}

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _check_min_rows(self, df: pd.DataFrame, n: int, label: str = "") -> bool:
        if len(df) < n:
            logger.debug(
                "%s[%s]: need ≥%d rows, got %d — returning HOLD",
                self.__class__.__name__,
                label,
                n,
                len(df),
            )
            return False
        return True
