"""RSI Mean-Reversion strategy.

Signal logic
------------
BUY  : RSI crosses **above** oversold threshold (default 30) — bounce from oversold.
SELL : RSI crosses **above** overbought threshold (default 70) — peak exhaustion.
HOLD : RSI stays within bands or crosses the other way.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from .base import BaseStrategy, Signal


class RSIMeanReversion(BaseStrategy):
    """Counter-trend strategy based on RSI extremes."""

    NAME = "RSI_MEAN_REVERSION"

    @classmethod
    def default_params(cls) -> dict:
        return {"rsi_period": 14, "oversold": 30, "overbought": 70}

    def on_tick(self, symbol: str, df: pd.DataFrame) -> Signal:
        period = self.params.get("rsi_period", 14)
        oversold = self.params.get("oversold", 30)
        overbought = self.params.get("overbought", 70)

        if not self._check_min_rows(df, period + 2, symbol):
            return Signal.HOLD

        rsi = ta.rsi(df["close"], length=period)
        if rsi is None:
            return Signal.HOLD

        valid = rsi.dropna()
        if len(valid) < 2:
            return Signal.HOLD

        prev, curr = float(valid.iloc[-2]), float(valid.iloc[-1])

        # Cross from below 30 → recovery BUY
        if prev < oversold and curr >= oversold:
            return Signal.BUY
        # Cross from below 70 → overbought SELL
        if prev < overbought and curr >= overbought:
            return Signal.SELL
        return Signal.HOLD
