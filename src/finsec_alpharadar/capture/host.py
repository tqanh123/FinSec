"""Host integration (plan 261022-1147 phase 2, R2.13): the ONLY capture module that reads the clock or sleeps
(`tests/test_capture_invariants.py` enforces it).

- `SystemClock`: tz-aware UTC wall time and `time.monotonic()`; `system_sleep`.
- `KeepAwake`: on Windows, `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` while capture runs, and
  `ES_CONTINUOUS` on exit (OBSERVED 2026-10-22: the set call returns the previous state 0x80000000, the reset returns
  0x80000001); a no-op elsewhere.
- `SingleInstanceLock`: a non-blocking OS lock on one byte of a lock file (`msvcrt.locking` on Windows, `fcntl.flock`
  elsewhere). The OS drops it when the process dies, so no lock is ever orphaned; a second handle, even in the same
  process, is refused (OBSERVED 2026-10-22, PermissionError errno 13).
- `run_command`: a probe subprocess with a timeout.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable, Sequence

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class SystemClock:
    """The real `Clock` of `capture.sources`."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


def system_sleep(seconds: float) -> None:
    """The real `Sleeper`: `time.sleep`, never negative."""
    time.sleep(max(0.0, seconds))


ExecutionStateSetter = Callable[[int], Any]


def _windows_setter() -> ExecutionStateSetter:
    import ctypes

    function = ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]  # Windows only
    function.restype = ctypes.c_uint32
    function.argtypes = [ctypes.c_uint32]
    return function  # type: ignore[no-any-return]


class KeepAwake:
    """Context manager keeping the system awake (not the display) while capture runs; a no-op off Windows."""

    def __init__(self, setter: ExecutionStateSetter | None = None, platform: str = sys.platform) -> None:
        self._setter = setter
        self._platform = platform
        self._active: ExecutionStateSetter | None = None

    def __enter__(self) -> KeepAwake:
        if self._platform == "win32":
            setter = self._setter if self._setter is not None else _windows_setter()
            previous = setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            if previous == 0:
                logger.warning("SetThreadExecutionState failed: the system may sleep")
            self._active = setter
        return self

    def __exit__(self, *exc: object) -> None:
        if self._active is not None:
            self._active(ES_CONTINUOUS)
            self._active = None


class AlreadyRunning(RuntimeError):
    """Another capture process holds the single-instance lock."""


class SingleInstanceLock:
    """Exclusive, non-blocking lock on `path` (created with its parent directory when missing)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: IO[bytes] | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise RuntimeError("the lock is already held by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as err:
            handle.close()
            raise AlreadyRunning(f"another capture process holds {self.path.name}: {type(err).__name__}") from err
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            logger.warning("could not unlock %s; closing the handle releases it", self.path.name)
        finally:
            handle.close()

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def run_command(argv: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of a probe command; raises `OSError` (not found) or `subprocess.TimeoutExpired`."""
    result = subprocess.run(
        list(argv), capture_output=True, text=True, errors="replace", timeout=timeout_s, check=False
    )
    return result.returncode, result.stdout, result.stderr
