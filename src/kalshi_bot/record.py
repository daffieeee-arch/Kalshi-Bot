"""Append-only JSONL writer for the production market-data stream."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


SCHEMA_VERSION = 1
SOURCE_ENV = "production"


class JsonlWriter:
    """Crash-safe enough for a single-process append log."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh: TextIO = self.path.open("a", encoding="utf-8")

    def close(self) -> None:
        self._fh.flush()
        self._fh.close()

    def write(
        self,
        payload: dict[str, Any],
        *,
        stream: str,
        ts_ms: int | None,
    ) -> dict[str, Any]:
        received = datetime.now(timezone.utc)
        row = {
            "schema_version": SCHEMA_VERSION,
            "source_env": SOURCE_ENV,
            "stream": stream,
            "ts_ms": ts_ms,
            "local_received_at": received.isoformat(),
            "payload": payload,
        }
        self._fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        self._fh.flush()
        return row


def extract_ts_ms(message: dict[str, Any]) -> int | None:
    """Prefer Kalshi event time over the local clock."""
    payload = message.get("msg")
    if isinstance(payload, dict):
        for key in ("ts_ms", "received_at", "source_ts_ms"):
            value = payload.get(key)
            if value is not None:
                return int(value)
    return None
