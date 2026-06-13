"""Offline sanity checks — run BEFORE trusting the bot with anything:

    python selftest.py

Needs only pyyaml + tzdata (no Kite account, no network, no Flask).
Verifies indicator math, every setup's entry logic, the no-trade filters,
the paper broker, risk sizing, hedge strike selection, and an end-to-end
synthetic backtest that must produce a breakout trade.
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timedelta, date

from bot.config import load_config
from bot import indicators as ind
from bot import strategy
from bot.models import Candle
from bot.util import IST, at_time, parse_hhmm

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def ts(day: date, h: int, m: int) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=IST)


# ----------------------------------------------------------------- helpers
def make_day(day: date, opens: float, drift_per_candle: float,
             candle_range: float, volume: float, n: int = 75,
             vol_boost_from: int = 9999, vol_boost: float = 2.0):
    """Synthetic 5-min day starting 09:15."""
    out, px = [], opens
    t = ts(day, 9, 15)
    for i in range(n):
        o = px
        c = px + drift_per_candle
        hi = max(o, c) + candle_range / 2
        lo = min(o, c) - candle_range / 2
        v = volume * (vol_boost if i >= vol_boost_from else 1.0)
        out.append(Candle(ts=t, open=o, high=hi, low=lo, close=c, volume=v))
        px = c
        t += timedelta(minutes=5)
    return out


def main() -> None:
    print("\n[1] config")
    try:
        cfg = load_config("config.yaml")
        check("config.yaml loads", True)
        for k in ("mode", "capital", "risk", "session", "strategy",
                  "execution", "instruments"):
            check(f"config has '{k}'", k in cfg)
        check("default mode is paper (safety)", cfg.get("mode") == "paper",
              f"mode={cfg.get('mode')}")
    except Exception as e:
        check("config.yaml loads", False, repr(e))
        print("\nCannot continue without config."); sys.exit(1)

    print("\n[2] module imports")
    for mod in ("bot.models", "bot.util", "bot.indicators", "bot.strategy",
                "bot.risk", "bot.hedge", "bot.candles", "bot.state",
                "bot.broker_paper", "bot.broker_kite", "bot.engine",
                "backtest.runner"):
        try:
            __import__(mod)
            check(f"import {mod}", True)
        except Exception as e:
            check(f"import {mod}", False, repr(e))
    try:
        import flask  # noqa: F401
        __import__("bot.dashboard")
        check("import bot.dashboard (flask)", True)
    except ImportError:
        print("  SKIP  bot.dashboard (flask not installed yet)")

    print("\n[3] camarilla math (H=22100 L=21900 C=22050)")
    lv = ind.camarilla(22100, 21900, 22050)
    check("H4 = 22160.0", abs(lv.h4 - 22160.0) < 0.01, f"{lv.h4}")
    check("H3 = 22105.0", abs(lv.h3 - 22105.0) < 0.01, f"{lv.h3}")
    check("L3 = 21995.0", abs(lv.l3 - 21995.0) < 0.01, f"{lv.l3}")
    check("L4 = 21940.0", abs(lv.l4 - 21940.0) < 0.01, f"{lv.l4}")
    check("H5 = (H/L)*C ≈ 22251.37", abs(lv.h5 - (22100 / 21900) * 22050) < 0.01)
    check("pivot = 22016.67", abs(lv.pivot - (22100 + 21900 + 22050) / 3) < 0.01)
    check("L5 mirrors H5", abs(lv.l5 - (22050 - (lv.h5 - 22050))) < 0.01)

    print("\n[4] RSI / ATR / VWAP")
    rising = [100 + i for i in range(40)]
    rsi = ind.wilder_rsi(rising, 14)
    check("RSI None before warmup", rsi[13] is None)
    check("RSI ~100 on straight rise", rsi[-1] is not None and rsi[-1] > 99,
          f"{rsi[-1]}")
    falling = [200 - i for i in range(40)]
    check("RSI ~0 on straight fall", ind.wilder_rsi(falling, 14)[-1] < 1)
    d0 = date(2026, 6, 1)
    cs = make_day(d0, 22000, 5, 20, 100000)
    atr = ind.wilder_atr(cs, 14)
    check("ATR warm and positive", atr[-1] is not None and atr[-1] > 0)
    check("ATR ≈ candle range+drift", atr[-1] is not None and 15 < atr[-1] < 35,
          f"{atr[-1]}")
    vw = ind.vwap(cs)
    check("VWAP inside day range",
          vw is not None and min(c.low for c in cs) < vw < max(c.high for c in cs))
    orh, orl, fin = ind.opening_range(cs, ts(d0, 9, 15), 15)
    check("OR locked after 15 min", fin and orh is not None)
    check("ORH = max of first 3 candles",
          abs(orh - max(c.high for c in cs[:3])) < 0.01)

    print("\n[5] no-trade filters")
    check("first30: 0.1% range blocks",
          strategy.first30_blocked(22000, 22011, 21989, 0.25))
    check("first30: 0.5% range allowed",
          not strategy.first30_blocked(22000, 22060, 21950, 0.25))
    ntcfg = cfg["strategy"]["no_trade"]
    check("RSI 45..50 in first hour blocks",
          strategy.rsi_band_block([45, 48, 50, 47], 40, ntcfg))
    check("RSI 70 escape unblocks",
          not strategy.rsi_band_block([45, 48, 70], 40, ntcfg))
    flat = make_day(d0, lv.pivot - 20, 4, 10, 1000, n=10)
    for i, c in enumerate(flat):   # force oscillation around pivot
        delta = 15 if i % 2 == 0 else -15
        c.close = lv.pivot + delta
        c.open = lv.pivot - delta
        c.high = max(c.open, c.close) + 2
        c.low = min(c.open, c.close) - 2
    check("chop detected on pivot oscillation",
          strategy.detect_chop(flat, lv, ntcfg))

    print("\n[6] setup logic (synthetic snapshots)")
    scfg = cfg["strategy"]
    icfg_n = cfg["instruments"]["NIFTY"]
    base = dict(
        symbol="NIFTY", ts=ts(d0, 10, 30),
        candle=Candle(ts=ts(d0, 10, 30), open=22170, high=22195, low=22160,
                      close=22190, volume=300000),
        ltp=22190.0, rsi=72.0, rsi_prev1=68.0, rsi_prev2=63.0,
        rsi_min_lookback=35.0, atr=25.0, vwap=22100.0, vol_sma=120000.0,
        levels=lv, orh=22120.0, orl=22040.0, or_final=True,
        prev_close=22050.0, pct_change=0.6, index_pct_change=0.4,
        minutes_since_open=75.0,
    )
    snap = strategy.Snapshot(**base)
    sig, _ = strategy.evaluate(snap, icfg_n, scfg)
    check("breakout fires when all conditions met",
          sig is not None and sig.setup == "breakout_long",
          "no signal" if sig is None else sig.setup)
    if sig:
        check("breakout stop just below H3", sig.stop < lv.h3)
        check("breakout T1 = H5", abs(sig.target1 - lv.h5) < 0.01)
    for k, v, label in (("rsi", 55.0, "RSI<=60"),
                        ("atr", 10.0, "ATR<=18"),
                        ("or_final", False, "OR not locked")):
        b2 = dict(base); b2[k] = v
        s2, _ = strategy.evaluate(strategy.Snapshot(**b2), icfg_n, scfg)
        check(f"breakout blocked when {label}", s2 is None)
    lowvol = dict(base)
    lowvol["candle"] = Candle(ts=base["ts"], open=22170, high=22195,
                              low=22160, close=22190, volume=100000)
    s3, _ = strategy.evaluate(strategy.Snapshot(**lowvol), icfg_n, scfg)
    check("breakout blocked without volume expansion", s3 is None)

    rev = dict(base)
    rev.update(
        candle=Candle(ts=base["ts"], open=22000, high=22010,
                      low=lv.l3 - 10, close=lv.l3 + 15, volume=200000),
        ltp=lv.l3 + 15, rsi=38.0, rsi_prev1=34.0, rsi_prev2=31.0,
        rsi_min_lookback=30.0)
    s4, _ = strategy.evaluate(strategy.Snapshot(**rev), icfg_n, scfg)
    check("reversal fires in L3 band with RSI turn",
          s4 is not None and s4.setup == "reversal_long",
          "no signal" if s4 is None else s4.setup)
    if s4:
        check("reversal stop below L4", s4.stop < lv.l4)
        check("reversal T1 = pivot, T2 = H3",
              abs(s4.target1 - lv.pivot) < 0.01 and abs(s4.target2 - lv.h3) < 0.01)

    icfg_r = cfg["instruments"]["RELIANCE"]
    lvr = ind.camarilla(1520, 1490, 1505)
    stock = dict(base)
    stock.update(symbol="RELIANCE", levels=lvr,
                 candle=Candle(ts=base["ts"], open=1516, high=1519, low=1514,
                               close=1518, volume=900000),
                 ltp=1518.0, vwap=1510.0, vol_sma=500000.0, orh=1512.0,
                 orl=1502.0, rsi=66.0)
    s5, _ = strategy.evaluate(strategy.Snapshot(**stock), icfg_r, scfg)
    check("stock momentum fires (RELIANCE)",
          s5 is not None and s5.setup == "stock_momentum",
          "no signal" if s5 is None else s5.setup)
    blocked = strategy.Snapshot(**dict(stock, blocked_day=True))
    s6, _ = strategy.evaluate(blocked, icfg_r, scfg)
    check("day-block overrides everything", s6 is None)

    print("\n[7] risk, paper broker, hedge")
    from bot.risk import RiskManager
    rm = RiskManager(cfg)
    check("NIFTY bucket cap = 3L", abs(rm.bucket_cap["nifty"] - 300000) < 1)
    check("1 lot fits at 2.5L margin", rm.lots_for("NIFTY", "index", 250000) == 1)
    check("0 lots at 3.5L margin", rm.lots_for("NIFTY", "index", 350000) == 0)
    check("hedge budget caps lots", rm.hedge_lots_affordable(50000, 3) == 1)
    rm.check_kill_switch(-25000)
    check("kill switch at -2% (−20k)", rm.halted)

    from bot.broker_paper import PaperBroker
    pb = PaperBroker(cfg)
    _, fill = pb.buy_market("NIFTY26JUNFUT", 75, 22000.0, symbol_hint="NIFTY")
    check("paper buy adds slippage", fill == 22001.0, f"{fill}")
    oid = pb.place_slm_sell("NIFTY26JUNFUT", 75, 21950.0, symbol_hint="NIFTY")
    check("SL not hit above trigger", pb.check_sl(oid, 21960.0) is None)
    hit = pb.check_sl(oid, 21949.0)
    check("SL fills below trigger w/ slip", hit == 21949.0, f"{hit}")
    check("SL one-shot", pb.check_sl(oid, 21900.0) is None)

    from bot import hedge
    rows = [{"tradingsymbol": f"NIFTY26JUN{k}PE", "instrument_token": k,
             "strike": float(k), "expiry": date(2026, 6, 18),
             "lot_size": 75, "instrument_type": "PE"}
            for k in range(21000, 23001, 50)]
    pick = hedge.pick_put(rows, 22000.0, 1.25, date(2026, 6, 13))
    check("hedge picks highest strike <= 1.25% OTM",
          pick is not None and pick["strike"] == 21700.0,
          str(pick and pick["strike"]))

    print("\n[8] end-to-end synthetic backtest (breakout day)")
    try:
        from backtest.runner import Backtester, summarize
        d1, d2 = date(2026, 6, 1), date(2026, 6, 2)
        day1 = make_day(d1, 21950, 1.5, 18, 100000)          # sets levels
        h1 = max(c.high for c in day1); l1 = min(c.low for c in day1)
        c1 = day1[-1].close
        lv2 = ind.camarilla(h1, l1, c1)
        # day 2: flat-ish first 3 candles (OR), then strong trend through H4
        day2 = make_day(d2, c1 + 10, 16, 24, 110000, n=66, vol_boost_from=4)
        candles = day1 + day2
        bt = Backtester(cfg, lots=1)
        trades = bt.run_symbol("NIFTY", candles, lot_size=75)
        check("backtest produced >=1 trade", len(trades) >= 1, f"{len(trades)}")
        if trades:
            t = trades[0]
            check("trade is breakout_long", t.setup == "breakout_long", t.setup)
            check("entry above H4", t.entry > lv2.h4,
                  f"entry {t.entry} vs H4 {lv2.h4:.1f}")
            check("net = gross - costs - hedge",
                  abs(t.pnl_net - (t.pnl_gross - t.costs - t.hedge_cost)) < 0.01)
            check("uptrend day ends profitable gross", t.pnl_gross > 0,
                  f"{t.pnl_gross}")
            s = summarize(trades)
            check("summary computes", s["trades"] == len(trades))
    except Exception as e:
        traceback.print_exc()
        check("backtest end-to-end", False, repr(e))

    print(f"\n{'=' * 46}\n  RESULT: {PASS} passed, {FAIL} failed\n{'=' * 46}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
