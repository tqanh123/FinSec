"""codegraph_snapshot_reader — the one door a caller reads a snapshot's
`_index.json` through, WITHOUT importing the `codegraph` package.

A flat module under `harness/scripts/`, outside the `codegraph` package, because
living inside it would cost every reader the package import below.
`codegraph._core.snapshot.store.load_dir` delegates to `read_index` below — one
door, not two — but this module itself imports nothing under `codegraph`, on
purpose: `import codegraph._core.
snapshot.store` alone executes `codegraph/__init__.py`, `_core/__init__.py`
and `snapshot/__init__.py`, which transitively pull in the WHOLE measurement
engine (adapters, descriptor store, path-driver scanning) plus
`ruamel.yaml` — measured at 150 modules (2026-09-26, @4d201e849) landing in `sys.modules` for a
caller that only wants to read one dict back off disk. `hs-run` (and
anything else that just needs `_index.json`) should not pay that weight, or
inherit that import surface, just to check a schema version.

ALLOWED IMPORTS: the stdlib, plus `hs_family.errors` (an existing
harness-internal primitive, not `codegraph`) for the shared exit-2 error
type, and `secret_scan_before_ship` for the git-hardening constants below
(also flat under `harness/scripts/`, stdlib-only itself — measured at 59
modules added, none of them `codegraph`). Nothing else — a third-party
import here that `hs-run` does not already carry would be new weight on
hs-run's own import path, which is exactly what this module exists to avoid
adding. Git access is `subprocess` directly (`_git_text_or_empty` below),
never `codegraph._core.git` — that module is under the package this reader
exists to stay outside of; it shares the SAME hardening constants instead of
a second copy, so the two doors cannot drift apart on this.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Union

import secret_scan_before_ship as secret_scan
from hs_family.errors import UsageError
from hs_family.versioning import int_version_problem

#: The schema versions of the kernel's two documents. SOURCE OF TRUTH: here (not
#: in `codegraph._core.descriptor.model`) so a reader outside the `codegraph`
#: package never has to import that package to learn them; `descriptor.model`
#: re-exports the descriptor's under the same name.
#:
#: Two numbers, not one: a project's `codemap.yaml` and a snapshot's `_index.json`
#: used to share a single constant, so a snapshot format change would have made
#: every descriptor in every repo unreadable.
DESCRIPTOR_SCHEMA_VERSION = 2
#: A snapshot of Python records only -- its bytes are what they always were.
SNAPSHOT_SCHEMA_VERSION = 2
#: A snapshot holding a record of another language, whose module id is a path
#: (`a/b.ts`): a reader that predates path ids refuses it rather than splitting
#: one on dots.
SNAPSHOT_SCHEMA_VERSION_MIXED = 3

#: Every snapshot `schema_version` this reader accepts.
KNOWN_SNAPSHOT_SCHEMA_VERSIONS: "frozenset[int]" = frozenset({SNAPSHOT_SCHEMA_VERSION,
                                                              SNAPSHOT_SCHEMA_VERSION_MIXED})

_INDEX_NAME = "_index.json"

#: `<stamp>-<commit8>`, an optional trailing content-key fragment (several
#: content-keyed dirty siblings of one commit share the first two fields) —
#: the snapshot store's own directory-naming contract. MOVED here from
#: `codegraph._core.snapshot.store.SNAP_RE` (that module now binds
#: `SNAP_RE = codegraph_snapshot_reader.SNAPSHOT_DIR_NAME_RE`, the same
#: re-export shape `worktree_key`/`snapshot_base` already use below) so a
#: reader outside the `codegraph` package — hs-run's facts locator — can
#: filter candidate directories BY NAME, no file touched, before deciding
#: which ones are worth an actual read.
SNAPSHOT_DIR_NAME_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{8}(?:-[0-9a-f]{8,64})?")


class SnapshotIndexError(UsageError):
    """`_index.json` is missing, unreadable, unparseable as JSON, or stamped
    with a `schema_version` this reader does not know (including absent —
    `dict.get` returns `None`, and `None` is never a member of
    `KNOWN_SNAPSHOT_SCHEMA_VERSIONS`). Inherits `UsageError`'s exit code (2, the
    hs-* family's "honest cannot-run") — a snapshot this old/new/broken is
    not a warning to route around, it is a refusal to read.
    """


# ── provenance: which trees one call read ───────────────────────────────────
#
# Every snapshot a caller reads comes through `read_index` below, and every tree
# measured fresh comes through `take_snapshot` (which reports it here via
# `record_measured`) — so these two are where a call learns WHICH trees its answer
# is about, with no verb carrying code of its own. Inside a `recording()` block each
# read or measure appends one event, in call order; outside one nothing is kept.
# A read made only to CHOOSE a snapshot (a store scan comparing ids, keys or
# timestamps) passes `record=False`: it is not a tree the answer came from.
# `codegraph._core.snapshot.freshness.provenance_rows` turns the events into the
# rows hs-code puts on its envelope.

_RECORDER: "contextvars.ContextVar[Optional[list]]" = contextvars.ContextVar(
    "codegraph_snapshot_reads", default=None)


@contextlib.contextmanager
def recording() -> "Iterator[list]":
    """Collect, in call order, one event per recorded snapshot read or fresh measure
    made inside the block."""
    events: list = []
    token = _RECORDER.set(events)
    try:
        yield events
    finally:
        _RECORDER.reset(token)


def recording_active() -> bool:
    """Whether a `recording()` block is open — a measure computes its row only then."""
    return _RECORDER.get() is not None


def record_measured(out_dir: "Union[str, Path]", row: dict) -> None:
    """A fresh measurement filed at `out_dir`, already described as `row`."""
    events = _RECORDER.get()
    if events is not None:
        events.append({"kind": "measure", "dir": str(Path(out_dir).resolve()), "row": row})


def _record_read(d: Path, data: dict) -> None:
    events = _RECORDER.get()
    if events is not None:
        events.append({"kind": "read", "dir": str(d.resolve()),
                       "snapshot": str(data.get("snapshot") or d.name),
                       "snapshot_commit": str(data.get("commit") or "") or None,
                       "roots": [r["path"] for r in data.get("roots") or []
                                 if isinstance(r, dict) and r.get("path")]
                       + [str(x) for x in data.get("config_inputs") or []]})


def read_index(snapshot_dir: "Union[str, Path]", *,
               unversioned_format: "Optional[str]" = None, record: bool = True) -> dict:
    """Read and validate ONE snapshot directory's `_index.json`.

    Validates `schema_version` and nothing heavier — no other key's shape or
    presence is checked here; a caller that needs more reads the returned
    dict itself. `codegraph._core.snapshot.store.load_dir` delegates to this
    exact function, so a caller going through either door sees identical
    validation.

    `unversioned_format`: the caller's own record says this snapshot was
    written in a named format that stamps no `schema_version` (the port
    domain's legacy format). Then the stamp must be ABSENT — a file carrying
    one disagrees with the record, and is refused rather than read either way.

    `record=False`: a read made only to choose among snapshots — kept out of the
    call's provenance (`recording()`).
    """
    d = Path(snapshot_dir)
    f = d / _INDEX_NAME
    if not f.is_file():
        raise SnapshotIndexError(f"{d} lacks {_INDEX_NAME} — regenerate the snapshot")
    try:
        text = f.read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotIndexError(f"{f} could not be read: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SnapshotIndexError(f"{f} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SnapshotIndexError(
            f"{f} does not decode to a JSON object (got {type(data).__name__})")
    got = data.get("schema_version")
    if unversioned_format is not None:
        if "schema_version" in data:
            raise SnapshotIndexError(
                f"{f} declares schema_version {got!r}, but it is recorded as the "
                f"unversioned {unversioned_format!r} format — rebuild it")
        if record:
            _record_read(d, data)
        return data
    if int_version_problem(got, KNOWN_SNAPSHOT_SCHEMA_VERSIONS) is not None:
        known = ", ".join(str(v) for v in sorted(KNOWN_SNAPSHOT_SCHEMA_VERSIONS))
        raise SnapshotIndexError(
            f"{f} declares schema_version {got!r}; known version(s): {known}")
    if record:
        _record_read(d, data)
    return data


# ── the cheap sidecar: selection metadata without a full `_index.json` parse ─
#
# `_index.json` on a real tree measures in the TENS of megabytes (this repo's
# own: 14,452,296 bytes) -- fine for a caller that wants the whole thing, but
# a SELECTION decision (which snapshot answers for the current commit) only
# ever needs `commit`/`tree_hash`/`roots`/`content_key`/`snapshot`/
# `measured_at`, a few hundred bytes. `_META_NAME` is written by the snapshot
# WRITER (`codegraph._core.snapshot.pipeline.take_snapshot`) right after
# `_index.json`, through `write_meta` below, and read back through
# `read_meta` -- ONE door for both directions, so the sidecar's shape lives
# in exactly one place rather than a writer and a reader each guessing the
# other's field names. A snapshot written BEFORE this sidecar existed simply
# lacks the file; `read_meta` returns `None` for that (never raises), and a
# caller's own fallback is `read_index` — slower, but correct. A sidecar
# that DOES exist and parse but declares a schema outside `KNOWN_SCHEMA_
# VERSIONS` is the one case `read_meta` DOES raise — see its own
# docstring below.

_META_NAME = "_meta.json"

#: The metadata sidecar's own field set, in the order `write_meta`'s own
#: keyword arguments declare them — the one place this sidecar's shape is
#: spelled out, so a test asserting "the sidecar carries everything
#: `select_snapshot` needs" reads it from here rather than a hand-copied
#: list that could drift from the real one.
META_FIELDS: "tuple[str, ...]" = (
    "schema_version", "commit", "snapshot", "package", "roots",
    "tool", "tree_hash", "content_key", "measured_at")


def write_meta(snapshot_dir: "Union[str, Path]", *, commit: str, snapshot: str,
               package: str, roots: "list[dict]", schema_version: int = SNAPSHOT_SCHEMA_VERSION,
               tool: "Union[dict, None]" = None, tree_hash: "Union[str, None]" = None,
               content_key: "Union[str, None]" = None,
               measured_at: "Union[float, None]" = None,
               descriptor_sha: "Union[str, None]" = None,
               languages: "Union[dict, None]" = None,
               config_inputs: "Union[list, None]" = None) -> None:
    """Write `_meta.json` from the writer's own already-in-hand values —
    never reads `_index.json` back to build it (the WRITER already holds
    every one of these as a local variable while building the full index;
    reconstructing them from the rendered index TEXT would cost the writer
    the same parse this sidecar exists to let a READER skip). Optional
    fields absent (`None`, the kernel format's own "not applicable" value —
    see `format.py`'s `index_json` docstring) are omitted from the file
    entirely, never written as `null`, matching the full index's own rule
    for the same fields."""
    meta: dict = {"schema_version": schema_version, "commit": commit,
                 "snapshot": snapshot, "package": package, "roots": list(roots)}
    if tool is not None:
        meta["tool"] = tool
    if tree_hash is not None:
        meta["tree_hash"] = tree_hash
    if content_key is not None:
        meta["content_key"] = content_key
    if measured_at is not None:
        meta["measured_at"] = measured_at
    if descriptor_sha is not None:
        meta["descriptor_sha"] = descriptor_sha
    if languages:
        meta["languages"] = languages
    if config_inputs:
        meta["config_inputs"] = list(config_inputs)
    d = Path(snapshot_dir)
    (d / _META_NAME).write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")


def descriptor_file_sha(root: "Union[str, Path]", name: str = "codemap.yaml") -> "Union[str, None]":
    """sha256 (short) of the descriptor file's bytes, `None` when there is none
    to read. Stamped by the writer as `descriptor_sha`; the facts engine,
    holding no descriptor loader, compares it with the file on disk now."""
    try:
        data = (Path(root) / name).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()[:16]


def stamp_commit_prefix(name: str) -> str:
    """`<stamp>-<commit8>` of a snapshot directory name, any content-key
    suffix stripped — the chronological part (the stamp is COMMIT time)."""
    return "-".join(name.split("-")[:3])


def chronological(dirs: "Iterable[Path]", measured_at: "Callable[[Path], float]") -> "list[Path]":
    """`dirs` oldest -> newest: by name across seconds (the stamp), by each
    one's own `measured_at` inside a group sharing the stamp. Two ties need it:
    content-keyed siblings of one commit (a key suffix carries no time and, as a
    string, sorts AFTER the plain name), and two commits stamped in the same
    second (their sha's hex order is no time at all). `measured_at` is asked
    only inside a tie. The ONE rule the store's "latest" and the facts engine
    both pick by."""
    by_name = sorted(dirs, key=lambda d: d.name)
    groups: "dict[str, list[Path]]" = {}
    for d in by_name:
        groups.setdefault(stamp_commit_prefix(d.name).rsplit("-", 1)[0], []).append(d)
    out: "list[Path]" = []
    for _prefix, group in sorted(groups.items()):
        out += group if len(group) == 1 else sorted(group, key=lambda d: (measured_at(d), d.name))
    return out


def read_meta(snapshot_dir: "Union[str, Path]") -> "Union[dict, None]":
    """`_meta.json` of one snapshot directory, or `None`.

    `None` covers a MISSING sidecar (a snapshot written before it existed)
    and one that fails to even parse as a JSON object (a partial write, a
    hand-edited file) — the same signal to every caller: "no shortcut
    here, fall back to `read_index`".

    A sidecar that DOES parse but declares a `schema_version` outside
    `KNOWN_SNAPSHOT_SCHEMA_VERSIONS` (including absent) is a DIFFERENT signal: the
    snapshot itself is unreadable, not merely lacking the cheap path. This now RAISES
    `SnapshotIndexError` for that case, the identical
    check `read_index` runs (`int_version_problem`, never a second,
    drifting copy of it) — a caller selecting among several candidates
    (`hs_run_facts.engine.select_snapshot`) must see this candidate as
    broken through EITHER door, never quietly accept a stale/future-schema
    meta as if it were fine because it happened to arrive via the cheap
    path."""
    p = Path(snapshot_dir) / _META_NAME
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    got = data.get("schema_version")
    if int_version_problem(got, KNOWN_SNAPSHOT_SCHEMA_VERSIONS) is not None:
        known = ", ".join(str(v) for v in sorted(KNOWN_SNAPSHOT_SCHEMA_VERSIONS))
        raise SnapshotIndexError(
            f"{p} declares schema_version {got!r}; known version(s): {known}")
    return data


# ── the tool stamp: which code produced this snapshot's bytes ───────────────
#
# Two snapshots of two different trees (or two runs of the same tree taken
# months apart, across an upgrade) can land in the same base directory —
# `codegraph._core.snapshot.pipeline.take_snapshot` already refuses to mix
# `mode` for exactly this reason (a daily pass and a frozen milestone in one
# base lie about the denominator). A CODE change to the measurement engine
# itself is the same shape of silent lie: every printed number still looks
# normal, but two snapshots in one base no longer mean the same thing. The
# `tool` stamp names the code that produced a snapshot so a reader can catch
# that, and this is the one place both write-time (what stamp does the
# CURRENT code carry) and compare-time (do two stamps agree) logic live —
# `pipeline.py` imports this module the same way `store.py` already does
# (`import codegraph_snapshot_reader`, no cycle: this module never imports
# anything under `codegraph`).

#: A stamp this reader could not find (an older snapshot, predating this
#: field, or an index this function was handed with no `tool` key at all).
#: Spelled out explicitly rather than compared as `None == None` reading
#: as "the same" by accident -- `stamp_mismatch` still treats two UNSTAMPED
#: sides as equal (neither carries information to disagree about), but an
#: UNSTAMPED side is never silently folded into a REAL one.
UNSTAMPED = "<unstamped>"

#: The files under this tree shape a snapshot's OUTPUT bytes -- `.py` code and the
#: assets a language pack reads beside it (a grammar query, a Node bridge, a table);
#: a change outside it (a hook, a rule, an unrelated script) does not, so the hash
#: intentionally never reads harness/ beyond this closure plus the language book and
#: the data files its rows declare under `stamp:`.
_TOOL_CODE_DIRNAME = "codegraph"
_TOOL_CODE_SELF = "codegraph_snapshot_reader.py"
_TOOL_CODE_SUFFIXES = frozenset({".py", ".scm", ".cjs", ".json", ".yaml"})
#: relative to harness/ (the parent of `scripts_dir`)
_LANGUAGE_BOOK = "data/hs-code-languages.yaml"


def _language_assets(harness_dir: Path) -> "list[Path]":
    """The language book plus every file its rows declare under `stamp:`, those that
    exist. Read with `yaml_io`, never through `codegraph`: this module imports none
    of it."""
    book = harness_dir / _LANGUAGE_BOOK
    if not book.is_file():
        return []
    import yaml_io
    out = [book]
    doc = yaml_io.safe_load(book.read_text(encoding="utf-8")) or {}
    for row in (doc.get("languages") or []) if isinstance(doc, dict) else []:
        for rel in (row.get("stamp") or []) if isinstance(row, dict) else []:
            f = harness_dir / str(rel)
            if f.is_file() and f not in out:
                out.append(f)
    return out


def compute_tool_stamp(scripts_dir: "Union[str, Path, None]" = None) -> "dict[str, Any]":
    """The CURRENT code's provenance stamp: `{"version": ..., "hash": ...}`.

    `version` is this harness's own version identity — `harness_release.
    read_release()["harness_version"]`, the same door `artifact_stamp.py`
    already stamps markdown provenance through (one source, not a second
    invented one).

    `hash` is a sha256 (truncated to 16 hex chars, the length `python_
    adapter.py`'s own `file_sha` already uses for a content fingerprint)
    over every `.py`/`.scm`/`.cjs`/`.json`/`.yaml` file under `codegraph/`, this
    reader module itself, the language book and the data files its rows declare
    — sorted, keyed by the path RELATIVE to `scripts_dir` (never an absolute
    machine path) so two hosts running byte-identical code compute
    the byte-identical hash. Depends on nothing about the tree a snapshot
    measures, and nothing about wall-clock time — only on the measurement
    code itself.

    `scripts_dir` defaults to this module's own directory (`harness/
    scripts/`, resolved from `__file__` rather than the working directory —
    the walk-up-from-cwd trap a bare relative path would otherwise fall
    into); a caller may pass another directory (tests proving path-
    independence; a future porting context) laid out the same way.
    """
    root = Path(scripts_dir).resolve() if scripts_dir is not None else Path(__file__).resolve().parent
    codegraph_dir = root / _TOOL_CODE_DIRNAME
    if not codegraph_dir.is_dir():
        raise SnapshotIndexError(
            f"{codegraph_dir} does not exist -- cannot stamp the code that "
            f"shapes snapshot output")
    files = [p for p in codegraph_dir.rglob("*") if "__pycache__" not in p.parts
             and p.suffix in _TOOL_CODE_SUFFIXES and p.is_file()]
    files.append(root / _TOOL_CODE_SELF)
    files.sort(key=lambda p: p.relative_to(root).as_posix())
    # keyed relative to harness/, after the code: a key never collides with a code path
    assets = sorted(_language_assets(root.parent), key=lambda p: p.relative_to(root.parent).as_posix())

    h = hashlib.sha256()
    for key, p in ([(f.relative_to(root).as_posix(), f) for f in files]
                   + [("../" + f.relative_to(root.parent).as_posix(), f) for f in assets]):
        h.update(key.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")

    import harness_release  # harness-internal, stdlib-only itself; no codegraph import
    release = harness_release.read_release(root.parent)
    stamp = {"version": release.get("harness_version", ""), "hash": h.hexdigest()[:16]}
    built_from = _dev_describe(root)
    if built_from:
        stamp["built_from"] = built_from
    return stamp


# Stamp keys a reader is shown and a comparison never reads. `built_from` names the
# dev checkout a stamp came from, so two measurements by different builds can be told
# apart by more than a hash; it differs on every commit, and stamp equality decides
# snapshot reuse, so letting it into the comparison would make every commit a miss.
_INFORMATIONAL_KEYS = ("built_from",)


def same_tool(a, b) -> bool:
    """Do two `tool` stamps name the same measuring code? The one comparison every
    reuse and drift check uses — `built_from` is never part of it."""
    def core(x):
        return ({k: v for k, v in x.items() if k not in _INFORMATIONAL_KEYS}
                if isinstance(x, dict) else x)
    return core(a) == core(b)


def _dev_describe(scripts_root: Path) -> "str | None":
    """`git describe` of the checkout a dev build runs from; None for a release
    (its version already names it) or off a git checkout."""
    import harness_release
    if harness_release.read_release(scripts_root.parent).get("harness_version") != _dev_version():
        return None
    checkout = scripts_root.parent.parent
    if not (checkout / ".git").exists():
        return None
    try:
        proc = subprocess.run(["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(checkout),
                               "describe", "--always", "--dirty", "--tags"],
                              capture_output=True, text=True, timeout=10,
                              env=secret_scan.git_safe_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    described = proc.stdout.strip()
    return described if proc.returncode == 0 and described else None


def _dev_version() -> str:
    import harness_release  # harness-internal, stdlib-only itself; no codegraph import
    return harness_release.DEV_VERSION


def _stamp_text(stamp: "dict[str, Any] | None") -> str:
    """One-line rendering of a stamp -- `UNSTAMPED` for an absent/malformed one.
    The dev version is spelled out: it is the version of a checkout with no
    release stamp, not a release numbered zero."""
    if not isinstance(stamp, dict) or not stamp:
        return UNSTAMPED
    version = stamp.get("version", "?")
    if version == _dev_version():
        version = f"{version} (a development checkout: no release number)"
    built = f" built_from={stamp['built_from']}" if stamp.get("built_from") else ""
    return f"version={version} hash={stamp.get('hash', '?')}{built}"


def current_code_label(scripts_dir: "Union[str, Path, None]" = None) -> str:
    """How a message names the code running now: `the current code`, plus the
    checkout's own `git describe` when it runs from a development checkout (its
    stamp version is the dev version, which names no release). Printed only --
    stored in the stamp only as `built_from`, which `same_tool` never compares:
    stamp equality decides reuse, and a commit id there would make every commit a
    mismatch."""
    root = Path(scripts_dir).resolve() if scripts_dir is not None else Path(__file__).resolve().parent
    described = _dev_describe(root)
    label = "the current code"
    return f"{label} (dev checkout {described})" if described else label


def stamp_mismatch(index_a: "dict[str, Any]", index_b: "dict[str, Any]", *,
                   labels: "tuple[str, str]" = ("the first snapshot", "the second snapshot"),
                   ) -> "Union[str, None]":
    """A warning naming both snapshots' `tool` stamps when they disagree,
    else `None`. Shared by the writer (comparing a fresh pass against the
    latest existing snapshot in the same base) and any later reader (a drift
    check comparing two snapshots after the fact) — ONE door, so neither can
    drift out of sync with the other. `labels` names the two sides in the
    caller's own words.

    The text says what the stamp IS (the hash of the measuring code, not of
    the tree), that each snapshot stands on its own, and what makes a
    comparison of the two sound.

    An index with no `tool` key at all (an older snapshot, or a synthetic
    index a caller built by hand) reads as `UNSTAMPED` -- distinct from
    every real stamp, so a real-vs-absent pair always warns. Two absent
    sides compare equal (neither carries a claim to disagree with).
    """
    a = index_a.get("tool") if isinstance(index_a, dict) else None
    b = index_b.get("tool") if isinstance(index_b, dict) else None
    if same_tool(a, b):
        return None
    la, lb = labels
    return (f"tool stamp mismatch — two snapshots measured by different codegraph "
            f"measuring code: {la}: {_stamp_text(a)}; {lb}: {_stamp_text(b)}. The hash "
            f"names the measuring code, so a difference between these two snapshots may "
            f"come from that code changing, not from the tree. Each snapshot is sound on "
            f"its own; before comparing them, re-measure the older tree with the current "
            f"code (`hs-code --apply graph snapshot` at that commit).")


# ── worktree key + snapshot-base layout ──────────────────────────────────────
#
# MOVED from `codegraph._core.snapshot.worktree_key` (that module now rebinds
# `_digest`/`worktree_toplevel`/`worktree_key` to these same function objects —
# `worktree_key = codegraph_snapshot_reader.worktree_key`, the identical
# re-export shape `descriptor.model.DESCRIPTOR_SCHEMA_VERSION` already uses for
# the constant above). A reader outside the `codegraph` package (hs-run's
# facts engine) needs to resolve a snapshot's OWN path layout without paying
# the whole-package import this module exists to avoid — `worktree_key` and
# the `codegraph/snapshots/<project>/<key>` suffix `snapshot_base` builds are
# both PURE (no measurement, no descriptor/pipeline dependency), so moving
# them costs nothing the original design cared about keeping inside the
# package. `head_tree_hash`, `dirty_scope`, `content_key`, `descriptor_digest`
# and `reuse_key` stay in `codegraph._core.snapshot.worktree_key` — those
# read a descriptor's `roots`, which is a `codegraph`-internal shape this
# reader has no reason to know.

#: Hex chars of the sha256 digest kept in a worktree key -- long enough that
#: two distinct absolute paths practically never collide, short enough that
#: the resulting directory name stays readable. Same value the original
#: `codegraph._core.snapshot.worktree_key._DIGEST_LEN` used.
_DIGEST_LEN = 12


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_DIGEST_LEN]


def _git_text_or_empty(repo: "Union[str, Path]", *args: str) -> str:
    """`git -C <repo> <args>`, stripped stdout, or `""` on ANY failure --
    missing `git` binary, a non-git directory, a non-zero exit. Mirrors
    `codegraph._core.git.git_text(..., check=False)`'s fail-open shape
    (that call site never raises either — `run_git` only raises when
    `check=True`) without importing the package that defines it; this
    module's own git access is `subprocess` directly, per its ALLOWED
    IMPORTS docstring. `repo` is a caller-supplied path this process does
    not own, so it carries the same hardening `run_git` does — `-c
    core.fsmonitor=false` plus `GIT_CONFIG_NOSYSTEM=1` — reusing
    `secret_scan_before_ship`'s constants rather than a private copy."""
    try:
        r = subprocess.run(
            ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(repo), *args],
            capture_output=True, env=secret_scan.git_safe_env())
    except OSError:
        return ""
    if r.returncode != 0:
        return ""
    return r.stdout.decode(errors="replace").strip()


def worktree_toplevel(repo: "Union[str, Path]") -> Optional[str]:
    """`git rev-parse --show-toplevel` of `repo`, or `None` outside a git
    checkout (a non-git tree has no toplevel to key reuse on) or any other
    git failure -- fail-open, matching every other query this reader and
    the `codegraph` package itself make."""
    top = _git_text_or_empty(repo, "rev-parse", "--show-toplevel")
    return top or None


def worktree_key(repo: "Union[str, Path]") -> str:
    """The snapshot-base path component for `repo`'s own worktree: a short
    digest of the absolute toplevel path, plus a readable tail (the
    directory's own name) -- so two worktrees of one repo (or two entirely
    unrelated trees sharing a directory name) land under two different
    snapshot bases and neither reads or overwrites the other's.

    Resolved through `--show-toplevel`, never the literal `repo` argument
    directly: a caller may point `--repo` at a SUBDIRECTORY of a worktree,
    and the key must still name that worktree's own root, not one path
    among many that resolve to it.

    Non-git tree (no toplevel): keyed by `repo`'s own resolved absolute path
    instead -- still unique per directory, just without the git vocabulary
    to call it a "worktree".
    """
    top = worktree_toplevel(repo)
    anchor = Path(top) if top else Path(repo).resolve()
    return f"{anchor.name}-{_digest(str(anchor))}"


def snapshot_base(data_root: "Union[str, Path]", project: str, root: "Union[str, Path]") -> Path:
    """The snapshot store's layout PORTION every reader/writer of a
    project's snapshots shares: `<data_root>/codegraph/snapshots/<project>/
    <worktree_key(root)>`. ONE definition -- `codegraph.graph.verbs.
    _snapshot_base` (the writer's `--out`/data-root resolution) and hs-run's
    facts locator (a reader) both call THIS function for the suffix, rather
    than each hand-joining the same four path segments.

    `data_root` is the CALLER's own job to resolve (`harness_paths.
    tool_data_root()` for a Bash-tool caller, an explicit `--out` directory
    that skips this function entirely, ...) -- this function never resolves
    one itself, so a caller needing a different tier or an escape hatch
    keeps that logic at its own call site instead of this shared layout
    function growing a caller-specific branch."""
    return Path(data_root) / "codegraph" / "snapshots" / project / worktree_key(root)
