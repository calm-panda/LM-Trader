"""Supertrend breakout strategy.

Signal logic
------------
BUY  : Supertrend direction flips from −1 (downtrend) to +1 (uptrend).
SELL : Supertrend direction flips from +1 (uptrend) to −1 (downtrend).
HOLD : Direction unchanged.

pandas-ta supertrend() returns columns:
  SUPERT_<len>_<mult>   — Supertrend line value
  SUPERTd_<len>_<mult>  — Direction: +1 uptrend, −1 downtrend
  SUPERTl_<len>_<mult>  — Long line (valid when uptrend)
  SUPERTs_<len>_<mult>  — Short line (valid when downtrend)
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from .base import BaseStrategy, Signal


class Supertrend(BaseStrategy):
    """Trend-following strategy using the ATR-based Supertrend indicator."""

    NAME = "SUPERTREND"

    @classmethod
    def default_params(cls) -> dict:
        return {"period": 7, "multiplier": 3.0}

    def on_tick(self, symbol: str, df: pd.DataFrame) -> Signal:
        period = int(self.params.get("period", 7))
        multiplier = float(self.params.get("multiplier", 3.0))

        if not self._check_min_rows(df, period * 2 + 2, symbol):
            return Signal.HOLD

        st = ta.supertrend(
            df["high"],
            df["low"],
            df["close"],
            length=period,
            multiplier=multiplier,
        )
        if st is None or st.empty:
            return Signal.HOLD

        dir_cols = [c for c in st.columns if c.startswith("SUPERTd")]
        if not dir_cols:
            return Signal.HOLD

        direction = st[dir_cols[0]].dropna()
        if len(direction) < 2:
            return Signal.HOLD

        prev_dir = int(direction.iloc[-2])
        curr_dir = int(direction.iloc[-1])

        if prev_dir == -1 and curr_dir == 1:
            return Signal.BUY
        if prev_dir == 1 and curr_dir == -1:
            return Signal.SELL
        return Signal.HOLD
