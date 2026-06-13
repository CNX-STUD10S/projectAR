"""OTM put selection for hedging from the Kite instruments dump.

Picks the nearest available expiry (weekly where it exists — e.g. Nifty;
otherwise the nearest monthly, e.g. Bank Nifty / stock options) and the
highest strike at or below spot * (1 - otm_pct/100).
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional


def pick_put(option_instruments: List[dict], spot: float, otm_pct: float,
             today: date, min_days_to_expiry: int = 0) -> Optional[dict]:
    """`option_instruments`: rows from kite.instruments("NFO") already filtered
    to name == underlying and instrument_type == "PE".

    Returns the chosen instrument row (with tradingsymbol, instrument_token,
    strike, expiry, lot_size) or None.
    """
    target = spot * (1.0 - otm_pct / 100.0)

    expiries = sorted({_expiry(r) for r in option_instruments
                       if _expiry(r) and (_expiry(r) - today).days >= min_days_to_expiry})
    if not expiries:
        return None
    expiry = expiries[0]

    candidates = [r for r in option_instruments
                  if _expiry(r) == expiry and float(r.get("strike") or 0) > 0
                  and float(r["strike"]) <= target]
    if not candidates:
        # nothing at/below target (illiquid chain?) — take the closest strike above
        above = [r for r in option_instruments if _expiry(r) == expiry
                 and float(r.get("strike") or 0) > 0]
        if not above:
            return None
        return min(above, key=lambda r: abs(float(r["strike"]) - target))

    return max(candidates, key=lambda r: float(r["strike"]))


def _expiry(row: dict):
    e = row.get("expiry")
    if e is None or e == "":
        return None
    if isinstance(e, date):
        return e
    try:  # string fallback "YYYY-MM-DD"
        from datetime import datetime as _dt
        return _dt.strptime(str(e)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
