"""Entry rules + no-trade filters for the Camarilla + RSI system.

This module is pure logic over a Snapshot — the live engine and the
backtester both call `evaluate()`, so behaviour is identical in test
and in production.

Plan encoding
-------------
breakout_long  : close > H4, close > ORH (locked), RSI > 60,
                 ATR > min, volume expansion. SL below H3, T1 = H5, then trail.
reversal_long  : price in L3 ± band, RSI was < 35 within lookback and rose
                 2 consecutive candles, ATR > min. SL below L4,
                 T1 = pivot, T2 = H3.
stock_momentum : above VWAP (if required), above ORH, RSI > 60,
                 volume > mult x avg (if set), outperforming Nifty (if set).
                 SL = ORL/VWAP (config), T1 = entry + R-multiple, then trail.

No-trade conditions (the plan's hard filters):
  - RSI pinned 40–55 through the first hour
  - first 30-minute range < 0.25%
  - ATR below threshold (encoded in each setup's checks)
  - price oscillating between H3 and L3 (blocks breakout entries)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from .models import Candle, Check, Levels, Signal


@dataclass
class Snapshot:
    """Everything the rules need, computed by engine/backtester per candle close."""
    symbol: str
    ts: datetime
    candle: Candle                       # last completed candle
    ltp: float
    rsi: Optional[float]
    rsi_prev1: Optional[float]
    rsi_prev2: Optional[float]
    rsi_min_lookback: Optional[float]    # min RSI over reversal lookback window
    atr: Optional[float]
    vwap: Optional[float]
    vol_sma: Optional[float]
    levels: Levels
    orh: Optional[float]
    orl: Optional[float]
    or_final: bool
    prev_close: float
    pct_change: float                    # vs prev close
    index_pct_change: Optional[float]    # Nifty benchmark, for relative strength
    minutes_since_open: float
    blocked_day: bool = False            # first-30-min range filter tripped
    rsi_band_blocked: bool = False       # RSI 40–55 first-hour filter active
    choppy: bool = False                 # H3..L3 oscillation
    block_reasons: List[str] = field(default_factory=list)


# ---------------------------------------------------------------- no-trade --

def first30_blocked(day_open_price: float, high_30: float, low_30: float,
                    min_range_pct: float) -> bool:
    """True => whole day is a no-trade day (range too compressed)."""
    if not day_open_price:
        return False
    rng_pct = (high_30 - low_30) / day_open_price * 100.0
    return rng_pct < min_range_pct


def rsi_band_block(rsi_history: Sequence[Optional[float]],
                   minutes_since_open: float, cfg: dict) -> bool:
    """RSI stuck in 40–55 through the first hour => stand aside.

    Before the hour completes: blocked while ALL observed RSI values sit in band.
    After the hour: stays blocked until RSI exits the band (if unblock enabled).
    """
    lo = cfg.get("rsi_chop_low", 40)
    hi = cfg.get("rsi_chop_high", 55)
    first_hour = cfg.get("first_hour_minutes", 60)
    vals = [v for v in rsi_history if v is not None]
    if not vals:
        return False
    if minutes_since_open <= first_hour:
        return all(lo <= v <= hi for v in vals)
    # past the first hour
    in_band_first_hour = all(lo <= v <= hi for v in vals[: max(1, int(first_hour // 5))])
    if not in_band_first_hour:
        return False
    if cfg.get("unblock_on_rsi_break", True):
        return lo <= vals[-1] <= hi   # unblocks once RSI escapes
    return True


def detect_chop(candles: Sequence[Candle], levels: Levels, cfg: dict) -> bool:
    """Oscillation between H3 and L3 without conviction: last N candles all
    closed inside [L3, H3] and price crossed the central pivot >= k times."""
    n = cfg.get("chop_lookback_candles", 8)
    k = cfg.get("chop_min_pivot_crossings", 2)
    if len(candles) < n:
        return False
    window = candles[-n:]
    if not all(levels.l3 <= c.close <= levels.h3 for c in window):
        return False
    crossings = 0
    for a, b in zip(window, window[1:]):
        if (a.close - levels.pivot) * (b.close - levels.pivot) < 0:
            crossings += 1
    return crossings >= k


# ----------------------------------------------------------------- helpers --

def _buffer(price: float, stop_buffer_pct: float) -> float:
    return price * stop_buffer_pct / 100.0


def _checks_pass(checks: List[Check]) -> bool:
    return all(c.ok for c in checks)


# ------------------------------------------------------------------ setups --

def breakout_long(s: Snapshot, icfg: dict, scfg: dict) -> Tuple[Optional[Signal], List[Check]]:
    lv = s.levels
    c = s.candle
    vol_mult = scfg.get("volume_expansion_mult", 1.3)
    atr_min = icfg.get("atr_min", 0)
    rsi_entry = icfg.get("rsi_entry", 60)

    checks = [
        Check("close > H4", c.close > lv.h4, f"{c.close:.1f} vs {lv.h4:.1f}"),
        Check("OR locked & close > ORH",
              s.or_final and s.orh is not None and c.close > s.orh,
              f"{c.close:.1f} vs {s.orh if s.orh else '—'}"),
        Check(f"RSI > {rsi_entry}", s.rsi is not None and s.rsi > rsi_entry,
              f"RSI {s.rsi:.1f}" if s.rsi is not None else "RSI warming up"),
        Check(f"ATR > {atr_min}", s.atr is not None and s.atr > atr_min,
              f"ATR {s.atr:.1f}" if s.atr is not None else "ATR warming up"),
    ]
    if icfg.get("require_volume_expansion", False):
        ok = s.vol_sma is not None and c.volume > vol_mult * s.vol_sma
        checks.append(Check(f"volume > {vol_mult}x avg", ok,
                            f"{c.volume:.0f} vs {vol_mult * s.vol_sma:.0f}" if s.vol_sma else "no vol avg"))
    checks.append(Check("not choppy (H3–L3 oscillation)", not s.choppy))

    if not _checks_pass(checks):
        return None, checks

    buf = _buffer(c.close, scfg.get("stop_buffer_pct", 0.04))
    sig = Signal(
        symbol=s.symbol, setup="breakout_long", side="LONG",
        entry_ref=s.ltp,
        stop=lv.h3 - buf,
        target1=lv.h5,
        target2=None,                      # trail after T1
        hedge_otm_pct=icfg.get("hedge_otm_pct", 1.0),
        reasons=[f"{x.name}: {x.detail}" for x in checks],
        ts=s.ts,
    )
    return sig, checks


def reversal_long(s: Snapshot, icfg: dict, scfg: dict) -> Tuple[Optional[Signal], List[Check]]:
    lv = s.levels
    c = s.candle
    band = icfg.get("reversal_band_points", 40)
    atr_min = icfg.get("atr_min", 0)
    oversold = scfg.get("rsi_reversal_oversold", 35)

    in_band = (c.low <= lv.l3 + band) and (c.close >= lv.l3 - band)
    rsi_was_oversold = s.rsi_min_lookback is not None and s.rsi_min_lookback < oversold
    rsi_rising2 = (
        s.rsi is not None and s.rsi_prev1 is not None and s.rsi_prev2 is not None
        and s.rsi > s.rsi_prev1 > s.rsi_prev2
    )

    checks = [
        Check(f"price in L3 ± {band}", in_band,
              f"low {c.low:.1f} / close {c.close:.1f} vs L3 {lv.l3:.1f}"),
        Check(f"RSI was < {oversold}", rsi_was_oversold,
              f"min {s.rsi_min_lookback:.1f}" if s.rsi_min_lookback is not None else "—"),
        Check("RSI rising 2 candles", rsi_rising2,
              f"{s.rsi_prev2:.1f} → {s.rsi_prev1:.1f} → {s.rsi:.1f}"
              if rsi_rising2 else "not rising"),
        Check(f"ATR > {atr_min}", s.atr is not None and s.atr > atr_min,
              f"ATR {s.atr:.1f}" if s.atr is not None else "warming up"),
    ]
    if not _checks_pass(checks):
        return None, checks

    buf = _buffer(c.close, scfg.get("stop_buffer_pct", 0.04))
    sig = Signal(
        symbol=s.symbol, setup="reversal_long", side="LONG",
        entry_ref=s.ltp,
        stop=lv.l4 - buf,
        target1=lv.pivot,
        target2=lv.h3,
        hedge_otm_pct=icfg.get("reversal_hedge_otm_pct", icfg.get("hedge_otm_pct", 1.0)),
        reasons=[f"{x.name}: {x.detail}" for x in checks],
        ts=s.ts,
    )
    return sig, checks


def stock_momentum(s: Snapshot, icfg: dict, scfg: dict) -> Tuple[Optional[Signal], List[Check]]:
    c = s.candle
    rsi_entry = icfg.get("rsi_entry", 60)
    checks: List[Check] = []

    if icfg.get("require_vwap", False):
        ok = s.vwap is not None and s.ltp > s.vwap
        checks.append(Check("above VWAP", ok,
                            f"{s.ltp:.1f} vs {s.vwap:.1f}" if s.vwap else "no VWAP"))
    checks.append(Check("OR locked & close > ORH",
                        s.or_final and s.orh is not None and c.close > s.orh,
                        f"{c.close:.1f} vs {s.orh if s.orh else '—'}"))
    checks.append(Check(f"RSI > {rsi_entry}", s.rsi is not None and s.rsi > rsi_entry,
                        f"RSI {s.rsi:.1f}" if s.rsi is not None else "warming up"))
    vol_mult = icfg.get("volume_mult")
    if vol_mult:
        ok = s.vol_sma is not None and c.volume > vol_mult * s.vol_sma
        checks.append(Check(f"volume > {vol_mult}x avg", ok))
    if icfg.get("require_outperform_index", False):
        ok = s.index_pct_change is not None and s.pct_change > s.index_pct_change
        checks.append(Check("outperforming Nifty", ok,
                            f"{s.pct_change:.2f}% vs {s.index_pct_change:.2f}%"
                            if s.index_pct_change is not None else "—"))

    if not _checks_pass(checks):
        return None, checks

    # Stock SL/targets are not specified in the plan — R-based defaults (config).
    if icfg.get("stock_stop_mode", "orl") == "vwap" and s.vwap is not None:
        stop = s.vwap - _buffer(c.close, scfg.get("stop_buffer_pct", 0.04))
    else:
        base = s.orl if s.orl is not None else c.low
        stop = base - _buffer(c.close, scfg.get("stop_buffer_pct", 0.04))
    if stop >= s.ltp:                       # degenerate stop — skip
        checks.append(Check("valid stop below entry", False, f"stop {stop:.1f} >= ltp"))
        return None, checks
    risk = s.ltp - stop
    t1 = s.ltp + icfg.get("stock_target_r", 1.5) * risk

    sig = Signal(
        symbol=s.symbol, setup="stock_momentum", side="LONG",
        entry_ref=s.ltp, stop=stop, target1=t1, target2=None,
        hedge_otm_pct=icfg.get("hedge_otm_pct", 3.0),
        reasons=[f"{x.name}: {x.detail}" for x in checks],
        ts=s.ts,
    )
    return sig, checks


_SETUPS = {
    "breakout_long": breakout_long,
    "reversal_long": reversal_long,
    "stock_momentum": stock_momentum,
}


# ---------------------------------------------------------------- evaluate --

def evaluate(s: Snapshot, icfg: dict, scfg: dict) -> Tuple[Optional[Signal], dict]:
    """Run all configured setups for this instrument.

    Returns (signal or None, {setup_name: [Check, ...]}).
    Hard day-blocks short-circuit everything; chop blocks breakouts only
    (an L3 touch is the edge of the band, not oscillation inside it).
    """
    all_checks: dict = {}

    if s.blocked_day:
        all_checks["no_trade"] = [Check("first 30-min range >= min", False,
                                        "; ".join(s.block_reasons) or "day blocked")]
        return None, all_checks
    if s.rsi_band_blocked:
        all_checks["no_trade"] = [Check("RSI escaped 40–55 band", False,
                                        "RSI pinned in 40–55 (first-hour filter)")]
        return None, all_checks

    for name in icfg.get("setups", []):
        fn = _SETUPS.get(name)
        if fn is None:
            continue
        sig, checks = fn(s, icfg, scfg)
        all_checks[name] = checks
        if sig is not None:
            return sig, all_checks
    return None, all_checks
