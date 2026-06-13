"""Indicator math: Wilder RSI, Wilder ATR, VWAP, volume SMA, Camarilla pivots.

All functions are pure — same code feeds the live engine and the backtester.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .models import Candle, Levels


def wilder_rsi(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """RSI with Wilder smoothing. Returns a list aligned to `closes`
    (None until enough data)."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out

    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def wilder_atr(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    """ATR with Wilder smoothing, aligned to `candles` (None until warm)."""
    n = len(candles)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out

    trs: List[float] = [0.0] * n
    trs[0] = candles[0].high - candles[0].low
    for i in range(1, n):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs[i] = max(h - l, abs(h - pc), abs(l - pc))

    atr = sum(trs[1 : period + 1]) / period
    out[period] = atr
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i] = atr
    return out


def vwap(candles: Sequence[Candle]) -> Optional[float]:
    """Session VWAP over the given (intraday) candles, volume-weighted typical price."""
    cum_pv, cum_v = 0.0, 0.0
    for c in candles:
        tp = (c.high + c.low + c.close) / 3.0
        cum_pv += tp * c.volume
        cum_v += c.volume
    if cum_v <= 0:
        return None
    return cum_pv / cum_v


def sma(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def camarilla(prev_high: float, prev_low: float, prev_close: float) -> Levels:
    """Standard Camarilla pivots. H5 uses the (H/L)*C extension; L5 mirrors it."""
    h, l, c = prev_high, prev_low, prev_close
    r = h - l
    h5 = (h / l) * c if l else c
    levels = Levels(
        pivot=(h + l + c) / 3.0,
        h1=c + r * 1.1 / 12.0,
        h2=c + r * 1.1 / 6.0,
        h3=c + r * 1.1 / 4.0,
        h4=c + r * 1.1 / 2.0,
        h5=h5,
        l1=c - r * 1.1 / 12.0,
        l2=c - r * 1.1 / 6.0,
        l3=c - r * 1.1 / 4.0,
        l4=c - r * 1.1 / 2.0,
        l5=c - (h5 - c),
    )
    return levels


def opening_range(candles: Sequence[Candle], session_open, minutes: int = 15):
    """(ORH, ORL, final) from candles whose ts falls inside the opening window.

    `final` is True once a candle at/after window end exists (range locked).
    """
    orh, orl, final = None, None, False
    for c in candles:
        mins = (c.ts - session_open).total_seconds() / 60.0
        if mins < 0:
            continue
        if mins < minutes:
            orh = c.high if orh is None else max(orh, c.high)
            orl = c.low if orl is None else min(orl, c.low)
        else:
            final = orh is not None
            break
    else:
        final = False
    return orh, orl, final
