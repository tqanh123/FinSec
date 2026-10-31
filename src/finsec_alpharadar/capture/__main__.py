"""Capture CLI (plan 261022-1147 phase 2, R2.14): `python -m finsec_alpharadar.capture <command>`.

Commands:
- `run`: single-instance lock -> keep-awake -> canary baseline -> startup -> watchdog thread -> poll forever;
- `once [--all]`: one cycle (every enabled source with `--all`, else only the due ones), then a count summary;
- `heartbeat`: probe, build and write one heartbeat;
- `backup --day YYYY-MM-DD`: back up one closed UTC day;
- `audit --state <snapshot> --from D --to D [--days trading|all] [--require-days 5] [--out report.json]`: reads only the
  snapshot and the config, never `FINSEC_DATA_ROOT` / `FINSEC_BACKUP_DIR`;
- `check-root`: check `FINSEC_DATA_ROOT` (and `FINSEC_BACKUP_DIR` when set) against the work-tree guard;
- `canary-reset`: take a new canary baseline; it does not take the process lock (only SQLite's own lock, with a busy
  timeout), so it works while `run` is running (red-team r1 LT4).

`run`, `once`, `heartbeat` and `backup` take the single-instance lock (red-team r1 LT10) and need both directories.
Exit codes: 0 ok; 1 audit failed or backup not verified; 2 usage or config error (missing directory variable included);
3 another capture process is running; 4 a directory is inside a git work tree that does not ignore it.

`--config` defaults to `<clone>/configs/capture.toml`. Only this entry point configures logging handlers
(`<data root>/ops/capture.log`, 5 MB x 10, plus stderr). Nothing runs on import.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import logging.handlers
import os
import platform
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator, Mapping, Sequence, TextIO

from finsec_alpharadar.capture.audit import DAY_MODES, audit
from finsec_alpharadar.capture.backup import BackupRefused, run_backup
from finsec_alpharadar.capture.config import CaptureConfig, CaptureConfigError, SourceKind, load_config_file, resolve_dirs
from finsec_alpharadar.capture.heartbeat import ProcessInfo, RunCommand, run_heartbeat, take_canary_baseline
from finsec_alpharadar.capture.host import AlreadyRunning, KeepAwake, SingleInstanceLock, SystemClock, system_sleep
from finsec_alpharadar.capture.host import run_command as host_run_command
from finsec_alpharadar.capture.poller import ExitFn, Poller, Watchdog, build_sources
from finsec_alpharadar.capture.record import capture_version, format_utc
from finsec_alpharadar.capture.sources import CaptureSource, Clock, Sleeper
from finsec_alpharadar.capture.storage import BlobStore, CaptureState, RawArchive, StorageError
from finsec_alpharadar.data_root import DataRootError, DataRootInsideWorkTreeError

logger = logging.getLogger("finsec_alpharadar.capture")

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_RUNNING, EXIT_ROOT_REFUSED = 0, 1, 2, 3, 4
CLONE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = CLONE_ROOT / "configs" / "capture.toml"
LOG_MAX_BYTES, LOG_BACKUPS = 5 * 1024 * 1024, 10
STATE_FILE, LOCK_FILE = Path("state") / "capture.sqlite", Path("state") / "capture.lock"

SourcesFactory = Callable[[CaptureConfig, Clock, Sleeper], Mapping[str, CaptureSource]]


def _default_sources(config: CaptureConfig, clock: Clock, sleeper: Sleeper) -> Mapping[str, CaptureSource]:
    return build_sources(config, clock=clock, sleeper=sleeper)


@dataclass
class CliFactory:
    """The process's real collaborators; tests replace them with fakes."""

    clock: Clock = field(default_factory=SystemClock)
    sleeper: Sleeper = system_sleep
    sources: SourcesFactory = _default_sources
    run_command: RunCommand = host_run_command
    keep_awake: Callable[[], ContextManager[Any]] = KeepAwake
    clone_root: Path = CLONE_ROOT
    pid: int = field(default_factory=os.getpid)
    stop: Callable[[], bool] = lambda: False  # `run` ends on Ctrl+C or a watchdog exit by default
    watchdog_sleep: Sleeper = system_sleep
    watchdog_exit: ExitFn | None = None  # None: os._exit
    out: TextIO | None = None  # None: sys.stdout at call time
    err: TextIO | None = None  # None: sys.stderr at call time


class _Exit(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m finsec_alpharadar.capture", description="VN capture-only poller")
    commands = parser.add_subparsers(dest="command", required=True)
    specs: dict[str, str] = {
        "run": "poll forever (the capture task)",
        "once": "one poll cycle, then a count summary",
        "heartbeat": "write one heartbeat",
        "backup": "back up one closed UTC day",
        "audit": "judge days from a backup state snapshot",
        "check-root": "check FINSEC_DATA_ROOT (and FINSEC_BACKUP_DIR when set)",
        "canary-reset": "take a new canary baseline (works while run is running)",
    }
    for name, help_text in specs.items():
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="capture TOML (default: configs/capture.toml)")
        if name == "once":
            sub.add_argument("--all", action="store_true", help="poll every enabled source, due or not")
        elif name == "backup":
            sub.add_argument("--day", required=True, help="UTC day YYYY-MM-DD, already closed")
        elif name == "audit":
            sub.add_argument("--state", required=True, type=Path, help="state-<day>.sqlite snapshot from backup")
            sub.add_argument("--from", dest="date_from", required=True, help="first local day YYYY-MM-DD")
            sub.add_argument("--to", dest="date_to", required=True, help="last local day YYYY-MM-DD")
            sub.add_argument("--days", choices=sorted(DAY_MODES), default="trading")
            sub.add_argument("--require-days", type=int, default=5, help="passing trading days in a row (default 5)")
            sub.add_argument("--out", type=Path, help="write the JSON report here")
    return parser


def _day(text: str, name: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as err:
        raise _Exit(EXIT_USAGE, f"{name} must be YYYY-MM-DD, got {text!r}") from err


def _dirs(config: CaptureConfig, env: Mapping[str, str], *, need_backup_dir: bool) -> CaptureConfig:
    try:
        resolved = resolve_dirs(config, env, need_backup_dir=need_backup_dir)
    except DataRootInsideWorkTreeError as err:
        raise _Exit(EXIT_ROOT_REFUSED, str(err)) from err
    except DataRootError as err:
        raise _Exit(EXIT_USAGE, str(err)) from err
    for name, path in (("FINSEC_DATA_ROOT", resolved.data_root), ("FINSEC_BACKUP_DIR", resolved.backup_dir)):
        if path is not None and not path.is_dir():
            raise _Exit(EXIT_USAGE, f"{name} {path} does not exist: create it first")
    return resolved


@contextlib.contextmanager
def _logging(data_root: Path | None) -> Iterator[None]:
    """Handlers for this command only: the rotating ops log under the data root (when known) and stderr."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if data_root is not None:
        (data_root / "ops").mkdir(exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            data_root / "ops" / "capture.log", maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        ))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    package = logging.getLogger("finsec_alpharadar")
    previous = package.level
    package.setLevel(logging.INFO)
    for handler in handlers:
        handler.setFormatter(formatter)
        package.addHandler(handler)
    try:
        yield
    finally:
        for handler in handlers:
            package.removeHandler(handler)
            handler.close()
        package.setLevel(previous)


@contextlib.contextmanager
def _locked(config: CaptureConfig) -> Iterator[None]:
    assert config.data_root is not None
    lock = SingleInstanceLock(config.data_root / LOCK_FILE)
    try:
        lock.acquire()
    except AlreadyRunning as err:
        raise _Exit(EXIT_RUNNING, f"capture is already running: {err}") from err
    try:
        yield
    finally:
        lock.release()


def _state(config: CaptureConfig, *, busy_timeout_s: float = 5.0) -> CaptureState:
    assert config.data_root is not None
    try:
        return CaptureState(config.data_root / STATE_FILE, busy_timeout_s=busy_timeout_s)
    except StorageError as err:
        raise _Exit(EXIT_USAGE, str(err)) from err


def _process(factory: CliFactory, started_at: datetime) -> ProcessInfo:
    return ProcessInfo(factory.pid, started_at, capture_version(), platform.python_version())


def _poller(config: CaptureConfig, factory: CliFactory, state: CaptureState, run_mode: str, **extra: Any) -> Poller:
    assert config.data_root is not None
    sources = dict(factory.sources(config, factory.clock, factory.sleeper))
    return Poller(
        config, sources, archive=RawArchive(config.data_root), blobs=BlobStore(config.data_root), state=state,
        clock=factory.clock, sleeper=factory.sleeper, run_mode=run_mode, pid=factory.pid, **extra,
    )


def _cmd_once(config: CaptureConfig, args: argparse.Namespace, factory: CliFactory, out: TextIO) -> int:
    with _locked(config):
        state = _state(config)
        try:
            poller = _poller(config, factory, state, "once")
            poller.startup()
            report = poller.run_cycle(all_sources=args.all)
        finally:
            state.close()
    print(f"cycle {report.cycle_id}: {len(report.polls)} source(s) polled", file=out)
    for poll in report.polls:
        kind = poll.outage_kind.value if poll.outage_kind is not None else "-"
        print(
            f"{poll.source} status={poll.status} outage={kind} items_total={poll.items_total} "
            f"items_new={poll.items_new} pages={poll.pages}",
            file=out,
        )
    return EXIT_OK


def _heartbeat_fn(config: CaptureConfig, factory: CliFactory, state: CaptureState, process: ProcessInfo,
                  sources: Mapping[str, CaptureSource] | None) -> Callable[[datetime], dict[str, Any]]:
    assert config.data_root is not None and config.backup_dir is not None
    data_root, backup_dir = config.data_root, config.backup_dir

    def beat(now: datetime) -> dict[str, Any]:
        vnstock = [cfg.name for cfg in config.sources if cfg.kind is SourceKind.VNSTOCK_EVENTS]
        versions = [getattr((sources or {}).get(name), "library_version", None) for name in vnstock]
        return run_heartbeat(
            state, config, now, backup_dir=backup_dir, data_root=data_root, clone_root=factory.clone_root,
            run_command=factory.run_command, process=process, sleeper=factory.sleeper,
            vnstock_version=next((v for v in versions if v), None),
        )

    return beat


def _cmd_heartbeat(config: CaptureConfig, factory: CliFactory, out: TextIO) -> int:
    with _locked(config):
        state = _state(config)
        try:
            now = factory.clock.now()
            heartbeat = _heartbeat_fn(config, factory, state, _process(factory, now), None)(now)
            state.commit(now)
        finally:
            state.close()
    print(f"heartbeat {heartbeat['status']} reasons={','.join(heartbeat['reasons']) or '-'}", file=out)
    return EXIT_OK


def _cmd_backup(config: CaptureConfig, args: argparse.Namespace, factory: CliFactory, out: TextIO) -> int:
    assert config.data_root is not None and config.backup_dir is not None
    day = _day(args.day, "--day")
    with _locked(config):
        state = _state(config)
        try:
            now = factory.clock.now()
            try:
                result = run_backup(config.data_root, config.backup_dir, state, day, now=now, sleeper=factory.sleeper)
            except BackupRefused as err:
                raise _Exit(EXIT_USAGE, str(err)) from err
            state.commit(now)
        finally:
            state.close()
    state_word = "verified" if result.verified else f"FAILED ({result.error})"
    if result.skipped:
        state_word = "skipped (already verified)"
    print(f"backup {day.isoformat()}{result.suffix}: {state_word}", file=out)
    return EXIT_OK if result.verified else EXIT_FAILED


def _cmd_run(config: CaptureConfig, factory: CliFactory) -> int:
    assert config.data_root is not None and config.backup_dir is not None
    data_root, backup_dir = config.data_root, config.backup_dir
    with _locked(config), factory.keep_awake():
        state = _state(config)
        watchdog = Watchdog(
            factory.clock, config.schedule.watchdog_limit_s, exit_fn=factory.watchdog_exit, sleep_fn=factory.watchdog_sleep
        )
        try:
            started_at = factory.clock.now()
            take_canary_baseline(state, config, factory.clone_root, started_at)
            sources = dict(factory.sources(config, factory.clock, factory.sleeper))
            heartbeat = _heartbeat_fn(config, factory, state, _process(factory, started_at), sources)

            def heartbeat_fn(now: datetime) -> None:
                heartbeat(now)

            def backup_fn(day: date, now: datetime) -> None:
                run_backup(data_root, backup_dir, state, day, now=now, sleeper=factory.sleeper)

            poller = Poller(
                config, sources, archive=RawArchive(data_root), blobs=BlobStore(data_root), state=state,
                clock=factory.clock, sleeper=factory.sleeper, run_mode="run", pid=factory.pid,
                heartbeat_fn=heartbeat_fn, backup_fn=backup_fn, watchdog=watchdog,
            )
            poller.startup()
            watchdog.start()
            logger.info("capture running: pid=%d sources=%s", factory.pid, ",".join(sources))
            try:
                poller.run_forever(factory.stop)
            except KeyboardInterrupt:
                logger.info("capture stopped by the user")
        finally:
            watchdog.stop()
            state.close()
    return EXIT_OK


def _cmd_audit(config: CaptureConfig, args: argparse.Namespace, out: TextIO) -> int:
    date_from, date_to = _day(args.date_from, "--from"), _day(args.date_to, "--to")
    if args.require_days < 0:
        raise _Exit(EXIT_USAGE, "--require-days must be >= 0")
    try:
        report = audit(args.state, config, date_from, date_to, days=args.days)
    except (StorageError, ValueError) as err:
        raise _Exit(EXIT_USAGE, str(err)) from err
    for day in report.days:
        verdict = "PASS" if day.passed else "FAIL"
        print(f"{day.day.isoformat()} {verdict} {','.join(day.reasons) or '-'} "
              f"ok_polls={sum(day.ok_polls.values())} expected={sum(day.expected_polls.values())} "
              f"items_new={day.items_new} unlogged_gap_s={day.unlogged_gap_s:.0f}", file=out)
    streak = report.longest_consecutive_passing_trading_days
    print(f"longest_consecutive_passing_trading_days={streak} required={args.require_days}", file=out)
    if args.out is not None:
        args.out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return EXIT_OK if streak >= args.require_days else EXIT_FAILED


def _cmd_check_root(config: CaptureConfig, env: Mapping[str, str], out: TextIO) -> int:
    resolved = _dirs(config, env, need_backup_dir=False)
    print(f"FINSEC_DATA_ROOT ok: {resolved.data_root}", file=out)
    if resolved.backup_dir is not None:
        print(f"FINSEC_BACKUP_DIR ok: {resolved.backup_dir}", file=out)
    return EXIT_OK


def _cmd_canary_reset(config: CaptureConfig, factory: CliFactory, out: TextIO) -> int:
    # No process lock; SQLite's own write lock with a busy timeout above the longest `run` transaction (one poll plus two
    # ticks, the alive threshold) [ASSUMED sufficient].
    state = _state(config, busy_timeout_s=config.schedule.alive_threshold_s)
    try:
        now = factory.clock.now()
        hashes = take_canary_baseline(state, config, factory.clone_root, now)
        state.commit(now)
    finally:
        state.close()
    print(f"canary baseline taken at {format_utc(now)} for {len(hashes)} file(s)", file=out)
    return EXIT_OK


def _dispatch(args: argparse.Namespace, env: Mapping[str, str], factory: CliFactory, out: TextIO) -> int:
    config = load_config_file(args.config)
    if args.command == "audit":
        return _cmd_audit(config, args, out)
    if args.command == "check-root":
        return _cmd_check_root(config, env, out)
    need_backup = args.command != "canary-reset"
    config = _dirs(config, env, need_backup_dir=need_backup)
    with _logging(config.data_root):
        if args.command == "canary-reset":
            return _cmd_canary_reset(config, factory, out)
        if args.command == "once":
            return _cmd_once(config, args, factory, out)
        if args.command == "heartbeat":
            return _cmd_heartbeat(config, factory, out)
        if args.command == "backup":
            return _cmd_backup(config, args, factory, out)
        return _cmd_run(config, factory)


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, factory: CliFactory | None = None) -> int:
    """Run one command; returns the exit code (see the module docstring). `env` defaults to `os.environ`."""
    factory = factory if factory is not None else CliFactory()
    out = factory.out if factory.out is not None else sys.stdout
    err = factory.err if factory.err is not None else sys.stderr
    try:
        args = _parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exit_:  # argparse already printed the usage
        return exit_.code if isinstance(exit_.code, int) else EXIT_USAGE
    try:
        return _dispatch(args, os.environ if env is None else env, factory, out)
    except _Exit as exit_:
        print(f"error: {exit_}", file=err)
        return exit_.code
    except CaptureConfigError as config_error:
        print(f"config error: {config_error}", file=err)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
