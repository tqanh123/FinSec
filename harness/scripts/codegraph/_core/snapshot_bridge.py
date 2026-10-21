"""snapshot_bridge — snapshot-shaped module records for test files a
`codegraph.graph snapshot` pass never scanned.

WHY THIS EXISTS
    `testmap.build_test_map` and `change.selection.select_tests` both read
    ONLY the modules a snapshot pass measured (`idx["units"]`) — a test tree
    living outside every declared root (the common `packages/<p>/src/...` +
    `packages/<p>/tests_*` shape, tests kept beside, not inside, `src/`) is
    invisible to both: `graph tests` reports 0 owned/0 shared for it, and
    `select_tests` never selects it for a change reachable only through it.
    Measured at e600dc2f0 on a real monorepo archive of that exact layout: 0 of 970 test
    files present in the snapshot, `graph tests packages/vsf-obs-sdk` -> 0
    owned/0 shared, `select_tests` for a change to `vsf.obs.sdk.sync` -> 0
    tests.

    This module supplies snapshot-shaped records for exactly those files —
    `bridge_records(idx, root)` returns a list a caller merges alongside the
    snapshot's own `idx["units"]` entries. Both `testmap.build_test_map` and
    `selection.select_tests` call it and process the result through the
    SAME code that already handles a real snapshot record — a bridged
    record's own shape (`imports_module_level`/`imports_function_local`/
    `symbols`) is built with `codegraph._core.snapshot.pipeline.
    build_symbol_entries` (the pipeline's own symbols-building loop,
    factored out rather than re-implemented) and `pipeline.edge_targets`
    (the pipeline's own import-classification, reused with a NARROWER
    `ns_roots`/`all_mods` — see INTERNAL-VS-EXTERNAL below).

INTERNAL-VS-EXTERNAL, AGAINST THE SNAPSHOT'S OWN MODULE SET
    The pipeline's own full resolution needs a `discover()` pass over EVERY
    source in the tree (namespace roots, flat-root bare-name rewriting,
    spelling aliases) — machinery a bridged file, by definition outside
    every declared root, was never part of. `edge_targets` still does the
    real classification work here, called with `ns_roots` inferred as the
    TOP-LEVEL segment of every module the snapshot already knows (`{m.
    split(".", 1)[0] for m in all_mods}`) and no flat-root/alias tables — an
    import target whose top segment names no known namespace reads as
    external (`continue`s out of `edge_targets`, same as today); one that
    does gets the pipeline's own longest-prefix match against `all_mods`.
    Weaker than the pipeline's own resolution (no flat-root bare-name
    rewriting, no spelling alias) — the same trade a bridged file's caller
    already accepts by construction: this is the file the real pass never
    measured.

CACHE
    Content-keyed (`sha256(file bytes) + a digest of this module's own code
    and the adapter's)` — a byte-identical file re-parses to the byte-
    identical record without re-running the adapter, and a change to either
    this module or the adapter invalidates every entry at once (the same law
    `codegraph_snapshot_reader.compute_tool_stamp` applies to the whole
    tool). Lives under `harness_paths.tool_data_root()/cache/
    codegraph-test-bridge/`; `harness_paths` unimportable, or its own data
    root unresolved (`data_root_unresolved`), degrades to NO cache — every
    call reparses, never an error. An unreadable cache entry (corrupt JSON,
    a half-written file from a killed process) is treated the same as a
    cache miss: recomputed, with one `note:` line on stderr naming the
    file, atomically rewritten. Writes go through `artifact_io.
    atomic_write_text` (`.tmp` + fsync + `os.replace`, same directory) so a
    killed process never leaves a torn cache file; a write failure is
    reported with one `note:` line on stderr, never swallowed silently.
    The cache directory is capped at `_CACHE_BOUND` entries, oldest by
    mtime pruned after a batch that grew it past the cap (`bounded_cache_
    dir.prune`, the SAME helper `hs_family.tree_scanning_tests` prunes its
    own cache with) — bounded, not unbounded growth across every repo this
    ever runs against.

WHAT IT CANNOT SEE
    * A file the running interpreter cannot parse (`SyntaxError`) is
      skipped, named in a `note:` line on stderr — never silently dropped,
      never a crash.
    * A non-git `root` yields nothing (`git ls-files` is the only
      file-finder here) and says so via the same `note:` channel.
    * `codegraph._core.testfiles.FilesLayout.is_test_file`'s own blind spot (a
      test-tree ancestor directory required — a flat repo with no `tests/`-
      shaped directory anywhere contributes nothing here) — the SAME rule
      `testmap`/`selection` already carry, not a second one.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import secret_scan_before_ship as secret_scan
from codegraph._core.adapter import adapter_for
from codegraph._core.adapter import python_adapter
from codegraph._core.snapshot.pipeline import build_symbol_entries, edge_targets
from codegraph._core.testfiles import FilesLayout, build_test_layout

#: `harness_paths`/`bounded_cache_dir`/`artifact_io` are imported LAZILY,
#: function-local, everywhere below — never at module level. This module
#: runs standalone outside the harness too (the module docstring's own
#: CACHE section); a top-level import of a harness-only flat module would
#: turn merely IMPORTING this file, standalone, into an ImportError before
#: any cache function even runs.

#: How many cache entries `_prune_cache` keeps -- generous enough that a
#: batch over one real monorepo's whole test tree (970 files, measured at e600dc2f0)
#: never starts evicting mid-run, small enough that the cache directory
#: does not grow without bound across every repo this module ever measures.
_CACHE_BOUND = 20000


def _code_digest() -> str:
    """sha256 (short) of this module's own source plus the Python adapter's
    -- a change to either invalidates every cached record at once, so a
    cache entry from before a classification fix is never handed back as
    current."""
    parts: "list[bytes]" = []
    for mod_path in (Path(__file__), Path(python_adapter.__file__)):
        try:
            parts.append(mod_path.read_bytes())
        except OSError:
            parts.append(b"")
    return hashlib.sha256(b"\x00".join(parts)).hexdigest()[:16]


def _cache_dir() -> "Path | None":
    """`None` -> no cache: `bounded_cache_dir`/`harness_paths` unimportable
    (this module runs standalone outside the harness too) or the data root
    unresolved."""
    try:
        import bounded_cache_dir
    except ImportError:
        return None
    return bounded_cache_dir.cache_subdir("codegraph-test-bridge")


def _cache_key(data: bytes, code_digest: str) -> str:
    return hashlib.sha256(data + code_digest.encode("utf-8")).hexdigest()


def _cache_read(cache_dir: "Path | None", key: str) -> "dict | None":
    if cache_dir is None:
        return None
    p = cache_dir / f"{key}.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"note: snapshot_bridge: unreadable cache entry {p} ({e}); recomputing", file=sys.stderr)
        return None


def _cache_write(cache_dir: "Path | None", key: str, record: "dict | None") -> None:
    """`artifact_io.atomic_write_text` (`.tmp` + fsync + `os.replace`, same
    directory) -- a killed process mid-write never leaves a torn file a
    later reader trusts. A write failure (unwritable disk, e.g.) is
    reported with one `note:` line on stderr, never swallowed silently —
    the cache is an optimization, but a failure worth knowing about, the
    same law `hs_family.tree_scanning_tests._write_cache` already follows."""
    if cache_dir is None:
        return
    import artifact_io
    target = cache_dir / f"{key}.json"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        artifact_io.atomic_write_text(target, json.dumps(record, sort_keys=True))
    except (OSError, artifact_io.CrossVolumeError) as e:
        print(f"note: snapshot_bridge: cache write to {target} failed: {e}", file=sys.stderr)


def _prune_cache(cache_dir: "Path | None") -> None:
    if cache_dir is None or not cache_dir.is_dir():
        return
    import bounded_cache_dir
    bounded_cache_dir.prune(cache_dir, keep=_CACHE_BOUND)


def _all_mods(idx: dict) -> "set[str]":
    return {m["module"] for mods in idx.get("units", {}).values() for m in mods}


def _git_tracked_test_paths(root: Path, layout: FilesLayout
                            ) -> "tuple[list[str], str | None]":
    """-> (repo-relative test paths, note). `git ls-files` is the only
    file-finder here (matches the module docstring's own "non-git yields
    nothing" law); `note` is set only on the non-git/failure path.

    `root` is a caller-supplied path this process does not own, so this
    carries the same hardening `codegraph._core.git.run_git` does — the
    `secret_scan_before_ship` constants, not a private copy."""
    try:
        proc = subprocess.run(
            ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(root), "ls-files", "-z"],
            capture_output=True, timeout=30, env=secret_scan.git_safe_env())
    except (OSError, subprocess.TimeoutExpired) as e:
        return [], f"snapshot_bridge: could not list git-tracked files under {root}: {e}"
    if proc.returncode != 0:
        return [], f"snapshot_bridge: {root} is not a git checkout — no bridged test files"
    paths = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]
    return sorted(p for p in paths if layout.is_test_file(p)), None


def _module_name_for(path: str) -> str:
    """A synthetic, path-derived module identity for a bridged record.
    Never compared against a REAL dotted module name anywhere (only against
    itself, inside the reverse-import walk this record's own `module` key
    feeds) — see the module docstring's INTERNAL-VS-EXTERNAL section for
    what IS resolved against the snapshot's real module set (the import
    TARGETS, not this file's own identity)."""
    stem = path[:-3] if path.endswith(".py") else path
    return stem.replace("/", ".")


def build_module_record(path: Path, repo: Path, rel_path: str, *,
                        all_mods: "set[str]") -> "dict | None":
    """Parse `path` with the adapter registered for its extension and
    return a snapshot-shaped module record (same fields `take_snapshot`
    writes) built by the SAME code the pipeline uses — `build_symbol_entries`
    for `symbols`, `edge_targets` for import classification (see the module
    docstring for the narrower `ns_roots` this passes). `None` when no
    adapter is registered for `path`'s extension. Raises `SyntaxError` from
    the adapter parse — the caller (`bridge_records`) decides what a
    cannot-parse file means; this function never swallows it."""
    adapter = adapter_for(path)
    if adapter is None:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    module = _module_name_for(rel_path)
    parts = rel_path.split("/")
    container = tuple(parts[:-1])
    parsed = adapter.parse(path, text, container, module)   # SyntaxError propagates

    ns_roots = {m.split(".", 1)[0] for m in all_mods}
    imp_mod: "set[str]" = set()
    imp_fn: "set[str]" = set()
    for rec in parsed.imports:
        if rec.under_type_checking:
            continue
        targets = edge_targets(rec, ns_roots, all_mods)
        (imp_fn if rec.in_function else imp_mod).update(targets)

    return {
        "path": rel_path,
        "module": module,
        "loc": parsed.loc,
        "bytes": parsed.nbytes,
        "file_sha": parsed.file_sha,
        "imports_module_level": sorted(imp_mod),
        "imports_function_local": sorted(imp_fn),
        "imports_external": [],
        "dup_module_assign": list(parsed.dup_module_assign),
        "ambiguous_imports": [],
        "symbols": build_symbol_entries(parsed),
        "payload": {},
        "path_drivers": [],
        "invoked_by": [],
    }


def bridge_records(idx: "dict | None", root: Path, layout: "FilesLayout | None" = None
                   ) -> "list[dict]":
    """Snapshot-shaped module records for every git-tracked test file under
    `root` that (a) matches `layout.is_test_file` (`layout`'s own resolved
    `tests_subdir`/`python_files` — `layout=None` self-builds one via
    `build_test_layout(root)`, the door a caller with no descriptor already
    loaded uses; `build_test_map` passes its OWN layout through instead, so
    one `build_test_map` call self-loads `codemap.yaml` at most once, not
    twice), and (b) is NOT already a snapshot record in `idx`. `idx=None`
    (no snapshot to read against — same degraded shape `selection.select_
    tests` already accepts) yields nothing: without a module set there is
    nothing to classify an import against, and no unit for a caller to
    attribute a touch to either.

    Cache-backed per file (see the module docstring's CACHE section); a
    parse failure or a missing adapter is skipped with a `note:` line on
    stderr naming the file, never a crash and never silent.
    """
    if idx is None:
        return []
    all_mods = _all_mods(idx)
    known_paths = {m["path"] for mods in idx.get("units", {}).values() for m in mods}
    known_paths |= {m["path"] for m in idx.get("test_modules") or ()}
    if layout is None:
        layout = build_test_layout(root)
    test_paths, note = _git_tracked_test_paths(root, layout)
    if note:
        print(f"note: {note}", file=sys.stderr)
    candidates = [p for p in test_paths if p not in known_paths]
    if not candidates:
        return []

    cache_dir = _cache_dir()
    code_digest = _code_digest()
    records: "list[dict]" = []
    for rel_path in candidates:
        abs_path = root / rel_path
        try:
            data = abs_path.read_bytes()
        except OSError as e:
            print(f"note: snapshot_bridge: could not read {rel_path}: {e}", file=sys.stderr)
            continue
        key = _cache_key(data, code_digest)
        cached = _cache_read(cache_dir, key)
        if cached is not None:
            records.append(cached)
            continue
        try:
            record = build_module_record(abs_path, root, rel_path, all_mods=all_mods)
        except SyntaxError as e:
            print(f"note: snapshot_bridge: skipping unparseable test file "
                 f"{rel_path}: {e}", file=sys.stderr)
            continue
        if record is None:
            continue
        _cache_write(cache_dir, key, record)
        records.append(record)
    _prune_cache(cache_dir)
    return records
