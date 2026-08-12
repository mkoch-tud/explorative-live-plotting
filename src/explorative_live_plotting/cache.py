"""Fingerprint-aware query result cache."""

from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any, Callable

import polars as pl


class QueryCache:
    def __init__(self, directory: Path, memory_entries: int = 16) -> None:
        self.directory = directory.expanduser().resolve()
        self.memory_entries = max(0, memory_entries)
        self._memory: OrderedDict[str, pl.DataFrame] = OrderedDict()
        self._lock = RLock()

    @staticmethod
    def key(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def get_or_compute(
        self, key: str, compute: Callable[[], pl.DataFrame]
    ) -> tuple[pl.DataFrame, str]:
        with self._lock:
            if key in self._memory:
                frame = self._memory.pop(key)
                self._memory[key] = frame
                return frame.clone(), "memory"
        path = self.directory / f"{key}.ipc"
        if path.is_file():
            frame = pl.read_ipc(path)
            self._remember(key, frame)
            return frame.clone(), "disk"
        frame = compute()
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / f".{key}.{os.getpid()}.tmp"
        try:
            frame.write_ipc(temporary)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        self._remember(key, frame)
        return frame.clone(), "computed"

    def clear(self) -> None:
        with self._lock:
            self._memory.clear()
        if self.directory.is_dir():
            for path in self.directory.glob("*.ipc"):
                path.unlink()

    def _remember(self, key: str, frame: pl.DataFrame) -> None:
        if self.memory_entries == 0:
            return
        with self._lock:
            self._memory[key] = frame
            self._memory.move_to_end(key)
            while len(self._memory) > self.memory_entries:
                self._memory.popitem(last=False)
