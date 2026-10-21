#!/usr/bin/env python3
"""graph_test_scope.py — for one plan-graph node, the test files it DECLARES.

Pure function `resolve_test_scope(plan_dir, node_id)`: reads `files_to_create`
+ `files_to_modify` for `node_id` out of `plan-graph.yaml`, filters through the
SAME `_TEST_PATH_RE` change_class_derivation already owns (no second
matcher), and cross-checks each match against disk. Never raises: every broken
input (missing/malformed sidecar, unknown node, wrong-typed `subtasks`)
degrades to a named `status` branch instead.

Six statuses:
  resolved         >=1 declared test path exists on disk
  declared_absent  >=1 declared test path matched, but NONE exist on disk
  no_test_paths    files_to_* declared, but nothing in it matches _TEST_PATH_RE
  malformed_declaration
                    files_to_* IS present but wrong-typed (a scalar where a
                    list belongs), so nothing could be read out of it — kept
                    apart from nothing_declared, whose wording would send the
                    author looking for a declaration that is already there
  nothing_declared node is a real subtask, but files_to_* is empty/absent —
                    kept distinct from no_test_paths: no scanner in this tree
                    already catches an empty files_to_* (it is valid YAML, not
                    a TBD sentinel), so an author who declared nothing looks
                    identical to plan_graph unless this function says so
  unknown_node      node_id is not a key under `subtasks:`
  no_graph          the sidecar is missing, malformed, or wrong-typed —
                    parse_phase_graph's own {"error": ...} surfaces verbatim

Content is never inspected (KISS) — a source file whose NAME matches the
test-path convention (harness/scripts/test_result_readers.py is the known
case) still resolves. `content_unverified` is True on every `resolved` result
so a caller running pytest over `files` knows to treat exit 5 (no tests
collected) as an expected outcome of this function's contract, not a crash.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))
import plan_graph  # noqa: E402
import scope_match  # noqa: E402 — the one path-glob matcher; a files_to_*
                     # entry is a glob (scope_match semantics), not only a
                     # literal path
from change_class_derivation import _TEST_PATH_RE  # noqa: E402

STATUSES = ("resolved", "declared_absent", "no_test_paths", "nothing_declared",
            "malformed_declaration", "unknown_node", "no_graph")


def _result(status, files=(), declared=(), content_unverified=False, error=None):
    return {
        "status": status,
        "files": sorted(set(files)),
        "declared": sorted(set(declared)),
        "content_unverified": content_unverified,
        "error": error,
    }


def _resolve_within_root(root: Path, rel_path: str):
    """`rel_path` resolved against `root`, or None when it escapes `root`
    (an absolute path elsewhere, or a `../` climb) — never raises."""
    cand = Path(rel_path)
    cand = cand if cand.is_absolute() else root / cand
    try:
        resolved = cand.resolve(strict=False)
        resolved.relative_to(root)
    except (ValueError, OSError):
        return None
    return resolved


def _fs_candidate_paths(root: Path) -> frozenset:
    """Filesystem-walk fallback for a plan root git has NO opinion about at
    all (a non-git project) — `git ls-files` cannot answer that question, so
    this reads disk directly with the same exclusions the git-backed pool
    gets for free: no descending into a nested checkout (a directory holding
    its own `.git`, e.g. a linked worktree or a submodule), and byte-compile
    cache dirs pruned outright (their contents can never match `_TEST_PATH_RE`
    anyway, but skipping the dir keeps a large `__pycache__` off the walk)."""
    import os
    out = []
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d != "__pycache__"
            and not os.path.exists(os.path.join(dirpath, d, ".git")))
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            out.append(Path(rel).as_posix())
    return frozenset(out)


def _glob_matches_on_disk(root: Path, entry: str) -> list:
    """Concrete paths under `root` that the glob `entry` matches — the
    grounded answer a caller can actually hand to pytest, as opposed to the
    glob string itself, which names no one file.

    Candidate pool is the SAME universe `plan_graph`'s own glob<->glob
    conflict grounding uses (`plan_graph._git_tracked_paths`), widened with
    `include_untracked=True`: a test-first file is written and read BEFORE
    `git add`, so a `--cached`-only pool drops it and silently recommends
    pytest over the old tests alone. A candidate is filtered through
    `_TEST_PATH_RE` — the SAME regex `resolve_test_scope` already used to
    decide `entry` itself was worth expanding — rather than a `.py`-only
    check, so a committed TS/Go/Java test glob resolves instead of reading
    `declared_absent`. A root git has no opinion about at all (not a git
    repo, not merely an empty one) falls back to a plain filesystem walk with
    the same exclusions — see `_fs_candidate_paths`.

    `_TEST_PATH_RE`'s first alternative matches ANY path under a `tests/`-named
    directory, byte-compile cache included — a `harness/tests/rops/` directory
    declaration would otherwise credit its own `__pycache__/*.pyc` siblings as
    resolved test files, so those are dropped explicitly rather than relying
    on the regex to reject them."""
    rx = scope_match.glob_to_regex(scope_match.normalize_dir_prefix(entry))
    candidates = plan_graph._git_tracked_paths(root, include_untracked=True)
    if not candidates and not plan_graph._is_git_repo(root):
        candidates = _fs_candidate_paths(root)
    return sorted(p for p in candidates
                  if _TEST_PATH_RE.search(p) and rx.match(p)
                  and not p.endswith(".pyc")
                  and "__pycache__" not in p.split("/"))


def _root_for(plan_dir: Path) -> Path:
    """The tree `plan_dir` itself lives in — see `plan_graph.root_for_plan_dir`
    for the full contract. Moved there (rather than kept as a private copy
    here) because `plan_graph.py`'s own CLI entry point needs the identical
    derivation and cannot import this module (this module already imports
    `plan_graph`)."""
    return plan_graph.root_for_plan_dir(plan_dir)


def resolve_test_scope(plan_dir, node_id: str) -> dict:
    """The test-file scope `node_id` declares in `plan_dir`'s plan-graph.yaml.
    Never raises — see module docstring for the 6-way status contract."""
    graph = plan_graph.parse_phase_graph(plan_dir)
    err = graph.get("error")
    if err:
        return _result("no_graph", error=err)

    subtasks = graph.get("subtasks") or {}
    if node_id not in subtasks:
        return _result("unknown_node")

    declared = plan_graph._files(graph, node_id, "files_to_create", "files_to_modify")
    if not declared:
        # A WRONG-TYPED declaration is not an absent one. `_files` drops a scalar
        # written where a list belongs (`files_to_create: path.py`, no dash), and
        # calling that `nothing_declared` — "this node declares no files at all" —
        # sends the author hunting for a declaration sitting right on the line.
        subtask = (graph.get("subtasks") or {}).get(node_id)
        if isinstance(subtask, dict) and any(
                subtask.get(k) is not None
                for k in ("files_to_create", "files_to_modify")):
            return _result("malformed_declaration")
        return _result("nothing_declared")

    test_paths = sorted(p for p in declared if _TEST_PATH_RE.search(p))
    if not test_paths:
        return _result("no_test_paths")

    root = _root_for(plan_dir)
    on_disk, absent = [], []
    for p in test_paths:
        # A declared entry is a path glob (`scope_match` semantics), not only
        # a literal path — `harness/tests/test_*.py` names every matching
        # file, and the literal string itself is never one of them. The
        # literal case stays a plain `.is_file()` check (unchanged): it needs
        # no filesystem walk, and it is the overwhelming majority of
        # declarations.
        if scope_match.has_glob_chars(scope_match.normalize_dir_prefix(p)):
            matches = _glob_matches_on_disk(root, p)
            if matches:
                on_disk.extend(matches)
            else:
                absent.append(p)
            continue
        real = _resolve_within_root(root, p)
        if real is not None and real.is_file():
            on_disk.append(p)
        else:
            absent.append(p)

    if on_disk:
        return _result("resolved", files=on_disk, declared=absent,
                        content_unverified=True)
    return _result("declared_absent", declared=absent)
