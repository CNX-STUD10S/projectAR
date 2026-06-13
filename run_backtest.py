"""Backtest CLI.

Examples:
    python run_backtest.py                          # all symbols found in data/history
    python run_backtest.py --symbols NIFTY,RELIANCE
    python run_backtest.py --from 2026-04-01 --to 2026-06-10
    python run_backtest.py --fetch                  # download history via Kite first

CSV files expected at data/history/<SYMBOL>_5min.csv
(columns: datetime,open,high,low,close,volume)
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from bot.config import load_config
from bot.util import IST, now_ist
from backtest.runner import (Backtester, group_by_day, load_csv, summarize,
                             write_trades_csv)

# lot sizes used when running purely from CSVs (live engine reads them from
# the Kite instruments dump instead). EDIT if exchanges revise lots.
DEFAULT_LOT_SIZES = {"NIFTY": 75, "BANKNIFTY": 35, "RELIANCE": 500,
                     "HDFCBANK": 550, "TATAMOTORS": 800}


def fetch(cfg: dict, symbols, days: int, out_dir: Path) -> None:
    """Download 5-min futures history via Kite (requires login.py first).
    Note: Kite serves history for the CURRENT contract, so deep history of
    expired contracts is not available through this shortcut."""
    from bot.broker_kite import KiteBroker
    br = KiteBroker(cfg)
    br.connect()
    br.load_instruments()
    to = now_ist()
    frm = to - timedelta(days=days)
    out_dir.mkdir(parents=True, exist_ok=True)
    for sym in symbols:
        fut = br.resolve_future(sym)
        candles = br.historical_5min(int(fut["instrument_token"]), frm, to)
        p = out_dir / f"{sym}_5min.csv"
        with open(p, "w", encoding="utf-8") as f:
            f.write("datetime,open,high,low,close,volume\n")
            for c in candles:
                f.write(f"{c.ts:%Y-%m-%d %H:%M:%S},{c.open},{c.high},"
                        f"{c.low},{c.close},{int(c.volume)}\n")
        print(f"  {sym}: {len(candles)} candles -> {p}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--dir", default="")
    ap.add_argument("--from", dest="frm", default="")
    ap.add_argument("--to", dest="to", default="")
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--fetch", action="store_true",
                    help="download history from Kite into data/history first")
    ap.add_argument("--fetch-days", type=int, default=90)
    args = ap.parse_args()

    cfg = load_config(args.config)
    hist_dir = Path(args.dir) if args.dir else Path(cfg["_data_dir"]) / "history"
    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               or list(cfg["instruments"].keys()))

    if args.fetch:
        fetch(cfg, symbols, args.fetch_days, hist_dir)

    frm = datetime.strptime(args.frm, "%Y-%m-%d").replace(tzinfo=IST) if args.frm else None
    to = datetime.strptime(args.to, "%Y-%m-%d").replace(tzinfo=IST) if args.to else None

    # Nifty reference for relative-strength checks (HDFCBANK)
    nifty_ref = None
    nifty_csv = hist_dir / "NIFTY_5min.csv"
    if nifty_csv.exists():
        nifty_ref = group_by_day(_clip(load_csv(nifty_csv), frm, to))

    bt = Backtester(cfg, lots=args.lots)
    all_trades = []
    print(f"\n{'symbol':<12}{'trades':>7}{'win%':>7}{'net ₹':>12}"
          f"{'PF':>6}{'maxDD ₹':>11}")
    print("-" * 55)
    for sym in symbols:
        p = hist_dir / f"{sym}_5min.csv"
        if not p.exists():
            print(f"{sym:<12}  (no CSV at {p} — skipped)")
            continue
        candles = _clip(load_csv(p), frm, to)
        if not candles:
            print(f"{sym:<12}  (no candles in range)")
            continue
        lot = DEFAULT_LOT_SIZES.get(sym, 1)
        trades = bt.run_symbol(sym, candles, lot, nifty_ref=nifty_ref)
        all_trades.extend(trades)
        s = summarize(trades)
        print(f"{sym:<12}{s.get('trades',0):>7}{s.get('win_rate_pct','—'):>7}"
              f"{s.get('net',0):>12,}{str(s.get('profit_factor','—')):>6}"
              f"{s.get('max_drawdown',0):>11,}")

    print("-" * 55)
    total = summarize(all_trades)
    print(f"{'TOTAL':<12}{total.get('trades',0):>7}{total.get('win_rate_pct','—'):>7}"
          f"{total.get('net',0):>12,}{str(total.get('profit_factor','—')):>6}"
          f"{total.get('max_drawdown',0):>11,}")
    if all_trades:
        out = Path(cfg["_data_dir"]) / "backtest" / f"trades_{now_ist():%Y%m%d_%H%M}.csv"
        write_trades_csv(all_trades, out)
        print(f"\nDetail: {out}")
        print("Reminder: hedge legs are approximated as premium debits; "
              "see backtest/runner.py docstring.")


def _clip(candles, frm, to):
    if frm:
        candles = [c for c in candles if c.ts >= frm]
    if to:
        candles = [c for c in candles if c.ts <= to]
    return candles


if __name__ == "__main__":
    main()
