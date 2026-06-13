"""Backtester: replays historical 5-min candles through the SAME
bot.strategy rules used live.

Scope (documented honestly):
  * Futures leg simulated per instrument with `--lots` (default 1).
  * Hedge puts are approximated as a premium DEBIT
    (execution.hedge_cost_pct_backtest % of futures notional per trade) —
    their crash-protection payoff is NOT modelled, so net results are
    slightly conservative on winners and optimistic on gap-crash days.
  * One entry per instrument per day (mirrors the daily plan).
  * Conservative intrabar ordering: stop is checked before target.

CSV format (data/history/<SYMBOL>_5min.csv):
    datetime,open,high,low,close,volume
    2026-05-04 09:15:00,22450.0,22461.5,22440.2,22455.0,125000
"""
from __future__ import annotations

import csv
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Dict, List, Optional

from bot import indicators as ind
from bot import strategy
from bot.models import Candle
from bot.util import IST, at_time, minutes_between, parse_hhmm, pct


# ------------------------------------------------------------------ loading

def load_csv(path: Path) -> List[Candle]:
    out: List[Candle] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            ts = _parse_dt(row.get("datetime") or row.get("date") or row.get("time"))
            if ts is None:
                continue
            out.append(Candle(
                ts=ts,
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume") or 0),
            ))
    out.sort(key=lambda c: c.ts)
    return out


def _parse_dt(s: Optional[str]):
    if not s:
        return None
    s = s.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def group_by_day(candles: List[Candle]) -> "OrderedDict[date, List[Candle]]":
    days: "OrderedDict[date, List[Candle]]" = OrderedDict()
    for c in candles:
        days.setdefault(c.ts.date(), []).append(c)
    return days


# --------------------------------------------------------------- simulation

@dataclass
class SimPosition:
    setup: str
    entry_ts: datetime
    entry: float
    stop: float
    target1: Optional[float]
    target2: Optional[float]
    lots: int
    qty: int
    t1_done: bool = False
    trail: Optional[float] = None
    realized: float = 0.0
    legs: List[str] = field(default_factory=list)

    @property
    def eff_stop(self) -> float:
        return self.trail if self.trail is not None else self.stop


@dataclass
class Trade:
    symbol: str
    day: date
    setup: str
    entry_ts: datetime
    entry: float
    exit_ts: datetime
    exit: float
    lots: int
    qty: int
    pnl_gross: float
    costs: float
    hedge_cost: float
    pnl_net: float
    exit_reason: str


class Backtester:
    def __init__(self, cfg: dict, lots: int = 1):
        self.cfg = cfg
        self.scfg = cfg["strategy"]
        self.ecfg = cfg["execution"]
        self.lots = lots
        ses = cfg["session"]
        self.open_t = parse_hhmm(ses["open"])
        self.or_minutes = int(ses.get("opening_range_minutes", 15))
        self.no_entry_after = parse_hhmm(ses["no_entry_after"])
        self.square_off = parse_hhmm(ses["square_off"])
        self.cm = int(self.scfg.get("candle_minutes", 5))

    # ---- per-symbol run -------------------------------------------------
    def run_symbol(self, symbol: str, candles: List[Candle],
                   lot_size: int,
                   nifty_ref: Optional[Dict[date, List[Candle]]] = None
                   ) -> List[Trade]:
        icfg = self.cfg["instruments"][symbol]
        days = group_by_day(candles)
        day_list = list(days.keys())
        trades: List[Trade] = []
        history: List[Candle] = []

        for i, d in enumerate(day_list):
            day_candles = days[d]
            if i == 0:
                history.extend(day_candles)
                continue
            prev = days[day_list[i - 1]]
            prev_h = max(c.high for c in prev)
            prev_l = min(c.low for c in prev)
            prev_c = prev[-1].close
            levels = ind.camarilla(prev_h, prev_l, prev_c)
            open_dt = at_time(d, self.open_t)

            nifty_day = (nifty_ref or {}).get(d)
            nifty_prev_close = None
            if nifty_ref is not None:
                nd = sorted(k for k in nifty_ref if k < d)
                if nd:
                    nifty_prev_close = nifty_ref[nd[-1]][-1].close

            today: List[Candle] = []
            pos: Optional[SimPosition] = None
            entered_today = False
            blocked_day, block_reasons = False, []
            first30_checked = False
            warmup = history[-120:]
            slip = self._slip(symbol, prev_c)

            for c in day_candles:
                # 1) manage open position on this candle's range
                if pos is not None:
                    done = self._manage(pos, c, slip)
                    if done is not None:
                        trades.append(self._close_trade(symbol, d, pos, *done,
                                                        lot_size, icfg))
                        pos = None

                today.append(c)
                all_c = warmup + today
                closes = [x.close for x in all_c]
                rsi_series = ind.wilder_rsi(closes, self.scfg.get("rsi_period", 14))
                atr_series = ind.wilder_atr(all_c, self.scfg.get("atr_period", 14))
                rsi_today = rsi_series[-len(today):]
                vw = ind.vwap(today)
                vols = [x.volume for x in all_c[:-1]]
                vol_sma = ind.sma(vols, self.scfg.get("vol_sma_period", 20))
                orh, orl, or_final = ind.opening_range(today, open_dt, self.or_minutes)
                mins_open = minutes_between(open_dt, c.ts) + self.cm

                nt = self.scfg.get("no_trade", {})
                if not first30_checked and mins_open >= 30:
                    f30 = [x for x in today
                           if minutes_between(open_dt, x.ts) < 30]
                    if f30 and strategy.first30_blocked(
                            f30[0].open, max(x.high for x in f30),
                            min(x.low for x in f30),
                            nt.get("min_first30_range_pct", 0.25)):
                        blocked_day = True
                        block_reasons = ["first 30-min range too small"]
                    first30_checked = True
                band_blocked = strategy.rsi_band_block(rsi_today, mins_open, nt)
                choppy = strategy.detect_chop(today, levels, nt)

                # 2) trail on candle close
                if pos is not None and pos.t1_done:
                    self._trail(pos, c, atr_series[-1])

                # 3) EOD square-off
                if c.ts.time() >= self.square_off:
                    if pos is not None:
                        trades.append(self._close_trade(
                            symbol, d, pos, c.ts, c.close - slip, "EOD",
                            lot_size, icfg))
                        pos = None
                    continue

                # 4) new entry
                if (pos is None and not entered_today
                        and c.ts.time() < self.no_entry_after):
                    valid_rsi = [v for v in rsi_series if v is not None]
                    lb = self.scfg.get("rsi_reversal_lookback", 6)
                    idx_pct = None
                    if nifty_day and nifty_prev_close:
                        ncs = [x for x in nifty_day if x.ts <= c.ts]
                        if ncs:
                            idx_pct = pct(ncs[-1].close, nifty_prev_close)
                    snap = strategy.Snapshot(
                        symbol=symbol, ts=c.ts, candle=c, ltp=c.close,
                        rsi=rsi_series[-1],
                        rsi_prev1=rsi_series[-2] if len(rsi_series) > 1 else None,
                        rsi_prev2=rsi_series[-3] if len(rsi_series) > 2 else None,
                        rsi_min_lookback=(min(valid_rsi[:-2][-lb:])
                                          if len(valid_rsi) >= lb + 2 else None),
                        atr=atr_series[-1], vwap=vw, vol_sma=vol_sma,
                        levels=levels, orh=orh, orl=orl, or_final=or_final,
                        prev_close=prev_c, pct_change=pct(c.close, prev_c),
                        index_pct_change=idx_pct,
                        minutes_since_open=mins_open,
                        blocked_day=blocked_day,
                        rsi_band_blocked=band_blocked,
                        choppy=choppy, block_reasons=block_reasons,
                    )
                    sig, _ = strategy.evaluate(snap, icfg, self.scfg)
                    if sig is not None:
                        entry = c.close + slip
                        pos = SimPosition(
                            setup=sig.setup, entry_ts=c.ts, entry=entry,
                            stop=sig.stop, target1=sig.target1,
                            target2=sig.target2, lots=self.lots,
                            qty=self.lots * lot_size)
                        entered_today = True

            if pos is not None:   # safety: close at day's last candle
                last = day_candles[-1]
                trades.append(self._close_trade(symbol, d, pos, last.ts,
                                                last.close - slip, "EOD",
                                                lot_size, icfg))
            history.extend(day_candles)
            history = history[-3000:]
        return trades

    # ---- mechanics ------------------------------------------------------
    def _slip(self, symbol: str, ref_price: float) -> float:
        s = self.ecfg.get("slippage", {})
        if symbol in s:
            return float(s[symbol])
        return ref_price * float(s.get("default_pct", 0.05)) / 100.0

    def _manage(self, pos: SimPosition, c: Candle, slip: float):
        """Returns (exit_ts, exit_price, reason) when fully closed, else None.
        Conservative: stop before target inside the same candle."""
        if c.low <= pos.eff_stop:
            return (c.ts, pos.eff_stop - slip,
                    "SL" if not pos.t1_done else "TRAIL")
        if not pos.t1_done and pos.target1 and c.high >= pos.target1:
            half = pos.lots * self.ecfg.get("partial_exit_at_t1_pct", 50) // 100
            if 1 <= half < pos.lots:
                lot_qty = pos.qty // pos.lots
                pos.realized += (pos.target1 - pos.entry) * half * lot_qty
                pos.lots -= half
                pos.qty -= half * lot_qty
                pos.legs.append(f"T1 {half} lots @ {pos.target1:.1f}")
            pos.t1_done = True
            pos.trail = max(pos.stop, pos.entry)   # breakeven
        if pos.t1_done and pos.target2 and c.high >= pos.target2:
            return (c.ts, pos.target2, "T2")
        return None

    def _trail(self, pos: SimPosition, c: Candle, atr: Optional[float]) -> None:
        mode = self.ecfg.get("trail_mode", "atr")
        cand = (c.close - atr * float(self.ecfg.get("trail_atr_mult", 1.5))
                if (mode == "atr" and atr) else c.low)
        if pos.trail is None or cand > pos.trail:
            pos.trail = cand

    def _close_trade(self, symbol: str, d: date, pos: SimPosition,
                     exit_ts: datetime, exit_price: float, reason: str,
                     lot_size: int, icfg: dict) -> Trade:
        gross = pos.realized + (exit_price - pos.entry) * pos.qty
        orig_qty = max(pos.qty, lot_size * self.lots)
        notional = pos.entry * lot_size * self.lots
        costs = notional * float(self.ecfg.get("costs_per_round_trip_pct", 0.06)) / 100.0
        hedge = notional * float(self.ecfg.get("hedge_cost_pct_backtest", 0.35)) / 100.0
        return Trade(symbol=symbol, day=d, setup=pos.setup,
                     entry_ts=pos.entry_ts, entry=round(pos.entry, 2),
                     exit_ts=exit_ts, exit=round(exit_price, 2),
                     lots=self.lots, qty=orig_qty,
                     pnl_gross=round(gross, 2), costs=round(costs, 2),
                     hedge_cost=round(hedge, 2),
                     pnl_net=round(gross - costs - hedge, 2),
                     exit_reason=reason)


# ------------------------------------------------------------------ report

def summarize(trades: List[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    nets = [t.pnl_net for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for n in nets:
        equity += n
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1),
        "gross": round(sum(t.pnl_gross for t in trades), 0),
        "costs": round(sum(t.costs for t in trades), 0),
        "hedge_cost": round(sum(t.hedge_cost for t in trades), 0),
        "net": round(sum(nets), 0),
        "avg_win": round(sum(wins) / len(wins), 0) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 0) if losses else 0,
        "profit_factor": (round(sum(wins) / abs(sum(losses)), 2)
                          if losses and sum(losses) != 0 else None),
        "max_drawdown": round(max_dd, 0),
    }


def write_trades_csv(trades: List[Trade], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "day", "setup", "entry_ts", "entry", "exit_ts",
                    "exit", "lots", "qty", "pnl_gross", "costs", "hedge_cost",
                    "pnl_net", "exit_reason"])
        for t in trades:
            w.writerow([t.symbol, t.day, t.setup, t.entry_ts, t.entry,
                        t.exit_ts, t.exit, t.lots, t.qty, t.pnl_gross,
                        t.costs, t.hedge_cost, t.pnl_net, t.exit_reason])
