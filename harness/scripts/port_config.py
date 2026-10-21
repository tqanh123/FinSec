#!/usr/bin/env python3
"""port_config.py — the `.port/port.yaml` checker, as one FLAT module under
`harness/scripts/`.

Moved out of `hs_run_port.py` (it used to own this inline) so the hs-code
side (which imports `codegraph`) and the hs-run side (which is never
allowed to) validate the SAME config through the ONE door, rather than the
hs-run copy growing its own second reader and the two drifting apart. This
module itself stays codegraph-free — every caller on hs-run's own import
path (`hs_run_port.py`) is checked by `test_codegraph_snapshot_reader.py`'s
import-closure gate, and that gate follows real imports, not a file-level
scan alone; a `codegraph` import landing here would still fail it one hop
away from `hs_run_port.py` itself.

Reads through `yaml_io.yaml_load`, never a second `import yaml` — see
`hs_run_port.py`'s own module docstring for why a second parser here would
read the same file a shade differently from every other config loader in
the tree.
"""
from __future__ import annotations

from pathlib import Path

import attrs as _attrs  # noqa: E402  (REQUIRED — see harness/requirements.txt)

import port_kinds
import yaml_io
from ambiguous_input import Ambiguous, NotFound
from hs_family.versioning import int_version_problem

# The exact top-level keys `port.yaml` may declare. An unrecognised key is
# reported by NAME rather than silently ignored — a typo'd `sourcess:` would
# otherwise leave `sources` unset and surface as the wrong problem entirely.
# Neither `namespace_root` nor `layout` belongs here: both moved to the
# TARGET's own `codemap.yaml` (`codegraph._core.descriptor`'s
# `namespace_root`/`packages_dir`/`src_subdir`/`tests_subdir` fields, read
# through `codegraph._core.layout.dist_root`/`dist_src_root`) — a port.yaml
# declaring either would otherwise give one target two, possibly
# disagreeing, layout declarations. `namespace_root` gets its own named
# migration message below (`_NAMESPACE_ROOT_MOVED_MSG`); `layout` has no
# reader left at all on this door, so it falls through to the generic
# unknown-top-level-key report like any other typo.
ALLOWED_TOP_KEYS = frozenset({
    "version", "preset", "sources", "project", "rules", "flows",
})

_NAMESPACE_ROOT_MOVED_MSG = (
    "`namespace_root` moved to the target's own `codemap.yaml` "
    "(descriptor `namespace_root:`) — remove it from `.port/port.yaml`")

# `rules:` only ever TIGHTENS or TURNS OFF a check — there is no key that
# loosens one, and no per-repo variant of a gate. A key outside this pair is
# not a typo the reader can guess the intent of.
_ALLOWED_RULE_KEYS = frozenset({"off", "tighten"})

# An `off` entry that does not carry all four is not a decision anyone can be
# held to later — who turned it off, when, and why are exactly the three
# facts a reason needs to still make sense in a review six months on.
_REQUIRED_OFF_FIELDS = ("id", "reason", "actor", "ts")

# The one rule id that can never be turned off, no matter how well-formed the
# entry naming it otherwise is. Checked independently of the four-field
# check above, so a *complete* attempt to disable it is still refused.
_UNKILLABLE_RULE_ID = "port.L0"

# `sources[]` entry keys. `id` is the only one anything reads unconditionally
# (`find_source`) — the rest are real, optional states (a repo with no
# `package` declared is a config that never needs `add-module`, not a broken
# one). `package` replaces the upstream hard-coded `_ROOT`/`gsm_agent`
# literal: `hs-code port add-module` reads it off the resolved source entry
# instead of a constant. `descriptor` is optional too: a project descriptor
# for THIS source, relative to the port root (the SAME base `repo:` already
# resolves against — `hs-code port snapshot`'s pinned mode is the one real
# reader, `codegraph_port_snapshot_verbs.py`). A real pre-`schema_version`
# descriptor is a project-local artifact describing how to read that
# source's old layout — it does not live inside the source tree itself
# (upstream `portkit/legacy.py:333-337`'s own `load_batch` reads it from the
# PLAN dir, never the source repo) — so `descriptor:` names where THIS repo
# keeps that file, falling back to the source tree's own `codemap.yaml`
# (today's only shape) when absent.
_ALLOWED_SOURCE_KEYS = frozenset({"id", "repo", "package", "src_subdir", "descriptor"})


@_attrs.define(frozen=True, kw_only=True)
class ConfigProblems:
    """Every way `port.yaml` failed validation, in the order they were found.

    A list rather than "the first problem" on purpose: two independent
    authors can each break a different rule in the same edit, and reporting
    only the first sends the second one through a whole extra run to learn
    about a problem that was sitting there the whole time.
    """

    problems: list = _attrs.field(factory=list)

    def as_dict(self) -> dict:
        return {"problems": list(self.problems)}


def read_config(path: Path):
    """Parse `path` as YAML.

    Returns `(value, problem)` — exactly one of the two is set. `problem` is
    already a ready-to-report sentence naming what went wrong; `value` is the
    parsed Python object when reading and parsing both succeeded (which may
    still not be a mapping — the caller checks that separately, because "not
    a mapping" and "does not parse at all" are the same class of problem, not
    two different questions).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, "`.port/port.yaml` could not be read (%s)" % exc
    try:
        value = yaml_io.yaml_load(text)
    except Exception as exc:  # noqa: BLE001 — any parse failure is one problem
        return None, "`.port/port.yaml` does not parse as YAML (%s)" % exc
    return value, None


def _validate_sources(sources) -> list:
    """Every `sources[]` entry's own problems — unknown key, missing/blank
    `id`, or a `package`/`repo`/`src_subdir`/`descriptor` present but not a
    non-empty string. Mirrors `port_batch_reader._validate_batch_config`'s
    own per-entry check for `batch.yaml sources[]`, the same discipline
    applied to `.port/port.yaml`'s own `sources[]` for the first time."""
    problems: list = []
    for i, s in enumerate(sources):
        if not isinstance(s, dict):
            problems.append("`sources[%d]` is not a mapping (got %s)"
                            % (i, type(s).__name__))
            continue
        for key in sorted(set(s) - _ALLOWED_SOURCE_KEYS):
            problems.append("`sources[%d]` has the unknown key `%s`" % (i, key))
        sid = s.get("id")
        if not isinstance(sid, str) or not sid.strip():
            problems.append("`sources[%d].id` is missing or not a non-empty string" % i)
        for key in ("repo", "package", "src_subdir", "descriptor"):
            v = s.get(key)
            if v is not None and (not isinstance(v, str) or not v.strip()):
                problems.append(
                    "`sources[%d].%s` must be a non-empty string when present, not %r"
                    % (i, key, v))
    return problems


def _validate_rules(rules) -> list:
    """The `rules:` block's own problems, isolated from the top-level checks
    so a caller reading `validate_config` can tell which layer a given
    problem came from without re-deriving it from the message text."""
    problems: list = []
    if rules is None:
        return problems
    if not isinstance(rules, dict):
        problems.append("`rules` is not a mapping (got %s)"
                        % type(rules).__name__)
        return problems

    bad_keys = sorted(set(rules) - _ALLOWED_RULE_KEYS)
    for key in bad_keys:
        problems.append(
            "`rules` has the key `%s`, which is neither `off` nor `tighten` "
            "— a gate can only be turned off or tightened, never loosened "
            "some third way" % key)

    off_entries = rules.get("off")
    if off_entries in (None, []):
        return problems
    if not isinstance(off_entries, list):
        problems.append("`rules.off` is not a list (got %s)"
                        % type(off_entries).__name__)
        return problems

    for i, entry in enumerate(off_entries):
        if not isinstance(entry, dict):
            problems.append("`rules.off[%d]` is not a mapping (got %s)"
                            % (i, type(entry).__name__))
            continue
        entry_id = entry.get("id")
        missing = [f for f in _REQUIRED_OFF_FIELDS if not entry.get(f)]
        if missing:
            problems.append(
                "`rules.off[%d]` (id=%r) is missing %s — an off entry with "
                "no reason/actor/ts recorded is not a decision anyone can be "
                "held to" % (i, entry_id, ", ".join(missing)))
        if entry_id == _UNKILLABLE_RULE_ID:
            problems.append(
                "`rules.off[%d]` turns off `%s`, which can never be turned "
                "off — remove this entry" % (i, _UNKILLABLE_RULE_ID))
    return problems


def validate_config(cfg) -> list:
    """Every problem `cfg` (the parsed value of `port.yaml`) has, in the order
    checked. An empty list means the config is valid — never `None`, so a
    caller can always test truthiness without a second `is None` branch."""
    if not isinstance(cfg, dict):
        return ["`.port/port.yaml` does not parse to a mapping (got %s)"
                % type(cfg).__name__]

    problems = []
    if "namespace_root" in cfg:
        problems.append(_NAMESPACE_ROOT_MOVED_MSG)
    for key in sorted(set(cfg) - ALLOWED_TOP_KEYS - {"namespace_root"}):
        problems.append("unknown top-level key `%s`" % key)

    version = cfg.get("version")
    # Shared with every other reader that compares a declared version
    # against a known set (`descriptor/store.py`'s schema check is the other
    # caller) — `type(...) is int` alone missed `2.0`/`True` (a bool IS an
    # int subclass, and both compare equal to the int under `==`); see
    # `hs_family/versioning.py`'s own docstring for the two real bugs this
    # closed.
    version_problem = int_version_problem(version, {1})
    if version_problem is not None:
        problems.append("`version` %s" % version_problem)

    sources = cfg.get("sources")
    if not isinstance(sources, list) or not sources:
        problems.append("`sources` is missing or not a non-empty list")
    else:
        problems.extend(_validate_sources(sources))

    problems.extend(_validate_rules(cfg.get("rules")))
    problems.extend(port_kinds.validate_project(cfg.get("project")))
    return problems


def load_and_validate(path: Path):
    """`read_config` + `validate_config` in one call — the shape every caller
    that just wants "is this config usable" actually wants, rather than
    re-deriving the `problem`-then-`validate_config` branch at each call
    site. Returns `(cfg, problems)`: `cfg` is `None` when the file could not
    even be read/parsed (the one problem it carries is `problems[0]`)."""
    cfg, read_problem = read_config(path)
    problems = [read_problem] if read_problem else validate_config(cfg)
    return cfg, problems


def find_source(cfg: dict, source_id: "str | None" = None):
    """One `sources[]` entry, by id — or the sole entry when the config
    declares exactly one and the caller named none (the common case: most
    repos port from one upstream). Returns `(entry, problem)`; `problem`
    names the ambiguity (several sources, no id given) or the miss (an id
    that names nothing declared) rather than guessing either way."""
    sources = cfg.get("sources") or []
    if source_id is None:
        if len(sources) == 1 and isinstance(sources[0], dict):
            return sources[0], None
        ids = sorted(str(s.get("id")) for s in sources if isinstance(s, dict))
        if len(ids) > 1:
            return None, Ambiguous("`.port/port.yaml` declares %d source(s)" % len(sources), "", ids,
                                   "pass --source <id> to say which one")
        return None, (
            "`.port/port.yaml` declares %d source(s) (%s) — pass --source "
            "<id> to say which one" % (len(sources), ", ".join(ids) or "none"))
    for s in sources:
        if isinstance(s, dict) and s.get("id") == source_id:
            return s, None
    return None, NotFound("no source named %r in `.port/port.yaml`" % source_id, "source", source_id)


def project_of(port_root) -> "dict | None":
    """`.port/port.yaml`'s `project:` block for `port_root`, or `None` when the config
    cannot even be read/parsed cleanly, or declares no `project:` — the caller's own
    `config_invalid` to report elsewhere; this never raises for that."""
    import port_workspace
    cfg, problems = load_and_validate(port_workspace.port_yaml_path(Path(port_root)))
    if problems or not isinstance(cfg, dict):
        return None
    project = cfg.get("project")
    return project if isinstance(project, dict) else None


def extra_steps_for_port_root(port_root, table=None) -> "tuple[str, ...]":
    """`port_kinds.steps_for_project` off `port_root`'s OWN `.port/port.yaml` — the one function
    `hs_run_port.py` and `port_runs.py` both call, so the lane each computes can never
    drift apart."""
    return port_kinds.steps_for_project(project_of(port_root), table)


def omitted_steps_for_port_root(port_root) -> "dict[str, str]":
    """`{step id: why}` for each standard-lane step the target gives nothing to
    run — today `rules` when the target's `codemap.yaml` declares no `rules:`.
    Run anyway it refuses every time, so the lane could never close; turned
    green it would read as a pass over nothing. Left out instead, with the why
    carried in `hs-run port next`'s warnings. A codemap that is absent or does
    not parse omits nothing: the step itself then reports what is wrong. Reads
    the top-level `rules:` key through `yaml_io`, never the descriptor loader —
    `hs-run`'s import closure never reaches `codegraph`. The one function
    `hs_run_port.py` and `port_runs.py` both call."""
    path = Path(port_root) / "codemap.yaml"
    try:
        doc = yaml_io.yaml_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — unreadable: the step reports it, not the lane
        return {}
    if not isinstance(doc, dict) or doc.get("rules"):
        return {}
    return {"rules": "%s declares no `rules:` — nothing checks the target tree's "
                     "architecture, so the `rules` step is left out of the lane" % path}


def step_values_for_port_root(port_root, batch) -> "dict[str, str | None]":
    """The project-side placeholder values a batch's step argv renders with:
    `app`, `upstream_app` (`project.upstream_app`, else `app` -- an in-place port
    keeps one app name), `upstream` (the read-only checkout of the batch's source
    at its pin that `hs-code port source` keeps -- never the source repo's own
    working tree, which may sit at any commit), and `from_snapshot` (the frozen snapshot
    of the batch's `from_batch`). A value that cannot be read is `None`,
    which `port_steps.render_argv` refuses by name for any step that needs it --
    the one function `hs_run_port.py` and `port_runs.py` both call."""
    import port_batch_reader
    import port_workspace
    project = project_of(port_root) or {}
    app = project.get("app")
    upstream = None
    cfg, problems = load_and_validate(port_workspace.port_yaml_path(Path(port_root)))
    batch_source, batch_problem = port_batch_reader.find_source(batch, None)
    if not problems and not batch_problem:
        source, source_problem = find_source(cfg, batch_source.get("id"))
        if not source_problem and source.get("repo") and batch_source.get("pinned_sha"):
            upstream = str(port_batch_reader.source_checkout(
                Path(port_root), str(batch_source.get("id")), str(batch_source["pinned_sha"])))
    from_snapshot = None
    if batch.from_batch:
        try:
            from_snapshot = port_batch_reader.load_batch(Path(port_root), batch.from_batch).frozen_snapshot
        except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError):
            from_snapshot = None
    return {"app": app, "upstream_app": project.get("upstream_app") or app, "upstream": upstream,
            "from_snapshot": from_snapshot}
