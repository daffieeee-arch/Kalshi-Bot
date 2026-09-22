"""Append-only JSONL segments and derived Parquet. JSONL is the raw source."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA_VERSION = 1
MAX_SEGMENT_BYTES = 32 * 1024 * 1024

COLUMNS: tuple[str, ...] = (
    "schema_version",
    "source_env",
    "stream",
    "received_at",
    "written_at",
    "source_ts",
    "provider_received_at",
    "payload",
    "market_ticker",
    "event_ticker",
    "seq",
    "sid",
    "event_kind",
    "side",
    "price_dollars",
    "delta_fp",
    "count_fp",
    "yes_price_dollars",
    "no_price_dollars",
    "trade_id",
    "taker_side",
    "taker_outcome_side",
    "taker_book_side",
    "is_block_trade",
    "index_id",
    "value",
    "avg_60s_value",
    "avg_60s_window_size",
    "avg_60s_window_start_ts_ms",
    "avg_60s_window_end_ts_exclusive",
    "final_minute_value",
    "final_minute_window_size",
    "final_minute_window_start_ts_ms",
    "final_minute_window_end_ts_exclusive",
    "fee_type",
    "fee_multiplier",
    "fee_type_override",
    "fee_multiplier_override",
    "status",
    "floor_strike",
    "open_time",
    "close_time",
    "result",
    "health_reason",
    "book_valid",
)

_ARROW = pa.schema(
    [pa.field("schema_version", pa.int32())]
    + [pa.field(name, pa.string()) for name in COLUMNS if name != "schema_version"]
)


def quarter_bucket(moment: datetime) -> datetime:
    """Floor a UTC timestamp to the current 15-minute window start."""
    if moment.tzinfo is None:
        raise ValueError("quarter_bucket requires a timezone-aware datetime")
    moment = moment.astimezone(timezone.utc)
    minute = (moment.minute // 15) * 15
    return moment.replace(minute=minute, second=0, microsecond=0)


def _cell(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass
class _Segment:
    stream: str
    bucket: datetime
    partial: Path
    handle: Any
    bytes_written: int = 0
    rows: int = 0


@dataclass
class SegmentWriter:
    """Write one JSONL segment per stream, then compact closed segments to Parquet."""

    root: Path
    source_env: str = "demo"
    max_bytes: int = MAX_SEGMENT_BYTES
    _open: dict[str, _Segment] = field(default_factory=dict)
    files_completed: int = 0

    def write(
        self,
        stream: str,
        *,
        received_at: datetime,
        payload: dict[str, Any],
        normalized: dict[str, Any] | None = None,
        source_ts: Any = None,
        provider_received_at: Any = None,
        now: datetime | None = None,
    ) -> None:
        written_at = now or datetime.now(timezone.utc)
        if written_at.tzinfo is None or received_at.tzinfo is None:
            raise ValueError("record timestamps must be timezone-aware UTC")
        record: dict[str, Any] = {name: None for name in COLUMNS}
        record.update(
            {
                "schema_version": SCHEMA_VERSION,
                "source_env": self.source_env,
                "stream": stream,
                "received_at": received_at.astimezone(timezone.utc).isoformat(),
                "written_at": written_at.astimezone(timezone.utc).isoformat(),
                "source_ts": _cell(source_ts),
                "provider_received_at": _cell(provider_received_at),
                "payload": json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str),
            }
        )
        for key, value in (normalized or {}).items():
            if key in record and key not in {"schema_version", "source_env", "stream", "payload"}:
                record[key] = _cell(value)
        self._append(stream, record, written_at.astimezone(timezone.utc))

    def close(self) -> None:
        for stream in list(self._open):
            self._rotate(stream)

    def _append(self, stream: str, record: dict[str, Any], written_at: datetime) -> None:
        bucket = quarter_bucket(written_at)
        segment = self._open.get(stream)
        if segment is not None and segment.bucket != bucket:
            self._rotate(stream)
            segment = None
        if segment is None:
            segment = self._start(stream, bucket, written_at)
        line = json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        encoded = line.encode("utf-8")
        segment.handle.write(encoded)
        segment.handle.flush()
        os.fsync(segment.handle.fileno())
        segment.bytes_written += len(encoded)
        segment.rows += 1
        if segment.bytes_written >= self.max_bytes:
            self._rotate(stream)

    def _start(self, stream: str, bucket: datetime, written_at: datetime) -> _Segment:
        directory = (
            self.root
            / f"source_env={self.source_env}"
            / f"date={written_at.date().isoformat()}"
            / f"stream={stream}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        stamp = written_at.strftime("%Y%m%dT%H%M%S%fZ")
        partial = directory / f"part-{stamp}-{os.getpid()}.jsonl.partial"
        handle = partial.open("ab")
        segment = _Segment(stream=stream, bucket=bucket, partial=partial, handle=handle)
        self._open[stream] = segment
        return segment

    def _rotate(self, stream: str) -> None:
        segment = self._open.pop(stream, None)
        if segment is None:
            return
        segment.handle.flush()
        os.fsync(segment.handle.fileno())
        segment.handle.close()
        if segment.rows == 0:
            segment.partial.unlink(missing_ok=True)
            return
        final = Path(str(segment.partial).removesuffix(".partial"))
        os.replace(segment.partial, final)
        compact_jsonl(final)
        self.files_completed += 1


def compact_jsonl(path: Path) -> Path:
    """Build a Parquet sibling. The JSONL file is left in place."""
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    columns: dict[str, list[Any]] = {name: [] for name in COLUMNS}
    for row in rows:
        for name in COLUMNS:
            value = row.get(name)
            if name == "schema_version":
                columns[name].append(int(value) if value is not None else None)
            else:
                columns[name].append(None if value is None else str(value))
    table = pa.table(columns, schema=_ARROW)
    out = path.with_suffix(".parquet")
    pq.write_table(table, out, compression="zstd")
    return out
