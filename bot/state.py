"""Thread-safe shared state — the live engine writes, the dashboard reads."""
from __future__ import annotations

import threading
from typing import Any, Dict


class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"instruments": {}, "global": {}}

    def update_instrument(self, symbol: str, payload: dict) -> None:
        with self._lock:
            self._data["instruments"].setdefault(symbol, {}).update(payload)

    def update_global(self, payload: dict) -> None:
        with self._lock:
            self._data["global"].update(payload)

    def snapshot(self) -> dict:
        with self._lock:
            # shallow-copy is enough: dashboard only reads
            return {
                "instruments": {k: dict(v) for k, v in self._data["instruments"].items()},
                "global": dict(self._data["global"]),
            }
