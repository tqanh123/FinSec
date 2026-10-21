#!/usr/bin/env python3
"""
memory_gap — the deterministic SCRIPT detector for "memory that looks unrecorded".

SCRIPT-only: it correlates persisted disk/graph state and emits structured
signals — it makes NO judgment (the LLM decides what to do about each). It NEVER
re-implements a detector it can import: fence detection is `check_fence.scan`; the
graph + `body_hash` + parse-error surface are `spec_graph`. One home per fact —
this module only correlates.

Signals at THIS tier (each `{type, severity, subject, evidence, suggested_writer}`):
  - `fence_breach` — a working-tree change landed OUTSIDE the declared ownership
    zones. Reuses `check_fence.scan` verbatim; the suggested writer is the
    in-zone move, not a memory write. Severity `warn`.
  - `parse_error` — an artifact the graph could not parse. Surfaced advisory so a
    malformed file is visible, never a crash. Severity `warn`.

The wider source funnel (validate_no_marker / approved_changed_no_dec /
judged_not_stored, plus the `--ack-no-dec` suppression) is deferred: those rest
on a validated-snapshot marker, a decision register `affects` set, and a verdict
cache this tier does not yet stand up. `REGISTERED_SIGNAL_TYPES` is the explicit
tier-1 contract — the presence-closure test asserts it, so a later tier widens it
in one place and a silent trim is caught.

Deterministic: same disk state → same `signals` list (sorted, no wall-clock in the
body). ALWAYS exits 0 (advisory) — any failure surfaces as a `parse_error` signal,
never a traceback.

CLI:
    memory_gap.py --root <project-dir>          # emit {signals:[...]}, exit 0
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from encoding_utils import configure_utf8_console
import check_fence
from spec_graph import build_graph

configure_utf8_console()


# The signal types this detector is wired to emit at this tier. Declared as DATA
# (not just implied by behavior) so the presence-closure test can assert the exact
# tier-1 set: a silent trim — dropping a signal without breaking an import — still
# fails that test. A later tier appends to this tuple in ONE place.
REGISTERED_SIGNAL_TYPES = ("fence_breach", "parse_error")


# ----------------------------------------------------------------------------
# Ack store — "I know about this fence_breach, it is intentional"
# ----------------------------------------------------------------------------
#
# There was no way to tell the detector "seen it, on purpose" — the only lever
# was to stop touching the file (defeats the point) or ignore the advisory
# forever (the fence_breach spam this whole change closes). This gives an
# operator/agent a real write path.
#
# The TTL is NOT optional. A permanent ack is how a fence gap gets voted through
# once and then forgotten: the exemption survives, the reason for it does not,
# and nobody is ever prompted to check whether it is still true. A TTL forces
# re-confirmation — re-run the ack — instead of fossilizing into a silent
# carve-out; when it lapses, `collect()` simply stops filtering that subject and
# the signal comes back on its own, no one has to remember to remove a line.
#
# Searched harness/data/*.yaml first for an existing home for a constant like
# this (the pattern `decision_register._confirm_ttl` reads `confirm_ttl_s` from
# decision-governance.yaml) — no fence/memory-gap config file exists yet, so
# this follows the OTHER established pattern instead: a plain module constant,
# same as `session_gc.TTL_DAYS` (14d, hardcoded) — with an explicit per-ack
# `--ttl-days` override at write time for a caller who needs something other
# than the default.

_ACK_FILENAME = "memory-gap-ack.json"
_ACK_TTL_DAYS_DEFAULT = 7.0


def _ack_state_dir(root) -> Path:
    """The state dir the ack store lives under: `HARNESS_STATE_DIR` (test/CI
    override, same env every other harness state reader honors) if set, else
    `<root>/.harness/state` — `root` is already the resolved project dir every
    caller here passes explicitly (unlike the hooks package, this script never
    infers a project root of its own)."""
    raw = os.environ.get("HARNESS_STATE_DIR")
    return Path(raw) if raw else Path(root) / ".harness" / "state"


def _ack_path(root) -> Path:
    return _ack_state_dir(root) / _ACK_FILENAME


def _subject_hash(subject: str) -> str:
    """A short, stable key for one subject (a single fence_breach file path).
    Hashed rather than used as a raw dict key so an unusual path (very long,
    odd characters) never becomes the on-disk JSON key verbatim; the read side
    (`_live_acked_subjects`) computes the same hash to look a subject up, so the
    two ends never need to agree on path-escaping rules."""
    return hashlib.sha256((subject or "").encode("utf-8")).hexdigest()[:16]


def _read_acks(root) -> Dict[str, Dict[str, Any]]:
    """The raw ack map `{subject_hash: {subject, acked_at, acked_by, reason,
    ttl_days}}`. Fail-open: missing/corrupt/unreadable -> {} (no ack filters
    anything — a broken ack store must never look like "everything is fine")."""
    try:
        p = _ack_path(root)
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        acks = data.get("acks") if isinstance(data, dict) else None
        return acks if isinstance(acks, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_acks(root, acks: Dict[str, Dict[str, Any]]) -> None:
    """Persist the ack map. Writes via a sibling temp file + atomic replace so a
    concurrent reader (the Stop hook's `collect()` call) never observes a
    half-written file."""
    p = _ack_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({"acks": acks}, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(p)


def ack_subject(root, subject: str, reason: str = "", actor: str = "",
                ttl_days: Optional[float] = None, now: Optional[str] = None) -> str:
    """Record "seen this fence_breach subject, it's intentional" — the public
    write path into the ack store (nothing else in this module writes it;
    `collect()`/`_fence_signals` only ever read it). A human runs this by hand
    (`memory_gap.py --ack <path> --reason "..."`) or an agent calls it directly.

    Every ack carries a TTL (default `_ACK_TTL_DAYS_DEFAULT`, override via
    `ttl_days`) — see the module note above for why a permanent ack is refused
    by design: it is a fence exemption that would otherwise never expire, never
    get re-reviewed, and never remind anyone it exists.

    Returns the subject's hash key (what landed in the store), so a caller can
    log or later look up exactly what was written."""
    root = Path(root).resolve()
    acks = _read_acks(root)
    key = _subject_hash(subject)
    acks[key] = {
        "subject": subject,
        "acked_at": now or _now_iso(),
        "acked_by": actor or "",
        "reason": reason or "",
        "ttl_days": float(ttl_days) if ttl_days is not None else _ACK_TTL_DAYS_DEFAULT,
    }
    _write_acks(root, acks)
    return key


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _parse_iso(ts: str) -> Optional[float]:
    """Epoch seconds for an `_now_iso`-shaped string, or None if unparsable."""
    try:
        return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _live_acked_subjects(root, now_s: Optional[float] = None) -> set:
    """The subject strings with a currently non-expired ack. An entry whose
    `acked_at`/`ttl_days` cannot be parsed is treated as NOT live (excluded from
    the set) rather than as an indefinite ack — a malformed record must fail
    toward showing the real signal, never toward silently hiding it."""
    now_s = time.time() if now_s is None else now_s
    live = set()
    for entry in _read_acks(root).values():
        if not isinstance(entry, dict):
            continue
        subject = entry.get("subject")
        acked_epoch = _parse_iso(entry.get("acked_at") or "")
        ttl_days = entry.get("ttl_days")
        if not subject or acked_epoch is None:
            continue
        if not isinstance(ttl_days, (int, float)) or isinstance(ttl_days, bool) or ttl_days <= 0:
            continue
        if now_s - acked_epoch <= ttl_days * 86400:
            live.add(subject)
    return live


# ----------------------------------------------------------------------------
# Signal builders
# ----------------------------------------------------------------------------

def _signal(stype: str, severity: str, subject: Optional[str], evidence: str,
            suggested_writer: str) -> Dict[str, Any]:
    return {
        "type": stype,
        "severity": severity,
        "subject": subject,
        "evidence": evidence,
        "suggested_writer": suggested_writer,
    }


# Cap on individually-listed fence breaches before they collapse into one aggregate
# signal. A session that legitimately touches many out-of-zone files must never flood
# the advisory into an over-report — so the tail is summarized, not enumerated, with
# the full count preserved. This cap is the tier's flood-control (check_fence reports
# every breach faithfully; the cap lives here, one layer up).
_FENCE_SIGNAL_CAP = 10


def _fence_signals(root) -> List[Dict[str, Any]]:
    """One `fence_breach` per file `check_fence.scan` reports outside the declared
    zones, up to `_FENCE_SIGNAL_CAP`; any overflow collapses into a single aggregate
    signal carrying the total count. Reuses the imported scan verbatim — no
    re-implementation of the porcelain walk.

    A subject with a still-live ack (`ack_subject`, not yet past its TTL) is
    filtered out HERE, before a signal is ever built for it — "acked" means the
    detector does not raise it at all, not "raises it but something downstream
    swallows it". The cap/overflow accounting below runs on the POST-ack breach
    list, so an acked file never counts toward "+N more" either."""
    breaches = check_fence.scan(Path(root))
    acked = _live_acked_subjects(root)
    if acked:
        breaches = [f for f in breaches if f.get("file") not in acked]
    out: List[Dict[str, Any]] = []
    for f in breaches[:_FENCE_SIGNAL_CAP]:
        out.append(_signal(
            "fence_breach", "warn", f["file"],
            f.get("detail") or f"{f['file']} touched outside the declared ownership zones.",
            "review the out-of-zone change; move it under a declared zone if it belongs there",
        ))
    extra = len(breaches) - _FENCE_SIGNAL_CAP
    if extra > 0:
        out.append(_signal(
            "fence_breach", "warn", None,
            f"+{extra} more files touched outside the declared zones ({len(breaches)} total).",
            "review the out-of-zone changes; move any that belong under a declared zone",
        ))
    return out


# ----------------------------------------------------------------------------
# Correlate → signals
# ----------------------------------------------------------------------------

def collect(root, include_parse_errors: bool = True) -> List[Dict[str, Any]]:
    """Build every tier-1 memory-gap signal for `root`, deterministically sorted.

    Order key = (type, subject) so the output is stable across runs (same disk
    state → same list). A malformed artifact surfaces as a `parse_error` signal
    (from the graph's `parse_errors`) — never a crash.

    `include_parse_errors` gates the SPEC-GRAPH parse (`build_graph`, which walks
    and parses every `docs/product/` artifact — the dominant cost). A caller that
    knows this turn cannot have introduced a docs/product parse error (it touched
    no artifact under that tree) passes False to skip the parse entirely and pay
    only the cheap fence scan. Default True preserves the CLI + every existing
    caller. The `parse_error` signal is a function of DISK state, so a standing
    breakage is caught by the docs-validation pipeline; this hot-path scoping only
    declines to re-derive it on turns that provably could not have caused one."""
    root = Path(root).resolve()

    signals: List[Dict[str, Any]] = []

    # Surface any artifact the graph could not parse (advisory — never block).
    # Skipped when the caller excludes parse errors — build_graph is the 220ms
    # cost, and it contributes ONLY these signals to this collector.
    if include_parse_errors:
        graph = build_graph(root)
        for pe in graph.get("parse_errors") or []:
            signals.append(_signal(
                "parse_error", "warn", pe.get("file"),
                f"{pe.get('file')} could not be parsed: {pe.get('error')}",
                "fix the artifact frontmatter (no memory write applies until it parses)",
            ))

    signals.extend(_fence_signals(root))

    signals.sort(key=lambda s: (s["type"], s.get("subject") or ""))
    return signals


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--ack", metavar="SUBJECT",
                    help="Acknowledge a fence_breach subject (the file path "
                         "check_fence.scan/collect() reports) as intentional. "
                         "Writes to the ack store and exits — does not also scan.")
    ap.add_argument("--reason", default="",
                    help="Why this subject is acked (recorded for audit, not enforced).")
    ap.add_argument("--ttl-days", type=float, default=None,
                    help="Override the default ack TTL (days, default %s). An ack "
                         "always expires; there is no permanent option." % _ACK_TTL_DAYS_DEFAULT)
    args = ap.parse_args()
    root = Path(args.root).resolve()

    if args.ack:
        actor = os.environ.get("HARNESS_USER") or os.environ.get("USER") or ""
        key = ack_subject(root, args.ack, reason=args.reason, actor=actor,
                          ttl_days=args.ttl_days)
        print(json.dumps({
            "acked": args.ack, "key": key,
            "ttl_days": args.ttl_days if args.ttl_days is not None else _ACK_TTL_DAYS_DEFAULT,
        }, ensure_ascii=False))
        return 0

    try:
        signals = collect(root)
        print(json.dumps({"signals": signals}, indent=2, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:  # noqa: BLE001 — advisory contract: never crash
        # Advisory: any unexpected failure surfaces as a parse_error signal + exit 0,
        # never a bare traceback.
        print(json.dumps(
            {"signals": [_signal("parse_error", "warn", None, str(exc),
                                 "investigate the spec state")]},
            ensure_ascii=False))
        return 0


if __name__ == "__main__":
    sys.exit(main())
