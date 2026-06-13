# Camarilla + RSI Intraday Bot (Nifty / Bank Nifty / Stocks + OTM Put Hedges)

Automated implementation of the finalized Camarilla + RSI system:
breakout and reversal entries gated by Camarilla levels, 15-min Opening
Range, RSI(14)/ATR(14) on 5-min candles, VWAP and volume filters — with a
protective OTM put bought alongside every futures entry.

> **Disclaimer:** this is trading software, not financial advice. Futures
> and options carry substantial risk; the no-trade filters and kill switch
> reduce, but do not remove, the risk of loss. Test in paper mode first.

## What's here

| File | Purpose |
|---|---|
| `config.yaml` | Every rule threshold from the plan — edit here, not in code |
| `selftest.py` | Offline checks of all math + rules (**run this first**) |
| `login.py` | Daily Kite login → saves access token |
| `run_live.py` | Live engine (paper or live orders) + dashboard |
| `run_backtest.py` | Backtest CSVs through the same rule code |
| `bot/strategy.py` | The actual entry rules + no-trade filters |
| `bot/engine.py` | Orchestration: ticks → candles → signals → orders |
| `data/` | Ledgers, backtest results, history CSVs (created on demand) |

## Setup

```bash
pip install -r requirements.txt
copy .env.example .env        # then edit: KITE_API_KEY / KITE_API_SECRET
python selftest.py            # must end "0 failed"
```

Kite Connect requires an active API subscription (and the historical-data
add-on for warmup candles and `--fetch` backtest data).

## Daily run

```bash
python login.py               # once each morning (token expires daily)
python run_live.py            # start before 09:15 IST
# dashboard: http://127.0.0.1:8787
```

The engine bootstraps previous-day OHLC → Camarilla levels, streams ticks,
builds 5-min candles, and evaluates every rule on candle close. The
dashboard shows each instrument's levels, ORH/ORL, RSI/ATR/VWAP, a live
pass/fail checklist per setup, open positions, and day P&L.

## Paper → live

The bot is **paper by default**: real Kite market data, simulated fills
(with slippage), trades written to `data/trades_YYYYMMDD.csv`.

Live orders require BOTH:
1. `mode: live` in `config.yaml`
2. `KITE_LIVE_CONFIRM=YES` in `.env`

Anything less and order calls are hard-blocked. Run paper for at least a
couple of weeks and compare its ledger to the backtest before going live.

**Compliance note:** exchange/SEBI rules for retail algo orders via broker
APIs have been evolving — check Zerodha's current algo policy for API
order registration/thresholds before enabling live mode.

## Execution model (what the bot does on a signal)

1. Buy futures (lots sized to the bucket allocation: Nifty 30% / Bank Nifty
   30% / stocks 25% shared, by margin per lot).
2. Buy protective PE at the configured OTM % — nearest available expiry is
   picked from the live instruments dump (weekly where it exists, e.g.
   Nifty; otherwise monthly). If the hedge budget (≤8%) can't cover the
   lots, the position is shrunk; **no hedge → no trade**.
3. Place SL-M below H3 (breakout) / L4 (reversal) / ORL-VWAP (stocks).
4. At Target 1 (H5 / pivot): book 50% (if ≥2 lots), move SL to breakeven,
   trail the rest by 1.5×ATR. Reversal exits the remainder at H3.
5. Hard square-off at 15:10, no fresh entries after 14:30.
6. Kill switch: −2% day P&L → square off everything and halt.

## Backtesting

```bash
# CSVs at data/history/<SYMBOL>_5min.csv  (datetime,open,high,low,close,volume)
python run_backtest.py --fetch            # or pull ~90 days via Kite
python run_backtest.py --from 2026-04-01 --to 2026-06-10
```

Honest limitations (see `backtest/runner.py` docstring): hedge puts are
modelled as a premium debit (default 0.35% of notional), not full option
payoffs; one entry per instrument per day; stop checked before target
within a candle (conservative); per-instrument simulation with `--lots`,
not full portfolio margin. Kite's API only serves history for current
contracts, so deep futures history needs your own data files.

## Decisions the plan left open (defaults chosen, all in config)

- **Stock SL/targets** weren't specified → SL below ORL (or VWAP), T1 at
  1.5R, then trail (`stock_stop_mode`, `stock_target_r`).
- **RSI 40–55 first-hour filter** unblocks once RSI breaks out of the band
  after the first hour (`unblock_on_rsi_break`).
- **Chop filter** (H3↔L3 oscillation) blocks breakouts but not the L3
  reversal — a reversal *requires* touching the band edge.
- **Entries between 09:30–09:45** are possible before the first-30-min
  range filter can be evaluated (it needs 30 minutes of data).
- **Bank Nifty "weekly" hedge**: weekly expiries no longer exist for it,
  so the nearest (monthly) expiry is used automatically.
- Verify F&O **symbol names** in `config.yaml` against your instruments
  dump (e.g. corporate-action renames); the bot fails fast if not found.
- Live fills are tracked at reference LTP — reconcile against the Kite
  order book; paper/backtest apply configured slippage instead.

## Capital plan encoded (₹10L)

Nifty 3.0L • Bank Nifty 3.0L • stock futures 2.5L (shared pool, priority
order) • hedge premium cap 0.8L • the rest stays as cash reserve. Priority
when signals compete: Nifty breakout → Bank Nifty breakout → Reliance →
HDFC Bank → Nifty L3 reversal (edit `strategy.priority`).
