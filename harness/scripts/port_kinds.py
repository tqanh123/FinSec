"""port_kinds.py — the ONE loader for `harness/data/port-project-kinds.yaml`, as one
FLAT module under `harness/scripts/` (sibling of `port_steps.py`, which owns the SAME
discipline for `port-steps.yaml`): `port_config.py` validates `.port/port.yaml`'s
`project:` block against this table, and `port_steps.lane_of`'s two callers
(`hs_run_port.py`, `port_runs.py`) both get the SAME extra step ids a batch's lane
gains from its project's declared `kind` through `port_config.extra_steps_for_port_root`
— one door, so a batch lane computed by
`hs-run port next` and one computed by `hs-code --step` can never drift apart.

ALLOWED IMPORTS: the stdlib, `yaml_io` and `harness_paths` — the exact same discipline
`port_steps.py`'s own module docstring states, for the exact same reason: this module
sits on `port_config.py`'s import path, which `hs_run_port.py` imports directly, so a
`codegraph` import landing here would cost every hs-run call that weight. It never
imports `port_config` back: this table judges one `project:` mapping handed to it, and
the `port_root`-scoped readers that first LOAD `.port/port.yaml` (`project_of`,
`extra_steps_for_port_root`, `step_values_for_port_root`) live in `port_config.py`,
the module that owns that file.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml_io

_DATA_REL = ("harness", "data", "port-project-kinds.yaml")

# The only two keys a `project:` block may carry — kept here (not in `port_config.py`)
# so the one place that knows what a KIND requires is also the one place that judges
# whether `project:` satisfies it.
_ALLOWED_PROJECT_KEYS = frozenset({"kind", "app", "upstream_app"})


class PortKindsConfigError(Exception):
    """`port-project-kinds.yaml` itself does not parse to the shape this module
    expects — a packaging bug, never a per-repo condition (the file ships with the
    harness; no project edits it)."""


@dataclass(frozen=True)
class KindDef:
    """One `project.kind` value's shape, exactly as declared in
    `port-project-kinds.yaml`."""
    id: str
    steps: "tuple[str, ...]"
    requires: "tuple[str, ...]"


class KindTable:
    """One loaded `port-project-kinds.yaml`: every declared kind's `KindDef`, keyed by
    id."""

    def __init__(self, kinds: "dict[str, KindDef]") -> None:
        self.kinds = kinds

    @property
    def known_ids(self) -> "frozenset[str]":
        return frozenset(self.kinds)


def default_path() -> Path:
    """`<bin_root>/harness/data/port-project-kinds.yaml` — the shipped table."""
    import harness_paths
    return harness_paths.bin_root().joinpath(*_DATA_REL)


def _parse(doc, path: Path) -> "dict[str, KindDef]":
    if not isinstance(doc, dict):
        raise PortKindsConfigError(
            "%s does not parse to a mapping (got %s)" % (path, type(doc).__name__))
    kinds_doc = doc.get("kinds")
    if not isinstance(kinds_doc, dict) or not kinds_doc:
        raise PortKindsConfigError(
            "%s: `kinds` must be a non-empty mapping of id -> kind def" % path)
    kinds: "dict[str, KindDef]" = {}
    for kind_id, raw in kinds_doc.items():
        if not isinstance(raw, dict):
            raise PortKindsConfigError(
                "%s: kinds[%r] is not a mapping (got %s)" % (path, kind_id, type(raw).__name__))
        steps = raw.get("steps")
        if not isinstance(steps, list) or not steps or not all(
                isinstance(s, str) and s for s in steps):
            raise PortKindsConfigError(
                "%s: kinds[%r].steps must be a non-empty list of non-empty strings"
                % (path, kind_id))
        requires = raw.get("requires") or []
        if not isinstance(requires, list) or not all(isinstance(r, str) and r for r in requires):
            raise PortKindsConfigError(
                "%s: kinds[%r].requires must be a list of non-empty strings" % (path, kind_id))
        kinds[kind_id] = KindDef(id=kind_id, steps=tuple(steps), requires=tuple(requires))
    return kinds


_CACHE: "dict[Path, KindTable]" = {}


def load_kinds(path: "Path | None" = None) -> KindTable:
    """The loaded table at `path` (default: the shipped `port-project-kinds.yaml`).
    Cached by resolved path — same reasoning as `port_steps.load_steps`."""
    p = Path(path) if path is not None else default_path()
    p = p.resolve()
    cached = _CACHE.get(p)
    if cached is not None:
        return cached
    if not p.is_file():
        raise PortKindsConfigError("%s does not exist" % p)
    doc = yaml_io.yaml_load(p.read_text(encoding="utf-8"))
    table = KindTable(kinds=_parse(doc, p))
    _CACHE[p] = table
    return table


def validate_project(project, table: "KindTable | None" = None) -> list:
    """Every problem `.port/port.yaml`'s `project:` block has against `table` (default:
    the shipped table). `project` absent (`None`) is valid — no kind declared means no
    equivalence steps, never an error (`port_config.py`'s own module docstring: `project`
    is an optional key). An empty list means valid, matching `port_config.
    validate_config`'s own convention."""
    if project is None:
        return []
    if not isinstance(project, dict):
        return ["`project` is not a mapping (got %s)" % type(project).__name__]
    if table is None:
        table = load_kinds()

    problems = []
    for key in sorted(set(project) - _ALLOWED_PROJECT_KEYS):
        problems.append("`project` has the unknown key `%s`" % key)

    kind = project.get("kind")
    if not isinstance(kind, str) or not kind:
        problems.append(
            "`project.kind` is missing or not a non-empty string (known kinds: %s)"
            % ", ".join(sorted(table.known_ids)))
        return problems
    kdef = table.kinds.get(kind)
    if kdef is None:
        problems.append(
            "`project.kind` %r is not declared in port-project-kinds.yaml (known: %s)"
            % (kind, ", ".join(sorted(table.known_ids))))
        return problems

    app = project.get("app")
    if "app" in kdef.requires:
        if not isinstance(app, str) or not app.strip() or ":" not in app:
            problems.append(
                "`project.kind` %r requires `project.app` as `module:attr` (e.g. "
                "pkg.main:app)" % kind)
    elif app is not None and (not isinstance(app, str) or not app.strip()):
        problems.append("`project.app` must be a non-empty string when present")
    up = project.get("upstream_app")
    if up is not None and (not isinstance(up, str) or ":" not in up or not up.strip()):
        problems.append("`project.upstream_app` must be `module:attr` when present "
                        "(the upstream app route-parity compares against)")
    return problems


def steps_for_project(project, table: "KindTable | None" = None) -> "tuple[str, ...]":
    """The extra step ids `project.kind` contributes to the standard lane — empty when
    `project` is absent, not a mapping, or names a kind `validate_project` would already
    reject: an invalid `project` is the CALLER's own `config_invalid` to report (`hs_run_
    port.py` already does, ahead of ever reading a batch's lane); this never raises for
    that, so one bad config never compounds into a SECOND, unrelated crash here."""
    if not isinstance(project, dict):
        return ()
    if table is None:
        table = load_kinds()
    kind = project.get("kind")
    kdef = table.kinds.get(kind) if isinstance(kind, str) else None
    return kdef.steps if kdef is not None else ()
