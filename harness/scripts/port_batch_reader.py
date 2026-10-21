"""port_batch_reader.py — the `port` domain's batch reader, as one FLAT
module under `harness/scripts/` (sibling of `port_workspace.py`,
`port_config.py`, `port_ledger_reader.py`: `hs-run` resolves a module by a
bare, dot-free name, and this reader is going to be `hs-run port next`'s
door onto the same layout, not only `hs-code port`'s).

LAYOUT — one repo, several batches, one shared ledger (never a per-batch
ledger the way the upstream tool laid it out):
    <port_root>/.port/batches/<name>/batch.yaml
    <port_root>/.port/batches/<name>/specs/*.yaml
    <port_root>/.port/batches/<name>/map/
    <port_root>/.port/snapshots/<name>/       (one `_index.json` per name)

`batch.yaml` v1 — the CLOSED key set is deliberately the minimum the verbs
that exist today actually read, nothing speculative added ahead of a real
caller:
    version: 1
    sources:
      - id: <str>              # matches a `.port/port.yaml` sources[].id
        pinned_sha: <40-hex>    # the commit the WHOLE batch is pinned to
    frozen_snapshot: <name>     # optional: a name under .port/snapshots/
    new_snapshot: <name>        # optional: a name under .port/snapshots/
    dists: [<str>, ...]         # optional: declared dist ids, no `tier` —
                                 # the `map` rule kind's own `dists:` param;
                                 # nothing else reads this key
    steps: [<str>, ...]         # optional: pick/reorder port-steps.yaml's
                                 # own standard `lane` — the step table
                                 # judges the ids (`port_steps.lane_of`)

`pinned_sha` exists because a node's own 8-hex anchor prefix can always be
re-resolved against the live source repo (`port_map_verb`'s pre-batch path
already does that, through `codegraph._core.git.resolve_ref`) — but a batch
is a promise that every line written under it agrees on ONE commit, so a
caller in batch mode never touches the source repo's git history at all:
the pinned value wins outright, and a node whose own anchor disagrees with
it is refused rather than silently trusted. `frozen_snapshot`/`new_snapshot`
name a directory under `.port/snapshots/`, not a path — `hs-run port next`
and `hs-code port` must agree on where a name resolves, and a bare name
(never a caller-supplied path) is the only way to keep that one door.

ALLOWED IMPORTS: the stdlib, `yaml_io` (the one YAML parser every config
reader in this tree shares), `hs_family` (declared sanctioned for this
module), `port_ledger_spec` (flat, no `codegraph` — only its node-key
grammar, `new_anchor`, is used here), and `codegraph_snapshot_reader` (flat too — reads `_index.json`
WITHOUT importing the `codegraph` package, see its own module docstring).
Nothing under `codegraph` itself: `hs_run_port.py` imports this exact
module (`list_batches`, its own `:159`), and `hs-run`/`hs_family` must
never carry that import weight — `harness/tests/test_codegraph_snapshot_
reader.py`'s import-closure gate holds every module reachable from
`hs_run*.py` to that. Deliberately does NOT resolve `port_root` itself (no
`port_workspace` import) — every function here takes `port_root` already
resolved, the same discipline `port_ledger_reader.py` already holds to.

`steps:` — a batch MAY declare `steps: [id, ...]` to pick/reorder the
standard step lane. This reader checks its SHAPE only (a list of non-empty
strings) in the same `_validate_batch_config` every other key goes through;
whether each id names a real step is the step table's question
(`port_steps.lane_of`), since that table sits on top of this module and
this module never imports it. Absent entirely means "use the standard
lane" — `Batch.steps` is `()` then.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import codegraph_snapshot_reader
import hs_family
import port_ledger_spec
import yaml_io
from ambiguous_input import NOT_FOUND, Ambiguous, NotFound
from hs_family.errors import UsageError
from hs_family.versioning import int_version_problem

_BATCHES_DIRNAME = "batches"
_SNAPSHOTS_DIRNAME = "snapshots"
_BATCH_CONFIG_NAME = "batch.yaml"

#: The bulk-anchor shorthand `port map`/`hs-run port next` resolve against a
#: batch's OWN latest new-tree snapshot — ported (algorithm-faithful) from
#: `ob-tools@60075cb:packages/portkit/src/portkit/ledger/ledger_io.py`'s own
#: `_anchor_batch` (:144-158). The literal, already-resolved `new@<hash8>`
#: form is a DIFFERENT string (no `batch` word) and never matches this
#: prefix — resolving it a second time would be a no-op at best.
BATCH_ANCHOR_PREFIX = "new@batch"

ALLOWED_TOP_KEYS = frozenset({
    "version", "sources", "frozen_snapshot", "new_snapshot", "dists", "steps",
    "plan", "handoff_sha256", "lane", "from_batch", "flows", "flow_builders",
})

_ALLOWED_SOURCE_KEYS = frozenset({"id", "pinned_sha"})

#: `handoff_sha256` — a sha256 hex digest (64 lowercase hex), the same shape
#: `hashlib.sha256(...).hexdigest()` produces. Distinct from `pinned_sha`'s
#: 40-hex git commit shape (`port_ledger_spec.is_sha40`) — a different
#: digest algorithm over a different kind of bytes (a file's own contents,
#: never a commit).
_HANDOFF_SHA256_RE = re.compile(r"[0-9a-f]{64}")



class BatchNotFoundError(NotFound, UsageError):
    """No `.port/batches/<name>/batch.yaml` under the port root — a real
    `--batch` typo, or a batch never started. Distinct from `BatchConfigError`
    the same way `port_workspace.PortRootNotFoundError` is distinct from
    `BadRepoPathError`: this is "nothing there" (exit 1), not "what is there
    is broken" (exit 2)."""
    exit_code = NOT_FOUND

    def __init__(self, message: str, name: str) -> None:
        NotFound.__init__(self, message, "batch", name)
        self.message = message


class SnapshotNotFoundError(NotFound, UsageError):
    """A snapshot the CALLER named (`--from-snapshot`) is not under
    `.port/snapshots/`. One that `batch.yaml` names and is missing is broken
    config, a plain `UsageError`."""
    exit_code = NOT_FOUND

    def __init__(self, message: str, name: str) -> None:
        NotFound.__init__(self, message, "snapshot", name)
        self.message = message


class BatchConfigError(UsageError):
    """`batch.yaml` exists but fails validation — an unknown key, a bad
    `version`, a `sources[]` entry missing `id`/`pinned_sha`, or a
    `pinned_sha` that is not 40 lowercase hex digits. Every problem found is
    reported, in the order checked, the same discipline `port_config.
    ConfigProblems` already uses for `.port/port.yaml`."""


@dataclass(frozen=True)
class Batch:
    """One batch's resolved shape. `frozen_snapshot`/`new_snapshot` are
    `None` when `batch.yaml` declares neither — a batch that has not frozen
    a snapshot yet, or has not built the new tree yet, is a real, valid
    state (mirrors upstream's own `BatchPaths.frozen: Path | None`), never
    an error on its own; a caller that actually NEEDS one names its own
    refusal when it is missing. `dists` is likewise `()` when undeclared —
    a caller that needs the declared dist list (the `map` rule kind's own
    `dists:` param, built by the port gate page) names its own refusal
    rather than guessing one from `specs/*.yaml` filenames."""

    name: str
    dir: Path
    sources: "tuple[Mapping[str, Any], ...]"
    frozen_snapshot: "str | None"
    new_snapshot: "str | None"
    dists: "tuple[str, ...]" = ()
    #: `()` when `batch.yaml` declares no `steps:` — "use the standard
    #: lane", never a copy of it: `port_steps.lane_of` reads the table's
    #: own lane when this is empty, so the standard lane changing in
    #: `port-steps.yaml` is never masked by a value frozen here at load time.
    steps: "tuple[str, ...]" = ()
    #: `None` until `/hs:port` pass 2 turns `.port/batches/<name>/
    #: handoff.json` into plan phases and records the plan path here
    #: A batch whose mechanical steps never produced
    #: a handoff item, or one whose items are not yet planned, is a normal
    #: state, not an error.
    plan: "str | None" = None
    #: `None` until the same pass 2 write. The sha256 of `handoff.json`'s
    #: own bytes AT THE TIME `plan` was recorded — `hs-run port next`
    #: compares this against the file's CURRENT hash to know whether the
    #: plan is stale (a step re-ran and changed the items since).
    handoff_sha256: "str | None" = None
    #: the named lane of `port-steps.yaml` this batch runs; `None` = the default.
    lane: "str | None" = None
    #: the batch this one catches up from (`hs-code port catch-up`); `None` for
    #: a batch that ports from scratch.
    from_batch: "str | None" = None
    #: the runs `hs-code port flow-trace` reads off the new tree: `{id, at,
    #: name?, builder?}` each; `flow_builders` adds builder libraries over the
    #: shipped `flow-builders.yaml` table.
    flows: "tuple[Mapping[str, Any], ...]" = ()
    flow_builders: "Mapping[str, Any] | None" = None

    @property
    def specs_dir(self) -> Path:
        return self.dir / "specs"

    @property
    def map_dir(self) -> Path:
        return self.dir / "map"


def batches_dir(port_root: "str | Path") -> Path:
    return Path(port_root) / ".port" / _BATCHES_DIRNAME


def snapshots_dir(port_root: "str | Path") -> Path:
    return Path(port_root) / ".port" / _SNAPSHOTS_DIRNAME


def sources_dir(port_root: "str | Path") -> Path:
    return Path(port_root) / ".port" / "sources"


def source_checkout(port_root: "str | Path", source_id: str, sha: str) -> Path:
    """`<port_root>/.port/sources/<id>-<sha8>` — the read-only checkout of one
    source at one pinned commit (`hs-code port source`); the directory tree
    walks never enter (`codegraph._core.walk.FOREIGN_TREES`)."""
    return sources_dir(port_root) / ("%s-%s" % (source_id, sha[:8]))


def snapshot_path(port_root: "str | Path", name: str) -> Path:
    """`<port_root>/.port/snapshots/<name>` — never a caller-typed path; a
    batch config only ever NAMES a snapshot, this is the one place that name
    becomes a directory."""
    return snapshots_dir(port_root) / name


ASSIGNED_FILENAME = "assigned.json"


def read_assigned(batch: "Batch") -> "dict[str, str]":
    """`<batch>/map/assigned.json` — the `module::name -> dist` sidecar `hs-code
    port assign` writes BESIDE the frozen snapshot (which is never rewritten).
    `{}` when assign has never written for this batch, a real state; `ValueError`
    when the file exists but is not a `str -> str` mapping — a corrupt sidecar
    must never read as "nothing assigned"."""
    import json
    p = batch.map_dir / ASSIGNED_FILENAME
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("%s does not parse as JSON (%s)" % (p, exc)) from exc
    if not isinstance(data, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise ValueError("%s does not parse to a mapping of string -> string" % p)
    return data


def node_dists(idx: dict, assigned: "Mapping[str, str] | None" = None) -> "list[tuple[str, dict, str | None]]":
    """`(module, symbol, dist)` for every symbol and method of a snapshot, in
    snapshot order — the ONE dist lookup every port verb reads a frozen node's
    dist through: the assign sidecar's entry wins, then the snapshot's own
    `dist` field (a snapshot upstream assigned in place), then — for a method —
    its class's dist."""
    sidecar = assigned or {}
    out: "list[tuple[str, dict, str | None]]" = []
    for _unit, mods in (idx.get("units") or {}).items():
        for m in mods:
            def visit(sym: dict, cls_dist: "str | None") -> None:
                d = sidecar.get("%s::%s" % (m["module"], sym["name"])) or sym.get("dist") or cls_dist
                out.append((m["module"], sym, d))
                for mm in sym.get("methods") or []:
                    visit(mm, d)

            for sym in m.get("symbols") or []:
                visit(sym, None)
    return out


SNAPSHOT_REGISTRY_NAME = "index.json"


def snapshot_registry_path(port_root: "str | Path") -> Path:
    return snapshots_dir(port_root) / SNAPSHOT_REGISTRY_NAME


def read_snapshot_registry(port_root: "str | Path") -> dict:
    """`.port/snapshots/index.json` — `{name: {commit, sha256[, format]}}`, what
    `hs-code port snapshot` recorded for every snapshot it built. `{}` when
    absent; `hs_family.UsageError` when present but not a JSON object: a broken
    registry is broken state, never read as empty (the next write would erase
    every recorded hash)."""
    import json
    path = snapshot_registry_path(port_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError("%s does not parse as JSON (%s)" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise UsageError("%s does not decode to a JSON object (got %s)"
                         % (path, type(data).__name__))
    return data


def read_snapshot_dir(port_root: "str | Path", snapshot_dir: "str | Path") -> dict:
    """One `.port/snapshots/<dir>/_index.json`, read in the format the registry
    records for it: a snapshot `port snapshot` built through a legacy descriptor
    is the unversioned legacy format and carries no `schema_version`. Raises
    `codegraph_snapshot_reader.SnapshotIndexError` like `read_index` itself."""
    d = Path(snapshot_dir)
    entry = read_snapshot_registry(port_root).get(d.name)
    fmt = entry.get("format") if isinstance(entry, dict) else None
    return codegraph_snapshot_reader.read_index(d, unversioned_format=fmt)


def list_batches(port_root: "str | Path") -> "list[str]":
    """Every batch NAME that has a `batch.yaml` under it, sorted. A
    directory under `batches/` with no `batch.yaml` (mid-creation, or a
    stray dir) is not a batch — never listed, never guessed at."""
    d = batches_dir(port_root)
    if not d.is_dir():
        return []
    return sorted(p.parent.name for p in d.glob("*/" + _BATCH_CONFIG_NAME))


def _validate_batch_config(cfg: Any) -> "list[str]":
    if not isinstance(cfg, dict):
        return ["`%s` does not parse to a mapping (got %s)"
                % (_BATCH_CONFIG_NAME, type(cfg).__name__)]

    problems: "list[str]" = []
    for key in sorted(set(cfg) - ALLOWED_TOP_KEYS):
        problems.append("unknown top-level key `%s`" % key)

    version_problem = int_version_problem(cfg.get("version"), {1})
    if version_problem is not None:
        problems.append("`version` %s" % version_problem)

    sources = cfg.get("sources")
    if not isinstance(sources, list) or not sources:
        problems.append("`sources` is missing or not a non-empty list")
    else:
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
            sha = s.get("pinned_sha")
            if not port_ledger_spec.is_sha40(sha):
                hint = ("" if sha is None or isinstance(sha, str) else
                        " (YAML read it as a %s: write the SHA in quotes)" % type(sha).__name__)
                problems.append(
                    "`sources[%d].pinned_sha` must be a 40-hex commit SHA, not %r%s"
                    % (i, sha, hint))

    for key in ("frozen_snapshot", "new_snapshot", "lane", "from_batch"):
        v = cfg.get(key)
        if v is not None and (not isinstance(v, str) or not v.strip()):
            problems.append(
                "`%s` must be a non-empty string when present, not %r" % (key, v))

    dists = cfg.get("dists")
    if dists is not None:
        if not isinstance(dists, list) or not all(
                isinstance(d, str) and d.strip() for d in dists):
            problems.append(
                "`dists` must be a list of non-empty strings when present, not %r" % (dists,))

    steps = cfg.get("steps")
    if steps is not None:
        if not isinstance(steps, list) or not all(
                isinstance(s, str) and s.strip() for s in steps):
            problems.append(
                "`steps` must be a list of non-empty strings when present, not %r" % (steps,))

    plan = cfg.get("plan")
    if plan is not None and (not isinstance(plan, str) or not plan.strip()):
        problems.append("`plan` must be a non-empty string when present, not %r" % (plan,))

    flows = cfg.get("flows")
    if flows is not None and not (isinstance(flows, list) and all(
            isinstance(f, dict) and isinstance(f.get("id"), str) and isinstance(f.get("at"), str)
            for f in flows)):
        problems.append("`flows` must be a list of mappings with string `id` and `at`, not %r"
                        % (flows,))
    builders = cfg.get("flow_builders")
    if builders is not None and not isinstance(builders, dict):
        problems.append("`flow_builders` must be a mapping of builder name -> call names, not %r"
                        % (builders,))

    handoff_sha256 = cfg.get("handoff_sha256")
    if handoff_sha256 is not None and not (
            isinstance(handoff_sha256, str) and _HANDOFF_SHA256_RE.fullmatch(handoff_sha256)):
        problems.append(
            "`handoff_sha256` must be a 64-hex sha256 digest, not %r" % (handoff_sha256,))

    return problems


def load_batch(port_root: "str | Path", name: str) -> Batch:
    """The one door onto one batch's `batch.yaml`. Raises `BatchNotFoundError`
    (no such batch) or `BatchConfigError` (batch.yaml is invalid) — never
    returns a partially-usable `Batch`."""
    bdir = batches_dir(port_root) / name
    cfg_path = bdir / _BATCH_CONFIG_NAME
    if not cfg_path.is_file():
        known = list_batches(port_root)
        raise BatchNotFoundError(
            "no batch named %r under %s (known: %s)"
            % (name, batches_dir(port_root), ", ".join(known) or "none"), name)
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BatchConfigError("%s could not be read (%s)" % (cfg_path, exc)) from exc
    try:
        cfg = yaml_io.yaml_load(text)
    except Exception as exc:  # noqa: BLE001 — any parse failure is one problem
        raise BatchConfigError("%s does not parse as YAML (%s)" % (cfg_path, exc)) from exc

    problems = _validate_batch_config(cfg)
    if isinstance(cfg, dict) and cfg.get("from_batch") == name:
        problems.append("`from_batch` names the batch itself (%r)" % name)
    if problems:
        raise BatchConfigError(
            "%s is invalid:\n" % cfg_path
            + "\n".join("  · %s" % p for p in problems))

    sources = tuple(cfg.get("sources") or ())
    return Batch(
        name=name, dir=bdir, sources=sources,
        frozen_snapshot=cfg.get("frozen_snapshot"),
        new_snapshot=cfg.get("new_snapshot"),
        dists=tuple(cfg.get("dists") or ()),
        steps=tuple(cfg.get("steps") or ()),
        plan=cfg.get("plan"),
        handoff_sha256=cfg.get("handoff_sha256"),
        lane=cfg.get("lane"), from_batch=cfg.get("from_batch"),
        flows=tuple(cfg.get("flows") or ()), flow_builders=cfg.get("flow_builders"))


def find_source(batch: Batch, source_id: "str | None" = None):
    """One `sources[]` entry of `batch`, by id — or the sole entry when the
    batch declares exactly one and the caller named none. Returns `(entry,
    problem)`, the same shape `port_config.find_source` already uses for
    `.port/port.yaml` — a caller checks `problem` and reports it verbatim,
    never re-derives the ambiguity message."""
    sources = batch.sources
    if source_id is None:
        if len(sources) == 1:
            return sources[0], None
        ids = sorted(str(s.get("id")) for s in sources)
        if len(ids) > 1:
            return None, Ambiguous("batch %r declares %d source(s)" % (batch.name, len(sources)), "", ids,
                                   "pass --source <id> to say which one")
        return None, (
            "batch %r declares %d source(s) (%s) — pass --source <id> to "
            "say which one" % (batch.name, len(sources), ", ".join(ids) or "none"))
    for s in sources:
        if s.get("id") == source_id:
            return s, None
    return None, NotFound("no source named %r in batch %r" % (source_id, batch.name), "source", source_id)


def read_snapshot(
        port_root: "str | Path", snapshot_name: str, *, caller_named: bool = False,
) -> "tuple[str, dict]":
    """The ONE door onto a named snapshot's `_index.json` — one read, one
    error mapping, handing back both the full 40-hex commit it stamps
    itself with AND the parsed index, so no second caller ever re-opens the
    same file through `codegraph_snapshot_reader.read_index` (that dead
    second read used to follow `snapshot_commit` at every call site: this
    replaces both with one). Raises `hs_family.UsageError` when the
    snapshot itself cannot be read (missing/unparseable/unknown schema —
    the SAME refusal `codegraph_snapshot_reader.read_index` already gives
    any other caller), and `hs_family.RefusedError` when the snapshot IS
    readable but its own `commit` field is not a resolvable 40-hex sha —
    the fallback the snapshot pipeline itself writes (`codegraph._core.
    snapshot.pipeline.commit_stamp`) when `git log` could not name one. A
    line this harness writes must carry a real `src.sha`; a snapshot that
    never learned its own commit cannot hand one out. `caller_named`: the
    name came from the command line, so an absent snapshot is not found
    (1), never broken; one a `batch.yaml` itself names and is missing falls
    through to the broken-state (2) read failure below."""
    idx_path = snapshot_path(port_root, snapshot_name)
    if caller_named and not idx_path.exists():
        have = sorted(p.name for p in snapshots_dir(port_root).glob("*")) if snapshots_dir(port_root).is_dir() else []
        raise SnapshotNotFoundError("no snapshot named %r under %s (known: %s)"
                                    % (snapshot_name, snapshots_dir(port_root), ", ".join(have) or "none"),
                                    snapshot_name)
    try:
        idx = read_snapshot_dir(port_root, idx_path)
    except codegraph_snapshot_reader.SnapshotIndexError as exc:
        raise hs_family.UsageError(str(exc)) from exc
    commit = idx.get("commit")
    if not port_ledger_spec.is_sha40(commit):
        raise hs_family.RefusedError(
            "snapshot %r's own commit stamp is not a resolvable 40-hex sha "
            "(got %r) — src.sha cannot be trusted from it" % (snapshot_name, commit))
    return commit, idx


def snapshot_commit(port_root: "str | Path", snapshot_name: str, *, caller_named: bool = False) -> str:
    """The full, 40-hex commit one named snapshot stamps its own `_index.
    json` with. A thin wrapper over `read_snapshot` for a caller that never
    needs the parsed index itself — see `read_snapshot` for the error
    mapping and the meaning of `caller_named`."""
    return read_snapshot(port_root, snapshot_name, caller_named=caller_named)[0]


def check_anchor_pinned(
        anchor: str, batch: Batch, source: Mapping[str, Any], *, what: str) -> str:
    """The ONE anchor/pin comparison door every `port_*.py`/
    `codegraph_port_*.py` module goes through — `harness/tests/port/
    test_port_anchor_pin_one_door.py`'s AST gate holds every other module
    in that glob to never itself write a compare mentioning `pinned` (an
    identifier, attribute or string constant), so a second, drifted
    comparison can't grow somewhere else unseen. `anchor` is either a
    caller-typed 8-hex node-key anchor (compared against `source`'s own
    `pinned_sha[:8]` — a node key only ever carries 8 hex digits) or a full
    40-hex commit, e.g. a frozen snapshot's own stamp (compared against the
    FULL pin — an 8-hex-only compare there would silently accept a snapshot
    that collides on the visible prefix but is not actually the pinned
    commit). Raises `hs_family.RefusedError` (exit 3 — a deliberate
    refusal, never a broken-state exit 2) naming `what` (the caller's own
    label for what was being checked), both 8-hex values, `batch.name` and
    `source`'s id, when they disagree. Returns the full 40-hex pin on
    success — the value a caller now trusts as `src.sha`, whichever form of
    `anchor` it passed in.

    BLIND SPOT (documented, not closed): the AST gate matches on the
    substring `pinned` inside a compare's own operands — an aliased binding
    that renames `pinned_sha`/`source["pinned_sha"]` to a name that does not
    itself contain `pinned` before comparing it elsewhere would not be
    caught. No module in this tree does that; a reviewer adding a new
    anchor check should route it through this function rather than rely on
    the gate to catch an alias."""
    pinned = str(source.get("pinned_sha"))
    match = anchor == pinned if len(anchor) == 40 else anchor == pinned[:8]
    if not match:
        shown = anchor[:8] if len(anchor) == 40 else anchor
        raise hs_family.RefusedError(
            "%s %r disagrees with batch %r's pinned source %r (%s…) — refusing"
            % (what, shown, batch.name, source.get("id"), pinned[:8]))
    return pinned


def pinned_frozen_snapshot(
        port_root: "str | Path", batch: Batch, source: Mapping[str, Any],
) -> "tuple[str, dict]":
    """`batch`'s `frozen_snapshot` commit AND its parsed index, the commit
    checked against `source`'s own `pinned_sha` before either is handed
    back: the invariant a batch promises is that its frozen snapshot IS the
    source repo at the pin, so every line keyed off it can carry the pin as
    its `src.sha` without re-resolving anything live against the source
    repo — and a caller that also needs the snapshot's node list (`cmd_
    assign`, `PORT_BATCH`, `port_gate_run`, `port_ledger_laws.collect`)
    gets it from this ONE read rather than a second `read_index` on the same
    path. Raises `hs_family.RefusedError` (exit 3 — a deliberate refusal,
    the request is well-formed but the SNAPSHOT disagrees with the batch's
    own promise, never a broken-state exit 2) when the frozen snapshot's
    commit is not the source's pin. Caller already checked `batch.
    frozen_snapshot` is set (mirrors `read_snapshot`'s own contract);
    re-checked here too as broken state (`hs_family.UsageError`, exit 2,
    the same answer every caller gives), and it narrows `str | None` to
    `str` for the call below."""
    if not batch.frozen_snapshot:
        raise hs_family.UsageError(
            "batch %r has no `frozen_snapshot` declared — nothing to check "
            "against a pin" % batch.name)
    commit, idx = read_snapshot(port_root, batch.frozen_snapshot)
    pinned = check_anchor_pinned(
        commit, batch, source, what="frozen snapshot %r's commit" % batch.frozen_snapshot)
    return pinned, idx


def pinned_frozen_commit(
        port_root: "str | Path", batch: Batch, source: Mapping[str, Any]) -> str:
    """`batch`'s `frozen_snapshot` commit, checked against `source`'s own
    `pinned_sha`. A thin wrapper over `pinned_frozen_snapshot` for a caller
    that never needs the parsed index itself."""
    return pinned_frozen_snapshot(port_root, batch, source)[0]


def new_batch_anchor(port_root: "str | Path", batch: Batch) -> str:
    """`new@<hash8>` at `batch`'s own `new_snapshot` — ported
    (algorithm-faithful) from upstream's `_anchor_batch`, resolved at CALL
    time (never cached): a hand-written `new@batch` line always means "the
    batch's latest new-tree snapshot right now", so a snapshot rebuilt
    between two calls in the same run resolves each call against its own
    latest, never a stale one read once and reused."""
    if not batch.new_snapshot:
        raise hs_family.RefusedError(
            "batch %r has no `new_snapshot` declared yet — write the "
            "literal `new@<hash8>` form instead" % batch.name)
    return port_ledger_spec.new_anchor(snapshot_commit(port_root, batch.new_snapshot))


def resolve_batch_anchor(port_root: "str | Path", batch: Batch, key_str: str) -> str:
    """`key_str` unchanged, unless it carries the `new@batch` shorthand as a
    PREFIX (checked as a prefix, never full-string equality — the real form
    is `new@batch::<module>::<name>`), in which case that shorthand is
    replaced by `new_batch_anchor(port_root, batch)`."""
    if not key_str.startswith(BATCH_ANCHOR_PREFIX):
        return key_str
    return key_str.replace(BATCH_ANCHOR_PREFIX, new_batch_anchor(port_root, batch), 1)
