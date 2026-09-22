"""Only one recorder may hold the data-directory lock."""

from pathlib import Path

import pytest

from kalshi_bot.recorder import ProcessLock, RecorderLockError


def test_second_lock_fails(tmp_path: Path) -> None:
    path = tmp_path / "recorder.lock"
    first = ProcessLock(path)
    first.acquire()
    second = ProcessLock(path)
    try:
        with pytest.raises(RecorderLockError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
