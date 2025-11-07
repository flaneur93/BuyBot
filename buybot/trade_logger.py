

"""Append-only CSV writer for executed trades."""

from __future__ import annotations

import csv
from pathlib import Path
from threading import Lock
from typing import Iterable, Sequence


class TradeLogger:
    HEADER = ("timestamp", "price", "spent", "balance_after")

    def __init__(self, base_dir: Path) -> None:
        self.path = Path(base_dir) / "trades.csv"
        self._lock = Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        if self.path.exists() and self.path.stat().st_size > 0:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.HEADER)

    def append(self, row: Sequence[object]) -> None:
        with self._lock, self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(row)
