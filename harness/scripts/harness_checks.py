"""The harness check ledger: what a harness-run check (`hs-code test run`, `check run`)
found for one plan phase, one line per run, in
`plans/<p>/artifacts/harness-checks-<phase>.jsonl`.

Append-only: each line goes out in ONE `write` on an O_APPEND descriptor (two runs at
once lose no line) and carries `actor` + `ts`; nothing reads, edits and rewrites it.
A line's `kind`:
    run                one run of a check: `check` (`<type>/<id>`), `type`, `status`
                       (PASS|FAIL|SKIP|BLOCKED|BROKEN), `format` + `file` (relative to
                       `artifacts/`, the key the DoD gate reads) + `sha256` of that file,
                       `inputs_digest` (what the result depends on: the tree the run started on,
                       which a run that changed it no longer proves -- `held`),
                       `reason` when it did not PASS
    baseline_approval  a decision about a check's baseline -- never a check result
    baseline_record    the same, written by the harness
    base_repin         the base a plan's policy floor is read at, moved by a DEC
Every reader filters on `kind`. A torn or non-object line is a `LedgerError`, never a
line skipped: a run that silently drops out reads like a run that never happened.

The harness never writes `verification.json` and never moves a phase's state: `cook
verify` (`fold_into`) folds the newest `run` line of each check into the record it
writes, refusing an agent check of the same name.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

KINDS = ("run", "baseline_approval", "baseline_record", "base_repin")
RUN_STATUSES = ("PASS", "FAIL", "SKIP", "BLOCKED", "BROKEN")
SOURCE = "harness-checks"


class LedgerError(ValueError):
    """A ledger line no reader can trust, or a line no writer may append."""


def torn_reason(e: "LedgerError") -> str:
    """Why a `--plan` run stops at a ledger it cannot read."""
    return ("the check ledger cannot be read (%s): a run is held to the plan's base and this "
            "one has none it can trust. Repair that line by hand -- the ledger is caged "
            "against shell writes and removals, so no agent command will" % e)


def ledger_path(plan_dir: Path, phase: str) -> Path:
    return Path(plan_dir) / "artifacts" / ("harness-checks-%s.jsonl" % phase)


def actor() -> str:
    hooks = str(Path(__file__).resolve().parents[1] / "hooks")
    if hooks not in sys.path:
        sys.path.append(hooks)
    from hook_runtime import resolve_actor
    return resolve_actor()


def file_sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _git(top: Path, *argv: str, data: "bytes | None" = None) -> bytes:
    import secret_scan_before_ship as secret_scan
    return subprocess.run(["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(top), *argv], input=data,
                          check=True, capture_output=True, env=secret_scan.git_safe_env()).stdout


def input_blobs(top: Path, rels: "list[str] | tuple[str, ...] | set[str]") -> "dict[str, str]":
    """(path -> blob id) of each of `rels` (relative to the checkout `top`): what a result
    depends on, by CONTENT. A clean file's blob id is the index's (never re-hashed); a
    modified or untracked one is hashed by `git hash-object`; a file that is not there
    reads as `absent`."""
    top = Path(top)
    index: "dict[str, str]" = {}
    for rec in _git(top, "ls-files", "-s", "-z").split(b"\0"):
        if rec:
            meta, path = rec.split(b"\t", 1)
            index[path.decode("utf-8", "surrogateescape")] = meta.split()[1].decode()
    modified = {p.decode("utf-8", "surrogateescape")
                for p in _git(top, "ls-files", "-m", "-z").split(b"\0") if p}
    want = sorted(set(rels))
    blobs = {r: index[r] for r in want if r in index and r not in modified}
    rehash = [r for r in want if r not in blobs and (top / r).is_file()]
    if rehash:
        out = _git(top, "hash-object", "--stdin-paths", data="\n".join(rehash).encode()).split()
        blobs.update(zip(rehash, (b.decode() for b in out)))
    return {r: blobs.get(r, "absent") for r in want}


def blobs_digest(blobs: "dict[str, str]") -> str:
    """sha256 over the (path, blob id) pairs of `input_blobs`."""
    h = hashlib.sha256()
    for r in sorted(blobs):
        h.update(("%s\0%s\n" % (r, blobs[r])).encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def held(before: "dict[str, str]", after: "dict[str, str]") -> "tuple[str, list[str]]":
    """The digest a run's line records, and the inputs that moved while it ran. A run whose
    inputs moved ran on no one tree: its line holds the tree it STARTED on, and the run
    itself is BLOCKED (`moved_reason`)."""
    changed = sorted(r for r in set(before) | set(after)
                     if before.get(r, "absent") != after.get(r, "absent"))
    return blobs_digest(before if changed else after), changed


def moved_reason(before: "dict[str, str]", changed: "list[str]", wrote_hint: str) -> str:
    """Why a run whose inputs moved under it proves nothing: a file that was there changed
    while it ran -- by the run or by anything else, the digests cannot tell -- and a file
    that was not there is one the run wrote and then reads."""
    wrote = [r for r in changed if before.get(r, "absent") == "absent"]
    moved = [r for r in changed if r not in wrote]
    parts = (["%s changed while it ran (by the run or by anything else): run it again on a "
              "still tree" % ", ".join(moved)] if moved else [])
    parts += ["it wrote %s, which it reads: %s" % (", ".join(wrote), wrote_hint)] if wrote else []
    return "the tree moved under the run, so it proves nothing: " + "; ".join(parts)


def verified_commits(plan: Path) -> "set[str]":
    """The commits the plan's verifications name (`blastRadius.commits` of each
    `verification-*.json` / `phase-latest-*.json`)."""
    out: "set[str]" = set()
    art = Path(plan) / "artifacts"
    for f in [*art.glob("verification-*.json"), *art.glob("phase-latest-*.json")]:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        br = rec.get("blastRadius") if isinstance(rec, dict) else None
        commits = br.get("commits") if isinstance(br, dict) else None
        if isinstance(commits, list):
            out.update(c for c in commits if isinstance(c, str) and c)
    return out


#: what a run leaves in a plan's artifacts -- and a run that is not BLOCKED happens only
#: under an open mark. Never `results/`: `write_verification` copies a result file there
#: with or without a mark
_RUN_LEFT = ("harness-runs",)


def marks_removed(plan: Path) -> "list[str]":
    """What a run left in `plan` while its phase-commits log holds no line and no re-pin
    names a base: its marks were removed (a mark taken now would re-anchor the plan at
    HEAD, owing nothing it changed before). Empty for a plan that never ran -- and for one
    from before the marks, whose verifications name no commit."""
    import phase_commits_log
    plan = Path(plan)
    if phase_commits_log.read_records(plan):
        return []
    art = plan / "artifacts"
    found = ["%s/" % t for t in _RUN_LEFT
             if (art / t).is_dir() and any(p.is_file() for p in (art / t).rglob("*"))]
    if verified_commits(plan):
        found.append("a verification naming its commits")
    for f in sorted(art.glob("harness-checks-*.jsonl")):
        phase = f.name[len("harness-checks-"):-len(".jsonl")]
        try:
            if rows(plan, phase, "base_repin"):
                return []
            ran = any(r.get("status") != "BLOCKED" for r in rows(plan, phase, "run"))
        except LedgerError:
            ran = True
        if ran:
            found.append(f.name)
    return found


def marks_removed_reason(plan: Path, found: "list[str]", phase: str = "<phase>") -> str:
    return ("%s holds what a run leaves (%s) but no line in artifacts/phase-commits.jsonl: its "
            "open marks were removed, and a mark taken now would re-anchor the plan at HEAD -- "
            "no mark taken here. Put its base back: hs-code check run <id> --repin-base <sha> "
            "--dec <DEC-id> --plan %s --phase %s" % (Path(plan).name, ", ".join(found), plan, phase))


def append(plan_dir: Path, phase: str, row: dict) -> dict:
    """Append one line; returns it as written."""
    plan_dir = Path(plan_dir)
    kind = row.get("kind")
    if kind not in KINDS:
        raise LedgerError("kind must be one of %s, got %r" % (", ".join(KINDS), kind))
    if kind == "run":
        if row.get("status") not in RUN_STATUSES or not row.get("check"):
            raise LedgerError("a run line needs `check` and a status in %s" % ", ".join(RUN_STATUSES))
        if row.get("file"):
            row = dict(row, sha256=file_sha256(plan_dir / "artifacts" / row["file"]))
    line = dict(row, plan=plan_dir.name, phase=phase, actor=actor(),
                ts=datetime.now(timezone.utc).isoformat())
    path = ledger_path(plan_dir, phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(line, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    return line


def rows(plan_dir: Path, phase: str, kind: Optional[str] = None) -> "list[dict]":
    path = ledger_path(plan_dir, phase)
    if not path.is_file():
        return []
    out = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError as e:
            raise LedgerError("%s line %d is not JSON (%s) -- a torn write; the ledger cannot "
                              "be read past it" % (path, n, e)) from None
        if not isinstance(row, dict) or row.get("kind") not in KINDS:
            raise LedgerError("%s line %d is not a ledger line (no known `kind`)" % (path, n))
        if kind is None or row["kind"] == kind:
            out.append(row)
    return out


def latest_runs(plan_dir: Path, phase: str) -> "dict[str, dict]":
    """The newest `run` line of each check, in first-run order."""
    out: "dict[str, dict]" = {}
    for row in rows(plan_dir, phase, "run"):
        out.pop(row["check"], None)
        out[row["check"]] = row
    return out


def newest_run(plan_dir: Path, check: str, phase: Optional[str] = None,
               accept: "Optional[Callable[[dict], bool]]" = None) -> Optional[dict]:
    """The newest `run` line of `check` that `accept` takes (every line, by default): in
    `phase`'s ledger, or -- no phase -- across every phase ledger of the plan, by `ts`."""
    phases = [phase] if phase else [p.name[len("harness-checks-"):-len(".jsonl")]
                                    for p in sorted((Path(plan_dir) / "artifacts").glob("harness-checks-*.jsonl"))]
    best: Optional[dict] = None
    for ph in phases:
        for row in rows(plan_dir, ph, "run"):
            if row.get("check") != check or (accept is not None and not accept(row)):
                continue
            if best is None or str(row.get("ts", "")) >= str(best.get("ts", "")):
                best = row
    return best


def provenance_problem(plan_dir: Path, row: Optional[dict],
                       digest_now: "Callable[[], str]") -> "Optional[tuple[str, str]]":
    """Why `row` -- a check's newest `run` line -- does not prove the check passes now, as
    (kind, detail); None when it does. The one predicate the DoD gate and `cook next`
    both ask. Kinds:
        missing     no run on record
        unmeasured  the run could not measure (BROKEN/BLOCKED): its reason, not a rerun
        failed      the run measured and did not PASS
        tampered    its result file is gone or no longer has the sha256 the run recorded
        stale       the inputs it was run on (`inputs_digest`) are not the tree's now
    `digest_now` is asked last: it reads the tree, and only a PASS still standing needs it.
    """
    if row is None:
        return "missing", "no run of it by the harness on record"
    status, reason = row.get("status"), row.get("reason") or ""
    if status in ("BROKEN", "BLOCKED"):
        return "unmeasured", "its newest run is %s: %s" % (status, reason or "no reason recorded")
    if status != "PASS":
        return "failed", "its newest run is %s%s" % (status, (": " + reason) if reason else "")
    rel = row.get("file")
    if not rel or not row.get("sha256"):
        return "tampered", "its newest run names no result file"
    path = Path(plan_dir) / "artifacts" / rel
    if not path.is_file():
        return "tampered", "its result file %s is gone" % rel
    if file_sha256(path) != row["sha256"]:
        return "tampered", "its result file %s no longer has the sha256 the run recorded" % rel
    if row.get("inputs_digest") != digest_now():
        return "stale", "its inputs changed since the run at %s" % row.get("ts", "?")
    return None


class Unreadable(Exception):
    """The checks a repo declares, or the policy they are held to, cannot be read."""


def unproven(root: Path, plan_dir: Path, changed: "list[str]", class_floor: "Optional[float]" = None,
             class_enforcement: str = "hard", scoped_ok: bool = False) -> "list[dict]":
    """Each check `changed` owes that the harness's own newest run does not prove now:
    `{check, enforcement, command, problem, why}` (`declared_checks.unproven`); `class_floor`
    is the change class's numeric line floor (it owes `coverage/total`); `scoped_ok` takes a
    narrower test run for the full one (a plan that defers its suite, in cook). Asked in a
    child process -- what is owed reads the code map through the code-graph engine, which
    the hooks and hs-run never import. `Unreadable` when a declaration cannot be read or
    the reader fails: a caller fails closed on it. Asked even with no `changed`: the
    reader adds what the plan changed since its base."""
    import process_group
    script = Path(__file__).resolve().parent / "declared_checks.py"
    try:
        argv = [sys.executable, str(script), "--root", str(root), "--plan", str(plan_dir),
                "--class-enforcement", class_enforcement]
        if class_floor is not None:
            argv += ["--class-floor", str(class_floor)]
        if scoped_ok:
            argv.append("--scoped-ok")
        r = process_group.run(argv,
                              input=json.dumps(list(changed)), capture_output=True, text=True,
                              cwd=str(root), timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unreadable("the declared-check reader did not answer: %s" % e) from None
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Unreadable("the declared-check reader failed (exit %d): %s"
                         % (r.returncode, r.stderr.strip()[-600:] or "no output")) from None
    if out.get("error"):
        raise Unreadable(out["error"])
    return list(out.get("unproven") or [])


def as_check(row: dict) -> dict:
    """A `run` line as a `verification.json` check: BROKEN folds as BLOCKED (it could not
    be measured; FAIL means it was, and came back wrong), its reason kept."""
    check = {"name": row["check"], "status": "BLOCKED" if row["status"] == "BROKEN" else row["status"],
             "source": SOURCE}
    for k in ("format", "file", "sha256", "inputs_digest", "reason"):
        if row.get(k):
            check[k] = row[k]
    return check


def fold_into(rec: dict, plan_dir: Path) -> "list[str]":
    """Fold the ledger of `rec`'s phase into `rec["checks"]`; returns the agent check
    names that collide with a harness check (the caller refuses the record)."""
    runs = latest_runs(plan_dir, rec["phase"])
    if not runs:
        return []
    agent = [c for c in rec.get("checks") or [] if isinstance(c, dict)]
    clash = sorted(str(c["name"]) for c in agent if c.get("name") in runs)
    if not clash:
        rec["checks"] = agent + [as_check(r) for r in runs.values()]
    return clash
