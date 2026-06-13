"""EMA Crossover strategy.

Signal logic
------------
BUY  : fast EMA crosses **above** slow EMA (previous bar below, current bar above).
SELL : fast EMA crosses **below** slow EMA.
HOLD : no crossover.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from .base import BaseStrategy, Signal


class EMACrossover(BaseStrategy):
    """Trend-following crossover on two exponential moving averages."""

    NAME = "EMA_CROSSOVER"

    @classmethod
    def default_params(cls) -> dict:
        return {"fast_ema": 9, "slow_ema": 21}

    def on_tick(self, symbol: str, df: pd.DataFrame) -> Signal:
        slow = self.params.get("slow_ema", 21)
        fast = self.params.get("fast_ema", 9)

        if not self._check_min_rows(df, slow + 2, symbol):
            return Signal.HOLD

        close = df["close"]
        ema_fast = ta.ema(close, length=fast)
        ema_slow = ta.ema(close, length=slow)

        if ema_fast is None or ema_slow is None:
            return Signal.HOLD

        prev_fast, curr_fast = float(ema_fast.iloc[-2]), float(ema_fast.iloc[-1])
        prev_slow, curr_slow = float(ema_slow.iloc[-2]), float(ema_slow.iloc[-1])

        if any(v != v for v in (prev_fast, curr_fast, prev_slow, curr_slow)):  # NaN check
            return Signal.HOLD

        if prev_fast < prev_slow and curr_fast > curr_slow:
            return Signal.BUY
        if prev_fast > prev_slow and curr_fast < curr_slow:
            return Signal.SELL
        return Signal.HOLD
