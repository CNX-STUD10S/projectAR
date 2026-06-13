"""Config loading: config.yaml + .env."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


def load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p.resolve()}")
    if load_dotenv:
        load_dotenv(p.parent / ".env")
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["_base_dir"] = str(p.parent.resolve())
    data_dir = Path(cfg["_base_dir"]) / cfg.get("paths", {}).get("data_dir", "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg["_data_dir"] = str(data_dir)
    return cfg


def live_interlock_ok(cfg: dict) -> bool:
    """Live orders require BOTH config mode=live AND env KITE_LIVE_CONFIRM=YES."""
    return cfg.get("mode") == "live" and os.environ.get("KITE_LIVE_CONFIRM") == "YES"
