"""Capital allocation, position sizing, kill switch.

Buckets (₹10L default):  Nifty 30% • Bank Nifty 30% • stock futures 25%
(shared pool) • hedge premium budget 5–8% • cash reserve untouched.
"""
from __future__ import annotations

import math
from typing import Dict, Optional


class RiskManager:
    def __init__(self, cfg: dict):
        self.capital = float(cfg["capital"])
        r = cfg["risk"]
        a = r["allocation"]
        self.max_daily_loss = self.capital * float(r["max_daily_loss_pct"]) / 100.0
        self.max_trades = int(r["max_trades_per_day"])
        self.bucket_cap: Dict[str, float] = {
            "nifty": self.capital * a["nifty_pct"] / 100.0,
            "banknifty": self.capital * a["banknifty_pct"] / 100.0,
            "stocks": self.capital * a["stocks_pct"] / 100.0,
        }
        self.hedge_cap = self.capital * a["hedge_budget_pct"] / 100.0
        self.bucket_used: Dict[str, float] = {k: 0.0 for k in self.bucket_cap}
        self.hedge_used = 0.0
        self.trades_today = 0
        self.halted = False
        self.halt_reason = ""

    # ---------------------------------------------------------------- state
    @staticmethod
    def bucket_for(symbol: str, kind: str) -> str:
        if symbol == "NIFTY":
            return "nifty"
        if symbol == "BANKNIFTY":
            return "banknifty"
        return "stocks"

    def can_enter(self) -> bool:
        return (not self.halted) and self.trades_today < self.max_trades

    def lots_for(self, symbol: str, kind: str, margin_per_lot: float,
                 max_lots: Optional[int] = None) -> int:
        """Lots that fit in the bucket's remaining allocation."""
        if margin_per_lot <= 0:
            return 0
        bucket = self.bucket_for(symbol, kind)
        remaining = self.bucket_cap[bucket] - self.bucket_used[bucket]
        lots = int(math.floor(remaining / margin_per_lot))
        if max_lots is not None:
            lots = min(lots, max_lots)
        return max(lots, 0)

    def hedge_lots_affordable(self, premium_per_lot: float, want_lots: int) -> int:
        """How many hedge lots fit in the remaining hedge premium budget."""
        if premium_per_lot <= 0:
            return want_lots
        remaining = self.hedge_cap - self.hedge_used
        affordable = int(math.floor(remaining / premium_per_lot))
        return max(0, min(want_lots, affordable))

    # ------------------------------------------------------------- mutation
    def register_entry(self, symbol: str, kind: str, margin_used: float,
                       hedge_premium: float) -> None:
        self.bucket_used[self.bucket_for(symbol, kind)] += margin_used
        self.hedge_used += hedge_premium
        self.trades_today += 1

    def register_exit(self, symbol: str, kind: str, margin_released: float) -> None:
        b = self.bucket_for(symbol, kind)
        self.bucket_used[b] = max(0.0, self.bucket_used[b] - margin_released)

    def check_kill_switch(self, total_day_pnl: float) -> bool:
        """True (and halt) when day loss breaches the limit."""
        if self.halted:
            return True
        if total_day_pnl <= -self.max_daily_loss:
            self.halted = True
            self.halt_reason = (
                f"KILL SWITCH: day P&L {total_day_pnl:,.0f} breached "
                f"-{self.max_daily_loss:,.0f}"
            )
            return True
        return False

    def snapshot(self) -> dict:
        return {
            "capital": self.capital,
            "bucket_cap": self.bucket_cap,
            "bucket_used": self.bucket_used,
            "hedge_cap": self.hedge_cap,
            "hedge_used": self.hedge_used,
            "trades_today": self.trades_today,
            "max_trades": self.max_trades,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
        }
