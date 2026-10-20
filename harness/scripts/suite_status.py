#!/usr/bin/env python3
"""suite_status.py — ONE classification of what the stored suite run says
about the tree standing at `root` RIGHT NOW.

Extracted out of `hs_run_suite.NEXT` so two readers share the exact same
answer for the exact same tree, rather than growing two independent copies of
"is this junit fresh":

    * `hs-run suite next` (`hs_run_suite.py`)     — the chain-printing reader:
      same states, plus the runnable `next_command` that produces a fresh one.
    * `hs-code test status` (`codegraph/test/status.py`) — the read-only
      glance: same states, the envelope's `data` (or a human line under `--text`), never runs anything.

Both call `classify(root)` and get back the SAME `SuiteStatus` for the same
tree; a bug fixed here is fixed for both at once, and the two can never
silently drift on what `stale` means.

FLAT + STDLIB-ONLY (plus `tree_content_key`, `codegraph_snapshot_reader`,
`test_result_readers`, `harness_paths` — themselves flat/stdlib-only) and
this module MUST NEVER import `codegraph`: `hs_run_suite.py` (an `hs_run*.py`
module) imports this module, and `test_codegraph_snapshot_reader.py`'s
closure gate walks the full import closure of every `hs_run*.py` module
looking for a `codegraph` import at any depth — this module sits directly in
that closure.

State vocabulary is `harness/scripts/hs_run.py`'s own `State` enum names,
reused verbatim (never a private spelling) so `hs-run suite next`'s `state`
and `hs-code test status`'s `data.state` are the identical string for the
identical tree:

    not_run  — no `junit.xml` recorded under `<data_root>/suite/<worktree_key>/` at all.
    stale    — a `junit.xml` exists but its `junit.meta.json` is missing/
               unreadable, or its recorded `content_key` does not match
               `tree_content_key.content_key(root)` measured right now, or
               the junit itself cannot be parsed.
    red      — a fresh junit with >=1 failure or error.
    green    — a fresh junit, 0 failures, 0 errors.

`pending` is reported ALONGSIDE any of the four states above (not a fifth
state): `junit.meta.pending.json` existing means some chain (`hs-run suite
next`'s own printed chain, or a hand run of the same steps) stamped a
fingerprint before its run finished and never promoted it — the run is either
still in flight, or it died mid-chain before step 5's `mv`.

WHAT IT CANNOT SEE
    * Freshness is measured only against `tree_content_key.content_key(root)`
      AT CALL TIME — a second call, one edit later, can answer differently
      for the exact same files this call already read.
    * A `pending` stamp only proves a chain WROTE it; this module has no way
      to tell a chain that is still running from one that died three days ago
      — both leave the identical file behind.
    * Nothing here resolves or validates a suite recipe (`ROPS_SUITE_LANE`
      and friends) — that is `hs_run_suite.py`'s own `_config_missing` path,
      entirely orthogonal to what is already sitting on disk.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import attrs as _attrs  # noqa: E402  (REQUIRED — see harness/requirements.txt)

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import codegraph_snapshot_reader  # noqa: E402 — flat; worktree_key only
import harness_paths  # noqa: E402
import test_result_readers  # noqa: E402
import tree_content_key  # noqa: E402


@_attrs.define(frozen=True, kw_only=True)
class SuiteArtifact:
    """A fresh junit's own read — the `red`/`green` states' payload. Same
    shape `hs_run_security.SweepArtifact` follows (a hand-built dict here is
    what `test_hs_run_records_are_declared_objects.py` exists to catch)."""

    junit: str
    total: int
    failed: int
    errors: int
    skipped: int
    passed: int

    def as_dict(self) -> dict:
        return _attrs.asdict(self)


@_attrs.define(frozen=True, kw_only=True)
class SuiteStatus:
    """One classification of the tree standing at `root`, read fresh on
    every `classify()` call — never cached across calls."""

    state: str
    junit_path: str
    reason: "Optional[str]" = None
    artifact: "Optional[SuiteArtifact]" = None
    ts: "Optional[str]" = None
    actor: "Optional[str]" = None
    command: "Optional[str]" = None
    pending: bool = False
    pending_ts: "Optional[str]" = None
    pending_actor: "Optional[str]" = None
    pending_command: "Optional[str]" = None

    def as_dict(self) -> dict:
        return _attrs.asdict(self)


def suite_dir(root: Path) -> Path:
    """`<data_root>/suite/<worktree_key>/` — the per-worktree directory every
    reader of this tree's suite state shares. `harness_paths.data_root_for`
    (never the bare `data_root()`) because `root` is always caller-supplied,
    never this process's own project."""
    return (harness_paths.data_root_for(root) / "suite"
            / codegraph_snapshot_reader.worktree_key(root))


def _read_meta(meta_path: Path) -> "Optional[dict]":
    """The `{content_key, command, actor, ts}` a prior chain wrote, or `None`
    when the file is missing OR unreadable OR not that shape — any of those
    means "cannot trust this junit's fingerprint," never distinguished
    further; there is nothing a caller could do differently for one over
    another."""
    try:
        blob = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict) or not isinstance(blob.get("content_key"), str):
        return None
    return blob


def classify(root: Path) -> SuiteStatus:
    """The shared classification. `root` is resolved fresh — never memoised,
    since the whole point is answering for the tree as it stands RIGHT NOW."""
    root = Path(root).resolve()
    sdir = suite_dir(root)
    junit_path = sdir / "junit.xml"
    meta_path = sdir / "junit.meta.json"
    pending_path = sdir / "junit.meta.pending.json"

    pending_meta = _read_meta(pending_path)
    pending = pending_path.is_file()
    pending_ts = pending_meta.get("ts") if pending_meta else None
    pending_actor = pending_meta.get("actor") if pending_meta else None
    pending_command = pending_meta.get("command") if pending_meta else None

    if not junit_path.is_file():
        return SuiteStatus(
            state="not_run", junit_path=str(junit_path),
            reason="no suite run recorded for this tree yet",
            pending=pending, pending_ts=pending_ts, pending_actor=pending_actor,
            pending_command=pending_command)

    current_key = tree_content_key.content_key(root)
    meta = _read_meta(meta_path)
    stale_reason = None
    if meta is None:
        stale_reason = ("%s is missing or unreadable — the stored junit's "
                        "fingerprint cannot be trusted" % meta_path)
    elif current_key is None or meta.get("content_key") != current_key:
        stale_reason = ("the stored junit was measured against a different tree "
                        "than the one standing here now")
    if stale_reason:
        return SuiteStatus(
            state="stale", junit_path=str(junit_path), reason=stale_reason,
            ts=(meta or {}).get("ts"), actor=(meta or {}).get("actor"),
            command=(meta or {}).get("command"),
            pending=pending, pending_ts=pending_ts, pending_actor=pending_actor,
            pending_command=pending_command)

    # Narrowed by the `stale_reason` check above: reaching here means
    # `meta is not None` (the only branch that leaves it `None` returns
    # first) — spelled out for mypy, which cannot see that across the
    # earlier `if`/`elif`.
    assert meta is not None

    try:
        summary = test_result_readers.read_junit(junit_path)
    except test_result_readers.TestResultError as exc:
        reason = "%s could not be read: %s" % (junit_path, exc)
        return SuiteStatus(
            state="stale", junit_path=str(junit_path), reason=reason,
            ts=meta.get("ts"), actor=meta.get("actor"), command=meta.get("command"),
            pending=pending, pending_ts=pending_ts, pending_actor=pending_actor,
            pending_command=pending_command)

    artifact = SuiteArtifact(junit=str(junit_path), total=summary["total"],
                             failed=summary["failed"], errors=summary["errors"],
                             skipped=summary["skipped"], passed=summary["passed"])
    state = "red" if (summary["failed"] or summary["errors"]) else "green"
    return SuiteStatus(
        state=state, junit_path=str(junit_path), artifact=artifact,
        ts=meta.get("ts"), actor=meta.get("actor"), command=meta.get("command"),
        pending=pending, pending_ts=pending_ts, pending_actor=pending_actor,
        pending_command=pending_command)
