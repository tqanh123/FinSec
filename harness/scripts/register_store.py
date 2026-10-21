#!/usr/bin/env python3
"""register_store — shared primitives for append-only fenced-record registers
(the Decision Register `DEC-<n>` today; any future register reuses this).

One ruling per `---`-fenced YAML block. This module is the single home for the
structural machinery — the block-splitter regex, the record-fence/heading
injection escape, the best-effort file lock, and the raw id scan — so a fix
lands once. Register modules keep only their own specifics (field schema,
render template, alloc/append flow).

Lock posture: POSIX uses fcntl.flock. On platforms without it (or when flock
refuses) the lock degrades to a no-op BUT warns once per process: multiple
agents appending the same register concurrently on this machine can overwrite
each other's records. The warning informs — it never blocks the write; one
clone per developer (the deployment model) makes the race rare, and the
notebook files are typically written by the main agent at task end.
# TODO research: cross-platform lock (portalocker?) for Windows multi-agent
"""

import contextlib
import os
import re
import sys
from pathlib import Path
from typing import List, Optional


def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` atomically AND durably so neither a concurrent reader
    nor a crash/power-loss ever sees a half-written or empty register. Delegates to
    artifact_io.atomic_write_text (same-dir .tmp fsync'd before os.replace + same-dev
    assert), preserving newline="" for literal newlines. Shared by the register
    writers (decision_register, backlog_register, glossary, cook/output config)."""
    import artifact_io
    artifact_io.atomic_write_text(path, text, newline="")

# Splits a register file into its `---`-fenced record blocks: each starts at a
# line-anchored `---` fence; `fm` = the YAML mini-frontmatter, `body` = prose.
RECORD_RE = re.compile(
    r"^---\s*\n(?P<fm>.*?)\n---\s*\n(?P<body>.*?)(?=^---\s*$|\Z)",
    re.DOTALL | re.MULTILINE,
)
# A body line that is a bare `---` fence would split the file into a phantom
# record under RECORD_RE; neutralized on write (see escape_injection).
INJ_FENCE_RE = re.compile(r"(?m)^(---+\s*)$")


def escape_injection(text, heading_re) -> str:
    """Backslash-escape record-fence + register-heading line anchors in
    caller-supplied MULTILINE prose (the rationale) so it cannot smuggle a
    phantom record or heading, while preserving the text (markdown-correct:
    `\\---`, `\\## ` render literally but no longer match the line-anchored
    patterns). `heading_re` is the register's own heading anchor."""
    out = INJ_FENCE_RE.sub(r"\\\1", text or "")
    return heading_re.sub(r"\\\1", out)


def sanitize_field(text, heading_re) -> str:
    """Neutralize injection in SINGLE-LINE fields (title, affects): collapse
    newlines to spaces — a one-line field has no legitimate line breaks, and
    without them no line-anchored fence/heading can form — then run the same
    escape for defense in depth."""
    flat = re.sub(r"[\r\n]+", " ", text or "").strip()
    return escape_injection(flat, heading_re)


_warned_degraded = False  # once-per-process guard for the degraded-lock warning


def _flock(fh, op) -> bool:
    """Try fcntl.flock; op is 'ex' (exclusive) or 'un' (unlock). True on
    success, False when fcntl is unavailable or the lock is refused."""
    try:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX if op == "ex" else fcntl.LOCK_UN)
        return True
    except (ImportError, OSError):
        return False


def _warn_degraded() -> None:
    global _warned_degraded
    if _warned_degraded:
        return
    _warned_degraded = True
    sys.stderr.write(
        "[register_store] warning: file lock unavailable on this platform — "
        "multiple agents appending the same register concurrently on this "
        "machine may overwrite each other's records. Single-writer use is "
        "unaffected.\n"
    )


@contextlib.contextmanager
def register_lock(lock_path):
    """Best-effort exclusive lock so alloc-id + append happen as ONE critical
    section (closes the TOCTOU window where two looped allocs could collide).
    Degrades to a warn-once no-op where flock is unavailable — informs about
    the concurrent-overwrite risk, never blocks the write."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    locked = _flock(fh, "ex")
    if not locked:
        _warn_degraded()
    try:
        yield
    finally:
        if locked:
            try:
                _flock(fh, "un")
            except OSError:
                pass  # unlock is advisory; close must still happen
        fh.close()


def scan_record_ids(text: str) -> List[str]:
    """Every raw `id:` value across all `---`-fenced blocks, INCLUDING blocks
    whose YAML is otherwise unparseable — the source-of-truth for id allocation
    so a corrupt-but-id-bearing block still reserves its number (a later repair
    can never collide with an id allocated meanwhile)."""
    ids: List[str] = []
    for m in RECORD_RE.finditer(text):
        im = re.search(r"^id:\s*(\S+)\s*$", m.group("fm"), re.MULTILINE)
        if im:
            ids.append(im.group(1).strip())
    return ids


class UnreadableSSOT(ValueError):
    """A register's YAML SSOT did not parse, or is not a list of records.

    Raised for WRITE paths only. A reader may degrade a parse failure to "no
    records"; a writer that does the same turns the failure into a truncation —
    it renders or dumps the empty list it was handed. Each register subclasses
    this together with its own error type, so a library caller that already
    catches that type keeps catching it."""

    def __init__(self, path, reason: str, line: Optional[int] = None):
        self.path = str(path)
        self.reason = reason
        self.line = line
        where = "%s line %d" % (self.path, line) if line else self.path
        super().__init__(
            "refusing to write: %s does not read as a register (%s) — nothing "
            "was written; repair the file by hand and re-run" % (where, reason))

    def finding(self) -> dict:
        return {"error": "ssot_unreadable", "written": False, "file": self.path,
                "line": self.line, "message": str(self)}


def _yaml_error_line(exc) -> Optional[int]:
    """1-based line of a YAML error: where the broken construct OPENS when the
    parser says so (an unterminated quote is reported at end-of-stream, which
    points at nothing useful), else where it failed."""
    for attr in ("context_mark", "problem_mark"):
        mark = getattr(exc, attr, None)
        if mark is not None and getattr(mark, "line", None) is not None:
            return int(mark.line) + 1
    return None


def load_yaml_list(path, error=UnreadableSSOT) -> list:
    """The SSOT at `path` as a list, strictly. Absent or empty file -> [] (a
    fresh register). A parse failure or a non-list top level raises `error`
    naming the file, the reason and, when the parser knows it, the line."""
    import yaml_io
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise error(path, "unreadable: %s" % exc)
    try:
        data = yaml_io.safe_load(text)
    except (yaml_io.YAMLError, ValueError) as exc:
        reason = " ".join(str(exc).split())
        raise error(path, "YAML parse error: %s" % reason, _yaml_error_line(exc))
    if data is None:
        return []
    if not isinstance(data, list):
        raise error(path, "top level is a %s, not a list of records"
                    % type(data).__name__)
    return data


# ── post-write hook ─────────────────────────────────────────────────────────────
# A repo whose own generated files embed the register (a hash of the ledger, a
# page per term) declares the command that regenerates them; the register runs it
# after a successful write. Repo-owned and found under --root, so it follows the
# tree being written (a temp copy, a linked worktree) rather than the session.

#: Exit rung: the write landed, the declared post-write command did not succeed.
EXIT_POST_WRITE_FAILED = 4

POST_WRITE_HELP = """post-write hook: a repo may declare, in <root>/docs/registers.yaml,
a command this register runs after a successful append, add, update, amend or render
(cwd = <root>, no shell; env HARNESS_REGISTER, HARNESS_REGISTER_EVENT =
append|add|update|amend|render, HARNESS_REGISTER_ROOT):

    post_write:
      decisions:                      # or: glossary
        command: [python3, tools/regen_pages.py]   # argv list, or one string
        timeout_s: 120                # optional, default 120

The result is reported under `post_write` (rc, stderr tail, and `changed`: the
files the command wrote, measured with git around it; they join `commit_together`).
A failing, missing, timed-out or misconfigured command exits 4 — the write itself
stands."""

_POST_WRITE_CONFIG = Path("docs") / "registers.yaml"
_POST_WRITE_TIMEOUT_S = 120
_TAIL_LINES = 20


def _tail(text: str) -> str:
    return "\n".join((text or "").strip().splitlines()[-_TAIL_LINES:])


def run_post_write(root, register: str, event: str) -> Optional[dict]:
    """Run the command `<root>/docs/registers.yaml` declares for `register`, or
    None when nothing is declared. Never raises: every failure — config that does
    not parse or has the wrong shape, a command that is missing, times out or
    exits non-zero — comes back as `ok: false` with what is known (rc, stderr
    tail, error), for the caller to print and turn into its exit rung."""
    import shlex
    import subprocess
    root = Path(root)
    cfg_path = root / _POST_WRITE_CONFIG
    if not cfg_path.is_file():
        return None
    import yaml_io
    try:
        cfg = yaml_io.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (yaml_io.YAMLError, ValueError, OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "rc": None, "command": None,
                "error": "%s does not parse: %s" % (cfg_path, " ".join(str(exc).split()))}
    hooks = cfg.get("post_write") if isinstance(cfg, dict) else None
    if hooks is None:
        return None
    if not isinstance(hooks, dict):
        return {"ok": False, "rc": None, "command": None,
                "error": "%s: post_write must be a mapping of register -> "
                         "{command, timeout_s}" % cfg_path}
    entry = hooks.get(register)
    if entry is None:
        return None
    command = entry.get("command") if isinstance(entry, dict) else None
    if isinstance(command, str):
        command = shlex.split(command)
    timeout = entry.get("timeout_s", _POST_WRITE_TIMEOUT_S) if isinstance(entry, dict) else None
    if (not isinstance(command, list) or not command
            or not all(isinstance(a, str) and a for a in command)):
        return {"ok": False, "rc": None, "command": command,
                "error": "%s: post_write.%s.command must be a non-empty argv list "
                         "or string" % (cfg_path, register)}
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
        return {"ok": False, "rc": None, "command": command,
                "error": "%s: post_write.%s.timeout_s must be a positive integer"
                         % (cfg_path, register)}
    env = dict(os.environ, HARNESS_REGISTER=register, HARNESS_REGISTER_EVENT=event,
               HARNESS_REGISTER_ROOT=str(root))
    import process_group
    before = _dirty_state(root)
    try:
        proc = process_group.run(command, cwd=root, env=env, capture_output=True,
                                 text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        err = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) \
            else (exc.stderr or "")
        return _with_changed({"ok": False, "rc": None, "command": command,
                              "error": "timed out after %ds" % timeout,
                              "stderr_tail": _tail(err)}, root, before)
    except OSError as exc:
        return {"ok": False, "rc": None, "command": command,
                "error": "could not start %s: %s" % (command[0], exc)}
    return _with_changed({"ok": proc.returncode == 0, "rc": proc.returncode,
                          "command": command, "stderr_tail": _tail(proc.stderr)},
                         root, before)


def _dirty_state(root) -> "Optional[dict]":
    """`{path relative to root: digest of its bytes, or None when absent}` for every
    path git reports as changed or untracked under `root`; None when `root` is in no
    git tree. The post-write command belongs to the repo, so which files it writes is
    measured, never declared: two of these, taken around the command, differ exactly
    on the paths whose bytes it moved. Another writer in the same tree during that
    window is credited to the command too: the measure is the tree, not the process.
    `--no-optional-locks` keeps this read from taking the index lock a concurrent
    commit needs."""
    import hashlib
    import subprocess
    import process_group
    root = Path(root).resolve()
    try:
        top = process_group.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True, timeout=30)
        if top.returncode:
            return None
        st = process_group.run(["git", "--no-optional-locks", "-C", str(root), "status",
                                "--porcelain=v1", "-z",
                                "--untracked-files=all", "--no-renames", "--", "."],
                               capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if st.returncode:
        return None
    base = Path(top.stdout.strip())
    state: "dict[str, Optional[str]]" = {}
    for entry in st.stdout.decode("utf-8", "surrogateescape").split("\0"):
        if len(entry) < 4:
            continue
        path = base / entry[3:]
        try:
            rel = path.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            state[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except IsADirectoryError:
            continue
        except OSError:
            state[rel] = None
    return state


def _with_changed(hook: dict, root, before: "Optional[dict]") -> dict:
    """`hook` with `changed`: the paths whose bytes the command moved. Left out when
    either side could not be measured, so an empty list always means "measured, and
    nothing moved"."""
    after = _dirty_state(root) if before is not None else None
    if before is not None and after is not None:
        missing = object()
        hook["changed"] = sorted(p for p in set(before) | set(after)
                                 if before.get(p, missing) != after.get(p, missing))
    return hook


def report_post_write(result: dict, register: str, hook: Optional[dict]) -> int:
    """Attach a post-write outcome to a CLI result and pick the exit rung: 0 when
    nothing ran or it succeeded, EXIT_POST_WRITE_FAILED (with a stderr line)
    otherwise. The write itself is never undone. The files a SUCCESSFUL command
    changed join a `commit_together` the write already named, so committing that
    list carries them too; a failed one's files stay under `post_write.changed`
    only, since they may be half-written."""
    if hook is None:
        return 0
    result["post_write"] = hook
    if hook["ok"] and hook.get("changed") and isinstance(result.get("commit_together"), list):
        result["commit_together"] = sorted(set(result["commit_together"]) | set(hook["changed"]))
    if hook["ok"]:
        return 0
    sys.stderr.write(
        "[warn] %s post-write command failed (rc=%s%s) — the write stands; "
        "re-run the command by hand once fixed.%s\n" % (
            register, hook.get("rc"),
            ", " + hook["error"] if hook.get("error") else "",
            ("\n" + hook["stderr_tail"]) if hook.get("stderr_tail") else ""))
    return EXIT_POST_WRITE_FAILED
