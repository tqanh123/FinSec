"""codegraph._core.incremental — run the declared rules on the CHANGED part only.

A big tree costs seconds per full rule pass; a merge request touches a handful
of modules. This module narrows what a rule SCANS to the modules the diff
touched — and refuses to trade correctness for that speedup.

NARROWING THE INPUT, NOT FILTERING THE OUTPUT
    Running everything and then hiding violations outside the diff would cost
    exactly as much as the full run — a speedup in the report only. So the
    narrowable rules are handed a REDUCED snapshot index holding just the
    touched modules. That is a real saving, and it is also where the danger
    lives: a rule that needs its neighbours sees fewer of them.

WHICH RULES MAY BE NARROWED (and why most may not)
    `threshold` reads one module's `loc` and that module's own text — a pure
    per-module measurement, safe to narrow.
    Every other kind in the kernel is GLOBAL and is always run full:
      * `layer_direction`, `same_tier_edge` walk EDGES — an edge from a touched
        module to an untouched one disappears from a narrowed index, so a
        reverse edge could vanish;
      * `dup_symbol` pairs modules ACROSS the tree — half a pair outside the
        diff makes the duplicate look unique;
      * `single_door` compares against a tree-wide baseline, so a narrowed run
        reports every absent label as "baseline-gone" — a false red.
    Narrowing those would not be an optimisation; it would be a different
    (wrong) gate. They are listed in `GLOBAL_KINDS` and always run full.

THE CONTRACT, AS CODE
    `assert_preserves_verdict()` runs both ways and raises when narrowing lost
    a VIOLATION that belongs to the changed region (any severity — losing a
    warn silently is the same class of bug as losing an error, it just does
    not flip the exit code). That is the bug class this module exists to
    prevent and the one a "make CI faster" change ships by accident; the
    tests run it, and the CLI reports it.

SEVERITY DRIVES THE REPORT'S OWN TALLY (deviation from the tool this was
ported from, matching the fix in `rules.base` — see that module's docstring
for the measured bug): the ORIGINAL summed `len(violations)` into `red`
regardless of a rule's declared severity, so a `severity: warn` rule still
failed an incremental pass. Here each rule's violations land in `red` only
when `RuleResult.is_red`, otherwise in `warned` — a warn rule reports without
failing, exactly like `rules`.

WHAT IT CANNOT SEE
    * The diff comes from git or from two snapshots. Both see TEXT: a rename
      that keeps the body is a path change only, and the caller decides whether
      that counts. Over-inclusion is preferred throughout — a module reported
      as touched that was not costs time, a module missed costs a red.
    * Changed files map to modules by PATH (`a/b/c.py` -> `a.b.c`). Code that
      lives outside the snapshot's roots is invisible to both runs alike.
    * A green narrowed run says "no violation among the visible, in the
      changed region" — never "the tree is clean". The dropped count is
      reported for exactly that reason.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .git import GitError, resolve_ref, run_git
from .graph.model import Graph
from .lang.python import resolver as _python
from .rules.base import RuleContext, RuleResult, RulesEngine, Violation

#: Rule kinds whose verdict depends on the tree BEYOND the module being judged.
#: Narrowing the scan input changes their answer, so they always run full.
GLOBAL_KINDS: frozenset[str] = frozenset({
    "layer_direction", "same_tier_edge", "dup_symbol", "single_door",
})


class IncrementalError(RuntimeError):
    """Cannot narrow honestly (bad diff, unreadable tree, lost violation) — exit 2."""


# ── diff -> touched modules ──────────────────────────────────────────────────

def changed_files(repo: Path, base: str, head: str = "HEAD") -> list[str]:
    """Repo-relative paths touched between two commits. Read-only git.

    `base`/`head` are resolved to real commit SHAs via `git.resolve_ref`
    BEFORE they ever reach `diff` argv — a value starting with `-` (e.g.
    `--output=<path>`, this module's own measured hole) is refused outright
    rather than interpolated into the `<base>..<head>` range git would
    otherwise parse as an option."""
    try:
        base_sha = resolve_ref(repo, base)
        head_sha = resolve_ref(repo, head)
        # `-z` (NUL-separated): plain `--name-only` C-quotes a non-ASCII path under
        # git's default `core.quotePath=true`, and the quoted string never resolves
        # back to a real module -- the change to it goes invisible to narrowing.
        raw = run_git(repo, "diff", "--name-only", "-z", "--end-of-options",
                      f"{base_sha}..{head_sha}")
    except (GitError, FileNotFoundError) as e:
        raise IncrementalError(f"git diff {base}..{head} failed: {e}") from e
    out = raw.decode("utf-8", "surrogateescape")
    return sorted(p for p in out.split("\0") if p.strip())


# `touched_modules` MOVED to the Python pack's resolver (`lang.python.resolver`):
# naming a changed file is the pack's question. Rebound to the same object.
touched_modules = _python.touched_modules


def _flat_modules(idx: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for _unit, mods in idx.get("units", {}).items():
        for m in mods:
            out[m["module"]] = m
    return out


def touched_from_snapshots(a: dict, b: dict) -> set[str]:
    """Modules whose symbol set, body, or presence changed between snapshots."""
    A, B = _flat_modules(a), _flat_modules(b)

    def sig(m: dict) -> tuple:
        return (m.get("file_sha"),
                tuple(sorted((s["name"], s.get("sha") or "")
                             for s in m.get("symbols", []))))

    touched = {mod for mod in set(A) | set(B) if sig(A[mod]) != sig(B[mod])} \
        if set(A) == set(B) else set(A) | set(B)
    return touched


# ── narrowing ─────────────────────────────────────────────────────────────

def is_global(kind: str) -> bool:
    return kind in GLOBAL_KINDS


def narrowed_index(idx: dict, touched: set[str]) -> dict:
    """A snapshot index holding only the touched modules.

    Keeps `roots`/`commit`/`snapshot`/`schema_version` intact — the context a
    rule needs to interpret what it sees. `totals` is recomputed so a reader
    of the narrowed index is not told the full tree size by accident.
    """
    units: dict[str, list[dict]] = {}
    for unit, mods in idx.get("units", {}).items():
        kept = [m for m in mods if m["module"] in touched]
        if kept:
            units[unit] = kept
    out = dict(idx)
    out["units"] = units
    out["totals"] = dict(idx.get("totals", {}))
    out["totals"]["units"] = len(units)
    out["totals"]["narrowed_to_modules"] = len(touched)
    return out


def _module_key(v: Violation) -> str:
    """The module a violation is ABOUT.

    `threshold` names it in `detail["module"]`. Kinds that only carry edge ends
    have no single owning module, which is exactly why they are global — the
    fallback here never decides a narrowable verdict.
    """
    mod = v.detail.get("module")
    if mod:
        return str(mod)
    return str(v.label).split("::", 1)[0].split(":", 1)[0].strip()


def region_violations(results: list[RuleResult], touched: set[str]) -> set[tuple]:
    """Violations (any severity) that belong to the changed region, as
    comparable keys."""
    out = set()
    for r in results:
        for v in r.violations:
            if _module_key(v) in touched:
                out.add((r.rule_id, v.cause, _module_key(v)))
    return out


def assert_preserves_verdict(full: list[RuleResult],
                             narrowed: list[RuleResult],
                             touched: set[str]) -> None:
    """Raise when narrowing LOST a violation that belongs to the changed
    region — any severity: a dropped warn is the same class of bug as a
    dropped error, even though only the error would have flipped the exit
    code."""
    lost = region_violations(full, touched) - {
        (r.rule_id, v.cause, _module_key(v))
        for r in narrowed for v in r.violations}
    if lost:
        raise IncrementalError(
            "incremental narrowing LOST violations inside the changed "
            "region — this is a gate-loosening bug, not a speedup: "
            f"{sorted(lost)[:5]}")


# ── the narrowed run ─────────────────────────────────────────────────────────

def run_incremental(idx: dict, rules: list[dict], repo: Path,
                    touched: set[str], *, descriptor: Any = None,
                    verify: bool = True) -> dict:
    """Run declared rules: narrow the per-module ones, run the global ones full.

    Returns a machine-readable report. `mode` per rule and
    `dropped_outside_diff` make the narrowing VISIBLE — a narrowed pass that
    hides how much it skipped is the same lie as an empty cell reading clean.
    `red`/`warned` split by `RuleResult.is_red` — see the module docstring's
    own section on why this differs from the tool it was ported from.
    """
    if not rules:
        raise IncrementalError("no rules to run — a descriptor with `rules:` "
                               "is required; an empty rule list is not a pass")
    sub = narrowed_index(idx, touched)
    narrow_ids = [r["id"] for r in rules if not is_global(r["kind"])]

    # The narrowed engine carries ONLY the narrowable rules and runs over the
    # reduced index — that is where the saving comes from. The full engine
    # carries the global ones (and, when verifying, everything).
    engine_narrow = RulesEngine([r for r in rules if not is_global(r["kind"])])
    engine_full = RulesEngine(rules)

    ctx_full = RuleContext(repo=repo, graph=Graph.from_snapshot(
        idx, groups=getattr(descriptor, "groups", None)),
        idx=idx, descriptor=descriptor)
    ctx_narrow = RuleContext(repo=repo, graph=Graph.from_snapshot(
        sub, groups=getattr(descriptor, "groups", None)),
        idx=sub, descriptor=descriptor)

    narrowed = {r.rule_id: r for r in engine_narrow.run(ctx_narrow)} \
        if narrow_ids else {}
    full = {r.rule_id: r for r in engine_full.run(ctx_full)}

    if verify and narrowed:
        assert_preserves_verdict(list(full.values()), list(narrowed.values()),
                                 touched)

    report: dict = {"touched_modules": sorted(touched), "rules": [],
                    "red": 0, "warned": 0, "dropped_outside_diff": 0,
                    "verified": bool(verify)}
    for rule in rules:
        rid, kind = rule["id"], rule["kind"]
        if is_global(kind):
            res = full[rid]
            red = len(res.violations) if res.is_red else 0
            warned = 0 if res.is_red else len(res.violations)
            report["rules"].append({"id": rid, "kind": kind, "mode": "full",
                                    "severity": res.severity, "red": red,
                                    "warned": warned,
                                    "violations": [
                                        {"cause": v.cause, "label": v.label,
                                         "detail": v.detail} for v in res.violations]})
            report["red"] += red
            report["warned"] += warned
            continue
        res = narrowed[rid]
        red = len(res.violations) if res.is_red else 0
        warned = 0 if res.is_red else len(res.violations)
        dropped = len(full[rid].violations) - len(res.violations)
        report["rules"].append({"id": rid, "kind": kind, "mode": "narrowed",
                                "severity": res.severity, "red": red,
                                "warned": warned, "dropped": max(dropped, 0),
                                "violations": [
                                    {"cause": v.cause, "label": v.label,
                                     "detail": v.detail} for v in res.violations]})
        report["red"] += red
        report["warned"] += warned
        report["dropped_outside_diff"] += max(dropped, 0)
    return report
