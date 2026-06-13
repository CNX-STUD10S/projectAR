"""Shared dataclasses used by live engine, paper broker and backtester."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, List


@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Levels:
    """Camarilla pivots computed from previous day's H/L/C."""
    pivot: float
    h1: float; h2: float; h3: float; h4: float; h5: float
    l1: float; l2: float; l3: float; l4: float; l5: float

    def as_dict(self) -> dict:
        return {k: round(v, 2) for k, v in asdict(self).items()}


@dataclass
class Check:
    """One entry condition with its pass/fail state (shown on dashboard)."""
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Signal:
    symbol: str
    setup: str                  # breakout_long | reversal_long | stock_momentum
    side: str                   # LONG
    entry_ref: float            # reference price at signal time
    stop: float
    target1: Optional[float]
    target2: Optional[float]    # None = pure trail after T1
    hedge_otm_pct: float
    reasons: List[str] = field(default_factory=list)
    ts: Optional[datetime] = None


@dataclass
class Position:
    symbol: str
    setup: str
    fut_tradingsymbol: str
    lots: int
    qty: int                    # lots * lot_size
    entry_price: float
    stop: float
    target1: Optional[float]
    target2: Optional[float]
    opened_at: datetime
    hedge_tradingsymbol: Optional[str] = None
    hedge_qty: int = 0
    hedge_entry: float = 0.0
    t1_done: bool = False
    trail_stop: Optional[float] = None
    sl_order_id: Optional[str] = None
    realized_pnl: float = 0.0
    closed: bool = False

    @property
    def effective_stop(self) -> float:
        return self.trail_stop if self.trail_stop is not None else self.stop
