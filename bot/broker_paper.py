"""Paper broker: same order interface as KiteBroker but fills are simulated
(at reference price ± slippage) and written to a CSV ledger.

Used with live Kite market data, so the simulation runs against real prices.
"""
from __future__ import annotations

import csv
import itertools
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from .util import now_ist

log = logging.getLogger("paper")

LEDGER_FIELDS = ["ts", "symbol", "tradingsymbol", "setup", "event", "side",
                 "qty", "price", "pnl", "note"]


class TradeLedger:
    """Append-only CSV ledger — used in BOTH paper and live mode."""

    def __init__(self, data_dir: str):
        d = Path(data_dir)
        d.mkdir(parents=True, exist_ok=True)
        self.path = d / f"trades_{now_ist():%Y%m%d}.csv"
        if not self.path.exists():
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=LEDGER_FIELDS).writeheader()

    def log(self, **row) -> None:
        row.setdefault("ts", now_ist().isoformat(timespec="seconds"))
        clean = {k: row.get(k, "") for k in LEDGER_FIELDS}
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=LEDGER_FIELDS).writerow(clean)


class PaperBroker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.slip_cfg = cfg["execution"]["slippage"]
        self._ids = itertools.count(1)
        # order_id -> dict(tradingsymbol, qty, trigger, active)
        self.sl_orders: Dict[str, dict] = {}

    # ------------------------------------------------------------ slippage --
    def _slip(self, symbol_hint: str, price: float) -> float:
        if symbol_hint in self.slip_cfg:
            return float(self.slip_cfg[symbol_hint])
        return price * float(self.slip_cfg.get("default_pct", 0.05)) / 100.0

    def _oid(self) -> str:
        return f"PAPER-{next(self._ids)}"

    # -------------------------------------------------------------- orders --
    def buy_market(self, tradingsymbol: str, qty: int, ref_price: float,
                   tag: str = "", symbol_hint: str = "") -> Tuple[str, float]:
        fill = ref_price + self._slip(symbol_hint or tradingsymbol, ref_price)
        log.info("[PAPER BUY ] %s x%d @ %.2f (%s)", tradingsymbol, qty, fill, tag)
        return self._oid(), round(fill, 2)

    def sell_market(self, tradingsymbol: str, qty: int, ref_price: float,
                    tag: str = "", symbol_hint: str = "") -> Tuple[str, float]:
        fill = ref_price - self._slip(symbol_hint or tradingsymbol, ref_price)
        log.info("[PAPER SELL] %s x%d @ %.2f (%s)", tradingsymbol, qty, fill, tag)
        return self._oid(), round(fill, 2)

    def place_slm_sell(self, tradingsymbol: str, qty: int, trigger: float,
                       symbol_hint: str = "") -> str:
        oid = self._oid()
        self.sl_orders[oid] = {"tradingsymbol": tradingsymbol, "qty": qty,
                               "trigger": trigger, "active": True,
                               "symbol_hint": symbol_hint or tradingsymbol}
        log.info("[PAPER SL  ] %s x%d trigger %.2f (%s)", tradingsymbol, qty,
                 trigger, oid)
        return oid

    def modify_slm(self, order_id: str, trigger: float) -> None:
        o = self.sl_orders.get(order_id)
        if o and o["active"]:
            o["trigger"] = trigger

    def cancel(self, order_id: str) -> None:
        o = self.sl_orders.get(order_id)
        if o:
            o["active"] = False

    # ---------------------------------------------------------- simulation --
    def check_sl(self, order_id: Optional[str], ltp: float) -> Optional[float]:
        """Called by the engine on every tick of the underlying.
        Returns the simulated fill price if the stop triggered, else None."""
        if not order_id:
            return None
        o = self.sl_orders.get(order_id)
        if not o or not o["active"]:
            return None
        if ltp <= o["trigger"]:
            o["active"] = False
            fill = o["trigger"] - self._slip(o["symbol_hint"], o["trigger"])
            log.info("[PAPER SL HIT] %s @ %.2f", o["tradingsymbol"], fill)
            return round(fill, 2)
        return None
