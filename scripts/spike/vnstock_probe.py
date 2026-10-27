"""Measure vnstock Gen 5 for real and guard the agent instruction files around it (plan 261022-1147, R1.4-R1.5).

Self-contained: stdlib only at import time; vnstock and pandas are imported inside `probe`, never at module level, and
the script never imports `finsec_alpharadar` (it is copied alone into the probe kit, outside the clone). Always run it
with `python -I` (isolated mode: no script directory, no user site, no PYTHON* variables on the import path).

Subcommands:
  hash   --out DIR --label NAME [--repo CLONE]   sha256 of agent instruction files; never imports vnstock.
  status --out DIR                              `vnstock status|env|doctor`, long tokens redacted -> vnstock-status.txt.
  probe  --out DIR --symbols VN30|A,B,C [...]    the R1.5 probe steps -> probe-summary.json + raw/*.json.

`probe-summary.json` holds metadata and derived numbers only. `raw/` holds licensed vendor data: it stays under --out,
outside the clone, and is never committed. Owner-run only for `status` and `probe` (decision D6: vnstock runs under a
separate Windows user). External API names follow the vendor docs (single source) and are labelled where used.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import os
import platform
import re
import subprocess
import sys
import time
import warnings
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

# Set before `import vnstock` (research vnstock-gen5.md §4, §7); the last two are 4.x switches, harmless for Gen 5.
TELEMETRY_ENV = {
    "DO_NOT_TRACK": "1",
    "VNSTOCK_TELEMETRY": "0",
    "VNSTOCK_DISABLE_AGENT_SETUP": "1",
    "VNSTOCK_DISABLE_GLOBAL_AGENT": "1",
}
# R1.5 step 2: any run of 24+ token characters is replaced before CLI output is written, so a key never reaches a file.
TOKEN = re.compile(r"[A-Za-z0-9_\-]{24,}")
REDACTED = "<redacted-token>"

# Machine-wide agent instruction files of the current user (issue #236 list, research §3). Antigravity's global rules
# under ~/.gemini/ are [ASSUMED].
GLOBAL_AGENT_FILES = (
    ".claude/CLAUDE.md",
    ".claude/settings.json",
    ".codex/AGENTS.md",
    ".codex/config.toml",
    ".gemini/GEMINI.md",
    ".gemini/config/AGENTS.md",
    ".gemini/settings.json",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
    ".github/copilot-instructions.md",
    ".clauderc",
)
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SYMBOL = re.compile(r"^[A-Z0-9]{2,10}$")
MAX_LISTING_ENTRIES = 5000

# A fixed past date for every historical request: never derived from the machine clock (R1.5 step 7).
DEFAULT_AS_OF = "2026-10-23"
# Event symbols (R1.5 step 5): 3 HOSE, 1 HNX, 1 UPCOM. Exchanges are [PRIOR]; the probe checks each against
# `Reference().equity.list()` and substitutes the first listed symbol of the right exchange on a mismatch.
DEFAULT_HOSE = ("FPT", "VNM", "HPG")
DEFAULT_HNX = ("PVS",)
DEFAULT_UPCOM = ("ACV",)
# Delisted from HOSE (R1.5 step 8), secondary press (searched 2026-10-26):
#   PSH, KPF: https://vneconomy.vn/hose-chot-ngay-huy-bo-niem-yet-doi-voi-co-phieu-kpf-va-psh.htm (2025)
#   BCG (2026):
#   https://cafeland.vn/tin-tuc/hon-880-trieu-co-phieu-bcg-cua-bamboo-capital-bi-huy-niem-yet-bat-buoc-tu-ngay-157-153183.html
DEFAULT_DELISTED = ("PSH", "KPF", "BCG")
SOURCES = ("vci", "kbs")
RATE_CADENCES_S = (6.0, 3.0, 2.0, 1.0, 0.5)
CALLS_PER_CADENCE = 10
EXDATE_WINDOW_DAYS = 10
MAX_EXDATES = 5
INTRADAY_LOOKBACK_DAYS = 200
CLI_TIMEOUT_S = 120
TRUNCATION_WORDS = ("truncat", "limit", "quota", "cắt", "giới hạn", "hạn mức")
STATUS_LINE_WORDS = ("tier", "cấp", "hạn", "expir", "device", "thiết bị", "store", "path", "đường dẫn", "version", "phiên bản")


class ProbeError(RuntimeError):
    """A usage or environment problem that stops the subcommand (exit 2)."""


def redact(text: str) -> str:
    return TOKEN.sub(REDACTED, text)


def utc_now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------------------- hashing


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file, "absent" when missing, "not_a_file" for a directory, "unreadable: <Error>" otherwise."""
    try:
        if not path.exists():
            return "absent"
        if not path.is_file():
            return "not_a_file"
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as err:
        return f"unreadable: {type(err).__name__}"


def listing(directory: Path, *, recursive: bool) -> list[dict[str, Any]] | str:
    """Names (relative, POSIX), kinds and sizes under `directory`; never contents. "absent" when it does not exist."""
    if not directory.is_dir():
        return "absent"
    entries: list[dict[str, Any]] = []
    try:
        paths = sorted(directory.rglob("*") if recursive else directory.iterdir())
    except OSError as err:
        return f"unreadable: {type(err).__name__}"
    for path in paths[:MAX_LISTING_ENTRIES]:
        try:
            kind = "dir" if path.is_dir() else "file" if path.is_file() else "other"
            size = path.stat().st_size if kind == "file" else None
        except OSError as err:
            kind, size = f"unreadable: {type(err).__name__}", None
        entries.append({"name": path.relative_to(directory).as_posix(), "kind": kind, "size": size})
    if len(paths) > MAX_LISTING_ENTRIES:
        entries.append({"name": f"... {len(paths) - MAX_LISTING_ENTRIES} more entries", "kind": "truncated", "size": None})
    return entries


def collect_hashes(repo: Path | None, home: Path, cwd: Path) -> dict[str, Any]:
    """Hashes of the repo's and the user's agent instruction files, plus name/size listings of ~/.vnstock and the cwd."""
    files: dict[str, str] = {}
    if repo is not None:
        for name in ("AGENTS.md", "CLAUDE.md"):
            files[f"repo:{name}"] = sha256_file(repo / name)
        agents = repo / ".agents"
        if agents.is_dir():
            for path in sorted(p for p in agents.rglob("*") if p.is_file()):
                files[f"repo:{path.relative_to(repo).as_posix()}"] = sha256_file(path)
        else:
            files["repo:.agents/"] = "absent"
        git = repo / ".git"
        if git.is_dir():
            hooks = git / "hooks"
            for path in sorted(p for p in hooks.iterdir() if p.is_file()) if hooks.is_dir() else []:
                files[f"repo:.git/hooks/{path.name}"] = sha256_file(path)
        else:
            # A linked worktree's .git is a file and its hooks live in the main checkout: hash from there.
            files["repo:.git"] = "is_a_file: run hash --repo on the main checkout" if git.is_file() else "absent"
        venv = repo / ".venv"
        for path in sorted(venv.rglob("*.pth")) if venv.is_dir() else []:
            files[f"repo:{path.relative_to(repo).as_posix()}"] = sha256_file(path)
    for relative in GLOBAL_AGENT_FILES:
        files[f"home:{relative}"] = sha256_file(home / relative)
    return {
        "files": files,
        "files_digest": hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest(),
        "vnstock_home": listing(home / ".vnstock", recursive=True),
        "cwd": str(cwd),
        "cwd_listing": listing(cwd, recursive=False),
    }


def diff_hashes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Keys whose hash changed, appeared or disappeared, and whether the two listings differ."""
    keys = sorted(set(before["files"]) | set(after["files"]))
    changed = [key for key in keys if before["files"].get(key) != after["files"].get(key)]
    return {
        "changed_files": changed,
        "vnstock_home_changed": before["vnstock_home"] != after["vnstock_home"],
        "cwd_listing_changed": before["cwd_listing"] != after["cwd_listing"],
    }


def write_new_json(path: Path, payload: Any) -> None:
    """Write JSON to a file that must not exist yet (evidence is never overwritten)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def replace_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def cmd_hash(args: argparse.Namespace) -> int:
    if not LABEL.match(args.label):
        raise ProbeError(f"--label must match {LABEL.pattern}")
    repo = Path(args.repo).resolve() if args.repo else None
    if repo is not None and not repo.is_dir():
        raise ProbeError(f"--repo {repo} is not a directory")
    target = Path(args.out) / f"hash-{args.label}.json"
    if target.exists():
        raise ProbeError(f"{target} exists; choose another --label or --out")
    hashes = collect_hashes(repo, Path.home(), Path.cwd())
    record = {
        "label": args.label,
        "created_at_utc": utc_now().isoformat(),
        "python": platform.python_version(),
        "executable": sys.executable,
        "isolated_mode": bool(sys.flags.isolated),
        "vnstock_in_sys_modules": "vnstock" in sys.modules,
        "repo": str(repo) if repo else None,
        "home": str(Path.home()),
        **hashes,
    }
    write_new_json(target, record)
    absent = sum(1 for value in hashes["files"].values() if value == "absent")
    print(f"wrote {target}: {len(hashes['files'])} entries ({absent} absent), files_digest {hashes['files_digest']}")
    return 0


# --------------------------------------------------------------------------------------------------- vnstock CLI


def vnstock_executable() -> Path:
    """The `vnstock` console script installed next to this interpreter (the probe venv's Scripts directory)."""
    return Path(sys.executable).parent / ("vnstock.exe" if os.name == "nt" else "vnstock")


def run_vnstock_cli(subcommand: str) -> dict[str, Any]:
    """`vnstock <subcommand>` with telemetry off; stdout and stderr are redacted before they are kept anywhere."""
    executable = vnstock_executable()
    entry: dict[str, Any] = {"command": f"vnstock {subcommand}"}
    if not executable.is_file():
        entry["error"] = f"{executable.name} not found next to {Path(sys.executable).name}"
        return entry
    env = {**os.environ, **TELEMETRY_ENV, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [str(executable), subcommand],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=CLI_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        entry["error"] = f"timed out after {CLI_TIMEOUT_S} s"
        return entry
    except OSError as err:
        entry["error"] = f"could not run: {type(err).__name__}"
        return entry
    entry.update(
        exit_code=result.returncode,
        seconds=round(time.perf_counter() - started, 3),
        stdout=redact(result.stdout),
        stderr=redact(result.stderr),
    )
    return entry


def cli_report(entries: list[dict[str, Any]]) -> str:
    blocks = []
    for entry in entries:
        header = f"$ {entry['command']}"
        if "error" in entry:
            blocks.append(f"{header}\n[probe error] {entry['error']}\n")
            continue
        timing = f"(exit {entry['exit_code']}, {entry['seconds']} s)"
        blocks.append(f"{header}   {timing}\n{entry['stdout']}\n[stderr]\n{entry['stderr']}\n")
    return "\n".join(blocks)


def status_lines(entries: list[dict[str, Any]]) -> list[str]:
    """Redacted CLI lines that mention tier, expiry, device, store path or version."""
    lines = []
    for entry in entries:
        for line in (entry.get("stdout") or "").splitlines():
            if any(word in line.lower() for word in STATUS_LINE_WORDS):
                lines.append(f"{entry['command']}: {line.strip()}"[:300])
    return lines


def collect_status() -> list[dict[str, Any]]:
    return [run_vnstock_cli(subcommand) for subcommand in ("status", "env", "doctor")]


def cmd_status(args: argparse.Namespace) -> int:
    target = Path(args.out) / "vnstock-status.txt"
    if target.exists():
        raise ProbeError(f"{target} exists; choose another --out")
    entries = collect_status()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(f"# vnstock CLI, {utc_now().isoformat()}; runs of 24+ token characters redacted\n\n{cli_report(entries)}")
    print(f"wrote {target}")
    return 0


# --------------------------------------------------------------------------------------------------- probe


def error_entry(err: BaseException) -> dict[str, Any]:
    code = getattr(err, "code", None)
    return {"type": type(err).__name__, "code": None if code is None else str(code), "message_head": redact(str(err))[:200]}


def jsonable(value: Any) -> Any:
    """A JSON-safe copy of attrs-like metadata (dates and other objects become strings)."""
    return json.loads(json.dumps(value, default=str))


class Probe:
    """Runs the R1.5 steps in order. A failing call is recorded in the error catalogue and the probe goes on."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.out = Path(args.out)
        self.raw = self.out / "raw"
        self.summary_path = self.out / "probe-summary.json"
        self.as_of = date.fromisoformat(args.as_of)
        self.home = Path.home()
        self.cwd = Path.cwd()
        self.catalogue: list[dict[str, Any]] = []
        self.summary: dict[str, Any] = {
            "kind": "vnstock_probe",
            "started_at_utc": utc_now().isoformat(),
            "as_of_date": self.as_of.isoformat(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "isolated_mode": bool(sys.flags.isolated),
            "telemetry_env": TELEMETRY_ENV,
            "steps": {},
        }
        self.vnstock: Any = None
        self.pd: Any = None
        self.ref: Any = None
        self.market_entry: Callable[[str], Any] | None = None
        self.seed: list[str] = []
        self.event_symbols: list[tuple[str, str]] = []
        self.exdate_candidates: list[dict[str, Any]] = []

    # ----------------------------------------------------------------------------------------- plumbing

    def save(self) -> None:
        self.summary["error_catalogue"] = self._catalogue_summary()
        self.summary["updated_at_utc"] = utc_now().isoformat()
        replace_json(self.summary_path, self.summary)

    def step(self, name: str, function: Callable[[], dict[str, Any]]) -> bool:
        print(f"step {name} ...", flush=True)
        try:
            self.summary["steps"][name] = {"status": "ok", **function()}
            ok = True
        except Exception as err:  # noqa: BLE001 - the probe records every failure and goes on to the next step
            self.catalogue.append({"step": name, "call": "step", **error_entry(err)})
            self.summary["steps"][name] = {"status": "error", **error_entry(err)}
            ok = False
        self.save()
        return ok

    def call(self, step: str, label: str, function: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
        """Run one vendor call, timing it and catching its warnings; an exception goes to the catalogue, not up."""
        info: dict[str, Any] = {"call": label}
        started = time.perf_counter()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                result = function()
            except Exception as err:  # noqa: BLE001 - a vendor error is a measurement here
                result = None
                info["error"] = error_entry(err)
                self.catalogue.append({"step": step, "call": label, **info["error"]})
        info["seconds"] = round(time.perf_counter() - started, 3)
        info["warnings"] = [
            {"category": warning.category.__name__, "message_head": redact(str(warning.message))[:200]} for warning in caught
        ]
        for warning in info["warnings"]:
            self.catalogue.append(
                {"step": step, "call": label, "type": warning["category"], "code": None, "message_head": warning["message_head"]}
            )
        return result, info

    def _catalogue_summary(self) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str | None], dict[str, Any]] = {}
        for entry in self.catalogue:
            key = (entry["type"], entry["code"])
            if key not in grouped:
                grouped[key] = {
                    "type": entry["type"],
                    "code": entry["code"],
                    "count": 0,
                    "steps": [],
                    "first_message_head": entry["message_head"],
                }
            grouped[key]["count"] += 1
            if entry["step"] not in grouped[key]["steps"]:
                grouped[key]["steps"].append(entry["step"])
        return list(grouped.values())

    def save_raw(self, name: str, result: Any) -> str:
        """Write a vendor result under raw/ (licensed data: never in the summary, never in the repo)."""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        path = self.raw / f"{safe}.json"
        if hasattr(result, "to_json"):
            records = json.loads(result.to_json(orient="records", date_format="iso", force_ascii=False))
            payload = {"call": name, "attrs": jsonable(dict(getattr(result, "attrs", {}) or {})), "records": records}
        else:
            payload = {"call": name, "value": jsonable(result)}
        replace_json(path, payload)
        return path.name

    def frame_profile(self, frame: Any) -> dict[str, Any]:
        """Columns, dtypes, rows, id presence/uniqueness and, per date/time column, time-of-day and timezone."""
        if not hasattr(frame, "columns"):
            return {"type": type(frame).__name__}
        try:
            return self._frame_profile(frame)
        except Exception as err:  # noqa: BLE001 - an odd vendor frame must not cost the step; the error is recorded
            return {"profile_error": error_entry(err)}

    def _frame_profile(self, frame: Any) -> dict[str, Any]:
        profile: dict[str, Any] = {
            "rows": int(len(frame)),
            "columns": [str(column) for column in frame.columns],
            "dtypes": {str(column): str(dtype) for column, dtype in frame.dtypes.items()},
            "attrs": redact(json.dumps(jsonable(dict(getattr(frame, "attrs", {}) or {})), ensure_ascii=False))[:500],
        }
        if "id" in frame.columns:
            ids = frame["id"].dropna()
            profile["id"] = {"present": True, "non_null": int(len(ids)), "unique": bool(ids.is_unique)}
        else:
            profile["id"] = {"present": False}
        profile["datetime_columns"] = {
            str(column): self.datetime_profile(frame[column])
            for column in frame.columns
            if self.pd.api.types.is_datetime64_any_dtype(frame[column])
            or re.search(r"date|time|_at$|ngay", str(column), re.IGNORECASE)
        }
        return profile

    def datetime_profile(self, series: Any) -> dict[str, Any]:
        values = series.dropna()
        info: dict[str, Any] = {"dtype": str(series.dtype), "non_null": int(len(values))}
        if self.pd.api.types.is_datetime64_any_dtype(series):
            tz = getattr(series.dt, "tz", None)
            info["tz"] = None if tz is None else str(tz)
            parsed = values
        else:
            # Shape of the raw strings with every digit replaced by 9: the format, never a value.
            info["format_patterns"] = sorted({re.sub(r"\d", "9", str(value))[:40] for value in values.head(200)})[:5]
            try:
                parsed = self.pd.to_datetime(values.astype(str), errors="coerce")
            except (TypeError, ValueError) as err:
                info["parse_error"] = type(err).__name__
                return info
            info["unparsed"] = int(parsed.isna().sum())
            parsed = parsed.dropna()
            tz = getattr(parsed.dt, "tz", None) if len(parsed) else None
            info["tz"] = None if tz is None else str(tz)
        if len(parsed):
            clock = parsed.dt
            info["non_midnight"] = int(((clock.hour != 0) | (clock.minute != 0) | (clock.second != 0)).sum())
        else:
            info["non_midnight"] = 0
        return info

    # ----------------------------------------------------------------------------------------- steps

    def step1_import(self) -> dict[str, Any]:
        before = collect_hashes(None, self.home, self.cwd)
        os.environ.update(TELEMETRY_ENV)
        self.vnstock = importlib.import_module("vnstock")
        after_import = collect_hashes(None, self.home, self.cwd)
        self.pd = importlib.import_module("pandas")
        self.summary["hash_before"] = before
        self.summary["hash_after_import"] = after_import
        return {
            "vnstock_version": getattr(self.vnstock, "__version__", None),
            "vnstock_module": getattr(self.vnstock, "__file__", None),
            "pandas_version": getattr(self.pd, "__version__", None),
            "diff_after_import": diff_hashes(before, after_import),
        }

    def step2_cli(self) -> dict[str, Any]:
        entries = collect_status()
        target = self.out / "vnstock-status-probe.txt"
        target.write_text(cli_report(entries), encoding="utf-8", newline="\n")
        return {
            "file": target.name,
            "exit_codes": {entry["command"]: entry.get("exit_code", entry.get("error")) for entry in entries},
            "status_lines": status_lines(entries),
        }

    def _signature(self, owner: Any, name: str) -> str | None:
        member = getattr(owner, name, None)
        if member is None:
            return None
        try:
            return f"{name}{inspect.signature(member)}"
        except (TypeError, ValueError) as err:
            return f"{name}: no signature ({type(err).__name__})"

    @staticmethod
    def _public(owner: Any) -> list[str]:
        return sorted(name for name in dir(owner) if not name.startswith("_"))

    def step3_api_surface(self) -> dict[str, Any]:
        vnstock = self.vnstock
        surface: dict[str, Any] = {"dir_vnstock": self._public(vnstock)}
        # `from vnstock import Reference` is VERIFIED vendor docs (tham-chieu-reference, research §6).
        self.ref = vnstock.Reference()
        surface["reference_attrs"] = self._public(self.ref)
        probe_symbol = self.args.hose[0]
        company = self.ref.company(probe_symbol)
        surface["company_attrs"] = self._public(company)
        surface["signatures"] = {name: self._signature(company, name) for name in ("events", "news")}
        # `Market().equity(symbol).ohlcv()/.trades()`: vendor docs thi-truong-market (fetched 2026-10-26, single
        # source); [ASSUMED] until this call works, so other public classes are listed for discovery as well.
        market_class = getattr(vnstock, "Market", None)
        if market_class is not None:
            equity = market_class().equity(probe_symbol)
            surface["market_equity_attrs"] = self._public(equity)
            for name in ("ohlcv", "trades"):
                surface["signatures"][name] = self._signature(equity, name)
            self.market_entry = lambda symbol: market_class().equity(symbol)
        surface["public_classes"] = {
            name: self._public(getattr(vnstock, name))
            for name in self._public(vnstock)
            if inspect.isclass(getattr(vnstock, name, None))
        }
        ohlcv_parameters = (surface["signatures"].get("ohlcv") or "").lower()
        surface["raw_bar_parameter_candidates"] = [
            word for word in ("adjust", "raw", "unadjusted", "dividend", "split") if word in ohlcv_parameters
        ]
        surface["seed"] = self.seed_universe()
        surface["event_symbols"] = self.pick_event_symbols()
        return surface

    def seed_universe(self) -> dict[str, Any]:
        """`--symbols VN30` reads `ref.index.members("VN30")` (VERIFIED vendor docs); otherwise the given list."""
        if self.args.symbols.upper() != "VN30":
            symbols = [symbol.strip().upper() for symbol in self.args.symbols.split(",") if symbol.strip()]
            self.seed = symbols
            return {"source": "--symbols", "symbols": symbols, "as_of_utc_date": utc_now().date().isoformat()}
        members, info = self.call("seed", "index.members(VN30)", lambda: self.ref.index.members("VN30"))
        if members is None:
            self.seed = list(self.args.hose)
            return {"source": "fallback --hose (index.members failed)", "symbols": self.seed, "call": info}
        column = "symbol" if "symbol" in members.columns else members.columns[0]
        self.seed = [str(symbol).upper() for symbol in members[column].tolist()]
        return {
            "source": "Reference().index.members('VN30')",
            "symbols": self.seed,
            "count": len(self.seed),
            "as_of_utc_date": utc_now().date().isoformat(),
            "attrs": redact(json.dumps(jsonable(dict(members.attrs or {})), ensure_ascii=False))[:500],
            "call": info,
        }

    def pick_event_symbols(self) -> dict[str, Any]:
        """3 HOSE, 1 HNX, 1 UPCOM, checked against `ref.equity.list()` (VERIFIED vendor docs; column names [ASSUMED])."""
        wanted = {"HOSE": list(self.args.hose), "HNX": list(self.args.hnx), "UPCOM": list(self.args.upcom)}
        frame, info = self.call("event_symbols", "equity.list()", lambda: self.ref.equity.list())
        report: dict[str, Any] = {"call": info, "substitutions": []}
        exchange_of: dict[str, str] = {}
        if frame is not None and {"symbol", "exchange"} <= set(frame.columns):
            self.save_raw("equity-list", frame)
            for symbol, exchange in zip(frame["symbol"], frame["exchange"], strict=True):
                normalized = str(exchange).upper().replace("HSX", "HOSE")
                exchange_of[str(symbol).upper()] = normalized
            report["exchange_counts"] = {key: int(value) for key, value in frame["exchange"].astype(str).value_counts().items()}
        else:
            report["note"] = "exchange list unavailable or without symbol/exchange columns; defaults used unverified"
        chosen: dict[str, list[str]] = {}
        for exchange, symbols in wanted.items():
            picked = []
            for symbol in symbols:
                actual = exchange_of.get(symbol)
                if exchange_of and actual != exchange:
                    substitute = next(
                        (s for s in sorted(exchange_of) if exchange_of[s] == exchange and s not in picked and SYMBOL.match(s)),
                        None,
                    )
                    report["substitutions"].append(
                        {"wanted": symbol, "expected": exchange, "listed_as": actual, "used": substitute}
                    )
                    if substitute is None:
                        continue
                    symbol = substitute
                picked.append(symbol)
            chosen[exchange] = picked
        self.event_symbols = [(symbol, exchange) for exchange, symbols in chosen.items() for symbol in symbols]
        report["chosen"] = chosen
        report["exchange_verified"] = bool(exchange_of)
        return report

    def step4_news(self) -> dict[str, Any]:
        symbol = self.args.hose[0]
        result, info = self.call("news", f"company({symbol}).news()", lambda: self.ref.company(symbol).news())
        return {"symbol": symbol, "returned": None if result is None else type(result).__name__, **info}

    def step5_events(self) -> dict[str, Any]:
        report: dict[str, Any] = {}
        self.exdate_candidates = []
        for symbol, exchange in self.event_symbols:
            for source in SOURCES:
                label = f"company({symbol}).events(source={source})"
                frame, info = self.call("events", label, lambda s=symbol, src=source: self.ref.company(s).events(source=src))
                entry: dict[str, Any] = {"exchange": exchange, "source": source, **info}
                if frame is not None:
                    entry["raw"] = self.save_raw(f"events-{symbol}-{source}", frame)
                    entry["profile"] = self.frame_profile(frame)
                    if source == "vci":
                        self.call("events", f"collect_exdates({symbol})", lambda s=symbol, f=frame: self.collect_exdates(s, f))
                report[f"{symbol}/{source}"] = entry
        return {"calls": report}

    def collect_exdates(self, symbol: str, frame: Any) -> None:
        """Past ex-right dates from `exright_date`; stock dividends/bonus shares (a ratio, no cash value) rank first."""
        if "exright_date" not in frame.columns:
            return
        for _, row in frame.iterrows():
            parsed = self.pd.to_datetime(row["exright_date"], errors="coerce")
            if self.pd.isna(parsed):
                continue
            ex_date = parsed.date()
            if not (self.as_of - timedelta(days=3 * 365) <= ex_date <= self.as_of - timedelta(days=EXDATE_WINDOW_DAYS)):
                continue
            ratio = row.get("exercise_ratio")
            cash = row.get("value_per_share")
            has_ratio = ratio is not None and not self.pd.isna(ratio) and str(ratio).strip() not in ("", "0")
            no_cash = cash is None or self.pd.isna(cash) or str(cash).strip() in ("", "0", "0.0")
            self.exdate_candidates.append(
                {
                    "symbol": symbol,
                    "ex_date": ex_date.isoformat(),
                    "stock_like": bool(has_ratio and no_cash),
                    "exercise_ratio": None if not has_ratio else str(ratio),
                    "event_code": None if "event_code" not in frame.columns else str(row.get("event_code")),
                }
            )

    def read_exdates_file(self, path: Path) -> list[dict[str, Any]]:
        """`SYMBOL,YYYY-MM-DD[,ratio]` per line; `#` comments and blank lines skipped."""
        rows = []
        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 2 or not SYMBOL.match(parts[0].upper()):
                raise ProbeError(f"{path}: line {number} is not SYMBOL,YYYY-MM-DD[,ratio]")
            rows.append(
                {
                    "symbol": parts[0].upper(),
                    "ex_date": date.fromisoformat(parts[1]).isoformat(),
                    "stock_like": None,
                    "exercise_ratio": parts[2] if len(parts) > 2 else None,
                    "event_code": None,
                }
            )
        return rows

    def step6_ohlcv_exdates(self) -> dict[str, Any]:
        if self.market_entry is None:
            raise ProbeError("no market entry point found in step 3 (vnstock.Market missing)")
        if self.args.exdates:
            picked = self.read_exdates_file(Path(self.args.exdates))[:MAX_EXDATES]
        else:
            seen: set[tuple[str, str]] = set()
            picked = []
            # Stock-like events first, the most recent first within each group.
            ranked = sorted(
                self.exdate_candidates,
                key=lambda c: (not c["stock_like"], -date.fromisoformat(c["ex_date"]).toordinal()),
            )
            for candidate in ranked:
                key = (candidate["symbol"], candidate["ex_date"])
                if key not in seen and len(picked) < MAX_EXDATES:
                    seen.add(key)
                    picked.append(candidate)
        results = []
        for candidate in picked:
            ex_date = date.fromisoformat(candidate["ex_date"])
            start = (ex_date - timedelta(days=EXDATE_WINDOW_DAYS)).isoformat()
            end = (ex_date + timedelta(days=EXDATE_WINDOW_DAYS)).isoformat()
            entry: dict[str, Any] = {**candidate, "window": [start, end], "sources": {}}
            for source in SOURCES:
                label = f"equity({candidate['symbol']}).ohlcv({start}..{end}, 1D, floating=4, source={source})"
                frame, info = self.call(
                    "ohlcv_exdates",
                    label,
                    lambda c=candidate, a=start, b=end, src=source: self.market_entry(c["symbol"]).ohlcv(
                        start=a, end=b, interval="1D", floating=4, source=src
                    ),
                )
                source_entry: dict[str, Any] = dict(info)
                if frame is not None:
                    source_entry["raw"] = self.save_raw(f"ohlcv-{candidate['symbol']}-{candidate['ex_date']}-{source}", frame)
                    source_entry["profile"] = self.frame_profile(frame)
                    source_entry.update(self.exdate_jump(frame, ex_date))
                entry["sources"][source] = source_entry
            results.append(entry)
        symbol = self.args.hose[0]
        trades, trades_info = self.call(
            "trades", f"equity({symbol}).trades(limit=5)", lambda: self.market_entry(symbol).trades(limit=5)
        )
        trades_entry: dict[str, Any] = dict(trades_info)
        if trades is not None:
            trades_entry["raw"] = self.save_raw(f"trades-{symbol}", trades)
            trades_entry["profile"] = self.frame_profile(trades)
            if hasattr(trades, "columns") and "price" in trades.columns and len(trades):
                trades_entry["price_magnitude_log10"] = self.magnitude(trades["price"])
        picked_from = "--exdates file" if self.args.exdates else "events exright_date"
        return {"exdates": results, "picked_from": picked_from, "trades": trades_entry}

    def magnitude(self, series: Any) -> int | None:
        """floor(log10(median)): 1 for tens (thousands of VND), 4 for tens of thousands (full VND)."""
        median = self.pd.to_numeric(series, errors="coerce").median()
        if self.pd.isna(median) or median <= 0:
            return None
        return int(f"{median:e}".split("e")[1])

    def exdate_jump(self, frame: Any, ex_date: date) -> dict[str, Any]:
        """close(T-1)/open(T) around the ex-date T and the price magnitude: derived numbers only, no bar values."""
        if not {"time", "open", "close"} <= set(frame.columns) or not len(frame):
            return {"jump": "columns time/open/close not found"}
        days = self.pd.to_datetime(frame["time"], errors="coerce").dt.date
        on_or_after = [index for index, day in enumerate(days) if day is not None and not self.pd.isna(day) and day >= ex_date]
        if not on_or_after or on_or_after[0] == 0:
            return {"jump": "no session before and on/after the ex-date in the window"}
        position = on_or_after[0]
        previous_close = float(frame["close"].iloc[position - 1])
        today_open = float(frame["open"].iloc[position])
        return {
            "first_session_on_or_after_ex_date": str(days.iloc[position]),
            "close_prev_over_open_t": round(previous_close / today_open, 6) if today_open else None,
            "price_magnitude_log10": self.magnitude(frame["close"]),
        }

    def step7_depth(self) -> dict[str, Any]:
        if self.market_entry is None:
            raise ProbeError("no market entry point found in step 3")
        symbol = self.args.hose[0]
        end = self.as_of.isoformat()
        report: dict[str, Any] = {"symbol": symbol, "end": end}
        for source in SOURCES:
            frame, info = self.call(
                "depth", f"equity({symbol}).ohlcv(2000-01-01..{end}, 1D, source={source})",
                lambda src=source: self.market_entry(symbol).ohlcv(start="2000-01-01", end=end, interval="1D", source=src),
            )
            report[f"daily_{source}"] = {**info, **self.depth_facts(frame, f"depth-daily-{symbol}-{source}")}
        start = (self.as_of - timedelta(days=INTRADAY_LOOKBACK_DAYS)).isoformat()
        frame, info = self.call(
            "depth", f"equity({symbol}).ohlcv({start}..{end}, 1m)",
            lambda: self.market_entry(symbol).ohlcv(start=start, end=end, interval="1m"),
        )
        report["intraday_1m"] = {"start": start, **info, **self.depth_facts(frame, f"depth-1m-{symbol}")}
        return report

    def depth_facts(self, frame: Any, raw_name: str) -> dict[str, Any]:
        if frame is None or not hasattr(frame, "columns"):
            return {}
        facts: dict[str, Any] = {"raw": self.save_raw(raw_name, frame), "profile": self.frame_profile(frame)}
        if "time" in frame.columns and len(frame):
            times = self.pd.to_datetime(frame["time"], errors="coerce").dropna()
            if len(times):
                facts["oldest"] = str(times.min())
                facts["newest"] = str(times.max())
        return facts

    def step8_delisted(self) -> dict[str, Any]:
        report: dict[str, Any] = {"sources_note": "see DEFAULT_DELISTED comment for the press URLs"}
        end = self.as_of.isoformat()
        for symbol in self.args.delisted:
            entry: dict[str, Any] = {}
            frame, info = self.call("delisted", f"company({symbol}).events()", lambda s=symbol: self.ref.company(s).events())
            entry["events"] = {**info, **({"rows": int(len(frame))} if hasattr(frame, "columns") else {})}
            if self.market_entry is not None:
                frame, info = self.call(
                    "delisted", f"equity({symbol}).ohlcv(2024-01-01..{end}, 1D)",
                    lambda s=symbol: self.market_entry(s).ohlcv(start="2024-01-01", end=end, interval="1D"),
                )
                entry["ohlcv"] = {**info, **self.depth_facts(frame, f"delisted-ohlcv-{symbol}")}
            report[symbol] = entry
        return report

    def step9_rate_limit(self) -> dict[str, Any]:
        """events() over the seed at 6 s, 3 s, 2 s, 1 s, 0.5 s (10 calls each, 50 at most); stop at the first throttle sign."""
        if not self.seed:
            raise ProbeError("the rate-limit seed is empty")
        before = collect_status()
        calls: list[dict[str, Any]] = []
        stop: dict[str, Any] | None = None
        index = 0
        for cadence in RATE_CADENCES_S:
            for _ in range(CALLS_PER_CADENCE):
                symbol = self.seed[index % len(self.seed)]
                index += 1
                started = time.monotonic()
                frame, info = self.call(
                    "rate_limit", f"company({symbol}).events()", lambda s=symbol: self.ref.company(s).events()
                )
                attrs = dict(getattr(frame, "attrs", {}) or {}) if frame is not None else {}
                attrs_text = json.dumps(jsonable(attrs), ensure_ascii=False).lower()
                truncation = [word for word in TRUNCATION_WORDS if word in attrs_text]
                calls.append(
                    {
                        "cadence_s": cadence,
                        "seconds": info["seconds"],
                        "error": info.get("error"),
                        "warnings": len(info["warnings"]),
                        "attrs_truncation_words": truncation,
                        "rows": int(len(frame)) if hasattr(frame, "columns") else None,
                    }
                )
                if info.get("error") or info["warnings"] or truncation:
                    stop = {
                        "cadence_s": cadence,
                        "call_number": index,
                        "error": info.get("error"),
                        "warnings": info["warnings"],
                        "attrs_truncation_words": truncation,
                    }
                    break
                time.sleep(max(0.0, cadence - (time.monotonic() - started)))
            if stop:
                break
        after = collect_status()
        clean = [call["cadence_s"] for call in calls if not (call["error"] or call["warnings"] or call["attrs_truncation_words"])]
        return {
            "calls_made": len(calls),
            "stopped_at": stop,
            "fastest_cadence_without_signal_s": min(clean) if clean else None,
            "calls": calls,
            "status_before": status_lines(before),
            "status_after": status_lines(after),
        }

    def step11_hash_after(self) -> dict[str, Any]:
        after = collect_hashes(None, self.home, self.cwd)
        self.summary["hash_after"] = after
        return {"diff_vs_before": diff_hashes(self.summary["hash_before"], after)}

    def run(self) -> int:
        self.raw.mkdir(parents=True, exist_ok=True)
        if not self.step("1_import", self.step1_import):
            print(f"vnstock import failed; see {self.summary_path}", file=sys.stderr)
            return 1
        self.step("2_cli", self.step2_cli)
        if not self.step("3_api_surface", self.step3_api_surface):
            self.step("11_hash_after", self.step11_hash_after)
            print(f"API surface step failed; see {self.summary_path}", file=sys.stderr)
            return 1
        self.step("4_news", self.step4_news)
        self.step("5_events", self.step5_events)
        self.step("6_ohlcv_exdates", self.step6_ohlcv_exdates)
        self.step("7_depth", self.step7_depth)
        self.step("8_delisted", self.step8_delisted)
        self.step("9_rate_limit", self.step9_rate_limit)
        self.step("11_hash_after", self.step11_hash_after)  # step 10, the error catalogue, is written with every save
        self.summary["finished_at_utc"] = utc_now().isoformat()
        self.save()
        print(f"wrote {self.summary_path}")
        return 0


def symbol_list(value: str) -> tuple[str, ...]:
    symbols = tuple(part.strip().upper() for part in value.split(",") if part.strip())
    bad = [symbol for symbol in symbols if not SYMBOL.match(symbol)]
    if not symbols or bad:
        raise argparse.ArgumentTypeError(f"expected comma-separated symbols, got {value!r}")
    return symbols


def cmd_probe(args: argparse.Namespace) -> int:
    date.fromisoformat(args.as_of)  # fail before any vendor call
    if args.exdates and not Path(args.exdates).is_file():
        raise ProbeError(f"--exdates {args.exdates} is not a file")
    if (Path(args.out) / "probe-summary.json").exists():
        raise ProbeError(f"{Path(args.out) / 'probe-summary.json'} exists; choose another --out")
    return Probe(args).run()


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    hash_parser = commands.add_parser("hash", help="hash agent instruction files (never imports vnstock)")
    hash_parser.add_argument("--repo", help="the git clone to include (optional; omit under the probe user)")
    hash_parser.add_argument("--out", required=True, help="output directory (outside the clone)")
    hash_parser.add_argument("--label", required=True, help="file label, e.g. before-dev, after-probe")
    hash_parser.set_defaults(handler=cmd_hash)

    status_parser = commands.add_parser("status", help="run vnstock status/env/doctor with redaction")
    status_parser.add_argument("--out", required=True, help="output directory (outside the clone)")
    status_parser.set_defaults(handler=cmd_status)

    probe_parser = commands.add_parser("probe", help="run the R1.5 probe steps (imports vnstock)")
    probe_parser.add_argument("--out", required=True, help="output directory (outside the clone)")
    probe_parser.add_argument(
        "--symbols", required=True, help="VN30 (index members at probe time) or A,B,C: the rate-limit seed"
    )
    probe_parser.add_argument(
        "--exdates", help="file of SYMBOL,YYYY-MM-DD[,ratio] lines overriding the ex-dates picked from events"
    )
    probe_parser.add_argument(
        "--as-of", default=DEFAULT_AS_OF, help=f"fixed past end date for history requests (default {DEFAULT_AS_OF})"
    )
    for flag, default, text in (
        ("--hose", DEFAULT_HOSE, "three HOSE event symbols"),
        ("--hnx", DEFAULT_HNX, "one HNX event symbol"),
        ("--upcom", DEFAULT_UPCOM, "one UPCOM event symbol"),
        ("--delisted", DEFAULT_DELISTED, "delisted symbols"),
    ):
        probe_parser.add_argument(flag, type=symbol_list, default=default, help=f"{text} (default {','.join(default)})")
    probe_parser.set_defaults(handler=cmd_probe)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not sys.flags.isolated:
        print("vnstock_probe: run with `python -I` (isolated mode)", file=sys.stderr)
        return 2
    try:
        return args.handler(args)
    except (ProbeError, ValueError, OSError) as err:
        print(f"vnstock_probe {args.command}: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
