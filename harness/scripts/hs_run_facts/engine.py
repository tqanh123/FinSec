"""hs_run_facts.engine — resolve a state's declared `facts:` against its
project's codegraph snapshot store.

`resolve(...)` is a pure function of its arguments (no import of `hs_run`
itself, so there is no cycle): it reads the snapshot store through
`codegraph_snapshot_reader` (never `codegraph`), calls each declared fact's
provider, and returns a dict of OVERRIDE keys the caller (`hs_run.
_apply_facts`) merges onto the verb's raw result.

The outcome table, by project state:
  - not adopted (no `codemap.yaml` at all, `_not_adopted_outcome`): the
    verb's OWN `state`/`exit_code`/`next_command`/`next_action` survive
    UNTOUCHED — each declared fact reports `unavailable: true` with a
    reason, plus one `extra_warnings` line. Deliberately never routed
    through `_stale_outcome`: that branch's `next_command` (`hs-code ...
    graph snapshot`) itself REQUIRES a `codemap.yaml` to run, so handing
    it out here would promise a deterministic step (exit 11) that cannot
    complete — adopting code-graph snapshots is a decision the verb this
    fact rides on does not own.
  - adopted, snapshot missing/stale/broken (`_stale_outcome`/
    `_error_outcome`): `state`/`exit_code`/`next_command`/`next_action`/
    `domain_state` ARE rewritten — the snapshot genuinely cannot answer
    for the current tree, and (missing/stale) the refresh command
    genuinely can succeed here.
  - adopted, snapshot fresh: the ordinary path, `{"facts": {...}}` only.

Zero network, zero heavy import: git access is `subprocess` directly, the
same bar `codegraph_snapshot_reader.py` holds itself to (`codegraph._core.
git` stays unimported so the `hs_run` import closure never touches
`codegraph` -- see `test_the_hs_run_family_import_closure_never_touches_
codegraph`) -- but every call still carries `secret_scan_before_ship`'s
hardening (`GIT_SAFE_CONFIG`/`git_safe_env`), the same posture `run_git`
uses, since `root` here is just as caller-supplied and untrusted as any
`codegraph` git call's own target.
"""
from __future__ import annotations

import hashlib
import importlib
import shlex
import subprocess
from pathlib import Path
from typing import Optional, Union

import codegraph_snapshot_reader as reader
import codemap_adoption
import harness_paths
import port_ledger_spec
import secret_scan_before_ship as secret_scan
import yaml_io

#: Every git call below runs against `root`, a caller-supplied repo this
#: process does not own — the same class of exposure `codegraph._core.git.
#: run_git` hardens against (a repo's own `core.fsmonitor` can name a
#: program git EXECUTES on `ls-files`). Imported from `secret_scan_before_
#: ship` (the ONE definition site every other git caller in the tree already
#: shares) rather than redefined — this module still makes its OWN
#: subprocess calls (never `codegraph._core.git` itself: `hs_run`'s import
#: closure must never reach `codegraph`, see this module's own docstring and
#: `test_the_hs_run_family_import_closure_never_touches_codegraph`), so the
#: hardening has to travel here as data, not as a shared runner function.
_GIT_SAFE = secret_scan.GIT_SAFE_CONFIG
_git_env = secret_scan.git_safe_env

def _build_refresh_command(base_command: str, root: Path) -> str:
    """`base_command` (the registry's own `fact_sources.<name>.
    refresh_command`, e.g. `"hs-code graph snapshot"` -- the registry names the
    CONCEPT, not a literal argv a driver can blindly exec) rewritten into
    the concrete command a `facts_stale` driver can run VERBATIM: `--repo
    <root>` and `--apply` spliced in right after the binary name, root
    shell-quoted (`shlex.join`, not a hand-built f-string — a root path
    holding a space must still round-trip through a shell). Both flags are
    needed: without `--repo` the verb re-resolves its own root through
    `harness_root.resolve_project_root()` (which can answer for a
    DIFFERENT project than the one hs-run is examining — the same
    bare-path trap CLAUDE.md's "Working conventions" section names), and
    `graph snapshot` defaults to a DRY RUN without `--apply` (writes
    nothing, so the very command meant to CURE staleness would silently
    no-op). Both land BEFORE the verb path (the family core's
    `CliBase.strip_shell_flags` accepts them from anywhere in argv,
    but this keeps the emitted string readable left-to-right: binary, then
    scope, then verb). A `base_command` that is empty or fails to tokenize
    (an editor typo in the registry) is returned unchanged -- `resolve()`'s
    caller still gets SOME string to show, rather than this function
    raising and taking down an otherwise-fine `facts_stale` outcome."""
    try:
        parts = shlex.split(base_command)
    except ValueError:
        return base_command
    if not parts:
        return base_command
    binary, verb = parts[0], parts[1:]
    return shlex.join([binary, "--repo", str(root), "--apply", *verb])

#: How many of the newest cache directories `_prune_cache` keeps per fact
#: engine instance — mirrors `codegraph`'s own `prune_snapshots` policy of
#: keeping a bounded recent set rather than growing forever.
_CACHE_KEEP = 20


# ── small, private git access (subprocess only, no codegraph._core.git) ─────

def _git_text(root: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", *_GIT_SAFE, "-C", str(root), *args],
                           capture_output=True, env=_git_env())
    except OSError:
        return ""
    if r.returncode != 0:
        return ""
    return r.stdout.decode(errors="replace").strip()


def _head(root: Path) -> str:
    return _git_text(root, "rev-parse", "HEAD")


def _resolved_commit(root: Path, ref: str) -> "Optional[str]":
    """`ref` (a snapshot file's own `commit` field, never produced by THIS
    process) validated as a full 40-hex SHA AND confirmed to name a real
    commit in `root` -- `None` on either failure. Mirrors the two-part
    check `codegraph._core.git.resolve_ref` applies to a caller-supplied
    ref, reimplemented locally (not called) for the same reason
    `_default_subjects` reimplements `changed_since`: this module's import
    closure must never reach `codegraph` (see the module docstring and
    `test_the_hs_run_family_import_closure_never_touches_codegraph`).

    Format first, via `port_ledger_spec.is_sha40` (the repo's own full-SHA
    check, reused rather than a second regex) -- `--output=<path>` fails
    here before it ever reaches an argv position. Existence second, via
    `rev-parse --verify --end-of-options <ref>^{commit}`: a well-formed but
    NONEXISTENT commit (a hand-edited or corrupted `_index.json`) fails
    THIS check instead of `git diff` silently returning empty stdout for a
    bad ref, which the old code could not tell apart from a real,
    successful "nothing changed" diff -- the exact ambiguity `codegraph.
    _core.snapshot.freshness._changed_paths` returns `None` (not `[]`) to
    avoid. `--end-of-options` is defense in depth even after the format
    check: it means `ref` can never be re-parsed as a flag by THIS call
    either, the same guarantee `resolve_ref` gives every downstream use."""
    if not port_ledger_spec.is_sha40(ref):
        return None
    resolved = _git_text(root, "rev-parse", "--verify", "--end-of-options",
                         "%s^{commit}" % ref)
    return resolved or None


def _diff_touches(root: Path, a: str, b: str, scope: "list[str]") -> bool:
    """True when `git diff --name-only a b [-- scope]` reports at least one
    path. Empty `scope` diffs the WHOLE tree (the conservative default: a
    snapshot with no declared roots has no narrower claim to stand on).

    `a` and `b` are validated through `_resolved_commit` before either
    reaches `git diff`'s argv -- `a` in particular is a snapshot file's own
    `commit` field, not something THIS process produced. An unresolvable
    ref (malformed, or well-formed but naming no real commit) returns
    `True` ("touches", never "no change"): the caller must not read
    "the comparison could not run" as "the comparison ran and found
    nothing", the same law `codegraph._core.snapshot.freshness.
    check_freshness` holds for its own `_changed_paths`.

    git-listing-exempt: existence-only -- only stdout's EMPTINESS is read here,
    never a printed name, so a C-quoted non-ASCII match still leaves stdout
    non-empty and this correct.
    """
    if not a or not b:
        return True  # cannot prove "no change" without both ends resolving
    a_sha = _resolved_commit(root, a)
    b_sha = _resolved_commit(root, b)
    if a_sha is None or b_sha is None:
        return True  # unresolvable ref -- never read as "nothing changed"
    args = ["diff", "--name-only", "--end-of-options", a_sha, b_sha]
    if scope:
        args += ["--", *scope]
    return bool(_git_text(root, *args).strip())


def _dirty_within(root: Path, scope: "list[str]") -> "list[str]":
    """Repo-relative paths `git status` reports as touched, scoped to `scope`
    by git's OWN pathspec argument -- never a manual prefix filter on the
    result. Two measured bugs the manual filter had:

    * a root of `.` (a flat project scanned at the repo root) built the
      prefix `./`, which matches NOTHING `git status` ever prints (paths
      come back repo-relative, never `./`-prefixed) -- every dirty file in a
      flat project's roots silently produced no warning.
    * default (non-`-z`) porcelain output C-quotes a non-ASCII path in
      double quotes with octal escapes (`"t\\341\\273\\207p..."`); the old
      code's `.strip('"')` only trimmed the surrounding quotes and left the
      octal escapes as the literal string, never the real filename.

    Fixed by letting git itself scope the output (its pathspec matching
    already handles `.`/subdirectories/gitignore correctly, no
    reimplementation needed here) and reading `-z` NUL-delimited records,
    which git never quotes or escapes: `--untracked-files=all` so a new
    directory's files are listed individually rather than folded into one
    directory entry. `scope` empty or exactly `["."]` -- no roots to narrow
    by, or a root that already means "the whole tree" -- diffs the whole
    repo (no pathspec argument at all), same conservative default
    `_diff_touches` uses.

    Rename/copy records carry the path TWICE with `-z` (new path in the
    `XY path` record, then the old path as its own following NUL record);
    only the new path is a currently-dirty path, so the old-path record is
    consumed and dropped, never appended.
    """
    normalized = [] if (not scope or scope == ["."]) else scope
    args = ["status", "--porcelain", "-z", "--untracked-files=all"]
    if normalized:
        args += ["--", *normalized]
    try:
        r = subprocess.run(["git", *_GIT_SAFE, "-C", str(root), *args],
                           capture_output=True, env=_git_env())
    except OSError:
        return []
    if r.returncode != 0:
        return []
    records = r.stdout.split(b"\0")
    paths: "list[str]" = []
    i = 0
    while i < len(records):
        rec = records[i]
        if len(rec) < 4:
            i += 1
            continue
        xy = rec[:2]
        path = rec[3:].decode("utf-8", errors="replace")
        paths.append(path)
        if b"R" in xy or b"C" in xy:
            i += 1  # the ORIG path of a rename/copy -- not a currently-dirty path itself
        i += 1
    return paths


def _git_lines_z(root: Path, *args: str) -> "list[str]":
    """`-z`-terminated stdout, split and decoded -- empty (never raising) on
    any git failure, the same fail-open shape every other git call in this
    module holds to (`resolve()`'s own contract: degrade, never crash).
    Hardened the same way `_git_text`/`_dirty_within` are: `root` is a
    caller-supplied repo this process does not own."""
    try:
        r = subprocess.run(["git", *_GIT_SAFE, "-C", str(root), *args],
                           capture_output=True, env=_git_env())
    except OSError:
        return []
    if r.returncode != 0:
        return []
    return [p.decode("utf-8", errors="replace")
            for p in r.stdout.split(b"\0") if p]


def _default_subjects(root: Path) -> "Optional[list[str]]":
    """the default subject set for a verb that passes no explicit
    `fact_subjects`: files that differ from the default branch's own
    merge-base with HEAD, PLUS every uncommitted/untracked file -- mirrors
    `codegraph._core.git.changed_since`'s shape (merge-base diff +
    `ls-files --others --exclude-standard`) but reimplemented locally
    rather than calling it: `changed_since` lives under the `codegraph`
    package, and this module's own import closure must never reach it
    (this file's own header docstring: "Zero network, zero heavy import";
    enforced by `test_the_hs_run_family_import_closure_never_touches_
    codegraph`, which a `from codegraph._core.git import changed_since`
    here trips even as a lazy, function-local import — the gate walks
    function bodies too). `changed_since` also RAISES on a git failure (an
    impact-verb usage error, its own docstring's contract); this module's
    own contract is "never raise, degrade to an outcome row instead", a
    second reason a bare call would not fit even set the import aside.

    What IS shared with `changed_since`, and must be: the hardening.
    `_git_text`/`_git_lines_z` carry the SAME `secret_scan_before_ship`
    posture `run_git` does, imported from the one definition site, so this
    local duplicate does not quietly reopen the fsmonitor-hook hole
    `codegraph._core.git.run_git` closes for every OTHER git caller in the
    tree. `review_recall._main_branch` (the existing default-branch
    resolver: origin/HEAD, then local main/master) decides the base --
    reused rather than a second copy of that lookup.

    `None` and `[]` are TWO DIFFERENT CLAIMS, never folded together:
    `None` means "could not compute a default at all" (no default branch,
    or it shares no history with HEAD) -- the honest "no subject-scoped
    answer" a provider is meant to read as "consider everything". `[]`
    means the default WAS computed and nothing differs from the merge-base
    with a clean working tree -- a real, narrow, deliberately EMPTY scope.
    Collapsing `[]` into `None` would hand a provider built for "the
    smallest possible answer" (say, cook's "tests reaching the changed
    files") the LARGEST one instead, exactly when the branch is freshest."""
    import review_recall  # noqa: PLC0415 -- lazy: only the no-explicit-subjects path pays this
    branch = review_recall._main_branch(str(root))
    if branch is None:
        return None
    mb = _git_text(root, "merge-base", branch, "HEAD")
    if not mb:
        return None
    tracked = _git_lines_z(root, "diff", "--name-only", "-z", mb)
    untracked = _git_lines_z(root, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(set(tracked) | set(untracked))


# ── project + snapshot-store resolution ──────────────────────────────────────

def project_name(root: Path) -> Optional[str]:
    """The `project:` key of `<root>/codemap.yaml`, through the shared
    `yaml_io` loader — never a second YAML dependency. No descriptor at all
    reads as "no snapshot to read", not a crash: a project that has not
    adopted codegraph yet is a normal, common state.

    PUBLIC (no leading underscore) on purpose: `simplify_gate.py` (a hook)
    calls this exact function rather than keeping its own copy -- one project-
    name reader, not two that can drift. A hook never calls `store_base`
    below (that resolves the data home through `harness_paths.
    tool_data_root()`, forbidden in a guard/hook context); this function
    alone carries no such dependency, so sharing it costs the hook nothing
    it does not already pay to read `codemap.yaml` itself."""
    cm = Path(root) / "codemap.yaml"
    if not cm.is_file():
        return None
    try:
        doc = yaml_io.safe_load(cm.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — an unreadable descriptor is "no snapshot", not a crash
        return None
    if not isinstance(doc, dict):
        return None
    project = doc.get("project")
    return str(project) if project else None


class DataHomeUnresolved(Exception):
    """The snapshot store's data home could not be resolved for this caller."""


def store_base(root: Path) -> Optional[Path]:
    """Where `root`'s project keeps its snapshots (`reader.snapshot_base`), or `None` when
    `root` declares no `project:` in a `codemap.yaml`. Raises `DataHomeUnresolved` when
    there is a project but no data home to find its store in. The tool-tier locator:
    `resolve` below and `docs_symbols` both start here -- deliberately not the ONLY
    locator in the tree, though: `simplify_gate._fan_in_counts` (a hook) must not reach
    the tool-tier resolver this uses (`harness_paths.tool_data_root()`), so it computes
    `base` on the strict, fail-closed `harness_paths.data_root()` instead and never calls
    this function -- two locators, one per tier, not a drift to reconcile."""
    project = project_name(root)
    if project is None:
        return None
    data_root = harness_paths.tool_data_root()
    if harness_paths.data_root_unresolved(data_root):
        raise DataHomeUnresolved(str(data_root))
    return reader.snapshot_base(data_root, project, root)


def _finished_snapshot_dirs(base: Path) -> "list[Path]":
    """Every subdirectory of `base` matching the store's own naming contract
    (`codegraph_snapshot_reader.SNAPSHOT_DIR_NAME_RE` — the exact rule
    `codegraph._core.snapshot.store.snapshot_dirs` already applies) AND
    carrying a readable-by-stat `_index.json`, lexicographic (= oldest-to-
    newest) order. Name-matching is not cosmetic: it is what lets
    `select_snapshot` below decide "is any candidate AT head" from the
    directory listing alone, no file touched yet — a directory that
    does not honor the naming contract is not a candidate this reader will
    ever recognize as a real snapshot, the same rule the store side already
    enforces on its own `snapshot_dirs`."""
    if not base.is_dir():
        return []
    return sorted(d for d in base.iterdir()
                  if d.is_dir() and reader.SNAPSHOT_DIR_NAME_RE.fullmatch(d.name)
                  and (d / "_index.json").is_file())


def _dir_commit_prefix(name: str) -> str:
    """The 8-hex commit fragment embedded in a snapshot directory's own
    NAME (`<stamp>-<commit8>[-<content_key12+>]`) — read from the string
    alone, no file touched. Mirrors `reader.stamp_commit_prefix`'s split,
    narrowed to just the commit field (the 3rd dash segment): a selection
    filter needs "this directory's commit", not its full chronological
    stamp."""
    parts = name.split("-")
    return parts[2] if len(parts) >= 3 else ""


class _Selection:
    """One resolved snapshot pick, or the reason none was usable.

    `status`: "fresh" (fill, maybe with a warning) | "stale" (facts_stale,
    exit 11) | "missing" (no store at all, facts_stale exit 11 reason
    missing) | "broken" (every candidate's own `_index.json` failed to
    parse, exit 2). `extra_warnings`: zero or more "snapshot X is
    unreadable, skipped" notes for candidates this pick itself skipped —
    distinct from `warning`, the single note about the PICKED
    snapshot itself (a dirty tree in its roots, or a stale-but-out-of-scope
    diff). `index_is_full`: whether `.index` is already a full `_index.json`
    parse (`True`) or the cheap `_meta.json` header (`False`) — `select_
    snapshot` never promotes a cheap header to a full read itself: the
    CALLER (`resolve()`) does that lazily, only on an actual cache miss, so
    a fully-cached repeat call never reads a full index at all."""
    __slots__ = ("status", "dir", "index", "warning", "error", "extra_warnings",
                "index_is_full")

    def __init__(self, status, *, dir=None, index=None, warning=None, error=None,
                extra_warnings=(), index_is_full=True):
        self.status = status
        self.dir = dir
        self.index = index
        self.warning = warning
        self.error = error
        self.extra_warnings = list(extra_warnings)
        self.index_is_full = index_is_full


def _roots_of(index: dict) -> "list[str]":
    return [str(r.get("path")) for r in (index.get("roots") or []) if r.get("path")]


def _dirty_scope(root: Path, header: dict) -> "list[str]":
    """The snapshot's roots PLUS the descriptor — the store's own dirty scope
    (`codegraph._core.snapshot.worktree_key.dirty_scope`), mirrored, never
    imported. Roots alone read a snapshot taken with `codemap.yaml` uncommitted
    (the first one `init run --apply` takes) as a clean-tree miss: `stale`, and
    the refresh handed over rewrote the same dirty measurement on every call.
    An empty or `.` scope already covers the whole tree and stays as is."""
    roots = _roots_of(header)
    if not roots or roots == ["."]:
        return roots
    return roots + ["codemap.yaml"] if (Path(root) / "codemap.yaml").is_file() else roots


def _read_snapshot_header(d: Path) -> "tuple[dict, bool]":
    """One candidate's SELECTION-relevant fields -> `(header, is_full_index)`.

    The cheap `_meta.json` sidecar (`codegraph_snapshot_reader.read_meta`)
    when the snapshot carries one — `write_meta`'s own field set already
    covers everything selection needs: `commit`, `tree_hash`, `roots`,
    `content_key`, `snapshot`. Falls back to a full `read_index` parse for a
    snapshot written before the sidecar existed (still correct, just slow).
    Raises `reader.SnapshotIndexError` exactly when `read_index` itself
    would; a missing sidecar is never an error by itself, only a signal to
    read the slow way."""
    meta = reader.read_meta(d)
    if meta is not None:
        return meta, False
    return reader.read_index(d), True


def _broken_error(base: Path, warnings: "list[str]") -> "reader.SnapshotIndexError":
    """Every candidate under `base` turned out unreadable -- the message
    names EACH one and why (from `warnings`, already built while trying
    them), not just a count: `str(selection.error)` is what a caller's
    `next_action` shows the operator, and "1 unreadable" alone drops the
    one fact (a bad schema_version, a truncated file, ...) that tells them
    what to actually go fix."""
    detail = "; ".join(warnings) if warnings else "no candidate directories"
    return reader.SnapshotIndexError("no readable snapshot under %s: %s" % (base, detail))


def select_snapshot(root: Path, base: Path) -> _Selection:
    """The outcome table's selection rule, ported (never imported) from
    ob-tools' `check_freshness` semantics: among snapshots with a finished
    index, prefer one measured AT the current HEAD; if none exists, take
    the newest and ask whether the diff since it touches that snapshot's
    own roots.

    Cost: candidates whose NAME already claims HEAD's commit are read
    first, and only those — the common case never touches a snapshot this
    pick will not use. An unreadable candidate is skipped
    with a warning naming it, never abandons the whole pick; `broken`
    (exit 2) is reserved for the case where NO candidate anywhere under
    `base` can be read at all. No silent dirty reuse: when a candidate
    's name claims HEAD but none of them carries an EXACT (`tree_hash`-
    matching) measurement of the clean HEAD tree, this must not silently
    hand back one of those dirty-content measurements as if it answered for
    the clean tree — the current tree's own cleanliness in the snapshot's
    roots decides between `stale` (refresh needed) and `fresh` with an
    explicit warning.
    """
    all_dirs = _finished_snapshot_dirs(base)
    if not all_dirs:
        return _Selection("missing")

    head = _head(root)
    warnings: "list[str]" = []
    excluded: "set[Path]" = set()
    headers: "dict[Path, tuple]" = {}
    current_descriptor = reader.descriptor_file_sha(root)

    def _read(d: Path):
        if d in headers:
            return headers[d]
        if d in excluded:
            return None
        try:
            headers[d] = _read_snapshot_header(d)
        except reader.SnapshotIndexError as exc:
            warnings.append("snapshot %s is unreadable, skipped: %s" % (d.name, exc))
            excluded.add(d)
            return None
        return headers[d]

    def _measured_at(d: Path) -> float:
        got = _read(d)
        return float((got[0].get("measured_at") if got else 0) or 0)

    def _other_descriptor(header: dict) -> bool:
        """Measured under a `codemap.yaml` that is no longer the one on disk.
        A snapshot carrying no stamp (written before it existed) is judged as
        before, never guessed at."""
        stamp = header.get("descriptor_sha")
        return bool(stamp and current_descriptor and stamp != current_descriptor)

    # The store's own "latest" rule (`reader.chronological`), never the name
    # order alone: a content-key suffix sorts AFTER its plain sibling as a
    # string whichever was written last, and that served a snapshot of a
    # retired descriptor while `hs-code` read the new one.
    all_dirs = reader.chronological(all_dirs, _measured_at)

    for _attempt in range(len(all_dirs) + 1):
        dirs = [d for d in all_dirs if d not in excluded]
        if not dirs:
            return _Selection("broken", error=_broken_error(base, warnings),
                              extra_warnings=warnings)

        by_name = [d for d in dirs if head and _dir_commit_prefix(d.name) == head[:8]]
        at_head = []
        for d in by_name:
            got = _read(d)
            if got is None:
                continue
            header, _is_full = got
            if header.get("commit") == head:
                at_head.append((d, header, _is_full))

        picked = None  # (dir, header, is_full, status, warning)
        # one measured under the descriptor in force wins over any that was not
        at_head = [t for t in at_head if not _other_descriptor(t[1])] or at_head
        if at_head:
            head_tree = _git_text(root, "rev-parse", "HEAD^{tree}")
            exact = [t for t in at_head if t[1].get("tree_hash") == head_tree]
            if exact:
                d, header, is_full = exact[-1]
                dirty = _dirty_within(root, _dirty_scope(root, header))
                warning = None
                if dirty:
                    warning = ("snapshot %s (commit %s) does not see %d uncommitted change(s) "
                              "in its roots or descriptor: %s"
                              % (d.name, header.get("commit"), len(dirty),
                                 ", ".join(sorted(dirty)[:5])))
                picked = (d, header, is_full, "fresh", warning)
            else:
                # at least one candidate's name (and commit field) claims
                # HEAD, but none of them measured the CURRENT clean tree --
                # every one here is itself a dirty (content-keyed) measurement
                # of some OTHER working-tree state at this same commit.
                d, header, is_full = at_head[-1]
                dirty = _dirty_within(root, _dirty_scope(root, header))
                if not dirty:
                    picked = (d, header, is_full, "stale", None)
                else:
                    warning = ("snapshot %s (commit %s) measured an uncommitted state that "
                              "may differ from the current one" % (d.name, header.get("commit")))
                    picked = (d, header, is_full, "fresh", warning)
        else:
            for d in reversed([x for x in dirs if x not in by_name]):
                got = _read(d)
                if got is None:
                    continue
                header, is_full = got
                scope = _roots_of(header)
                if _diff_touches(root, str(header.get("commit") or ""), head, scope):
                    picked = (d, header, is_full, "stale", None)
                else:
                    warning = ("snapshot %s measures commit %s; HEAD is now %s, but the diff "
                              "between them does not touch this snapshot's own roots"
                              % (d.name, header.get("commit"), head))
                    picked = (d, header, is_full, "fresh", warning)
                break

        if picked is None:
            continue  # every candidate seen this attempt was unreadable — retry, narrower

        d, header, is_full, status, warning = picked
        if _other_descriptor(header):
            status, warning = "stale", None
        return _Selection(status, dir=d, index=header, warning=warning,
                          extra_warnings=warnings, index_is_full=is_full)

    return _Selection("broken", error=_broken_error(base, warnings), extra_warnings=warnings)


# ── provider dispatch + cap enforcement ──────────────────────────────────────

def _resolve_provider(spec: dict):
    module_name = str(spec["module"])
    entry_name = str(spec.get("entry") or "entry")
    mod = importlib.import_module(module_name)
    fn = getattr(mod, entry_name, None)
    if fn is None:
        raise AttributeError("%s has no attribute %r" % (module_name, entry_name))
    return fn


def _apply_cap(raw: dict, cap: Optional[int]) -> dict:
    """Trim a provider's own `{value, total, truncated, basis, ...}` to
    `cap` elements — defense in depth: a provider is expected to respect
    its own cap, this is the floor every fact result shares regardless.
    `total`/`basis` are never invented: a provider that omits them gets
    `len(value)`/`None`, never a silently fabricated number.

    `value` a LIST (`code.tests_reached`): trimmed to the first `cap`
    entries, the provider's own order preserved (already sorted by the
    provider). `value` a DICT (`code.importers`'s `{subject: [importers]}`
    shape): capped by KEY COUNT, not by summing every inner list — a dict
    keeps `cap` SUBJECTS in full rather than truncating every subject's own
    importer list a little, since a subject with its importer list cut off
    partway would read as "these are ALL the importers", the exact silent-0
    shape this whole contract exists to avoid. Kept deterministic (sorted
    keys) so which `cap` subjects survive a trim never depends on provider
    iteration order.

    `not_in_snapshot` (a provider's own "subject never measured" list —
    `code.tests_reached`/`code.importers` both carry one) passes through
    UNCHANGED when the provider supplied one, and is left ABSENT (never an
    invented empty list) when it did not — the same "never fabricate a key"
    law `total`/`basis` already follow.

    `unresolved` (a provider's candidates found by name but not resolved — a
    method's uses through an instance of unknown type) passes through the same
    way: never folded into `value`, never invented when absent.

    `warning` (a per-fact blind-spot disclosure, e.g. "this only walks the
    import graph") passes through the same way, and — unlike `basis`, which
    only ever lives inside this one fact's own record — is ALSO what
    `resolve()`'s caller folds into the envelope's top-level
    `extra_warnings`, so a reader who checks only `warnings` still sees it
    without opening each fact's own record. Carried in the CACHED dict too
    (not just the fresh-call path): a cache HIT never re-invokes the
    provider, so the disclosure would silently stop appearing on the
    second and every later call if it were not part of what gets cached."""
    value = raw.get("value")
    total = raw.get("total")
    truncated = bool(raw.get("truncated"))
    if isinstance(value, list):
        if total is None:
            total = len(value)
        if cap is not None and len(value) > cap:
            value = value[:cap]
            truncated = True
    elif isinstance(value, dict):
        if total is None:
            total = len(value)
        if cap is not None and len(value) > cap:
            keys = sorted(value)[:cap]
            value = {k: value[k] for k in keys}
            truncated = True
    out = {"value": value, "total": total, "truncated": truncated,
           "basis": raw.get("basis")}
    not_in_snapshot = raw.get("not_in_snapshot")
    if not_in_snapshot:
        out["not_in_snapshot"] = list(not_in_snapshot)
    unresolved = raw.get("unresolved")
    if isinstance(unresolved, dict) and isinstance(value, dict):
        unresolved = {k: v for k, v in unresolved.items() if k in value}
    if unresolved:
        out["unresolved"] = unresolved
    warning = raw.get("warning")
    if warning:
        out["warning"] = str(warning)
    return out


#: `resolve()`'s aggregate ceiling on the COMBINED serialized `facts` dict, in
#: UTF-8 bytes — `_apply_cap` above bounds each fact's own COUNT (list length
#: / dict key count), never its total byte size, so a fact whose entries are
#: individually large can still blow the envelope while obeying its own cap.
#:
#: Basis (measured at 9393b459b on this repo's own codegraph snapshot, 1600 modules —
#: see the phase report for the full numbers): a realistic call (34 files
#: changed across 5 real commits, both `code.tests_reached`/`code.importers`
#: filled together) serializes to ~25.6 KB. The worst combined case
#: constructible under the EXISTING per-fact caps (`review.ready`'s 50-
#: subject `code.importers` cap, subjects chosen as the 50 highest-fan-in
#: files in the repo, `code.tests_reached` run over the same 50) serializes
#: to ~110 KB. 64 KiB sits BELOW that measured adversarial case — so this
#: ceiling is provably load-bearing on a real, constructible input, not a
#: number that would never fire — and comfortably above the realistic case
#: (~2.5x headroom).
_FACTS_BYTES_CEILING = 65536


def _apply_aggregate_ceiling(facts: dict, ceiling: int = _FACTS_BYTES_CEILING) -> Optional[str]:
    """Trims per-fact `value`s IN PLACE, largest-serialized-fact-first, until
    `json.dumps(facts)` fits under `ceiling` UTF-8 bytes. Returns a warning
    naming what was trimmed, or `None` when nothing needed trimming (the
    common case: `resolve()`'s own docstring contract is "degrade, never
    raise", and a warning that fires on every call would train a reader to
    ignore it).

    Runs AFTER `_apply_cap` and AFTER the per-fact cache read/write loop —
    on purpose, and never written back into the cache: this is a property
    of what facts THIS CALL declared TOGETHER (a state that later declares
    a different sibling set would need a different trim), not any one
    fact's own per-provider cap, which the cache already keys on.

    Only a `value` that is a list or dict is shrinkable — a fact whose
    value is a marker dict (`stale`/`unavailable`) has nothing in it to
    trim and is left untouched. Never drops a fact's shrinkable value
    below 1 entry: doing so would fold "measured, huge" into "measured,
    nothing", the exact silent-0 shape `not_in_snapshot` exists to keep
    distinct from an honest empty value elsewhere in this contract.

    The floor can still lose: a single fact whose value is ALREADY 1
    entry (nothing to shrink at all — e.g. `not_in_snapshot` narrowed a
    dict to one huge subject) or the combined total of every fact driven
    down to 1 entry each can both still exceed `ceiling` on their own.
    The loop's own exit condition (`shrinkable` runs out) always
    terminates it either way, so this is never a hang — but the RETURNED
    warning must say the ceiling could not be met and name the actual
    final size, never the "trimmed ... to fit" phrasing that implies
    success when the payload is still over budget."""
    import json

    def _size() -> int:
        return len(json.dumps(facts, sort_keys=True).encode("utf-8"))

    if _size() <= ceiling:
        return None

    trimmed_ids: "list[str]" = []
    # `unresolved` (name-matched candidates, never part of `value`) is trimmed first:
    # halve every subject's file list in the largest such fact, each list kept to 1.
    loose = {fid for fid, f in facts.items()
             if any(len(v) > 1 for v in ((f or {}).get("unresolved") or {}).values())}
    while _size() > ceiling and loose:
        fid = max(loose, key=lambda k: len(json.dumps(facts[k]["unresolved"]).encode("utf-8")))
        un = facts[fid]["unresolved"]
        facts[fid]["unresolved"] = {k: v[:max(1, len(v) // 2)] for k, v in un.items()}
        facts[fid]["unresolved_truncated"] = True
        if fid not in trimmed_ids:
            trimmed_ids.append(fid)
        if not any(len(v) > 1 for v in facts[fid]["unresolved"].values()):
            loose.discard(fid)

    shrinkable = {fid for fid, f in facts.items()
                 if isinstance((f or {}).get("value"), (list, dict))
                 and len(f["value"]) > 1}
    guard = 0
    while _size() > ceiling and shrinkable and guard < 128:
        guard += 1
        fid = max(shrinkable,
                  key=lambda k: len(json.dumps(facts[k]).encode("utf-8")))
        value = facts[fid]["value"]
        half = max(1, len(value) // 2)
        if isinstance(value, list):
            facts[fid]["value"] = value[:half]
        else:
            keys = sorted(value)[:half]
            facts[fid]["value"] = {k: value[k] for k in keys}
            un = facts[fid].get("unresolved")
            if un:
                facts[fid]["unresolved"] = {k: v for k, v in un.items() if k in keys}
        facts[fid]["truncated"] = True
        if fid not in trimmed_ids:
            trimmed_ids.append(fid)
        if half <= 1:
            shrinkable.discard(fid)

    final_size = _size()
    if final_size > ceiling:
        return ("facts payload could not fit under %d bytes -- final size "
               "%d bytes after trimming %s"
               % (ceiling, final_size, ", ".join(trimmed_ids) or "nothing shrinkable"))
    if not trimmed_ids:
        return None
    return ("facts payload exceeded %d bytes -- trimmed %s to fit"
           % (ceiling, ", ".join(trimmed_ids)))


# ── cache: one JSON per (snapshot identity, fact id) ─────────────────────────

def _subjects_digest(subjects) -> str:
    """A stable digest of `subjects` for the cache key -- sorted
    before hashing so caller-side ORDER never matters, only membership.

    `None` ("no subject scope at all" -- see `_default_subjects`'s
    docstring) reads as "the whole snapshot" and gets the empty string (no
    suffix at all), so a fact declared with no `subjects` concept keeps
    its earlier cache key unchanged. A computed-but-EMPTY list is a
    DIFFERENT, real scope ("nothing changed" is not "no scope was
    computed") and gets its OWN digest -- `subjects is None` is the exact
    check, never a bare truthiness test, which would fold `[]` into
    `None` right back into the bug this exists to fix."""
    if subjects is None:
        return ""
    normalized = "\n".join(sorted(str(s) for s in subjects))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _cache_key(index: dict, base: Path, snapshot_dir: Path, subjects=None) -> str:
    """Unique ACROSS worktrees: `content_key` when the index carries one
    (two worktrees can hold same-NAMED snapshot dirs with different
    content); falls back to `<snapshot base>/<snapshot dir name>` — the
    base already carries the worktree key segment `codegraph_snapshot_
    reader.snapshot_base` builds, so the fallback is still worktree-unique
    even without a content key. `subjects` (the resolved fact-scope
    file list -- an explicit override, or the default) is folded in too
    -- the SAME snapshot answering for two different subject sets (two
    calls with different `--pending`/change scopes) must not collide on
    one cached value."""
    ck = index.get("content_key")
    base_key = str(ck) if ck else hashlib.sha256(
        str(base / snapshot_dir.name).encode("utf-8")).hexdigest()[:16]
    sdig = _subjects_digest(subjects)
    return "%s-%s" % (base_key, sdig) if sdig else base_key


def _cache_dir(key: str) -> Path:
    return harness_paths.tool_state_dir() / "code-facts" / key


def _provider_fingerprint(spec: dict) -> str:
    """A digest of the provider module's own source. The cache key names the
    snapshot and the subjects, not the code that answered: without this, a
    harness upgrade that rewrites a provider's `basis`/`warning` kept serving
    the old sentence from cache until the snapshot changed. An unreadable source
    yields a fixed token — a miss for any entry written under a real digest."""
    try:
        src = importlib.import_module(str(spec["module"])).__file__
        if src is None:
            return "nosource"
        return hashlib.sha256(Path(src).read_bytes()).hexdigest()[:12]
    except Exception:  # noqa: BLE001 — a fingerprint failure is a cache miss, never a crash
        return "nosource"


def _cache_file(key: str, fact_id: str, fingerprint: str) -> Path:
    return _cache_dir(key) / ("%s-%s.json" % (fact_id, fingerprint))


def _cache_read(key: str, fact_id: str, fingerprint: str):
    p = _cache_file(key, fact_id, fingerprint)
    if not p.is_file():
        return None
    try:
        import json
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cache_write(key: str, fact_id: str, fingerprint: str, value: dict) -> None:
    import json
    d = _cache_dir(key)
    try:
        d.mkdir(parents=True, exist_ok=True)
        _cache_file(key, fact_id, fingerprint).write_text(json.dumps(value), encoding="utf-8")
    except OSError:
        return  # a cache write failure degrades to "compute again next time", never a crash
    _prune_cache(d.parent)


def _prune_cache(facts_root: Path) -> None:
    """Keep the `_CACHE_KEEP` most-recently-written snapshot-id directories
    under `code-facts/`, oldest dropped first — bounded disk, same shape
    `codegraph`'s own `prune_snapshots` already uses."""
    try:
        dirs = sorted((d for d in facts_root.iterdir() if d.is_dir()),
                     key=lambda d: d.stat().st_mtime)
    except OSError:
        return
    for d in dirs[:-_CACHE_KEEP]:
        try:
            for child in d.iterdir():
                child.unlink()
            d.rmdir()
        except OSError:
            continue


# ── the outcome table ────────────────────────────────────────────────────────

#: The functions here that write a new `state` over a domain's answer. Read by
#: `verb_result_shapes.facts_overlay_states`, so the gates and the generated state
#: maps know these states are reachable even though no domain module returns them.
OVERLAY_OUTCOMES = ("_stale_outcome", "_offer_outcome")


def _stale_outcome(*, state: str, reason: str, refresh_command: Optional[str],
                   declared: "list[str]", note: str) -> dict:
    if refresh_command:
        return {
            "state": "facts_stale",
            "domain_state": state,
            "exit_code": 11,
            "next_command": refresh_command,
            "next_action": ("code fact(s) %s need a fresh snapshot (%s) — run "
                            "`%s` then retry" % (", ".join(declared), note, refresh_command)),
            "facts": {f: {"stale": True, "reason": reason} for f in declared},
        }
    return {
        "state": "facts_stale",
        "domain_state": state,
        "exit_code": 10,
        "next_command": None,
        "next_action": ("code fact(s) %s need a fresh snapshot (%s), and no "
                        "refresh_command is declared for their source yet" %
                        (", ".join(declared), note)),
        "facts": {f: {"stale": True, "reason": reason} for f in declared},
    }


def _not_adopted_outcome(*, declared: "list[str]", root: Path, state: str = "") -> dict:
    """A project with NO `codemap.yaml` at all has not ADOPTED code-graph
    snapshots — a normal, common state (`project_name`'s own docstring),
    never a staleness problem. UNLIKE `_stale_outcome`, this never
    rewrites `state`/`exit_code`/`next_command`/`next_action` — the caller
    (`_apply_facts`) merges this outcome's keys onto the verb's own result,
    so an outcome with none of those keys leaves the verb's own answer
    exactly as it was.

    MEASURED bug this fixes: the old code routed a not-adopted project
    through `_stale_outcome`, exit 11, `next_command = "hs-code --repo
    <root> --apply graph snapshot"`. That command itself REQUIRES a
    `codemap.yaml` to run at all — on the exact repo this outcome was
    built for, it exits 2, "No descriptor at <root>/codemap.yaml". Exit 11
    is a PROMISE that the named step can complete; handing over a command
    that structurally cannot succeed on the tree it was built for breaks
    that promise, and it does so for EVERY caller of a domain that
    declares `facts:`, not a corner case — adopting code-graph snapshots
    is a decision the verb this fact rides on does not own.

    Each declared fact reports `unavailable: true` with a `reason` naming
    the missing `codemap.yaml`; ONE warning names the same thing for a
    reader who checks only the envelope's top-level `warnings`, matching
    the same two-places-at-once law the per-fact `warning` key already
    follows for the blind-spot disclosure."""
    reason = "no codemap.yaml at %s" % root
    facts = {f: {"unavailable": True, "reason": reason} for f in declared}
    adoption = codemap_adoption.status(root)
    if adoption.state == "unsupported":
        remedy = "the code map cannot read this repo yet: %s" % adoption.reason
    else:
        remedy = codemap_adoption.REMEDY
    warning = "code fact(s) %s unavailable: %s — %s" % (", ".join(declared), reason, remedy)
    if adoption.state == "offer":
        return _offer_outcome(state=state, root=root, facts=facts, warning=warning)
    return {"facts": facts, "extra_warnings": [warning]}


def _offer_outcome(*, state: str, root: Path, facts: dict, warning: str) -> dict:
    """Someone is at the terminal, the repo has no map and a language it can
    read, and nobody has answered for this session: ask, once. The caller
    (`hs_run._apply_facts`) keeps the verb's own answer in `deferred` and drops
    this rewrite for a verb that exited 2 or 3; both answers name a command that
    really runs, and either one ends the offer for the session."""
    repo = shlex.quote(str(root))
    return {
        "state": "codemap_offer",
        "domain_state": state,
        "exit_code": 10,
        "next_command": None,
        "next_action": (
            "This step reads code facts and the repo has no code map yet. Ask the user "
            "(ask_user). Yes -> `hs-code --repo %s init run --apply`; no -> `hs-code --repo "
            "%s init decline`. Then run this step again; its own answer is in `deferred`."
            % (repo, repo)),
        "facts": facts,
        "extra_warnings": [warning],
    }


def _error_outcome(message: str) -> dict:
    return {"state": "error", "exit_code": 2, "next_command": None,
           "next_action": message}


#: A provider declares `subjects: explicit` in its `fact_providers` row when
#: it has NO default subject scope to fall back on -- `code.callers`'s
#: symbol-qualname list is meaningful only when a caller (today, a model
#: running `plan next --fact-subjects`) names it; `_default_subjects`'s
#: file-diff heuristic answers a different question ("what changed") that
#: does not translate into "which functions do you mean".
_EXPLICIT_SUBJECTS_MARKER = "explicit"

_NO_SUBJECTS_REASON = ("no subjects named -- re-run `hs-run %s next` with "
                       "--fact-subjects module::name[,...] (that verb takes the flag)")


def path_subjects(snapshot: dict, subjects) -> "tuple[list, dict]":
    """(subjects as a path-scoped fact reads them, {named: path} for what moved).
    A `module::symbol` or a bare dotted module the snapshot measures becomes the
    file defining that module; a path, or a name the snapshot does not know, passes
    unchanged. Order kept, duplicates dropped — two symbols of one module are one
    file. A symbol-scoped fact (`subjects: explicit`) gets the names as given."""
    records = [m for mods in (snapshot.get("units") or {}).values() for m in mods]
    records += list(snapshot.get("test_modules") or ())
    by_module = {m["module"]: m["path"] for m in records
                 if isinstance(m, dict) and m.get("module") and m.get("path")}
    out: list = []
    mapped: dict = {}
    for s in subjects:
        path = by_module.get(str(s).split("::", 1)[0])
        if path is not None and (path != s):
            mapped[s] = path
        else:
            path = s
        if path not in out:
            out.append(path)
    return out, mapped


def _requires_explicit_subjects(spec: dict) -> bool:
    return str(spec.get("subjects") or "") == _EXPLICIT_SUBJECTS_MARKER


def _no_subjects_outcome(*, declared: "list[str]", domain: str, expected=None) -> dict:
    """Mirrors `_not_adopted_outcome`'s own contract exactly: the verb's own
    `state`/`exit_code`/`next_command`/`next_action` survive UNTOUCHED.
    Never routed through `_stale_outcome` (which promises a refresh command
    can cure the problem) -- no amount of re-running `hs-code graph
    snapshot` produces subjects nobody named; the only cure is
    `--fact-subjects` on the NEXT call, which is a caller decision, not a
    disk state `hs-run` can resolve on its own.

    `expected` is what the verb found to name, when it looked:
      None      — the verb did not look; the warning stands (a caller may have
                  had something to name).
      []        — it looked and its own artifacts name no symbol: there was
                  nothing to pass, so no warning. The per-fact record keeps the
                  reason, so the empty fact still explains itself.
      [names]   — the artifacts name symbols nobody passed: the warning stays
                  and carries the exact `--fact-subjects` value."""
    reason = _NO_SUBJECTS_REASON % domain
    facts = {f: {"unavailable": True, "reason": reason} for f in declared}
    if expected is not None and not expected:
        return {"facts": facts}
    warning = "code fact(s) %s unavailable: %s" % (", ".join(declared), reason)
    if expected:
        warning += " -- the plan names %d: --fact-subjects %s" % (
            len(expected), ",".join(expected))
    return {"facts": facts, "extra_warnings": [warning]}


def _with_skip_warnings(out: dict, selection: "_Selection") -> dict:
    """Fold `selection.extra_warnings` (the skipped-broken-candidate notes)
    into `out["extra_warnings"]` — every outcome branch shares this, not
    just the fresh-fill path, since a snapshot can be skipped as unreadable
    on the way to a `stale`/`missing` verdict too."""
    if selection.extra_warnings:
        existing = list(out.get("extra_warnings") or [])
        existing.extend(selection.extra_warnings)
        out["extra_warnings"] = existing
    return out


def declared_branch(frontmatter) -> "Optional[str]":
    """The branch a plan's frontmatter names, for `resolve(expected_branch=)` —
    None when absent, not a string, or still the scaffold's `TBD` placeholder."""
    value = frontmatter.get("branch") if isinstance(frontmatter, dict) else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    return None if not value or value.upper() == "TBD" else value


def _branch_mismatch(root: Path, expected: "Optional[str]") -> "Optional[str]":
    """A warning when the checkout does not hold the declared branch's tip.

    Facts measure the CHECKOUT. A work branch cut from the declared one holds its
    every commit, so what was measured includes the declared work: no warning, by
    ancestry, never by name. Nothing is re-measured, and the warning names no
    checkout to switch to: picking a tree is the caller's call, and checking out
    an integration branch to measure it invites committing on it."""
    if not expected:
        return None
    head = _head(root)
    current = _git_text(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    where = ("branch %s @ %s" % (current, head[:9]) if current
             else "a detached HEAD @ %s" % (head[:9] or "an unreadable HEAD"))
    tip = _git_text(root, "rev-parse", "--verify", "--quiet", "--end-of-options",
                    expected + "^{commit}")
    if not tip:
        return ("code facts measured the checkout (%s); the declared branch %s does not "
                "resolve here, so they cannot be said to cover it" % (where, expected))
    if head and _is_ancestor(root, tip, head):
        return None
    return ("code facts measured the checkout (%s), which does not hold the declared "
            "branch %s @ %s — they describe this tree, not that branch's work"
            % (where, expected, tip[:9]))


def _is_ancestor(root: Path, older: str, newer: str) -> bool:
    try:
        r = subprocess.run(["git", *_GIT_SAFE, "-C", str(root), "merge-base",
                            "--is-ancestor", older, newer],
                           capture_output=True, env=_git_env())
    except OSError:
        return False
    return r.returncode == 0


def _stamp_provenance(fact: dict, selection: "_Selection") -> None:
    """Snapshot id + the commit it measured, on every filled fact — fresh or a
    cache hit (an entry cached before this key existed still gets it)."""
    fact.setdefault("snapshot", selection.index.get("snapshot") or selection.dir.name)
    fact["snapshot_commit"] = selection.index.get("commit")


def resolve(*, root: "Union[str, Path]", declared: "list[str]", subjects,
           providers: dict, sources: dict, domain: str, state: str,
           expected_subjects=None, expected_branch=None) -> dict:
    """The whole `facts:` contract for one state, as a dict of override
    keys. Never raises: every failure is one of the outcome table's own
    rows, because hs-run's stdout is always valid JSON regardless of what a
    provider or the store underneath it does.

    `expected_branch`: the branch the caller's work lands on (a plan's
    frontmatter `branch:`), or None. A fill on any other checkout carries a
    `_branch_mismatch` warning."""
    root = Path(root)

    missing_providers = sorted(f for f in declared if f not in providers)
    if missing_providers:
        return _error_outcome(
            "fact(s) declared with no fact_providers entry: %s" % ", ".join(missing_providers))

    # `subjects: explicit` facts (`_requires_explicit_subjects`) have no
    # default scope to compute, so with nothing named (`subjects is None`,
    # never a truthiness check -- `_default_subjects`'s None-vs-[] law) each
    # one is `unavailable` and never reaches a provider: the file-diff
    # default answers "what changed", not "which functions do you mean".
    # Decided per fact -- a state may declare one beside default-scope facts,
    # which resolve as usual. Checked BEFORE adoption/snapshot resolution, so
    # a state holding only such facts never pays a freshness check (or a
    # snapshot-store lookup at all) for a fact nobody asked anything of.
    unnamed = ([f for f in declared if _requires_explicit_subjects(providers[f])]
               if subjects is None else [])
    if unnamed:
        named = _no_subjects_outcome(declared=unnamed, domain=domain,
                                     expected=expected_subjects)
        rest = [f for f in declared if f not in unnamed]
        if not rest:
            return named
        merged = resolve(root=root, declared=rest, subjects=None, providers=providers,
                         sources=sources, domain=domain, state=state,
                         expected_branch=expected_branch)
        merged["facts"] = {**(merged.get("facts") or {}), **named["facts"]}
        merged["extra_warnings"] = (list(merged.get("extra_warnings") or [])
                                    + list(named.get("extra_warnings") or []))
        return merged

    # Adoption is checked FIRST, before any `refresh_command` is even built:
    # a not-adopted project (no `codemap.yaml` at all) degrades to
    # `_not_adopted_outcome` — never `_stale_outcome` — because the command
    # `_stale_outcome` would hand over cannot succeed on a tree with no
    # descriptor to begin with (see that function's own docstring for the
    # measured rc=2 this branch used to promise away). An ADOPTED project
    # (codemap.yaml present) whose snapshot is merely missing or stale still
    # reaches `_stale_outcome` below, exactly as before — there, the SAME
    # command genuinely can succeed.
    project = project_name(root)
    if project is None:
        return _not_adopted_outcome(declared=declared, root=root, state=state)

    refresh_command = None
    used_sources = {providers[f].get("source") for f in declared}
    for src in used_sources:
        spec = sources.get(src) or {}
        rc = spec.get("refresh_command")
        if rc:
            refresh_command = _build_refresh_command(str(rc), root)
    try:
        base = store_base(root)
    except DataHomeUnresolved:
        return _error_outcome(
            "cannot resolve the snapshot store's data home for fact(s) %s" % ", ".join(declared))
    if base is None:
        return _not_adopted_outcome(declared=declared, root=root, state=state)

    selection = select_snapshot(root, base)

    if selection.status == "broken":
        return _error_outcome(str(selection.error))
    if selection.status == "missing":
        return _with_skip_warnings(
            _stale_outcome(state=state, reason="missing", refresh_command=refresh_command,
                           declared=declared, note="no snapshot under %s" % base),
            selection)
    if selection.status == "stale":
        return _with_skip_warnings(
            _stale_outcome(
                state=state, reason="stale", refresh_command=refresh_command, declared=declared,
                note="snapshot %s measures %s, HEAD is now %s, and the diff touches its roots"
                    % (selection.dir.name, selection.index.get("commit"), _head(root))),
            selection)

    # an explicit `subjects` from the verb always wins; `None` is
    # the "no override" signal that means "compute the default" -- an EMPTY
    # list is a caller's own explicit "no subjects" and must not be
    # overwritten. Resolved HERE, not earlier: every early-return path above
    # (missing provider, no codemap, no/broken/stale snapshot) never reaches
    # a provider call, so it never needs this -- no git cost paid for an
    # outcome that was never going to use it.
    if subjects is None:
        subjects = _default_subjects(root)

    key = _cache_key(selection.index, base, selection.dir, subjects)
    facts = {}
    full_index: "list[dict]" = []  # lazy 1-slot cache: promoted at most once per call

    def _full():
        if not full_index:
            if selection.index_is_full:
                full_index.append(selection.index)
            else:
                full_index.append(reader.read_index(selection.dir))
        return full_index[0]

    for fact_id in declared:
        fingerprint = _provider_fingerprint(providers[fact_id])
        cached = _cache_read(key, fact_id, fingerprint)
        if cached is not None:
            _stamp_provenance(cached, selection)
            facts[fact_id] = cached
            continue
        try:
            snap = _full()
        except reader.SnapshotIndexError as exc:
            return _error_outcome(
                "snapshot %s became unreadable while filling fact(s): %s"
                % (selection.dir.name, exc))
        spec = providers[fact_id]
        given = subjects
        mapped: dict = {}
        if subjects and not _requires_explicit_subjects(spec):
            given, mapped = path_subjects(snap, subjects)
        try:
            fn = _resolve_provider(spec)
            raw = fn({"root": root, "domain": domain, "state": state,
                      "subjects": given, "snapshot": snap})
        except Exception as exc:  # noqa: BLE001 — a provider crash degrades, never crashes hs-run
            return _error_outcome("%s provider crashed: %s: %s" % (fact_id, type(exc).__name__, exc))
        filled = _apply_cap(dict(raw or {}), spec.get("cap"))
        if mapped:
            filled["subjects_as_paths"] = mapped
        filled["snapshot"] = snap.get("snapshot") or selection.dir.name
        _stamp_provenance(filled, selection)
        facts[fact_id] = filled
        _cache_write(key, fact_id, fingerprint, filled)

    # Per-fact `warning`s (the blind-spot disclosure `code.tests_reached`/
    # `code.importers` carry on every fill) fold into the SAME
    # `extra_warnings` key `_with_skip_warnings` already uses for
    # selection-level notes — one list `hs_run._apply_facts` merges wholesale
    # into the envelope's top-level `warnings`. Read from `facts` itself
    # (built above from BOTH the cache-hit and fresh-call branches), so a
    # cache hit still surfaces the disclosure — it never re-invokes the
    # provider, so `raw` does not exist on that path. Order-stable
    # (declaration order, not set iteration) and de-duplicated: two facts on
    # one state sharing the identical sentence must not print it twice.
    provider_warnings: "list[str]" = []
    for fact_id in declared:
        w = facts.get(fact_id, {}).get("warning")
        if w and w not in provider_warnings:
            provider_warnings.append(w)

    # The aggregate byte ceiling, AFTER every per-fact cap has already run
    # and AFTER the cache loop — never persisted to cache (see the
    # function's own docstring: this is a property of what THIS CALL
    # declared together, not any one fact's own per-provider cap).
    aggregate_warning = _apply_aggregate_ceiling(facts)

    out: dict = {"facts": facts}
    if selection.warning:
        out["extra_warnings"] = [selection.warning]
    branch_warning = _branch_mismatch(root, expected_branch)
    if branch_warning:
        out["extra_warnings"] = list(out.get("extra_warnings") or []) + [branch_warning]
    if provider_warnings:
        existing = list(out.get("extra_warnings") or [])
        existing.extend(w for w in provider_warnings if w not in existing)
        out["extra_warnings"] = existing
    if aggregate_warning:
        existing = list(out.get("extra_warnings") or [])
        existing.append(aggregate_warning)
        out["extra_warnings"] = existing
    return _with_skip_warnings(out, selection)
