"""Tick → N-minute candle aggregation for the live engine."""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from .models import Candle
from .util import floor_to_candle


class CandleBuilder:
    """Aggregates ticks into fixed-interval candles.

    Kite ticks carry `volume_traded` = cumulative day volume, so candle
    volume = cumulative at candle end - cumulative at candle start.
    add_tick() returns the just-CLOSED candle when a new bucket starts.
    """

    def __init__(self, minutes: int = 5):
        self.minutes = minutes
        self.current: Optional[Candle] = None
        self._vol_base = 0.0
        self._last_cum_vol = 0.0

    def add_tick(self, ts: datetime, price: float,
                 cum_volume: Optional[float]) -> Optional[Candle]:
        bucket = floor_to_candle(ts, self.minutes)
        cum = float(cum_volume) if cum_volume is not None else self._last_cum_vol
        closed: Optional[Candle] = None

        if self.current is None:
            self.current = Candle(ts=bucket, open=price, high=price,
                                  low=price, close=price, volume=0.0)
            self._vol_base = cum
        elif bucket > self.current.ts:
            closed = self._finalize(cum_at_close=self._last_cum_vol)
            self.current = Candle(ts=bucket, open=price, high=price,
                                  low=price, close=price, volume=0.0)
            self._vol_base = self._last_cum_vol

        c = self.current
        c.high = max(c.high, price)
        c.low = min(c.low, price)
        c.close = price
        self._last_cum_vol = max(cum, self._last_cum_vol)  # cumulative never decreases
        c.volume = max(0.0, self._last_cum_vol - self._vol_base)
        return closed

    def _finalize(self, cum_at_close: float) -> Candle:
        c = self.current
        c.volume = max(0.0, cum_at_close - self._vol_base)
        return c

    def force_close(self) -> Optional[Candle]:
        """Close the in-progress candle (EOD)."""
        if self.current is None:
            return None
        closed = self._finalize(self._last_cum_vol)
        self.current = None
        return closed
