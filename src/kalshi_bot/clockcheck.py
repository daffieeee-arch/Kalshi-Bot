"""Read host clock sync status without failing the recorder when the tool is missing."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


def clock_status() -> dict[str, Any]:
    """Return timedatectl facts. Missing or unreadable NTP is reported, not fatal."""
    if shutil.which("timedatectl") is None:
        return {"available": False, "reason": "timedatectl_missing"}
    try:
        completed = subprocess.run(
            ["timedatectl", "show"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": type(exc).__name__}
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        return {
            "available": False,
            "reason": "timedatectl_failed",
            "returncode": completed.returncode,
            "detail": detail[0] if detail else "",
        }
    fields: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"Timezone", "NTP", "NTPSynchronized", "TimeUSec"}:
            fields[key] = value
    return {"available": True, **fields}


def disk_status(path: Path) -> dict[str, int]:
    usage = shutil.disk_usage(path if path.exists() else path.parent)
    return {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
