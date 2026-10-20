#!/usr/bin/env python3
"""plan_facts.py — a plan-size dashboard: FACTS about a plan's size, a rank
against old plans, and descriptive labels — never a split recommendation.

Why the old heuristic is gone: the deleted engine's cell-count formula recommended
`multi` on every plan in a repo-wide calibration pass — a signal with zero
discriminating power. Even the connectivity rule this module DOES implement (an
`edge-collector` rule, the best of several measured) only reaches roughly 1-in-6
recall against a hand-labeled set of real plans, and most of its misses were a
plan whose phases form one causal chain — the collector rule alone cannot see
that. So the machine stopped trying to conclude: it reports the numbers
(`gather_facts`) plus a rank against old plans (`historical_dataset`) plus a
plain description (`describe`'s `labels`), and a HUMAN decides whether to split,
at the plan's existing approval step — this module has no opinion.

Every label this module can produce is swept by `_contains_banned_wording` — a
generated string is never allowed to say "should split" in any spelling this
module's own vocabulary uses (see test_plan_facts.py's wording gate).

NO `import plan_graph` HERE, ON PURPOSE: `plan_graph.py`'s own CLI calls into
this module (a `--facts` flag) to print this dashboard's JSON, so this module
importing `plan_graph` back would close a two-file cycle (both modules would
depend on each other). Every plan_graph primitive this module needs — parsing
the sidecar, and the graph-size numbers themselves — is passed in by the
caller instead: `parse_graph(plan_dir) -> dict` and `facts_of(graph) -> dict`
(shaped exactly like `plan_graph.parse_phase_graph` and
`plan_graph.graph_size_facts`), plus `root_of(plan_dir) -> Path` (shaped like
`plan_graph.root_for_plan_dir`) for `describe`'s default-root resolution.
This module has no standalone CLI of its own for the same reason: it cannot
supply a default for those callables without importing the module it must
not import, so `plan_graph.py --facts <plan_dir>` is the one real entry point.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))
import yaml_io  # noqa: E402

#: Below this many old plans, a percentile reads as false precision — "to hơn
#: 50% của 2 plan cũ" tells the reader nothing a plain count would not. Matches
#: this repo's own floor-of-evidence habit (a rate needs enough n before a
#: number is worth printing) without borrowing the Wilson machinery built for
#: an actual accuracy CLAIM — this is a rank, not a pass/fail rate.
_MIN_N_FOR_PERCENT = 5

_CACHE_NAME = "plan-facts-history.json"

#: Any spelling this module's OWN generated labels must never contain — the
#: wording gate (facts, never advice). Deliberately narrow: this is not a
#: general adversarial detector, it is a closed vocabulary this module itself
#: writes, so a finite banned-phrase list is the right tool, not a paraphrase-
#: proof classifier this small a surface does not need.
_BANNED_PHRASES = (
    "nên tách", "khuyên tách", "hãy tách", "gợi ý tách", "cần tách",
    "should split", "recommend split", "recommend a split", "suggest split",
    "consider splitting",
)

#: Every label string this module's code can actually emit — the wording
#: gate's own fixture (test_plan_facts.py sweeps this list, and proves the
#: sweep itself catches a violation by feeding it a seeded banned phrase
#: separately, never by trusting this list alone).
LABEL_VOCABULARY_FOR_WORDING_GATE = (
    "to hơn 50% của 10 plan cũ",
    "chỉ có 2 plan cũ",
    "không đo được",
    "có 3 cụm độc lập",
    "1 cụm — không tách được theo cạnh phụ thuộc",
    "chuỗi dài nhất: 4 pha",
)


def _contains_banned_wording(text: str) -> bool:
    low = text.lower()
    return any(phrase in low for phrase in _BANNED_PHRASES)


# ------------------------------------------------------------------ gather ---

def gather_facts(plan_dir, parse_graph, facts_of) -> dict:
    """The plan-size facts reported for ONE plan: declared files,
    directories, phases, independent clusters (the edge-collector rule),
    longest chain, and which phases are collectors. `{"error": ...}` when the
    plan carries no `plan-graph.yaml` (or it fails to parse) — "cannot
    measure" is the honest answer, never a guess.

    `parse_graph`/`facts_of` are the caller's own `plan_graph.parse_phase_graph`
    / `plan_graph.graph_size_facts` — injected rather than imported (module
    docstring: this file must never `import plan_graph`, or the two-file
    dependency closes into a cycle)."""
    graph = parse_graph(plan_dir)
    if graph.get("error"):
        return {"error": graph["error"]}
    return facts_of(graph)


# --------------------------------------------------------- historical set ---

def _run_git(root, *args) -> "str | None":
    try:
        r = subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, timeout=60)
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _all_ever_tracked_plan_graphs(root) -> set:
    """Every repo-relative `plans/<slug>/plan-graph.yaml` path git has EVER
    recorded, across every ref (`--all`) — a plan removed by a later cleanup
    still counts: the ranking population is the current `plans/` tree union
    every historical plan-graph.yaml git ever held.

    `-z`: plain `--name-only` C-quotes any non-ASCII byte in a path under git's
    default `core.quotePath=true` — a quoted stand-in never splits into a real
    `plans/<slug>/plan-graph.yaml` triple, so a non-ASCII plan slug would silently
    drop out of the whole ranking population.
    """
    out = _run_git(root, "log", "--all", "--format=", "--name-only", "-z",
                   "--", "plans/*/plan-graph.yaml")
    if out is None:
        return set()
    return {p.strip() for p in out.split("\0") if p.strip()}


def _content_at_last_commit(root, rel_path: str) -> "str | None":
    """`rel_path`'s content at the NEWEST commit that still has it — each plan
    read at the last commit that still carried it. Walks commits newest-first and
    stops at the first one `git show` can actually read; a commit that only
    DELETED the path fails there and the walk continues to the one before it."""
    out = _run_git(root, "log", "--all", "--format=%H", "--", rel_path)
    if not out:
        return None
    for sha in out.splitlines():
        sha = sha.strip()
        if not sha:
            continue
        blob = _run_git(root, "show", "%s:%s" % (sha, rel_path))
        if blob is not None:
            return blob
    return None


def _graph_from_yaml_text(text: str) -> "dict | None":
    """The same edges/subtasks normalization `plan_graph.parse_phase_graph`
    applies to a file on disk, applied here to a blob read out of git history
    instead — `None` on anything unparseable (never raises, matching every
    other reader in this module's family)."""
    try:
        data = yaml_io.safe_load(text)
    except yaml_io.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    for key, want in (("edges", list), ("subtasks", dict)):
        val = data.get(key)
        if val is None:
            data[key] = want()
        elif not isinstance(val, want):
            return None
    return data


def _historical_facts_uncached(root, parse_graph, facts_of) -> dict:
    """slug -> facts, for every plan-graph.yaml this project's git history
    ever tracked. A plan still on disk is read LIVE (its current content); one
    that is gone is read at its last commit (`_content_at_last_commit`)."""
    root = Path(root)
    out: dict = {}
    for rel in sorted(_all_ever_tracked_plan_graphs(root)):
        parts = rel.split("/")
        if len(parts) != 3 or parts[0] != "plans" or parts[2] != "plan-graph.yaml":
            continue
        slug = parts[1]
        disk_path = root / rel
        graph: "dict | None"
        if disk_path.is_file():
            graph = parse_graph(disk_path.parent)
            if graph.get("error"):
                continue
        else:
            text = _content_at_last_commit(root, rel)
            if text is None:
                continue
            graph = _graph_from_yaml_text(text)
            if graph is None:
                continue
        out[slug] = facts_of(graph)
    return out


def _cache_path(root) -> "Path | None":
    """`<root's own data home>/cache/plan-facts-history.json` — root-scoped
    (`harness_paths.data_root_for`, not the ambient `tool_data_root()`) so two
    different repos, or a repo and a throwaway test fixture, never share one
    cache slot. `None` (no cache, always recompute) when `harness_paths` is
    unimportable or the root's data home does not resolve — a cache is an
    optimization here, never a requirement for a correct answer."""
    try:
        import harness_paths
    except ImportError:
        return None
    try:
        base = harness_paths.data_root_for(root)
    except Exception:  # noqa: BLE001 — degrade to no cache, never crash on this
        return None
    if harness_paths.data_root_unresolved(base):
        return None
    return base / "cache" / _CACHE_NAME


def historical_dataset(root, parse_graph, facts_of) -> dict:
    """slug -> facts, for every plan-graph.yaml this project's `plans/` tree
    (live or in git history) has ever carried — the ranking population a
    candidate plan is compared against. Cached by HEAD: unchanged HEAD hands
    back the cached facts without re-walking git history; a HEAD it has not
    seen before recomputes and overwrites the cache (one entry, not a growing
    log — there is only ever one "current" answer worth keeping)."""
    root = Path(root)
    head = (_run_git(root, "rev-parse", "HEAD") or "").strip() or None
    cache_file = _cache_path(root)
    if cache_file is not None and head is not None and cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict) and cached.get("head") == head:
            facts = cached.get("facts")
            if isinstance(facts, dict):
                return facts
    data = _historical_facts_uncached(root, parse_graph, facts_of)
    if cache_file is not None and head is not None:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"head": head, "facts": data}, sort_keys=True),
                          encoding="utf-8")
            tmp.replace(cache_file)
        except OSError:
            pass
    return data


# --------------------------------------------------------- rank + labels ---

def _percentile_label(value: int, historical_values: list) -> str:
    n = len(historical_values)
    if n == 0:
        return "không có plan cũ để so"
    if n < _MIN_N_FOR_PERCENT:
        return "chỉ có %d plan cũ" % n
    at_or_below = sum(1 for v in historical_values if v <= value)
    pct = round(100 * at_or_below / n)
    return "to hơn %d%% của %d plan cũ" % (pct, n)


def _cluster_label(n_clusters: int) -> str:
    if n_clusters <= 1:
        return "1 cụm — không tách được theo cạnh phụ thuộc"
    return "có %d cụm độc lập" % n_clusters


def describe(plan_dir, parse_graph, facts_of, root_of, root=None, historical=None) -> dict:
    """Facts + rank + descriptive labels for one plan — the dashboard the
    plan-approval step's `ask_user` block is meant to hand to a human.
    `parse_graph`/`facts_of`/`root_of` are the caller's own `plan_graph`
    functions (module docstring: injected, never imported, to keep the
    dependency one-directional). `historical`, when given, overrides
    `historical_dataset(root, parse_graph, facts_of)` (for a caller that
    already resolved it, and for tests that want a controlled population
    without a real git-history walk). The plan's OWN slug is excluded from
    its own ranking population, on disk or in `historical` alike — a plan
    should never be ranked against itself."""
    plan_dir = Path(plan_dir)
    facts = gather_facts(plan_dir, parse_graph, facts_of)
    if facts.get("error"):
        return {"error": facts["error"], "labels": ["không đo được"]}

    if historical is None:
        if root is None:
            root = root_of(plan_dir)
        historical = historical_dataset(root, parse_graph, facts_of)
    slug = plan_dir.name
    historical = {k: v for k, v in historical.items() if k != slug}

    values = [v["declared_files"] for v in historical.values()
             if isinstance(v, dict) and isinstance(v.get("declared_files"), int)]
    labels = [
        _percentile_label(facts["declared_files"], values),
        _cluster_label(facts["independent_clusters"]),
        "chuỗi dài nhất: %d pha" % facts["longest_chain"],
    ]
    for label in labels:
        assert not _contains_banned_wording(label), (
            "plan_facts generated a banned recommendation phrase: %r" % label)
    return {"facts": facts, "old_plan_count": len(historical), "labels": labels}
