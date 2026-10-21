#!/usr/bin/env python3
"""plan_approval.py — write the plan-approval artifact (personal-first SLIM).

No roster, no quorum, no role rule: self-approval is deliberate anti-drift
discipline, not an anti-fraud check (actor strings are env-derived and
spoofable by design, so a role gate would prove nothing). The real guard is
plan_hash, which binds the approval to the exact plan body.

plan_hash pins the plan's NORMALIZED content: YAML frontmatter is stripped
from every file, a hand-off block's stand-alone `applied:` marker line is
stripped from every file, and the `## Phases` section is stripped from plan.md
before hashing. Those are exactly the two regions the cook workflow legitimately
mutates after approval (status flips, phase table updates) — hashing them
verbatim would go stale on every run and train reviewers to rubber-stamp.
The one exception is the `plan-graph.yaml` sidecar: it is hashed RAW (no
frontmatter strip), because a sidecar opening with `---` would otherwise be
swallowed to "" by the frontmatter rule and drop out of the hash entirely.
Trade-off, on purpose: status metadata is not drift-guarded; the body (the
thing approval is about) is. Any other edit ⇒ re-approve.

The artifact is the only in-session write path for plans/*/artifacts/
plan-approval.yaml (legacy .json primaries are kept in place; both sit on
the write-guard list in installed repos), and this CLI refuses to write on
a bad verdict, missing sidecar, empty rationale, or an unresolvable author.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import frontmatter_parser  # noqa: E402
from plan_files import (  # noqa: E402,F401 — the files the hash covers
    DEFAULT_REFERENCE_DIRS, _FRONTMATTER_RE, _plan_files, _reference_dir_entries,
    _walk_reference_dirs, reference_dir_problems, reference_files)
import fs_guard  # noqa: E402
import harness_paths  # noqa: E402
import plan_status  # noqa: E402
import redteam_disposition  # noqa: E402

_HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.append(str(_HOOKS_DIR))
import hook_runtime  # noqa: E402
import trace_log  # noqa: E402

SCHEMA = "plan-approval/v1"
# Bumped whenever `_normalized_text`, `_plan_files` or the plan_hash combination
# changes; a receipt records the scheme it was hashed under, so a drift on an
# older receipt can say the hashing may be what moved.
HASH_SCHEME = 2

_PHASES_SECTION_RE = re.compile(r"(?ms)^## Phases\s*$.*?(?=^## |\Z)")
# Narrow, deliberate exceptions to the frontmatter strip below. Frontmatter is
# stripped because it is where status metadata lives, and status is SUPPOSED to
# move after approval — hashing it would report drift on every ordinary
# workflow step. But a handful of keys in there are not status: a shipped
# reader branches on them, so flipping one after approval changes what the
# harness does while the anchor says nothing moved.
#
# Cut by KEY, never by block. Stitching the whole frontmatter back would close
# the hole and turn `status: pending -> done` into fake drift, and a gate that
# fires on normal work gets disabled.
#
# Each entry is measured from its reader, not guessed:
#   plan.md `grid:`  — toggles the coverage-grid gate family
#                      (grid_emit_guard.require_emit, write_approval's grid
#                      arm, grid_flag_carry)
#   plan.md `defer_suite:` — toggles whether cook's suite gate defers
#                      (defer_suite_carry, hs_run_cook._run_preflight)
#   plan.md `posture:` — forces cook's implement posture past the declared-file
#                      threshold (workflow_weight.declared_posture, read by
#                      hs_run_cook._phase_posture and hooks/cook_delegate_nudge.py)
#   phase   `in_place` / `mode` / `delegate` / `owns` — the per-phase inline waiver
#                      cook_delegate_nudge read until the posture moved to
#                      workflow-weight (75bb14f6d); no reader since, still pinned.
#                      The live per-plan force is plan.md `posture:` above.
#   plan.md `reference_dirs:` — selects which files the hash itself covers
#                      (reference_files); dropping a dir must not pass as no-op.
_PINNED_FM_KEYS = {
    "plan": ("grid", "defer_suite", "posture", "reference_dirs"),
    "phase": ("in_place", "mode", "delegate", "owns"),
}


def _pinned_fm_lines(frontmatter: str, keys) -> list:
    """The `key:` lines to fold back, each with its indented continuation.

    `owns:` is routinely written as a YAML block list, so a line-only match
    would pin the key and miss every value under it — the waiver would still be
    editable after approval through the one key whose VALUE is the scope."""
    out: list = []
    for key in keys:
        pattern = re.compile(
            r"(?m)^%s:.*$(?:\n(?:[ \t]+\S.*|[ \t]*-[ \t].*))*" % re.escape(key))
        out.extend(m.group(0) for m in pattern.finditer(frontmatter))
    return out


def _pinned_fm_values(frontmatter: str, keys) -> list:
    """The RESOLVED value of each pinned key, as the readers actually see it.

    The line pin above folds source text; every reader that branches on these
    keys goes through a YAML parser instead. Anywhere the two disagree, the
    value a reader acts on is editable without moving the approved digest —
    measured with an alias whose anchor sits on a line the line pin does not
    fold (`_flag: &flag true` / `defer_suite: *flag`): the resolved value went
    True -> False, the digest did not move. Folding the parsed value closes the
    class rather than that one spelling.

    Only keys PRESENT in the parsed frontmatter contribute, so a file carrying
    none of them still hashes byte-identically to before any of this existed.
    Unparseable frontmatter contributes nothing: no reader can resolve a value
    from it either, and the line pin still covers the source text.
    """
    parsed = frontmatter_parser.parse_text(frontmatter, "<frontmatter>")
    if not parsed.get("ok"):
        return []
    fm = parsed.get("frontmatter") or {}
    if not isinstance(fm, dict):
        return []
    out: list = []
    for key in keys:
        if key not in fm:
            continue
        out.append("%s=%s" % (
            key, json.dumps(fm[key], sort_keys=True, default=str)))
    return out


# -------------------------------------------------------------- normalize ---

def normalize_actor(actor) -> str:
    """`user:<u>/agent:<a>` → `user:<u>`, then casefold + strip — the agent
    suffix is a persona of the same person, and so are case/whitespace variants
    of the same identity (a git email is commonly mixed-case). Comparing the
    normalized form is what makes `reviewer != author` hold against a casing
    difference, so the self-review block cannot be sidestepped by approving from
    `BOB@x.com` what `bob@x.com` authored. (the normalization extends bare
    agent-suffix stripping with case + whitespace insensitivity.)"""
    # .strip('"').strip("'") closes F1: a plan author read from `author:`
    # frontmatter arrives WITH surrounding quotes, and a quoted form must
    # normalize equal to the bare actor or a self-review/self-override slips.
    return str(actor).split("/agent:")[0].strip().strip('"').strip("'").strip().casefold()


# ------------------------------------------------------- normalized hashes ---

# What a document points its reader at: a markdown link target (optionally
# <bracketed>) or a backticked path. `#section` and `:line` suffixes name a place
# in the file, not another file.
_LINK_TARGET = re.compile(r"\]\(\s*<?([^)\s>]+)>?[^)]*\)|`([^`\s]+)`")
_LOCATION_SUFFIX = re.compile(r"(?:#.*|:\d+(?:[-:]\d+)?)$")


def unhashed_links(plan_dir) -> list:
    """Plan-dir files a phase file points at that the approval hash does not
    cover, as plan-relative paths. A phase file is the executor's instructions,
    so what it links is read as part of the plan; outside `reference_dirs` such a
    file can change after approval with no drift. `artifacts/` is left out: it
    is state, not instructions. A link resolves against the phase file's own
    dir, then the plan dir, then — for a repo-rooted spelling — the part after
    the plan dir's name."""
    plan_dir = Path(plan_dir)
    base = plan_dir.resolve()
    hashed = {f.resolve() for f in _plan_files(plan_dir)}
    phases = [f for f in _plan_files(plan_dir) if f.name.startswith("phase-")
              and f.suffix == ".md" and f.parent.resolve() in (base, base / "phases")]
    marker = base.name + "/"
    out = []
    for doc in phases:
        try:
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _LINK_TARGET.finditer(text):
            target = _LOCATION_SUFFIX.sub("", m.group(1) or m.group(2))
            if not target or "://" in target:
                continue
            tail = target.split(marker, 1)[1] if marker in target else None
            for cand in (doc.parent / target, base / target, base / tail if tail else None):
                if cand is None:
                    continue
                try:
                    p = cand.resolve()
                    rel = p.relative_to(base)
                except (OSError, ValueError):
                    continue
                if not p.is_file():
                    continue
                if p not in hashed and rel.parts[0] != "artifacts":
                    out.append(rel.as_posix())
                break
    return sorted(set(out))


# A pause narrows the approved scope (the phase leaves the completion count) and
# lifting one restores it. The hash leaves the pause lines out and the record pins
# the paused set instead, so only a NEW pause re-opens approval (`drift_reason`).
_PAUSE_KEYS = frozenset({"paused", "paused_reason", "reopen_when"})
_PAUSE_KEY_RE = re.compile(r"^([ \t]*)(?:paused|paused_reason|reopen_when)[ \t]*:")


def _without_pause_lines(text: str) -> str:
    """The sidecar minus each pause key line and its continuation lines (indented
    deeper). A sidecar with no pause line comes back byte-identical."""
    out, depth = [], None
    for line in text.splitlines(keepends=True):
        if depth is not None:
            if not line.strip() or len(line) - len(line.lstrip(" \t")) > depth:
                continue
            depth = None
        m = _PAUSE_KEY_RE.match(line)
        if m:
            depth = len(m.group(1))
            continue
        out.append(line)
    return "".join(out)


def _sidecar_text(text: str) -> str:
    """Raw, so comments and layout stay pinned; minus the pause lines when that
    removes every pause key. A flow-style `{paused: ...}` cannot be cut by line,
    so that sidecar stays raw and lifting its pause still reads as drift."""
    stripped = _without_pause_lines(text)
    if stripped == text:
        return text
    import yaml_io
    try:
        data = yaml_io.safe_load(stripped)
    except yaml_io.YAMLError:
        return text
    subtasks = (data or {}).get("subtasks") if isinstance(data, dict) else None
    if isinstance(subtasks, dict) and any(
            isinstance(st, dict) and _PAUSE_KEYS & set(st) for st in subtasks.values()):
        return text
    return stripped


def paused_phases(plan_dir) -> list:
    """The sidecar's paused phases, sorted — what an approval pins in place of the
    pause lines its hash leaves out."""
    import plan_graph
    graph = plan_graph.parse_phase_graph(plan_dir)
    return [] if graph.get("error") else sorted(plan_graph.paused_phases(graph))


_FM_END = re.compile(rb"(?m)^(?:---|\.\.\.)[ \t]*$")
# A `status:` line whose value is a plain scalar (an optional trailing comment
# aside). An anchor or alias on it can feed a pinned key, so that line stays in.
_FM_STATUS_LINE = re.compile(
    rb"(?m)^status[ \t]*:[ \t]*(?:[\w.-]+|\"[^\"\n&*]*\"|'[^'\n&*]*')?[ \t]*(?:#[^\n]*)?$\n?")


def _raw_digest(path: Path, keep_status: bool = False) -> str:
    """Digest of the file's bytes. FROZEN — a receipt keeps these digests for as
    long as the plan lives, so this definition must never change; that is what
    makes it a proof across harness versions where `_normalized_text` is not.
    The one exception: a markdown frontmatter's plain-scalar `status:` line(s),
    which the workflow flips right after approval and on every step — never
    pinned, and as a plain scalar never the source of a pinned value."""
    data = path.read_bytes()
    if not keep_status and path.suffix == ".md" and data.startswith(b"---"):
        end = _FM_END.search(data[3:])
        if end:
            cut = 3 + end.start()
            data = _FM_STATUS_LINE.sub(b"", data[:cut]) + data[cut:]
    return hashlib.sha256(data).hexdigest()[:12]


def raw_hashes(plan_dir) -> dict:
    """plan-dir-relative path → `_raw_digest`, for every hashed file. What lets a
    later harness tell "this file moved" from "this harness hashes it
    differently" — `file_hashes` alone answers under today's normalization only."""
    base = Path(plan_dir)
    return {f.relative_to(base).as_posix(): _raw_digest(f) for f in _plan_files(plan_dir)}


def _bytes_unchanged(plan_dir: Path, rel: str, rec) -> bool:
    """True when `rel`'s bytes are proven identical to what the receipt saw: its
    recorded raw digest matches, or — a receipt written before raw digests were
    kept — its recorded digest was taken over these very bytes (normalization
    only removes text, so equality with the raw digest means nothing moved)."""
    f = plan_dir / rel
    if not f.is_file():
        return False
    recorded_raw = (rec.get("raw_hashes") or {}).get(rel)
    if recorded_raw is not None:
        return recorded_raw == _raw_digest(f)
    return (rec.get("file_hashes") or {}).get(rel) == _raw_digest(f, keep_status=True)


def drifted_files(plan_dir, rec, now_hash=None) -> "list | None":
    """The plan files whose content moved since `rec` was written; None when the
    plan hash still matches (nothing to name)."""
    plan_dir = Path(plan_dir)
    if rec.get("plan_hash") == (now_hash or plan_hash(plan_dir)):
        return None
    recorded = rec.get("file_hashes") or {}
    now = file_hashes(plan_dir)
    return sorted(
        set(k for k in now if now[k] != recorded.get(k)
            and not (k in recorded and _bytes_unchanged(plan_dir, k, rec)))
        | set(k for k in recorded if k not in now))


def newly_paused(plan_dir, rec) -> list:
    """The phases paused after `rec` was written."""
    plan_dir = Path(plan_dir)
    recorded = rec.get("file_hashes") or {}
    if "paused" in rec:
        before = set(rec.get("paused") or [])
    elif "plan-graph.yaml" in recorded and _bytes_unchanged(plan_dir, "plan-graph.yaml", rec):
        # A receipt from before the paused set was recorded hashed the pause
        # lines themselves; an untouched sidecar holds the pauses it approved.
        before = set(paused_phases(plan_dir))
    else:
        before = set()
    return sorted(set(paused_phases(plan_dir)) - before)


def approval_status(plan_dir, root=None) -> dict:
    """Read-only: whether the plan's approval still clears the gate, by the gate's own
    check, and what moved since it was written. Writes nothing."""
    import artifact_check
    plan_dir = Path(plan_dir)
    out: dict = {"ok": True, "plan": plan_dir.name}
    rec, problem = artifact_check._load_artifact(plan_dir, "plan-approval")
    if rec is None:
        out.update(holds=False, verdict=None,
                   reason="no plan approval recorded" if problem == "missing"
                   else "plan approval %s" % problem)
        return out
    reason = artifact_check._check_artifact(plan_dir, "plan-approval", root)
    now = plan_hash(plan_dir)
    out.update(holds=reason is None, verdict=rec.get("verdict"), ts=rec.get("ts"),
               plan_hash=rec.get("plan_hash"), plan_hash_now=now,
               drifted=drifted_files(plan_dir, rec, now) or [],
               newly_paused=newly_paused(plan_dir, rec), reason=reason)
    return out


def brief(result: dict) -> dict:
    """`result` with its full record swapped for what a reader acts on: the verdict,
    the plan hash, how many files it pins and when. The record is on disk at
    `artifact`; `--json` prints it whole."""
    rec = result.get("record")
    if not isinstance(rec, dict):
        return result
    out = {k: v for k, v in result.items() if k != "record"}
    out.update({k: rec[k] for k in ("verdict", "plan_hash", "ts") if k in rec})
    if "file_hashes" in rec:
        out["files"] = len(rec.get("file_hashes") or {})
    for k in ("decision", "decided_by"):
        if k in rec:
            out[k] = rec[k]
    return out


def drift_reason(plan_dir, rec) -> "str | None":
    """Why an APPROVED record no longer covers the plan on disk, or None. The one
    predicate the gate and the plan projection's drift classifier both read.

    A file counts as moved only when its digest differs under today's
    normalization AND its bytes are not proven unchanged since the receipt
    (`_bytes_unchanged`), so changing how the harness normalizes a file never
    reads as an edit to it. A file the receipt never saw is moved: the receipt
    cannot vouch for it."""
    plan_dir = Path(plan_dir)
    recorded = rec.get("file_hashes") or {}
    drifted = drifted_files(plan_dir, rec)
    if drifted is not None:
        if drifted or not recorded:
            older = ("" if rec.get("hash_scheme") == HASH_SCHEME else
                     " (this receipt was written by an older harness that hashed "
                     "plans differently and kept no byte digest for these files, so "
                     "a listed file may only be hashed differently, not edited)")
            return ("plan content changed after approval (drifted: %s)%s — the plan "
                    "body must be re-approved: re-run plan_approval.py. Frontmatter "
                    "status, the plan.md Phases table and lifting a pause are exempt; "
                    "any body edit re-opens approval — that friction is the "
                    "anti-drift working, not a bug"
                    % (", ".join(drifted) or "unknown — re-run plan_approval.py", older))
    newly = newly_paused(plan_dir, rec)
    if newly:
        return ("phase(s) paused after approval: %s — a pause takes a phase out of "
                "what done means, so the narrower plan must be re-approved: re-run "
                "plan_approval.py" % ", ".join(newly))
    return None


def _normalized_text(path: Path) -> str:
    if path.suffix != ".md" and path.name != "plan-graph.yaml":
        # A linked non-markdown file (a probe script, a data sample) has no
        # frontmatter or status region: its bytes are the content.
        return path.read_bytes().decode("utf-8", errors="replace")
    if path.name == "plan.md" or path.name.startswith("phase-"):
        text = path.read_text(encoding="utf-8")
    else:
        # A reference file is the author's material in whatever encoding it came
        # in; hashing it must not crash the gate that reads the hash.
        text = path.read_bytes().decode("utf-8", errors="replace")
    if path.name == "plan-graph.yaml":
        # Raw-hash the sidecar: it has no mutable status region to strip, and a
        # sidecar that opens with `---` would be swallowed to "" by the frontmatter
        # strip and collide on the empty-sha digest. One special-case here covers
        # both plan_hash and file_hashes (both route through this function).
        return _sidecar_text(text)
    # plan.md and a phase file pin DIFFERENT keys, because different readers
    # branch on them; everything not listed stays stripped as before.
    keys = _PINNED_FM_KEYS["plan" if path.name == "plan.md" else "phase"]
    fm_match = _FRONTMATTER_RE.match(text)
    raw_fm = fm_match.group(0) if fm_match else ""
    pinned = (_pinned_fm_lines(raw_fm, keys)
              + _pinned_fm_values(raw_fm, keys)) if fm_match else []
    text = _FRONTMATTER_RE.sub("", text, count=1)
    if pinned:
        # NUL-delimited so a pinned line can never collide with legitimate body
        # content. A file whose frontmatter carries none of these keys hashes
        # byte-identically to before this existed, which is what keeps already
        # -approved plans from all reporting drift at once.
        text = "".join("\x00%s\x00\n" % line for line in pinned) + text
    # A hand-off block's `applied:` marker is written AFTER approval by design
    # (it records that the block was pasted), in plan.md or a phase file alike.
    # Hashing it would drop an approved plan to approval_invalid over a
    # bookkeeping line. A file with no such line hashes byte-identically.
    import plan_handoff
    text = plan_handoff.APPLIED_LINE.sub("", text)
    if path.name == "plan.md":
        # Only plan.md owns a legitimately-mutating `## Phases` section;
        # the same heading in a phase file is body and stays pinned.
        # Stripped AFTER the pin-prepend above, which is order-equivalent: the
        # pinned lines carry no `## Phases` heading, so neither order can eat
        # the other's text. Stated because the previous shape did it the other
        # way round and the digests must stay identical for shipped approvals.
        text = _PHASES_SECTION_RE.sub("", text, count=1)
    return text


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]

_APPROVAL_EXTS = (".yaml", ".json")


def _existing_primary(plan_dir):
    """The on-disk primary approval (plan-approval.yaml preferred, .json legacy)
    or None when neither exists yet."""
    art = Path(plan_dir) / "artifacts"
    for ext in _APPROVAL_EXTS:
        cand = art / ("plan-approval%s" % ext)
        if cand.is_file():
            return cand
    return None


def _dump_record(rec: dict, path) -> None:
    """Write an approval record via the shared gate-artifact writer: run_seq stamp (D1)
    from the orchestrator-exported env + atomic same-dir .tmp + os.replace. Format is
    implied by the extension; a dev without an orchestrator gets run_seq:null."""
    import artifact_io
    artifact_io.stamp_and_write(Path(path), rec)


def _import_grid_guard():
    """Import seam for ``grid_emit_guard`` — tests monkeypatch THIS (not the
    bare ``import``) to force an ImportError deterministically at the
    coverage-grid gate. Door 1's ``_provenance_ok`` already fails CLOSED on
    the identical fault (its "unverifiable" state still blocks); this seam
    is what lets door 2 be pinned to match instead of failing open one step
    earlier, at import time."""
    import grid_emit_guard
    return grid_emit_guard


def file_hashes(plan_dir) -> dict:
    """plan-dir-relative POSIX path → sha256-12hex of that file's normalized
    content. Lets the gate name exactly WHICH file drifted after approval.

    Keyed on the relative path, not the basename: a plan carrying BOTH the
    flat (phase-1.md) and phases/ (phases/phase-1.md) layouts — the "mixed
    layout" plan_layout_check.py already warns about — used to collide both
    onto the same basename key, silently dropping one file's digest and
    leaving the drift message unable to name which file actually moved."""
    base = Path(plan_dir)
    return {f.relative_to(base).as_posix(): _digest(_normalized_text(f))
            for f in _plan_files(plan_dir)}


def plan_hash(plan_dir) -> str:
    """sha256-12hex over the plan dir's HASHED REGION, sorted by name and
    filename-delimited so renames change the hash too.

    "The whole plan dir" is what this used to claim and is not what it does —
    a sentence that led a reader to conclude the opposite of the truth. The
    region is `_plan_files`' name-pattern allow-list (plan.md, phase-*.md at
    either layout, plan-graph.yaml) MINUS what `_normalized_text` strips by
    content. Everything else is outside, and outside is not an oversight in
    four of the five cases — it is state that is SUPPOSED to move after
    approval, where hashing it would report drift on ordinary work:

      * plan.md frontmatter other than the pinned keys (`grid`, `defer_suite`) —
        `status:` changes on every workflow step, while those two are consent
        decisions a reader branches on and are therefore pinned, both as the
        source line and as the value a YAML parser resolves from it;
      * plan.md's `## Phases` section;
      * a phase file's frontmatter other than `in_place` / `mode` / `delegate` /
        `owns` — `status:` tracks the phase being worked and must stay free to
        move, while those four are read by `hooks/cook_delegate_nudge.py` to
        decide whether the delegate signal fires and are therefore pinned (again
        line AND resolved value: the two disagree wherever YAML offers an
        indirection, and the value is the half a reader acts on);
      * plan-graph.yaml's pause lines (`paused`, `paused_reason`, `reopen_when`)
        — the record pins the paused SET instead, and `drift_reason` re-opens
        approval only for a phase paused after it (lifting a pause widens scope).

    The fifth is a real boundary rather than a moving region: a file matching
    NO name pattern (`notes.md`) and outside the plan's reference dirs
    (`reference_files`: `refs/` unless plan.md `reference_dirs:` names others)
    leaves no trace. Sweeping the whole dir was considered and declined — a plan
    dir accumulates scratch files and living logs (a checklist, a decision log, a
    hand-off), and a gate that breaks on each one gets routed around. Following
    every link was measured and declined for the same reason: plan.md links
    those logs. What a phase file links outside the hashed region is named at
    approval time (`unhashed_links`) instead of hashed. See
    `harness/tests/test_the_approval_hash_states_what_it_does_not_cover.py`,
    which pins every line of this paragraph as a measurement."""
    h = hashlib.sha256()
    for f in _plan_files(plan_dir):
        h.update(f.name.encode("utf-8"))
        h.update(b"\x00")
        h.update(_normalized_text(f).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:12]


# ----------------------------------------------------------------- author ---

_AUTHOR_FM_RE = re.compile(r"^author:\s*(.+?)\s*$", re.MULTILINE)


def _author_from_trace(plan_name) -> "str | None":
    """Best-effort: actor of a plan-creation trace event for this plan."""
    try:
        trace_dir = harness_paths.trace_dir()
        for f in sorted(trace_dir.glob("trace-*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") == "plan_created" \
                        and rec.get("target") == plan_name:
                    return rec.get("actor")
    except OSError:
        pass
    return None


def _resolve_author(plan_dir: Path) -> "str | None":
    """Trace event (if a plan_created event exists) → plan.md frontmatter
    `author:` → None (caller must demand --author; guessing an author would
    let the role check pass against the wrong person)."""
    author = _author_from_trace(plan_dir.name)
    if author:
        return author
    pm = plan_dir / "plan.md"
    try:
        fm = _FRONTMATTER_RE.match(pm.read_text(encoding="utf-8"))
    except OSError:
        return None
    if fm:
        m = _AUTHOR_FM_RE.search(fm.group(0))
        if m:
            return m.group(1)
    return None


# ------------------------------------------------------------------ write ---

def write_approval(plan_dir, verdict, rationale, author=None,
                   reviewer=None) -> dict:
    """Write plans/<plan>/artifacts/plan-approval.yaml (legacy .json primaries
    refresh in place). Personal-first SLIM: no roster, no quorum, no role rule —
    self-approval is deliberate discipline (anti-drift), not an anti-fraud check.
    The APPROVED verdict still requires the plan-graph sidecar; the plan_hash still
    binds the approval to the exact plan body. Refuses (no write) on a bad verdict,
    missing sidecar, empty rationale, or unresolvable author."""
    plan_dir = Path(plan_dir)
    # Case-insensitive verdict: a valid intent in any case (approved / ApprOvEd /
    # " approved ") normalizes to the canonical upper form, so it is never a
    # false-positive reject. Garbage still fails the membership check below.
    verdict = str(verdict).strip().upper()
    if not (plan_dir / "plan.md").is_file():
        return {"ok": False,
                "error": "no plan.md in %s — point --plan at a plan dir or its "
                         "plan.md" % plan_dir}
    # The phase-DAG sidecar is a mandatory plan artifact (hs:plan step 5). An
    # APPROVED verdict is refused without it — presence gate, fail-closed; a
    # REJECTED verdict needs no sidecar (you can reject an incomplete plan).
    if verdict == "APPROVED" and not (plan_dir / "plan-graph.yaml").is_file():
        return {"ok": False,
                "error": "no plan-graph.yaml in %s — the phase-DAG sidecar is "
                         "mandatory for approval (author it in hs:plan step 5: "
                         "edges + per-phase file ownership), then re-approve"
                         % plan_dir}
    # Parity with the plan-graph presence gate above: a plan that DECLARES grid
    # mode (`grid: true` in plan.md frontmatter, stamped by hs:plan --grid) must
    # ship the macro coverage-grid artifact. An APPROVED verdict is refused
    # without it — presence-only (the coverage-grid VERDICT is never read here; a
    # `reject`/`needs-detail` verdict stays advisory). Grid not declared -> no
    # requirement.
    #
    # This door used to re-derive that whole decision itself by reaching into
    # FIVE of grid_emit_guard's own underscore-prefixed helpers by name
    # (find_coverage_grid, _has_no_real_content, _provenance_ok,
    # _axis_selection_ok, and — once the ruleset-mark cross-check existed —
    # _ruleset_mark_ok), plus its own copy of `resolve_grid_mode` wrapped in a
    # bare `except Exception: grid_required = False`. Two problems, the same
    # root cause: (1) that broad except converted ANY resolver fault into
    # "grid not required" — which skips every layer below and writes
    # APPROVED, the exact hole this fix closes (see the regression test); and
    # (2) renaming one of those five underscore helpers already caused a
    # silent gate outage once, because nothing type-checks a private-name
    # reference across modules. Routing through the SAME public
    # `grid_guard.require_emit(plan_dir)` the CLI's `--require` door runs
    # closes both: one call, no private coupling, and `resolve_grid_mode`
    # only ever runs INSIDE `require_emit` now (never wrapped in a try/except
    # here) — a fault there surfaces as a real, uncaught error instead of a
    # silently granted approval. `require_emit`'s own message already carries
    # this door's previously-pinned substrings (coverage-grid / fill /
    # ENVIRONMENT / provenance+engine / axis / ruleset), so mapping it
    # through with an appended "then re-approve" keeps this door's own
    # wording convention intact rather than dropping it.
    if verdict == "APPROVED":
        try:
            grid_guard = _import_grid_guard()
        except ImportError as e:
            return {"ok": False,
                    "error": "cannot evaluate the coverage-grid gate for %s — "
                             "the grid_emit_guard module failed to import "
                             "(%s). This is an ENVIRONMENT fault; fix it, "
                             "then re-approve" % (plan_dir, e)}

        grid_message, grid_code = grid_guard.require_emit(plan_dir)
        if grid_code != 0:
            return {"ok": False, "error": "%s, then re-approve" % grid_message}
    # A red-team REVISE left undisposed refuses APPROVED here exactly as it holds
    # `plan next` at needs_redteam_disposition — one predicate, so the approval
    # door is not the way around the step. REJECTED needs no disposition.
    if verdict == "APPROVED":
        held = redteam_disposition.check(
            plan_dir, redteam_disposition.reports(plan_dir)).blocking
        if held:
            return {"ok": False, "error": "%s, then re-approve" % "; ".join(held)}
    if verdict == "APPROVED":
        import plan_graph
        graph = plan_graph.parse_phase_graph(plan_dir)
        bad_post = [] if graph.get("error") else plan_graph.find_post_problems(graph)
        if bad_post:
            return {"ok": False, "error": "plan-graph.yaml %s — then re-approve"
                                          % "; ".join(bad_post)}
        bad_refs = reference_dir_problems(plan_dir)
        if bad_refs:
            return {"ok": False, "error": "%s — then re-approve (plan.md "
                                          "`reference_dirs:` names the reference "
                                          "dirs)" % "; ".join(bad_refs)}
    if verdict not in ("APPROVED", "REJECTED"):
        return {"ok": False,
                "error": "verdict must be APPROVED or REJECTED (got %r)"
                         % verdict}
    if not (rationale or "").strip():
        return {"ok": False, "error": "a non-empty --rationale is required"}

    reviewer = reviewer or hook_runtime.resolve_actor()
    author = (author or "").strip() or _resolve_author(plan_dir)
    if not author:
        return {"ok": False,
                "error": "cannot resolve the plan author (no creation trace "
                         "event, no `author:` frontmatter in plan.md) — pass "
                         "--author user:<who> explicitly; refusing to write "
                         "an approval with an empty author"}

    # project_root(), NOT root(): this value is only ever handed to plans-plane helpers —
    # fs_guard.assert_under(target, "plans", root=root) below and plan_status.flip_status's
    # own containment splice. Both resolve the PROJECT's plans/, so the hint has to name
    # the project. It is the same ruling `:448` follows, and the same variable-carries-one-
    # job check: nothing here reads an engine resource off this root, so migrating it
    # cannot drag a bin-scoped read along with it.
    #
    # Why it needed checking separately instead of riding along with :448 —
    # fs_guard._zone_base honours an explicit `root` ONLY when HARNESS_BIN_ROOT is unset.
    # Under a global bin it picks the base per zone itself and discards the hint, so this
    # site is INVISIBLE in that layout and load-bearing in the other. Measuring one layout
    # and generalising is how it was nearly missed.
    #
    # Not a widened fence: the allowed root moves from the harness checkout's plans/ to the
    # worked-on project's plans/ — a different tree, not a larger one. Under a per-project
    # install the two coincide; under a global bin the hint is discarded either way. The
    # fence still refuses a target outside the project's plans/ — pinned by
    # test_plan_approval_project_root.py::test_a_target_outside_the_project_plans_zone_is_still_refused.
    root = harness_paths.project_root()

    # A plan.md (or phase file) containing bytes that are not valid UTF-8
    # raises UnicodeDecodeError out of _normalized_text's bare
    # `read_text(encoding="utf-8")` — this must surface as a readable error,
    # not an uncaught traceback out of write_approval.
    try:
        ph = plan_hash(plan_dir)
        fh = file_hashes(plan_dir)
    except UnicodeDecodeError as e:
        return {"ok": False,
                "error": "cannot compute the plan hash for %s — plan.md (or "
                         "a phase file) contains bytes that are not valid "
                         "UTF-8 (%s); fix the file's encoding, then retry"
                         % (plan_dir, e)}

    rec = {
        "schema": SCHEMA,
        "plan": plan_dir.name,
        "plan_hash": ph,
        "file_hashes": fh,
        "raw_hashes": raw_hashes(plan_dir),
        "hash_scheme": HASH_SCHEME,
        "paused": paused_phases(plan_dir),
        "author": author,
        "reviewer": reviewer,
        "verdict": verdict,
        "rationale": rationale,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    # Personal-first SLIM: one self-approval per plan. Refresh the primary in
    # place (keeping a legacy .json format) or write plan-approval.yaml. No
    # co-sign fan-out, no reviewer-hash files — quorum/roster are gone.
    art = plan_dir / "artifacts"
    existing_primary = _existing_primary(plan_dir)
    target = existing_primary if existing_primary is not None else art / "plan-approval.yaml"
    try:
        fs_guard.assert_under(target, "plans", root=root)
    except fs_guard.FenceError as e:
        # Match write_approval's contract: every failure returns a structured
        # error, never an uncaught crash.
        return {"ok": False, "error": str(e)}
    target.parent.mkdir(parents=True, exist_ok=True)
    _dump_record(rec, target)
    trace_log.append_event("plan_approval", "approval_written",
                           actor=reviewer, target=plan_dir.name,
                           status=verdict,
                           note="plan_hash=%s author=%s" % (
                               rec["plan_hash"], author))
    # Reflect the verdict into the plan's status: an APPROVED plan moves
    # pending -> approved so the board shows it as reviewed-and-ready rather than
    # lumped with un-reviewed pending plans. Ordering matters — the flip happens
    # AFTER the artifact is written, and plan_hash strips frontmatter, so flipping
    # status never invalidates the approval just recorded. error_on_other=False
    # makes APPROVED on an in_progress/approved/completed plan a benign idempotent
    # no-op; only `pending` flips. The flip is secondary to the approval: if it
    # fails (containment, missing status line) the artifact still stands, so a
    # flip error is logged and swallowed rather than failing the approval.
    if verdict == "APPROVED":
        try:
            flip = plan_status.flip_status(
                plan_dir, allowed_from={"pending"}, to="approved",
                error_on_other=False, root=root)
            if not flip.ok:
                sys.stderr.write(
                    "[plan_approval] approval written but status flip skipped: "
                    "%s\n" % flip.message)
        except Exception as e:  # noqa: BLE001 — flip is secondary to the approval
            sys.stderr.write(
                "[plan_approval] approval written but status flip errored: %s\n"
                % e)
    out = {"ok": True, "artifact": str(target), "record": rec}
    loose = unhashed_links(plan_dir) if verdict == "APPROVED" else []
    if loose:
        dirs = _reference_dir_entries(plan_dir)[0]
        where = dirs[0] + "/" if dirs else "a reference dir"
        shown = ", ".join(loose[:8]) + (" and %d more" % (len(loose) - 8)
                                        if len(loose) > 8 else "")
        out["warnings"] = [
            "phase files link %d plan-dir file(s) this approval does not pin: %s — "
            "an instruction kept there can change later with no drift; keep "
            "instructions under %s (or list their dir in plan.md `reference_dirs:`), "
            "a log that moves by design can stay" % (len(loose), shown, where)]
    return out


# --------------------------------------------------------- size decision ---

_SIZE_DECISION_RECEIPT = "scope-sizing.json"  # must match hs_run_plan._SCOPE_RECEIPT
_SIZE_DECISION_VALUES = ("keep", "split")


def _read_size_receipt(path: Path) -> dict:
    """Existing scope-sizing.json content, or {} when absent/unreadable — the
    same degrade-to-{} contract every other receipt reader in this codebase
    already uses, so a first write never has to special-case "no file yet"."""
    if not path.is_file():
        return {}
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def write_size_decision(plan_dir, decision) -> dict:
    """Merge `decided_by: "user"` + `decision` into
    plans/<plan>/artifacts/scope-sizing.json, preserving every OTHER key
    already on disk (`sizing`/`manifest`/anything else an older sizing run
    wrote) — this is the human's keep/split answer
    ADDED to the receipt, never a hand-written replacement for it. A
    hand-typed `{"decided_by": ..., "decision": ...}` following the file's
    own next_action used to overwrite the whole receipt and lose the sizing
    facts already there — this function, and the `--size-decision` CLI flag
    that calls it, is what a driver runs instead of writing the JSON by
    hand. Atomic same-dir write (artifact_io.atomic_write_text), so a
    reader never observes a half-written file."""
    plan_dir = Path(plan_dir)
    if decision not in _SIZE_DECISION_VALUES:
        return {"ok": False,
                "error": "decision must be one of %s (got %r)"
                         % (_SIZE_DECISION_VALUES, decision)}
    root = harness_paths.project_root()
    target = plan_dir / "artifacts" / _SIZE_DECISION_RECEIPT
    try:
        fs_guard.assert_under(target, "plans", root=root)
    except fs_guard.FenceError as e:
        return {"ok": False, "error": str(e)}
    rec = _read_size_receipt(target)
    rec["decided_by"] = "user"
    rec["decision"] = decision
    target.parent.mkdir(parents=True, exist_ok=True)
    import artifact_io
    artifact_io.atomic_write_text(
        target, json.dumps(rec, ensure_ascii=False, indent=2) + "\n")
    trace_log.append_event("plan_approval", "size_decision_written",
                           actor=hook_runtime.resolve_actor(),
                           target=plan_dir.name, status=decision)
    return {"ok": True, "artifact": str(target), "record": rec}


# -------------------------------------------------------------------- CLI ---

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Write the plan-approval artifact "
                    "(plans/<plan>/artifacts/plan-approval.yaml).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "REQUIRED every call: --plan, plus at least one of --verdict or\n"
            "  --size-decision (both may be passed together in one call).\n"
            "--verdict additionally requires --rationale.\n"
            "CONDITIONALLY REQUIRED: --author \u2014 needed ONLY when the author\n"
            "  cannot be resolved automatically. Resolution order:\n"
            "    1. a `plan_created` trace event for this plan (auto), else\n"
            "    2. an `author:` line in plan.md frontmatter (auto), else\n"
            "    3. you MUST pass --author user:<id> (the call is refused\n"
            "       otherwise \u2014 an empty author is never written).\n"
            "  Source for --author: the plan's own author, as `user:<id>`\n"
            "  (e.g. user:you@example.com); read plan.md frontmatter `author:`\n"
            "  or use the current HARNESS_USER identity.\n"
            "NOT a flag: `reviewer` is auto-resolved from the current actor;\n"
            "  the plan-graph sidecar (plan-graph.yaml) must exist for APPROVED.\n"
            "--size-decision keep|split records the human's plan-size choice\n"
            "  (merged into artifacts/scope-sizing.json, never overwriting what\n"
            "  is already there) \u2014 pass it ALONE to answer just that question\n"
            "  on a plan that is already APPROVED, no re-approval forced.\n"
            "--status reads, writes nothing: whether the approval still clears the\n"
            "  gate (`holds`, `reason`), the recorded and current plan hash, and\n"
            "  the files that moved since (`drifted`). Exit 0 when it holds, 1 not.\n"
            "A write prints the verdict, plan hash, file count and artifact path;\n"
            "  --json prints the whole record (every file's hashes)."
        ))
    ap.add_argument("--plan", required=True,
                    help="[REQUIRED] the plan: a plan dir, a path to its plan.md "
                         "(any file in the dir resolves to the dir), or a bare "
                         "name under plans/")
    ap.add_argument("--verdict", default=None,
                    type=lambda s: s.strip().upper(),
                    choices=("APPROVED", "REJECTED"),
                    help="APPROVED or REJECTED (case-insensitive: approved / "
                         "ApprOvEd / REJECTED all accepted). Requires "
                         "--rationale. Omit when this call only records "
                         "--size-decision on an already-approved plan.")
    ap.add_argument("--rationale", default=None,
                    help="[REQUIRED with --verdict] one-line reason for the verdict")
    ap.add_argument("--size-decision", default=None,
                    choices=("keep", "split"),
                    help="record the human's keep/split plan-size decision \u2014 "
                         "merged into artifacts/scope-sizing.json alongside "
                         "whatever an older sizing run already wrote there, never "
                         "replacing it. May be passed alone (no --verdict "
                         "needed) or together with --verdict in one call.")
    ap.add_argument("--author", default=None,
                    help="[CONDITIONAL] the PLAN's author as user:<id>. Required "
                         "ONLY when no `plan_created` trace event and no plan.md "
                         "`author:` frontmatter resolve one. Source: plan.md "
                         "`author:` or the HARNESS_USER identity.")
    ap.add_argument("--status", action="store_true",
                    help="read-only: does the approval still clear the gate, and which "
                         "files moved since it was written")
    ap.add_argument("--json", action="store_true",
                    help="print the whole written record, every file's hashes included")
    args = ap.parse_args(argv)
    if args.status and (args.verdict or args.size_decision):
        ap.error("--status only reads: drop --verdict/--size-decision")

    if not args.verdict and not args.size_decision and not args.status:
        result = {"ok": False,
                  "error": "nothing to do: pass --verdict, --size-decision, "
                           "or both"}
        print(json.dumps(result, ensure_ascii=False))
        return 1
    if args.verdict and not (args.rationale or "").strip():
        result = {"ok": False,
                  "error": "--rationale is required when --verdict is given"}
        print(json.dumps(result, ensure_ascii=False))
        return 1

    # --plan accepts a plan dir, a path to its plan.md (any file inside the plan
    # dir), or a bare name under plans/ — resolve all three to the plan directory.
    plan_arg = Path(args.plan)
    if plan_arg.is_file():
        plan_dir = plan_arg.parent
    elif plan_arg.is_dir():
        plan_dir = plan_arg
    else:
        # project_root(), NOT root(): a plans/ lookup belongs to the PROJECT plane.
        # root() is bin_root() — the shipped programs — which under a global install is a
        # different tree from the project being worked on, and even under a self-hosted
        # install resolves from __file__ (the harness checkout) rather than the cwd's
        # project. Either way a bare name resolved against it missed, fell through to the
        # relative `plan_arg`, and the CLI answered "no plan.md in <name>".
        #
        # write_approval's own root is migrated too, for its own reasons — see the
        # comment there. The two sites are NOT interchangeable: this line CONSTRUCTS a
        # path, so nothing downstream can override a wrong root; the write site passes a
        # containment HINT that fs_guard overrides under a global bin. That is why each
        # one had to be measured in both layouts rather than swept together.
        under_plans = harness_paths.project_root() / "plans" / args.plan
        plan_dir = under_plans if under_plans.is_dir() else plan_arg

    if args.status:
        if not (plan_dir / "plan.md").is_file():
            print(json.dumps({"ok": False, "error": "no plan.md in %s" % plan_dir},
                             ensure_ascii=False))
            return 1
        status = approval_status(plan_dir)
        print(json.dumps(status, ensure_ascii=False))
        return 0 if status["holds"] else 1

    def shown(r):
        return r if args.json else brief(r)

    # A --verdict-only call prints write_approval's result unwrapped (summarized
    # unless --json). --size-decision, alone or with --verdict, prints one flat dict
    # keyed by call, `ok` the AND of whichever calls ran.
    if args.verdict and not args.size_decision:
        result = write_approval(plan_dir, verdict=args.verdict,
                                rationale=args.rationale, author=args.author)
        print(json.dumps(shown(result), ensure_ascii=False))
        return 0 if result.get("ok") else 1

    ok = True
    result = {}
    if args.verdict:
        verdict_result = write_approval(plan_dir, verdict=args.verdict,
                                        rationale=args.rationale,
                                        author=args.author)
        result["verdict"] = shown(verdict_result)
        ok = ok and bool(verdict_result.get("ok"))
    if not ok:
        # A refused verdict leaves nothing behind: the caller retries the whole
        # command, and a size answer already on disk would outlive that retry.
        result["ok"] = False
        print(json.dumps(result, ensure_ascii=False))
        return 1
    size_result = write_size_decision(plan_dir, args.size_decision)
    result["size_decision"] = shown(size_result)
    ok = ok and bool(size_result.get("ok"))
    result["ok"] = ok
    print(json.dumps(result, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
