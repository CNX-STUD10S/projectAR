"""Live trading engine: ticks → candles → indicators → rules → orders.

Runs identically in paper mode (real Kite data, simulated fills) and live
mode (real orders, guarded by the interlock). All rule logic lives in
bot.strategy — this file is orchestration only.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from . import indicators as ind
from . import strategy
from .broker_kite import KiteBroker
from .broker_paper import PaperBroker, TradeLedger
from .candles import CandleBuilder
from .models import Candle, Position, Signal
from .risk import RiskManager
from .state import SharedState
from .util import at_time, minutes_between, now_ist, parse_hhmm, pct

log = logging.getLogger("engine")


class InstrumentRuntime:
    def __init__(self, symbol: str, icfg: dict, candle_minutes: int):
        self.symbol = symbol
        self.icfg = icfg
        self.kind = icfg.get("kind", "stock")
        self.fut: dict = {}
        self.token: int = 0
        self.lot_size: int = 0
        self.margin_per_lot: float = 0.0
        self.levels = None
        self.prev_close: float = 0.0
        self.warmup: List[Candle] = []
        self.today: List[Candle] = []
        self.builder = CandleBuilder(candle_minutes)
        self.ltp: float = 0.0
        self.hedge_ltp: float = 0.0
        self.rsi_today: List[Optional[float]] = []
        self.orh: Optional[float] = None
        self.orl: Optional[float] = None
        self.or_final: bool = False
        self.blocked_day: bool = False
        self.block_reasons: List[str] = []
        self.rsi_band_blocked: bool = False
        self.choppy: bool = False
        self.first30_checked: bool = False
        self.position: Optional[Position] = None
        self.last_checks: dict = {}
        self.last_snapshot_meta: dict = {}


class Engine:
    def __init__(self, cfg: dict, data: KiteBroker, state: SharedState):
        self.cfg = cfg
        self.scfg = cfg["strategy"]
        self.ecfg = cfg["execution"]
        self.data = data
        self.paper = cfg.get("mode", "paper") != "live"
        self.exec: object = PaperBroker(cfg) if self.paper else data
        self.ledger = TradeLedger(cfg["_data_dir"])
        self.state = state
        self.risk = RiskManager(cfg)

        ses = cfg["session"]
        self.open_t = parse_hhmm(ses["open"])
        self.or_minutes = int(ses.get("opening_range_minutes", 15))
        self.no_entry_after_t = parse_hhmm(ses["no_entry_after"])
        self.square_off_t = parse_hhmm(ses["square_off"])
        self.candle_minutes = int(self.scfg.get("candle_minutes", 5))

        self.instruments: Dict[str, InstrumentRuntime] = {
            sym: InstrumentRuntime(sym, icfg, self.candle_minutes)
            for sym, icfg in cfg["instruments"].items()
        }
        self.routes: Dict[int, Tuple[str, str]] = {}   # token -> (role, symbol)
        self.pending: List[Signal] = []
        self.realized_pnl: float = 0.0
        self.squared_off: bool = False
        self.ticker = None

        prio = self.scfg.get("priority", [])
        self._prio_rank = {p: i for i, p in enumerate(prio)}

    # ============================================================ bootstrap
    def bootstrap(self) -> None:
        self.data.connect()
        self.data.load_instruments()
        for sym, rt in self.instruments.items():
            rt.fut = self.data.resolve_future(sym)
            rt.token = int(rt.fut["instrument_token"])
            rt.lot_size = int(rt.fut["lot_size"])
            o, h, l, c = self.data.prev_day_ohlc(rt.token)
            rt.prev_close = c
            rt.levels = ind.camarilla(h, l, c)
            rt.warmup = self.data.warmup_candles(rt.token, n=120,
                                                 minutes=self.candle_minutes)
            rt.margin_per_lot = self.data.margin_per_lot(
                rt.fut, float(rt.icfg.get("margin_per_lot_fallback", 0)))
            self.routes[rt.token] = ("fut", sym)
            log.info("%s -> %s lot=%d margin/lot=%.0f H4=%.1f H3=%.1f L3=%.1f",
                     sym, rt.fut["tradingsymbol"], rt.lot_size,
                     rt.margin_per_lot, rt.levels.h4, rt.levels.h3, rt.levels.l3)
            self.state.update_instrument(sym, {
                "tradingsymbol": rt.fut["tradingsymbol"],
                "levels": rt.levels.as_dict(),
                "prev_close": rt.prev_close,
            })
        self.state.update_global({
            "mode": self.cfg.get("mode", "paper"),
            "capital": self.risk.capital,
            "live_orders": (not self.paper),
        })

    def subscribe_tokens(self) -> List[int]:
        return [rt.token for rt in self.instruments.values()]

    # ================================================================ ticks
    def on_ticks(self, ticks: List[dict]) -> None:
        try:
            for t in ticks:
                token = t.get("instrument_token")
                route = self.routes.get(token)
                if not route:
                    continue
                role, sym = route
                rt = self.instruments[sym]
                price = t.get("last_price") or 0.0
                if price <= 0:
                    continue
                if role == "hedge":
                    rt.hedge_ltp = price
                    continue
                ts = t.get("exchange_timestamp") or now_ist()
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=now_ist().tzinfo)
                rt.ltp = price
                closed = rt.builder.add_tick(ts, price, t.get("volume_traded"))
                if closed is not None and self._in_session(closed.ts):
                    self._on_candle_close(rt, closed)
                self._manage_position_tick(rt)
            self._flush_signals()
            self._clock_checks()
            self._publish()
        except Exception:
            log.exception("on_ticks error")

    def on_order_update(self, data: dict) -> None:
        """Live mode: detect SL-M fills from Kite postbacks."""
        if self.paper:
            return
        try:
            oid = str(data.get("order_id", ""))
            status = data.get("status", "")
            if status != "COMPLETE":
                return
            for rt in self.instruments.values():
                pos = rt.position
                if pos and not pos.closed and pos.sl_order_id == oid:
                    price = float(data.get("average_price") or pos.effective_stop)
                    self._finalize_exit(rt, price, pos.qty_remaining
                                        if hasattr(pos, "qty_remaining") else pos.qty,
                                        "SL")
        except Exception:
            log.exception("order update error")

    # ========================================================= candle close
    def _on_candle_close(self, rt: InstrumentRuntime, candle: Candle) -> None:
        rt.today.append(candle)
        all_c = rt.warmup + rt.today
        closes = [c.close for c in all_c]
        rsi_series = ind.wilder_rsi(closes, self.scfg.get("rsi_period", 14))
        atr_series = ind.wilder_atr(all_c, self.scfg.get("atr_period", 14))
        rt.rsi_today = rsi_series[-len(rt.today):]

        vols = [c.volume for c in all_c[:-1]]
        vol_sma = ind.sma(vols, self.scfg.get("vol_sma_period", 20))
        vw = ind.vwap(rt.today)

        open_dt = at_time(candle.ts.date(), self.open_t)
        rt.orh, rt.orl, rt.or_final = ind.opening_range(
            rt.today, open_dt, self.or_minutes)
        mins_open = minutes_between(open_dt, candle.ts) + self.candle_minutes

        self._update_no_trade_flags(rt, mins_open, open_dt)
        rt.choppy = strategy.detect_chop(rt.today, rt.levels,
                                         self.scfg.get("no_trade", {}))

        # trail management on candle close
        if rt.position and not rt.position.closed:
            self._trail(rt, candle, atr_series[-1])

        rsi = rsi_series[-1]
        rsi_p1 = rsi_series[-2] if len(rsi_series) > 1 else None
        rsi_p2 = rsi_series[-3] if len(rsi_series) > 2 else None
        valid_rsi = [v for v in rsi_series if v is not None]
        lookback = self.scfg.get("rsi_reversal_lookback", 6)
        rsi_min_lb = (min(valid_rsi[:-2][-lookback:])
                      if len(valid_rsi) >= lookback + 2 else None)

        nifty = self.instruments.get("NIFTY")
        idx_pct = (pct(nifty.ltp, nifty.prev_close)
                   if nifty and nifty.ltp and nifty.prev_close else None)

        snap = strategy.Snapshot(
            symbol=rt.symbol, ts=candle.ts, candle=candle, ltp=rt.ltp,
            rsi=rsi, rsi_prev1=rsi_p1, rsi_prev2=rsi_p2,
            rsi_min_lookback=rsi_min_lb,
            atr=atr_series[-1], vwap=vw, vol_sma=vol_sma,
            levels=rt.levels, orh=rt.orh, orl=rt.orl, or_final=rt.or_final,
            prev_close=rt.prev_close,
            pct_change=pct(rt.ltp, rt.prev_close),
            index_pct_change=idx_pct,
            minutes_since_open=mins_open,
            blocked_day=rt.blocked_day,
            rsi_band_blocked=rt.rsi_band_blocked,
            choppy=rt.choppy,
            block_reasons=rt.block_reasons,
        )
        rt.last_snapshot_meta = {
            "rsi": round(rsi, 1) if rsi is not None else None,
            "atr": round(atr_series[-1], 1) if atr_series[-1] is not None else None,
            "vwap": round(vw, 1) if vw else None,
            "vol": candle.volume, "vol_sma": round(vol_sma, 0) if vol_sma else None,
        }

        if rt.position is None and self._entries_allowed(candle.ts):
            sig, checks = strategy.evaluate(snap, rt.icfg, self.scfg)
            rt.last_checks = self._checks_jsonable(checks)
            if sig is not None:
                log.info("SIGNAL %s %s @ %.1f stop %.1f t1 %s",
                         sig.symbol, sig.setup, sig.entry_ref, sig.stop, sig.target1)
                self.pending.append(sig)
        else:
            _, checks = strategy.evaluate(snap, rt.icfg, self.scfg)
            rt.last_checks = self._checks_jsonable(checks)

    def _update_no_trade_flags(self, rt: InstrumentRuntime, mins_open: float,
                               open_dt) -> None:
        nt = self.scfg.get("no_trade", {})
        if not rt.first30_checked and mins_open >= 30:
            first30 = [c for c in rt.today
                       if minutes_between(open_dt, c.ts) < 30]
            if first30:
                hi = max(c.high for c in first30)
                lo = min(c.low for c in first30)
                opn = first30[0].open
                if strategy.first30_blocked(opn, hi, lo,
                                            nt.get("min_first30_range_pct", 0.25)):
                    rt.blocked_day = True
                    rt.block_reasons.append(
                        f"first 30-min range {(hi-lo)/opn*100:.2f}% < "
                        f"{nt.get('min_first30_range_pct', 0.25)}%")
                    log.info("%s NO-TRADE DAY: %s", rt.symbol, rt.block_reasons[-1])
            rt.first30_checked = True
        rt.rsi_band_blocked = strategy.rsi_band_block(rt.rsi_today, mins_open, nt)

    @staticmethod
    def _checks_jsonable(checks: dict) -> dict:
        return {setup: [{"name": c.name, "ok": c.ok, "detail": c.detail}
                        for c in lst] for setup, lst in checks.items()}

    # ============================================================== signals
    def _flush_signals(self) -> None:
        if not self.pending:
            return
        sigs, self.pending = self.pending, []
        sigs.sort(key=lambda s: self._prio_rank.get(f"{s.symbol}:{s.setup}", 99))
        for sig in sigs:
            try:
                self._execute(sig)
            except Exception:
                log.exception("execute failed for %s %s", sig.symbol, sig.setup)

    def _execute(self, sig: Signal) -> None:
        rt = self.instruments[sig.symbol]
        if rt.position is not None or not self.risk.can_enter():
            return
        if not self._entries_allowed(now_ist()):
            return

        lots = self.risk.lots_for(sig.symbol, rt.kind, rt.margin_per_lot)
        if lots <= 0:
            log.info("%s skipped: no allocation left (margin/lot %.0f)",
                     sig.symbol, rt.margin_per_lot)
            return

        # --- hedge: the system mandates a protective put on every position
        put = self.data.resolve_put(sig.symbol, rt.ltp, sig.hedge_otm_pct)
        if put is None:
            log.warning("%s skipped: no OTM put found", sig.symbol)
            return
        put_ts = put["tradingsymbol"]
        try:
            prem = self.data.ltp([f"NFO:{put_ts}"]).get(f"NFO:{put_ts}", 0.0)
        except Exception:
            prem = 0.0
        put_lot = int(put.get("lot_size") or rt.lot_size)
        lots = min(lots, self.risk.hedge_lots_affordable(prem * put_lot, lots))
        if lots <= 0:
            log.info("%s skipped: hedge premium exceeds hedge budget", sig.symbol)
            return

        qty = lots * rt.lot_size
        hedge_qty = lots * put_lot
        fut_ts = rt.fut["tradingsymbol"]

        _, entry_fill = self._order("buy", fut_ts, qty, rt.ltp,
                                    tag=f"{sig.setup}", hint=sig.symbol)
        _, hedge_fill = self._order("buy", put_ts, hedge_qty, prem,
                                    tag="hedge", hint=sig.symbol)
        sl_id = self._place_sl(fut_ts, qty, sig.stop, hint=sig.symbol)

        self.risk.register_entry(sig.symbol, rt.kind,
                                 margin_used=lots * rt.margin_per_lot,
                                 hedge_premium=prem * hedge_qty)
        rt.position = Position(
            symbol=sig.symbol, setup=sig.setup, fut_tradingsymbol=fut_ts,
            lots=lots, qty=qty, entry_price=entry_fill, stop=sig.stop,
            target1=sig.target1, target2=sig.target2, opened_at=now_ist(),
            hedge_tradingsymbol=put_ts, hedge_qty=hedge_qty,
            hedge_entry=hedge_fill, sl_order_id=sl_id,
        )
        rt.hedge_ltp = hedge_fill
        # stream the hedge price too
        token = int(put["instrument_token"])
        self.routes[token] = ("hedge", sig.symbol)
        if self.ticker is not None:
            try:
                self.ticker.subscribe([token])
                self.ticker.set_mode(self.ticker.MODE_LTP, [token])
            except Exception:
                log.warning("could not subscribe hedge token %s", token)

        for ev, ts_, q, px in (("ENTRY", fut_ts, qty, entry_fill),
                               ("HEDGE", put_ts, hedge_qty, hedge_fill)):
            self.ledger.log(symbol=sig.symbol, tradingsymbol=ts_, setup=sig.setup,
                            event=ev, side="BUY", qty=q, price=px,
                            note=f"stop={sig.stop:.1f} t1={sig.target1}")
        log.info("ENTERED %s %s: %d lots (%d qty) @ %.2f, hedge %s @ %.2f",
                 sig.symbol, sig.setup, lots, qty, entry_fill, put_ts, hedge_fill)

    # ====================================================== position manage
    def _manage_position_tick(self, rt: InstrumentRuntime) -> None:
        pos = rt.position
        if pos is None or pos.closed or rt.ltp <= 0:
            return

        # paper SL simulation
        if self.paper:
            fill = self.exec.check_sl(pos.sl_order_id, rt.ltp)
            if fill is not None:
                self._finalize_exit(rt, fill, pos.qty, "SL")
                return

        # Target 1: book partial, stop -> breakeven, start trailing
        if not pos.t1_done and pos.target1 and rt.ltp >= pos.target1:
            exit_lots = pos.lots * self.ecfg.get("partial_exit_at_t1_pct", 50) // 100
            if exit_lots >= 1 and exit_lots < pos.lots:
                exit_qty = exit_lots * rt.lot_size
                _, fill = self._order("sell", pos.fut_tradingsymbol, exit_qty,
                                      rt.ltp, tag="T1", hint=rt.symbol)
                chunk = (fill - pos.entry_price) * exit_qty
                pos.realized_pnl += chunk
                self.realized_pnl += chunk
                pos.lots -= exit_lots
                pos.qty -= exit_qty
                self.ledger.log(symbol=rt.symbol, tradingsymbol=pos.fut_tradingsymbol,
                                setup=pos.setup, event="T1_PARTIAL", side="SELL",
                                qty=exit_qty, price=fill, pnl=round(chunk, 2))
            pos.t1_done = True
            pos.trail_stop = max(pos.stop, pos.entry_price)
            self._modify_sl(pos, pos.trail_stop, hint=rt.symbol)
            log.info("%s T1 hit — SL to breakeven %.2f, trailing on", rt.symbol,
                     pos.trail_stop)

        # Target 2 (reversal: pivot -> H3): exit remainder
        if pos.t1_done and pos.target2 and rt.ltp >= pos.target2:
            self._exit_market(rt, "T2")

    def _trail(self, rt: InstrumentRuntime, candle: Candle,
               atr: Optional[float]) -> None:
        pos = rt.position
        if pos is None or pos.closed or not pos.t1_done:
            return
        mode = self.ecfg.get("trail_mode", "atr")
        if mode == "atr" and atr:
            candidate = candle.close - atr * float(self.ecfg.get("trail_atr_mult", 1.5))
        else:
            candidate = candle.low
        if pos.trail_stop is None or candidate > pos.trail_stop:
            pos.trail_stop = candidate
            self._modify_sl(pos, candidate, hint=rt.symbol)
            log.info("%s trail SL -> %.2f", rt.symbol, candidate)

    def _exit_market(self, rt: InstrumentRuntime, reason: str) -> None:
        pos = rt.position
        if pos is None or pos.closed:
            return
        if pos.sl_order_id:
            try:
                self.exec.cancel(pos.sl_order_id)
            except Exception:
                pass
        _, fill = self._order("sell", pos.fut_tradingsymbol, pos.qty, rt.ltp,
                              tag=reason, hint=rt.symbol)
        self._finalize_exit(rt, fill, pos.qty, reason)

    def _finalize_exit(self, rt: InstrumentRuntime, fut_fill: float,
                       qty: int, reason: str) -> None:
        pos = rt.position
        if pos is None or pos.closed:
            return
        chunk = (fut_fill - pos.entry_price) * qty
        pos.realized_pnl += chunk
        self.realized_pnl += chunk
        self.ledger.log(symbol=rt.symbol, tradingsymbol=pos.fut_tradingsymbol,
                        setup=pos.setup, event=f"EXIT_{reason}", side="SELL",
                        qty=qty, price=fut_fill, pnl=round(chunk, 2))

        # close the hedge with the futures leg
        if pos.hedge_qty > 0 and pos.hedge_tradingsymbol:
            ref = rt.hedge_ltp if rt.hedge_ltp > 0 else pos.hedge_entry
            _, hfill = self._order("sell", pos.hedge_tradingsymbol, pos.hedge_qty,
                                   ref, tag="hedge_close", hint=rt.symbol)
            hpnl = (hfill - pos.hedge_entry) * pos.hedge_qty
            pos.realized_pnl += hpnl
            self.realized_pnl += hpnl
            self.ledger.log(symbol=rt.symbol, tradingsymbol=pos.hedge_tradingsymbol,
                            setup=pos.setup, event="HEDGE_CLOSE", side="SELL",
                            qty=pos.hedge_qty, price=hfill, pnl=round(hpnl, 2))

        pos.closed = True
        self.risk.register_exit(rt.symbol, rt.kind,
                                margin_released=pos.lots * rt.margin_per_lot)
        log.info("CLOSED %s (%s) — position P&L %.0f, day realized %.0f",
                 rt.symbol, reason, pos.realized_pnl, self.realized_pnl)
        rt.position = None

    # ============================================================== clocked
    def _clock_checks(self) -> None:
        now = now_ist()
        if not self.squared_off and now.time() >= self.square_off_t:
            self.square_off_all("EOD")
            self.squared_off = True
        total = self.day_pnl()
        if self.risk.check_kill_switch(total) and not self.squared_off:
            log.error(self.risk.halt_reason)
            self.square_off_all("KILL_SWITCH")
            self.squared_off = True

    def square_off_all(self, reason: str) -> None:
        for rt in self.instruments.values():
            if rt.position and not rt.position.closed:
                self._exit_market(rt, reason)

    # =============================================================== orders
    def _order(self, side: str, ts: str, qty: int, ref: float,
               tag: str, hint: str) -> Tuple[str, float]:
        fn = self.exec.buy_market if side == "buy" else self.exec.sell_market
        if self.paper:
            return fn(ts, qty, ref, tag=tag, symbol_hint=hint)
        return fn(ts, qty, ref, tag=tag)

    def _place_sl(self, ts: str, qty: int, trigger: float, hint: str) -> str:
        if self.paper:
            return self.exec.place_slm_sell(ts, qty, trigger, symbol_hint=hint)
        return self.exec.place_slm_sell(ts, qty, trigger)

    def _modify_sl(self, pos: Position, trigger: float, hint: str) -> None:
        if not pos.sl_order_id:
            return
        try:
            self.exec.modify_slm(pos.sl_order_id, trigger)
        except Exception:
            log.exception("SL modify failed")

    # ================================================================ misc
    def _entries_allowed(self, ts) -> bool:
        if self.squared_off or self.risk.halted:
            return False
        t = ts.time() if hasattr(ts, "time") else ts
        return t < self.no_entry_after_t

    def _in_session(self, ts) -> bool:
        return ts.time() >= self.open_t

    def day_pnl(self) -> float:
        unreal = 0.0
        for rt in self.instruments.values():
            pos = rt.position
            if pos and not pos.closed:
                unreal += (rt.ltp - pos.entry_price) * pos.qty
                if pos.hedge_qty and rt.hedge_ltp > 0:
                    unreal += (rt.hedge_ltp - pos.hedge_entry) * pos.hedge_qty
        return self.realized_pnl + unreal

    def _publish(self) -> None:
        for sym, rt in self.instruments.items():
            pos = rt.position
            self.state.update_instrument(sym, {
                "ltp": rt.ltp,
                "pct_change": round(pct(rt.ltp, rt.prev_close), 2) if rt.prev_close else 0,
                "orh": rt.orh, "orl": rt.orl, "or_final": rt.or_final,
                "blocked_day": rt.blocked_day,
                "block_reasons": rt.block_reasons,
                "rsi_band_blocked": rt.rsi_band_blocked,
                "choppy": rt.choppy,
                "checks": rt.last_checks,
                "meta": rt.last_snapshot_meta,
                "position": None if not pos or pos.closed else {
                    "setup": pos.setup, "lots": pos.lots, "qty": pos.qty,
                    "entry": pos.entry_price, "stop": pos.effective_stop,
                    "target1": pos.target1, "t1_done": pos.t1_done,
                    "hedge": pos.hedge_tradingsymbol,
                    "upnl": round((rt.ltp - pos.entry_price) * pos.qty, 0),
                },
            })
        self.state.update_global({
            "ts": now_ist().isoformat(timespec="seconds"),
            "day_pnl": round(self.day_pnl(), 0),
            "realized_pnl": round(self.realized_pnl, 0),
            "squared_off": self.squared_off,
            "risk": self.risk.snapshot(),
        })
