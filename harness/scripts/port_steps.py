"""port_steps.py — the ONE loader for `harness/data/port-steps.yaml`, the
port domain's standard step lane, as one FLAT module under
`harness/scripts/` (sibling of `port_batch_reader.py`/`port_runs.py`:
`hs-run` resolves a module by a bare, dot-free name, and both `hs_run_port.
py` and `port_batch_reader.py` need this exact table — one reads it to
render a step's command, the other to validate a batch's own `steps:`
pick/reorder against the same known-id set. Two readers of the same YAML
through two ad hoc parsers is how they drift; this is the one door.

ALLOWED IMPORTS: the stdlib, `yaml_io` (the one YAML parser every config
reader in this tree shares) and `harness_paths` (to find where `harness/
data/port-steps.yaml` ships from — bin-root, the same tree the shipped
registries themselves resolve against). Nothing under `codegraph`: this
module sits on `port_batch_reader.py`'s own import path, which
`hs_run_port.py` imports directly, so the same import-weight discipline
applies here.

PLACEHOLDERS are a CLOSED set, resolved by `render_argv` from values the
caller already has in hand (a loaded `Batch`, the batch's own port root, and
— only for a `per_dist: true` step — one dist string):
    {batch}             batch.name
    {dist}              the caller-given dist (per_dist steps only)
    {frozen_snapshot}   batch.frozen_snapshot
    {new_snapshot}      batch.new_snapshot
    {new_snapshot_dir}  str(snapshot_dir_fn(port_root, batch.new_snapshot))
    {ledger_path}       str(ledger_path_fn(port_root))
    {baseline}          `.port/batches/<batch>/route-parity-baseline.yaml`
    {app}, {upstream}, {upstream_app}
                        the caller-given project `values` (a project kind's own
                        steps read them off `.port/port.yaml` through
                        `port_config.step_values_for_port_root`; this module never
                        imports `port_kinds` itself, the caller resolves them)
An argv template naming a placeholder whose value is `None` (e.g.
`{frozen_snapshot}` on a batch that never declared one) raises
`StepRenderError` naming the missing field — never silently renders the
literal string `None` into a command a caller might actually run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml_io

_DATA_REL = ("harness", "data", "port-steps.yaml")

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")


class PortStepsConfigError(Exception):
    """`port-steps.yaml` itself does not parse to the shape this module
    expects — a packaging bug, never a per-repo condition (the file ships
    with the harness; no project edits it)."""


class UnknownStepError(ValueError):
    """A batch's `steps:` names an id the step table does not declare."""


class StepRenderError(Exception):
    """A step's argv template names a placeholder this batch cannot supply
    right now (e.g. `{frozen_snapshot}` when the batch declares none) — the
    caller's own missing-config problem, reported by name rather than
    rendering the literal string `None` into a command."""


@dataclass(frozen=True)
class StepDef:
    """One step kind's shape, exactly as declared in `port-steps.yaml`."""
    id: str
    argv: "tuple[str, ...]"
    per_dist: bool
    writes: bool


def default_path() -> Path:
    """`<bin_root>/harness/data/port-steps.yaml` — the shipped table."""
    import harness_paths
    return harness_paths.bin_root().joinpath(*_DATA_REL)


#: The lane a batch runs when its `batch.yaml` names none.
DEFAULT_LANE = "port"


def _parse(doc, path: Path) -> "tuple[dict[str, tuple[str, ...]], dict]":
    if not isinstance(doc, dict):
        raise PortStepsConfigError(
            "%s does not parse to a mapping (got %s)" % (path, type(doc).__name__))
    lanes_doc = doc.get("lanes")
    if not isinstance(lanes_doc, dict) or not lanes_doc:
        raise PortStepsConfigError(
            "%s: `lanes` must be a non-empty mapping of lane name -> step ids" % path)
    lanes: "dict[str, tuple[str, ...]]" = {}
    for name, ids in lanes_doc.items():
        if not isinstance(ids, list) or not ids or not all(isinstance(s, str) and s for s in ids):
            raise PortStepsConfigError(
                "%s: `lanes.%s` must be a non-empty list of step ids" % (path, name))
        lanes[str(name)] = tuple(ids)
    if DEFAULT_LANE not in lanes:
        raise PortStepsConfigError(
            "%s: `lanes.%s` is missing — the lane a batch naming none runs" % (path, DEFAULT_LANE))
    steps_doc = doc.get("steps")
    if not isinstance(steps_doc, dict) or not steps_doc:
        raise PortStepsConfigError(
            "%s: `steps` must be a non-empty mapping of id -> step def" % path)
    steps: dict = {}
    for step_id, raw in steps_doc.items():
        if not isinstance(raw, dict):
            raise PortStepsConfigError(
                "%s: steps[%r] is not a mapping (got %s)"
                % (path, step_id, type(raw).__name__))
        argv = raw.get("argv")
        if not isinstance(argv, list) or not argv or not all(
                isinstance(w, str) and w for w in argv):
            raise PortStepsConfigError(
                "%s: steps[%r].argv must be a non-empty list of non-empty "
                "strings" % (path, step_id))
        per_dist = raw.get("per_dist")
        writes = raw.get("writes")
        if not isinstance(per_dist, bool) or not isinstance(writes, bool):
            raise PortStepsConfigError(
                "%s: steps[%r].per_dist and .writes must both be booleans"
                % (path, step_id))
        steps[step_id] = StepDef(id=step_id, argv=tuple(argv), per_dist=per_dist,
                                 writes=writes)
    for name, ids in lanes.items():
        unknown_lane = [s for s in ids if s not in steps]
        if unknown_lane:
            raise PortStepsConfigError(
                "%s: `lanes.%s` names step id(s) %s not declared under `steps`"
                % (path, name, unknown_lane))
    return lanes, steps


class StepTable:
    """One loaded `port-steps.yaml`: every named lane's step order plus every
    declared step's `StepDef`, keyed by id. `lane` is the default lane."""

    def __init__(self, lanes: "dict[str, tuple[str, ...]]", steps: "dict[str, StepDef]") -> None:
        self.lanes = lanes
        self.steps = steps

    @property
    def lane(self) -> "tuple[str, ...]":
        return self.lanes[DEFAULT_LANE]

    @property
    def known_ids(self) -> "frozenset[str]":
        return frozenset(self.steps)


_CACHE: "dict[Path, StepTable]" = {}


def lane_of(batch_steps: "tuple[str, ...] | list[str]", table: StepTable,
           extra_steps: "tuple[str, ...] | list[str]" = (),
           omit_steps: "tuple[str, ...] | list[str]" = (),
           lane: "str | None" = None) -> "list[str]":
    """The lane one batch runs: its own `steps:` pick (order kept) when it
    declares one, else the table's standard `lane` with `extra_steps` (a
    project kind's own equivalence steps — `port_config.
    extra_steps_for_port_root`) appended right after `rules` (or at the very
    end when the lane has no `rules` step — there is only one lane shape
    today, `rules` second-to-last, but the append point is "after rules",
    never "unconditionally last"). A batch that DOES declare its own
    `steps:` picks/reorders the FULL known id set (kind steps included, since
    they are declared ids like any other) — `extra_steps` contributes
    nothing there; the batch's own list is authoritative, unchanged from
    before this parameter existed.

    `omit_steps` (`port_config.omitted_steps_for_port_root`: a step the target
    gives nothing to run, e.g. `rules` with no `rules:` declared) leaves the
    standard lane only, after the kind steps found their place — a batch's own
    `steps:` that names one keeps it and gets that step's own refusal.

    `lane` (the batch's own `lane:`, `None` = `DEFAULT_LANE`) picks which named
    lane is the standard one; a name the table does not declare raises
    `UnknownStepError` naming every lane it does.

    An id `table` does not declare (from either `batch_steps` or
    `extra_steps`) raises `UnknownStepError` naming every such entry and
    every known id, so the fix is one edit. The batch reader checks only the
    shape of `steps:`; the ids are this table's to judge."""
    known = sorted(table.known_ids)
    lane_name = lane or DEFAULT_LANE
    if lane_name not in table.lanes:
        raise UnknownStepError("unknown lane %r (known: %s)"
                               % (lane_name, ", ".join(sorted(table.lanes))))
    if not batch_steps:
        ids = list(table.lanes[lane_name])
        bad = ["kind step %r is not declared under `steps:` in port-steps.yaml" % sid
               for sid in extra_steps if sid not in table.known_ids]
        if bad:
            raise UnknownStepError("%s (known: %s)" % ("; ".join(bad), ", ".join(known)))
        if extra_steps:
            i = ids.index("rules") + 1 if "rules" in ids else len(ids)
            ids = ids[:i] + list(extra_steps) + ids[i:]
        return [sid for sid in ids if sid not in omit_steps]
    bad = ["`steps[%d]` names unknown step id %r" % (i, sid)
           for i, sid in enumerate(batch_steps) if sid not in table.known_ids]
    if bad:
        raise UnknownStepError("%s (known: %s)" % ("; ".join(bad), ", ".join(known)))
    return list(batch_steps)


def load_steps(path: "Path | None" = None) -> StepTable:
    """The loaded table at `path` (default: the shipped `port-steps.yaml`).
    Cached by resolved path — the shipped file never changes mid-process,
    and a test pointing `path` at its own fixture gets its own cache slot,
    never a stale hit from an earlier default-path load."""
    p = Path(path) if path is not None else default_path()
    p = p.resolve()
    cached = _CACHE.get(p)
    if cached is not None:
        return cached
    if not p.is_file():
        raise PortStepsConfigError("%s does not exist" % p)
    doc = yaml_io.yaml_load(p.read_text(encoding="utf-8"))
    lanes, steps = _parse(doc, p)
    table = StepTable(lanes=lanes, steps=steps)
    _CACHE[p] = table
    return table


def render_argv(
        step: StepDef, *, batch, port_root, dist: "str | None" = None,
        values: "Mapping[str, str | None] | None" = None, snapshot_dir_fn=None,
        ledger_path_fn=None) -> "list[str]":
    """`step.argv` with every `{placeholder}` substituted from `batch` /
    `port_root` / `dist`, plus the project's own `values` (`app`, `upstream`,
    `upstream_app` -- `port_config.step_values_for_port_root`). `snapshot_dir_fn`/`ledger_path_fn` default to the
    real `port_batch_reader.snapshot_path` / `port_ledger_reader.ledger_path`
    — overridable so a caller that already imported them (or a test) never
    pays for a second import of either.

    Raises `StepRenderError` when a placeholder the template names resolves
    to `None` — never renders the literal word `None` into a command a
    caller might run verbatim."""
    if snapshot_dir_fn is None:
        import port_batch_reader
        snapshot_dir_fn = port_batch_reader.snapshot_path
    if ledger_path_fn is None:
        import port_ledger_reader
        ledger_path_fn = port_ledger_reader.ledger_path

    import port_batch_reader as _batches
    project_values = dict(values or {})
    values = {
        "batch": batch.name,
        "dist": dist,
        "frozen_snapshot": batch.frozen_snapshot,
        "new_snapshot": batch.new_snapshot,
        "new_snapshot_dir": (str(snapshot_dir_fn(port_root, batch.new_snapshot))
                             if batch.new_snapshot else None),
        "ledger_path": str(ledger_path_fn(port_root)),
        "baseline": str(_batches.batches_dir(port_root) / batch.name / "route-parity-baseline.yaml"),
        "app": project_values.get("app"),
        "upstream": project_values.get("upstream"),
        "upstream_app": project_values.get("upstream_app"),
        "from_snapshot": project_values.get("from_snapshot"),
    }

    def _resolved(name: str, word: str) -> str:
        if name not in values:
            raise StepRenderError(
                "step %r's argv word %r names an unknown placeholder "
                "{%s}" % (step.id, word, name))
        value = values[name]
        if value is None:
            raise StepRenderError(
                "step %r needs {%s}, but batch %r declares none — fix "
                "batch.yaml before this step can render" % (step.id, name, batch.name))
        return value

    def _sub(word: str) -> str:
        for name in _PLACEHOLDER_RE.findall(word):
            word = word.replace("{%s}" % name, _resolved(name, word))
        return word

    return [_sub(w) for w in step.argv]


def parse_step_value(value: str) -> "tuple[str, str, str | None]":
    """`BATCH/STEP[/DIST]` -> `(batch, step, dist_or_none)`. Raises
    `ValueError` naming the value when the shape is not 2 or 3 non-empty
    `/`-separated parts."""
    parts = [p for p in value.split("/")]
    if len(parts) not in (2, 3) or not all(parts):
        raise ValueError(
            "--step %r must be BATCH/STEP[/DIST] (2 or 3 non-empty "
            "`/`-separated parts)" % value)
    batch, step = parts[0], parts[1]
    dist = parts[2] if len(parts) == 3 else None
    return batch, step, dist
