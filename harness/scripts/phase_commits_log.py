#!/usr/bin/env python3
"""phase_commits_log.py — the reader of a plan's append-only phase-commits log,
and the reopen facts drawn from it.

`plans/<p>/artifacts/phase-commits.jsonl` is written by `hs-run cook` (one line
per phase open mark, per verify, per reopen). Every reader draws its lines from
here: `hs_run_cook`, `finalize_plan` under a hook, the hard-stage gate in
`artifact_check`, the delegate nudge.

A `kind: reopen` line is written when `hs-run cook open --reason` takes a
`completed` plan back to `in_progress`. A phase it names is owed a verify until a
`kind: verify` line for that phase follows it in the file AND carries a `ts` no
earlier than its own: a verify written before the reopen and moved after it by a
merge of the log still predates it.
"""
import json
from pathlib import Path

FILE_NAME = "phase-commits.jsonl"


def path(plan_dir) -> Path:
    return Path(plan_dir) / "artifacts" / FILE_NAME


def read_records(plan_dir) -> list:
    """Every line that parses to a JSON object, oldest-first. Any other line
    degrades to "not there": a reader other steps' happy path does not depend on
    never takes one down."""
    p = path(plan_dir)
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def reopens(plan_dir) -> list:
    return [r for r in read_records(plan_dir) if r.get("kind") == "reopen"]


def reverify_owed(plan_dir) -> "tuple[dict | None, list]":
    """(the last reopen line, every phase some reopen named that no later verify
    has answered). Every reopen counts, not only the last: a reopen whose flip
    never landed is followed by another, and the first one's phases are still owed."""
    records = read_records(plan_dir)
    owed: dict = {}
    last = None
    for i, rec in enumerate(records):
        if rec.get("kind") != "reopen":
            continue
        last, ts = rec, str(rec.get("ts") or "")
        answered = {r.get("phase") for r in records[i + 1:]
                    if r.get("kind") == "verify" and str(r.get("ts") or "") >= ts}
        for phase in rec.get("phases") or []:
            if phase not in answered:
                owed.setdefault(phase, None)
    return last, list(owed)
