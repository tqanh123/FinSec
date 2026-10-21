#!/usr/bin/env python3
"""simplify_gate.py — PreToolUse(Bash) gate: oversized unsimplified diffs.

Fires only at the human-checkpoint stages (pr/ship/deploy). When the working
diff breaches a size threshold, it asks for a simplification pass before the
stage advances. Posture (user-locked 2026-06-20): default ON + SOFT (warn),
escalatable to HARD (block); ALL knobs — mode, thresholds, exclusions — live in
the write_guarded harness/data/simplify-policy.yaml and are human-only. There is
deliberately NO env knob: an agent cannot turn this off or move a threshold.

FAIL-OPEN by design — the inverse of gate_stage. gate_stage proves a required
step RAN, so its own crash must fail CLOSED. This gate is a quality heuristic; a
crashing heuristic (bad git state, odd payload) must NEVER block a ship, so it
hand-rolls main() rather than using run_compliance_hook and continues on any
internal error.

Generated files (manifest, lockfiles) are excluded so a legitimate generated-
output ship never self-blocks. The size signal is git churn vs HEAD plus the
line count of untracked files.
"""

import json
import os
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(_HERE)
sys.path.append(os.path.join(os.path.dirname(_HERE), "scripts"))

import hook_runtime  # noqa: E402

HOOK_CLASS = "compliance"
_HOOK = "simplify_gate"

# Default posture shipped when the policy file is absent: ON + SOFT. Generated
# artifacts are excluded so the harness never self-blocks on its own manifest /
# a lockfile refresh.
_DEFAULT_STAGES = ["pr", "ship", "deploy"]
_DEFAULT_THRESHOLDS = {"loc_delta": 400, "file_count": 8, "single_file_loc": 200}
_DEFAULT_EXCLUDE = [
    "**/manifest.json", "harness/manifest.json",
    "*.lock", "**/*.lock",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "poetry.lock", "Cargo.lock", "go.sum",
]
_MODES = ("off", "warn", "block")
_POLICY_NAME = "simplify-policy.yaml"


def _git(root, *args):
    """git stdout (str), stderr/returncode ignored — fail-open to ''."""
    try:
        return subprocess.run(["git", *args], cwd=str(root), text=True,
                              capture_output=True, timeout=5).stdout
    except Exception:  # noqa: BLE001 — heuristic must never raise into the hook
        return ""


def resolve_policy(root):
    """Posture from the write_guarded policy file ONLY (no env knob — the file guard is
    the lever, and an env layer would hand it back). Resolved through the shared hook
    door so the path is declared once; missing or malformed → SOFT defaults (mode
    'warn'); never raises."""
    pol = {
        "mode": "warn",
        "stages": list(_DEFAULT_STAGES),
        "thresholds": dict(_DEFAULT_THRESHOLDS),
        "exclude": list(_DEFAULT_EXCLUDE),
    }
    try:
        # The HOOK door: never raises, and every fallback it takes is named. This file
        # takes no env layer by design (see config-layers.yaml), so the door here buys
        # shared resolution and the ratchet, not a new override.
        sys.path.append(str(Path(__file__).resolve().parent.parent / "scripts"))
        import config_layers
        raw, _diag = config_layers.read_soft(_POLICY_NAME, root=root)
        if isinstance(raw, dict) and raw:
            if raw.get("mode") in _MODES:
                pol["mode"] = raw["mode"]
            if isinstance(raw.get("stages"), list) and raw["stages"]:
                pol["stages"] = [str(s) for s in raw["stages"]]
            th = raw.get("thresholds")
            if isinstance(th, dict):
                for k in ("loc_delta", "file_count", "single_file_loc"):
                    v = th.get(k)
                    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                        pol["thresholds"][k] = v
            if isinstance(raw.get("exclude"), list):
                pol["exclude"] = [str(x) for x in raw["exclude"]]
    except Exception:  # noqa: BLE001 — malformed config falls to SOFT defaults
        pass
    return pol


def _excluded(path, globs):
    """True if path matches any glob. fnmatch does not treat '**' specially, so a
    '**/name' pattern is additionally checked against the basename and the full
    norm with the '**/' prefix stripped — that lets '**/manifest.json' exclude a
    ROOT-level manifest.json, not only nested ones."""
    norm = str(path).replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]
    for g in globs:
        if fnmatch(norm, g) or fnmatch(base, g):
            return True
        if g.startswith("**/"):
            tail = g[3:]
            if fnmatch(base, tail) or fnmatch(norm, tail):
                return True
    return False


def compute_signals(root, exclude):
    """Diff size vs HEAD. Tracked churn (added+removed per numstat) plus the line
    count of untracked files, minus excluded generated paths."""
    files = {}
    for line in _git(root, "diff", "--numstat", "--ignore-all-space", "HEAD").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if _excluded(path, exclude):
            continue
        a = int(added) if added.isdigit() else 0
        r = int(removed) if removed.isdigit() else 0
        files[path] = files.get(path, 0) + a + r
    # `-z`: plain `ls-files` C-quotes any non-ASCII byte in a path under git's default
    # `core.quotePath=true`, and the quoted string never resolves back to a real file
    # (below reads it off disk) -- an untracked non-ASCII file would silently drop out
    # of the diff-size signal entirely.
    for path in _git(root, "ls-files", "-z", "--others", "--exclude-standard").split("\0"):
        path = path.strip()
        if not path or _excluded(path, exclude):
            continue
        try:
            p = Path(root, path)
            if p.stat().st_size > 1_000_000:  # skip files >1MB (binary/heavy artifacts)
                continue
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files[path] = files.get(path, 0) + len(content.splitlines())
    return {
        "total_loc": sum(files.values()),
        "file_count": len(files),
        "max_file_loc": max(files.values()) if files else 0,
        "files": sorted(files),
    }


def evaluate(signals, thresholds):
    """List of breach strings (empty ⇒ within budget)."""
    breaches = []
    if signals.get("total_loc", 0) > thresholds["loc_delta"]:
        breaches.append("total churn %d > %d"
                        % (signals.get("total_loc", 0), thresholds["loc_delta"]))
    if signals.get("file_count", 0) > thresholds["file_count"]:
        breaches.append("files %d > %d"
                        % (signals.get("file_count", 0), thresholds["file_count"]))
    if signals.get("max_file_loc", 0) > thresholds["single_file_loc"]:
        breaches.append("largest file %d > %d"
                        % (signals.get("max_file_loc", 0), thresholds["single_file_loc"]))
    return breaches


def _fan_in_counts(root, files):
    """{path: fan-in count} for `files` (already known to be changed), read
    from this project's own snapshot SELECTION -- `hs_run_facts.engine.
    select_snapshot`, the exact same rule `hs-run`'s `facts:` contract
    resolves a snapshot through, reused here rather than a second
    hand-rolled "which snapshot answers for this tree" walk. Fan-in is
    reported ONLY when that selection comes back `fresh`; `stale` (a
    snapshot of another commit), `missing` (no store at all) and `broken`
    (every candidate unreadable, including an unknown `schema_version` --
    `codegraph_snapshot_reader.SnapshotIndexError`, the "2.3 reader"'s own
    exit-2 case) all collapse to `{}` -- no fan-in text, no error, same as
    a project with no `codemap.yaml` at all.

    `select_snapshot` never raises `SnapshotIndexError` itself -- it
    absorbs a broken/unreadable candidate into its own `broken` status
    internally, the same guarantee `hs_run_facts.engine.resolve` relies on
    without wrapping the call in a `try`. The one place this function still
    needs its own catch is promoting a cheap header pick to a full index
    parse (mirroring `engine.resolve`'s own `_full()` promotion) -- a
    snapshot the cheap `_meta.json` sidecar did not fully validate can still
    fail there.

    A hook never calls `hs_run_facts.engine.store_base` -- that resolves the
    snapshot store's data home through the tool-tier resolver, which a
    guard/hook must not reach (its CWD-walk escape hatch is for a Bash-tool
    script, not a gate; `test_bin_data_root_split.py` bans the resolver's
    name from every file under `harness/hooks/`). `base` is computed here on
    the strict, fail-closed `data_root()` instead and handed to
    `select_snapshot` as a plain parameter -- that function itself never
    reaches the tool-tier resolver.
    """
    if not files:
        return {}
    try:
        import codegraph_snapshot_reader as reader
        import codegraph_change_reader
        import harness_paths
        from hs_run_facts import engine
    except Exception:  # noqa: BLE001
        return {}

    data_root = harness_paths.data_root()
    if harness_paths.data_root_unresolved(data_root):
        return {}
    project = engine.project_name(root)
    if project is None:
        return {}

    base = reader.snapshot_base(data_root, project, root)
    selection = engine.select_snapshot(root, base)
    if selection.status != "fresh":
        return {}
    try:
        idx = selection.index if selection.index_is_full else reader.read_index(selection.dir)
    except reader.SnapshotIndexError:
        return {}

    return codegraph_change_reader.fan_in(idx, files)


def _fan_in_detail(root, files):
    """`" fan-in: path=N, ..."` for `files` with a fresh snapshot to read, or
    "" when none is available -- see `_fan_in_counts` for every case that
    collapses to the empty string."""
    counts = _fan_in_counts(root, files)
    if not counts:
        return ""
    return " fan-in: %s." % ", ".join("%s=%d" % (p, counts[p]) for p in sorted(counts))


def core(data):
    """None ⇒ pass; string ⇒ block reason (HARD mode only). SOFT mode warns to
    stderr and returns None."""
    import harness_paths
    import stage_detector

    command = hook_runtime.bash_command(data)

    root = harness_paths.root()
    pol = resolve_policy(root)
    if pol["mode"] == "off":
        return None

    stage = stage_detector.detect_stage(command)
    if stage is None or stage not in pol["stages"]:
        return None

    # policy resolves bin-global off root; the diff is a PROJECT concern, so
    # size it against the project tree (root==project under self-host).
    project_root = harness_paths.project_root()
    signals = compute_signals(project_root, pol["exclude"])
    breaches = evaluate(signals, pol["thresholds"])
    if not breaches:
        return None
    detail = "; ".join(breaches)
    fan_in = _fan_in_detail(project_root, signals["files"])

    if pol["mode"] == "block":
        return ("oversized unsimplified diff at %s (%s).%s Spawn the code-simplifier agent "
                "to collapse the diff before this stage, or a human may relax the "
                "thresholds in harness/data/simplify-policy.yaml (agent-locked)."
                % (stage, detail, fan_in))
    # SOFT (warn): advisory only, never blocks.
    sys.stderr.write("[nudge] simplify_gate: large diff at %s (%s).%s Consider "
                     "the code-simplifier agent before this stage.\n" % (stage, detail, fan_in))
    return None


def main():
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        raw = ""
    try:
        data = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:  # noqa: BLE001 — unparseable payload ⇒ nothing to gate
        data = {}
    try:
        if not hook_runtime.hook_enabled(_HOOK, HOOK_CLASS):
            hook_runtime.emit_continue()
            sys.exit(0)
        reason = core(data)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — FAIL-OPEN: heuristic crash never blocks a ship
        hook_runtime.log_hook_error(_HOOK, e)
        hook_runtime.emit_continue()
        sys.exit(0)
    if reason:
        sys.stderr.write("[%s] BLOCKED: %s\n" % (_HOOK, reason))
        sys.exit(2)
    hook_runtime.emit_continue()
    sys.exit(0)


if __name__ == "__main__":
    main()
