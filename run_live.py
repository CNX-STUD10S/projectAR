"""Run the live engine (paper or live mode per config.yaml).

    python run_live.py            # uses config.yaml next to this file
    python run_live.py --config other.yaml

Dashboard:  http://127.0.0.1:8787
"""
from __future__ import annotations

import argparse
import logging
import threading

from bot.broker_kite import KiteBroker
from bot.config import load_config
from bot.dashboard import create_app
from bot.engine import Engine
from bot.state import SharedState
from bot.util import now_ist, parse_hhmm

log = logging.getLogger("run_live")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)
    mode = cfg.get("mode", "paper")
    print("=" * 64)
    print(f"  Camarilla + RSI bot — mode: {mode.upper()}")
    if mode == "live":
        from bot.config import live_interlock_ok
        if live_interlock_ok(cfg):
            print("  *** LIVE ORDERS ENABLED — real money at risk ***")
        else:
            print("  mode=live but KITE_LIVE_CONFIRM!=YES -> orders BLOCKED")
    print("=" * 64)

    state = SharedState()
    data = KiteBroker(cfg)
    engine = Engine(cfg, data, state)
    engine.bootstrap()

    # ---- dashboard thread
    app = create_app(state, cfg)
    dcfg = cfg.get("dashboard", {})
    host, port = dcfg.get("host", "127.0.0.1"), int(dcfg.get("port", 8787))
    threading.Thread(
        target=lambda: app.run(host=host, port=port, debug=False,
                               use_reloader=False),
        daemon=True,
    ).start()
    log.info("Dashboard: http://%s:%d", host, port)

    close_t = parse_hhmm(cfg["session"]["close"])
    if now_ist().time() > close_t:
        log.warning("Market is closed — engine will idle until ticks arrive.")

    # ---- websocket feed (blocking)
    ticker = data.ticker()
    engine.ticker = ticker
    tokens = engine.subscribe_tokens()

    def on_connect(ws, _resp):
        log.info("Ticker connected — subscribing %s", tokens)
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)

    def on_ticks(_ws, ticks):
        engine.on_ticks(ticks)

    def on_order_update(_ws, data_):
        engine.on_order_update(data_)

    def on_close(_ws, code, reason):
        log.warning("Ticker closed: %s %s", code, reason)

    def on_error(_ws, code, reason):
        log.error("Ticker error: %s %s", code, reason)

    ticker.on_connect = on_connect
    ticker.on_ticks = on_ticks
    ticker.on_order_update = on_order_update
    ticker.on_close = on_close
    ticker.on_error = on_error
    ticker.connect(threaded=False)   # reconnects automatically


if __name__ == "__main__":
    main()
