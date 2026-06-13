"""One-time daily Kite login: exchanges a request_token for an access_token.

Usage:
    python login.py
Then open the printed URL, log in, copy the `request_token` from the
redirect URL and paste it here. Token is saved to .kite_tokens.json
(valid until ~6 AM next day per Zerodha policy).
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from bot.config import load_config


def main() -> None:
    cfg = load_config()
    api_key = os.environ.get("KITE_API_KEY", "")
    api_secret = os.environ.get("KITE_API_SECRET", "")
    if not api_key or not api_secret:
        raise SystemExit("Set KITE_API_KEY and KITE_API_SECRET in .env first.")

    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=api_key)
    print("\n1) Open this URL and log in:\n")
    print("   " + kite.login_url())
    print("\n2) After login you are redirected to your redirect URL with")
    print("   ?request_token=XXXX — paste that token below.\n")
    request_token = input("request_token: ").strip()

    session = kite.generate_session(request_token, api_secret=api_secret)
    tokens = {
        "access_token": session["access_token"],
        "public_token": session.get("public_token", ""),
        "user_id": session.get("user_id", ""),
        "date": date.today().isoformat(),
    }
    out = Path(cfg["_base_dir"]) / cfg["kite"]["tokens_file"]
    out.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    print(f"\nSaved access token to {out}")
    print("You can now run:  python run_live.py")


if __name__ == "__main__":
    main()
