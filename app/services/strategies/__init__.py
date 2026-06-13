"""Strategy registry — maps class_name strings stored in the DB to strategy classes."""

from __future__ import annotations

from typing import Type

from .base import BaseStrategy, Signal
from .ema_crossover import EMACrossover
from .rsi_mean_reversion import RSIMeanReversion
from .supertrend import Supertrend

REGISTRY: dict[str, Type[BaseStrategy]] = {
    EMACrossover.NAME: EMACrossover,
    RSIMeanReversion.NAME: RSIMeanReversion,
    Supertrend.NAME: Supertrend,
}

# ── Default seed rows for the DB ──────────────────────────────────────────────

DEFAULT_STRATEGIES = [
    {
        "name": "EMA Crossover 9/21",
        "class_name": EMACrossover.NAME,
        "symbols": "RELIANCE,TCS,INFY",
        "timeframe": "5m",
        "params": '{"fast_ema": 9, "slow_ema": 21}',
    },
    {
        "name": "RSI Mean Reversion",
        "class_name": RSIMeanReversion.NAME,
        "symbols": "HDFCBANK,ICICIBANK,SBIN",
        "timeframe": "15m",
        "params": '{"rsi_period": 14, "oversold": 30, "overbought": 70}',
    },
    {
        "name": "Supertrend Breakout",
        "class_name": Supertrend.NAME,
        "symbols": "WIPRO,AXISBANK,KOTAKBANK",
        "timeframe": "15m",
        "params": '{"period": 7, "multiplier": 3.0}',
    },
]

__all__ = [
    "BaseStrategy",
    "Signal",
    "EMACrossover",
    "RSIMeanReversion",
    "Supertrend",
    "REGISTRY",
    "DEFAULT_STRATEGIES",
]
