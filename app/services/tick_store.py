from __future__ import annotations

"""In-memory store for latest tick data per instrument."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Tick:
    instrument_token: str
    symbol: str
    ltp: float
    volume: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)


class TickStore:
    """Thread-safe enough for single-process Pi deployment."""

    def __init__(self) -> None:
        self._ticks: dict[str, Tick] = {}

    def update(self, tick: Tick) -> None:
        self._ticks[tick.instrument_token] = tick

    def get(self, instrument_token: str) -> Tick | None:
        return self._ticks.get(instrument_token)

    def get_by_symbol(self, symbol: str) -> Tick | None:
        for tick in self._ticks.values():
            if tick.symbol == symbol:
                return tick
        return None

    def all_ticks(self) -> list[Tick]:
        return list(self._ticks.values())


tick_store = TickStore()
