"""Zerodha Kite Connect adapter: data (instruments, historical, ticks) and
live order placement (guarded by the live interlock).

In paper mode this class is used for DATA ONLY — orders go to PaperBroker.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import hedge
from .config import live_interlock_ok
from .models import Candle
from .util import IST, now_ist

log = logging.getLogger("kite")

try:
    from kiteconnect import KiteConnect, KiteTicker
except ImportError:  # allows backtests/selftest without kiteconnect installed
    KiteConnect = None
    KiteTicker = None


class KiteBroker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.live = live_interlock_ok(cfg)
        self.api_key = os.environ.get("KITE_API_KEY", "")
        self.kite = None
        self._nfo: List[dict] = []          # full NFO instruments dump
        self._by_name_fut: Dict[str, List[dict]] = {}
        self._by_name_pe: Dict[str, List[dict]] = {}

    # ------------------------------------------------------------- session --
    def connect(self) -> None:
        if KiteConnect is None:
            raise RuntimeError("kiteconnect not installed — pip install kiteconnect")
        if not self.api_key:
            raise RuntimeError("KITE_API_KEY missing in .env")
        tokens_path = Path(self.cfg["_base_dir"]) / self.cfg["kite"]["tokens_file"]
        if not tokens_path.exists():
            raise RuntimeError("No access token. Run:  python login.py")
        tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
        if tokens.get("date") != date.today().isoformat():
            log.warning("Access token is not from today — if API calls fail, "
                        "re-run: python login.py")
        self.kite = KiteConnect(api_key=self.api_key)
        self.kite.set_access_token(tokens["access_token"])
        profile = self.kite.profile()   # validates the session early
        log.info("Kite session OK for %s (live orders: %s)",
                 profile.get("user_id"), self.live)

    # --------------------------------------------------------- instruments --
    def load_instruments(self) -> None:
        self._nfo = self.kite.instruments("NFO")
        self._by_name_fut.clear()
        self._by_name_pe.clear()
        for row in self._nfo:
            nm = row.get("name", "")
            t = row.get("instrument_type", "")
            if t == "FUT":
                self._by_name_fut.setdefault(nm, []).append(row)
            elif t == "PE":
                self._by_name_pe.setdefault(nm, []).append(row)
        log.info("Loaded %d NFO instruments", len(self._nfo))

    def resolve_future(self, name: str) -> dict:
        """Nearest-expiry future for an underlying name (e.g. NIFTY, RELIANCE)."""
        rows = self._by_name_fut.get(name, [])
        today = now_ist().date()
        live_rows = [r for r in rows if _expiry(r) and _expiry(r) >= today]
        if not live_rows:
            raise RuntimeError(
                f"No future found for '{name}' in instruments dump. "
                f"Check the symbol name in config.yaml (e.g. post-corporate-action renames)."
            )
        return min(live_rows, key=_expiry)

    def resolve_put(self, name: str, spot: float, otm_pct: float) -> Optional[dict]:
        rows = self._by_name_pe.get(name, [])
        return hedge.pick_put(rows, spot, otm_pct, now_ist().date())

    # ----------------------------------------------------------------- data --
    def prev_day_ohlc(self, token: int) -> Tuple[float, float, float, float]:
        """(open, high, low, close) of the last completed trading day."""
        to = now_ist()
        frm = to - timedelta(days=12)
        rows = self.kite.historical_data(token, frm, to, "day")
        rows = [r for r in rows if r["date"].date() < to.date()]
        if not rows:
            raise RuntimeError(f"No daily history for token {token}")
        r = rows[-1]
        return r["open"], r["high"], r["low"], r["close"]

    def warmup_candles(self, token: int, n: int = 120,
                       minutes: int = 5) -> List[Candle]:
        """Last `n` completed 5-min candles (spans previous sessions)."""
        to = now_ist()
        frm = to - timedelta(days=7)
        rows = self.kite.historical_data(token, frm, to, f"{minutes}minute")
        out = [Candle(ts=r["date"], open=r["open"], high=r["high"],
                      low=r["low"], close=r["close"], volume=r.get("volume") or 0)
               for r in rows]
        # drop today's candles — engine rebuilds today live from ticks
        out = [c for c in out if c.ts.date() < to.date()]
        return out[-n:]

    def historical_5min(self, token: int, frm: datetime, to: datetime) -> List[Candle]:
        """Chunked 5-minute history fetch (Kite limits range per request)."""
        out: List[Candle] = []
        cur = frm
        while cur < to:
            end = min(cur + timedelta(days=60), to)
            rows = self.kite.historical_data(token, cur, end, "5minute")
            out.extend(Candle(ts=r["date"], open=r["open"], high=r["high"],
                              low=r["low"], close=r["close"],
                              volume=r.get("volume") or 0) for r in rows)
            cur = end
        # de-dup on ts
        seen, ded = set(), []
        for c in out:
            if c.ts not in seen:
                seen.add(c.ts)
                ded.append(c)
        return ded

    def ltp(self, instrument_keys: List[str]) -> Dict[str, float]:
        """instrument_keys like 'NFO:NIFTY24JANFUT'. Returns key -> last price."""
        data = self.kite.ltp(instrument_keys)
        return {k: v["last_price"] for k, v in data.items()}

    def margin_per_lot(self, fut_row: dict, fallback: float) -> float:
        try:
            params = [{
                "exchange": "NFO",
                "tradingsymbol": fut_row["tradingsymbol"],
                "transaction_type": "BUY",
                "variety": "regular",
                "product": "MIS",
                "order_type": "MARKET",
                "quantity": int(fut_row["lot_size"]),
            }]
            res = self.kite.order_margins(params)
            total = float(res[0]["total"])
            if total > 0:
                return total
        except Exception as e:
            log.warning("order_margins failed (%s) — using fallback %.0f", e, fallback)
        return fallback

    # --------------------------------------------------------------- orders --
    def _guard(self) -> None:
        if not self.live:
            raise RuntimeError(
                "LIVE ORDER BLOCKED: set mode: live in config.yaml AND "
                "KITE_LIVE_CONFIRM=YES in .env to enable real orders."
            )

    def buy_market(self, tradingsymbol: str, qty: int, ref_price: float,
                   tag: str = "") -> Tuple[str, float]:
        self._guard()
        oid = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR, exchange="NFO",
            tradingsymbol=tradingsymbol, transaction_type=self.kite.TRANSACTION_TYPE_BUY,
            quantity=qty, product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET, tag=(tag or None))
        return str(oid), ref_price   # fill tracked at reference LTP; reconcile in Kite

    def sell_market(self, tradingsymbol: str, qty: int, ref_price: float,
                    tag: str = "") -> Tuple[str, float]:
        self._guard()
        oid = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR, exchange="NFO",
            tradingsymbol=tradingsymbol, transaction_type=self.kite.TRANSACTION_TYPE_SELL,
            quantity=qty, product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_MARKET, tag=(tag or None))
        return str(oid), ref_price

    def place_slm_sell(self, tradingsymbol: str, qty: int,
                       trigger: float) -> str:
        self._guard()
        oid = self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR, exchange="NFO",
            tradingsymbol=tradingsymbol, transaction_type=self.kite.TRANSACTION_TYPE_SELL,
            quantity=qty, product=self.kite.PRODUCT_MIS,
            order_type=self.kite.ORDER_TYPE_SLM, trigger_price=round(trigger, 1))
        return str(oid)

    def modify_slm(self, order_id: str, trigger: float) -> None:
        self._guard()
        self.kite.modify_order(variety=self.kite.VARIETY_REGULAR,
                               order_id=order_id, trigger_price=round(trigger, 1))

    def cancel(self, order_id: str) -> None:
        self._guard()
        try:
            self.kite.cancel_order(variety=self.kite.VARIETY_REGULAR,
                                   order_id=order_id)
        except Exception as e:
            log.warning("cancel %s failed: %s", order_id, e)

    # --------------------------------------------------------------- ticker --
    def ticker(self):
        tokens_path = Path(self.cfg["_base_dir"]) / self.cfg["kite"]["tokens_file"]
        tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
        return KiteTicker(self.api_key, tokens["access_token"])


def _expiry(row: dict):
    e = row.get("expiry")
    if isinstance(e, datetime):
        return e.date()
    if isinstance(e, date):
        return e
    try:
        return datetime.strptime(str(e)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
