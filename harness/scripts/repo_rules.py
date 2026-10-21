#!/usr/bin/env python3
"""repo_rules.py — a repo's own rules, routed into the cook envelope by what the
phase at hand creates or touches.

The envelope's `rules` come from the shipped registry, so they can only ever name
the harness's own rule files. A repo convention its own gate enforces (say, a log
point density on every new source file) never reached the phase that went red on
it. A repo declares such rules in `cook-rules.yaml` — human-edited YAML, read
through the config-layer door, so it lives in the project's data home
(`.harness/cook-rules.yaml`) or wherever HARNESS_COOK_RULES points:

    rules:
      - path: docs/conventions/dense-log.md   # repo-relative document to read
        when:
          creates: ["src/**/*.py"]            # a file the phase creates matches
          touches: ["db/migrations/*"]        # a file it creates/modifies/deletes

A rule applies when ANY glob under ANY `when` key matches the phase's declared
files (`plan-graph.yaml` `files_to_*`). The routed path is project-relative, so
`hs_run.reference_roots_for` resolves it under the project, never the bin.

Advisory routing only: a declared rule ADDS a document to read and can widen no
lane, so it is safe in the writeable project zone. A malformed entry is never
dropped silently — each one costs a warning line in the envelope.
"""
from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

CONFIG = "cook-rules.yaml"
_WHEN_KEYS = ("creates", "touches")
#: The cook states a phase is being built or repaired in — where its rules apply.
ROUTED_STATES = ("needs_implementation", "phase_needs_work", "phase_failed")


def load(root=None) -> "tuple[dict, list]":
    """(document, warnings) from the config-layer door. Never raises."""
    try:
        import config_layers
        doc, _diag = config_layers.read_soft(CONFIG, root=root)
    except Exception as exc:  # noqa: BLE001 — routing is advisory; a read fault is a warning
        return {}, ["%s unreadable: %s" % (CONFIG, exc)]
    return (doc if isinstance(doc, dict) else {}), []


def _entry_problem(i: int, entry) -> "str | None":
    where = "%s: rule #%d ignored" % (CONFIG, i + 1)
    if not isinstance(entry, dict):
        return "%s: not a mapping" % where
    path = entry.get("path")
    if not isinstance(path, str) or not path.strip():
        return "%s: `path` must be a non-empty repo-relative path" % where
    p = PurePosixPath(path)
    if p.is_absolute() or ".." in p.parts:
        return "%s: `path` %r must stay inside the repo" % (where, path)
    when = entry.get("when")
    if not isinstance(when, dict) or not when:
        return "%s: `when` must name %s" % (where, " and/or ".join(_WHEN_KEYS))
    unknown = sorted(k for k in when if k not in _WHEN_KEYS)
    if unknown:
        return "%s: unknown `when` key(s) %s (known: %s)" % (
            where, ", ".join(map(str, unknown)), ", ".join(_WHEN_KEYS))
    for k, globs in when.items():
        if not isinstance(globs, list) or not all(isinstance(g, str) and g for g in globs):
            return "%s: `when.%s` must be a list of globs" % (where, k)
    return None


def select(doc: dict, *, creates, touches) -> "tuple[list, list]":
    """(rule paths that apply, warnings). `creates` is the phase's
    files_to_create; `touches` is create + modify + delete."""
    import scope_match
    rules = (doc or {}).get("rules")
    if rules is None:
        return [], []
    if not isinstance(rules, list):
        return [], ["%s: `rules` must be a list" % CONFIG]
    files = {"creates": list(creates or ()), "touches": list(touches or ())}
    paths: list = []
    warnings: list = []
    for i, entry in enumerate(rules):
        problem = _entry_problem(i, entry)
        if problem:
            warnings.append(problem)
            continue
        if any(scope_match.scope_matches(globs, files[k])
               for k, globs in entry["when"].items()):
            if entry["path"] not in paths:
                paths.append(entry["path"])
    return paths, warnings


def attach(plan_dir, result: dict) -> None:
    """Route this repo's matching rules onto a cook result (`repo_rules`, popped
    by the dispatcher into the envelope's `rules`), at a state where a phase is
    being built. Absent when nothing applies; warnings ride the result's own."""
    if result.get("state") not in ROUTED_STATES or not result.get("phase"):
        return
    try:
        import plan_graph
        graph = plan_graph.parse_phase_graph(plan_dir)
        if graph.get("error"):
            return
        phase = result["phase"]
        creates = sorted(plan_graph._files(graph, phase, "files_to_create"))
        touches = sorted(plan_graph.declared_files(graph, phase))
        doc, warnings = load(plan_graph.root_for_plan_dir(plan_dir))
        paths, more = select(doc, creates=creates, touches=touches)
    except Exception as exc:  # noqa: BLE001 — advisory routing never breaks `next`
        paths, warnings, more = [], ["%s: routing failed: %s" % (CONFIG, exc)], []
    if warnings or more:
        result.setdefault("warnings", []).extend(warnings + more)
    if paths:
        result["repo_rules"] = paths
