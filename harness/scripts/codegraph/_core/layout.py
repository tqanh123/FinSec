"""dotted_of — one repo-relative file path -> one Python dotted import name.

Replaces the upstream mirror (`portkit/ledger/carve.py:58-67`), which only
understood a tree with a literal `src` PATH SEGMENT anywhere in it — a flat
layout (a directory added to `sys.path` directly, no `src/`) always came back
`None` there, so a tool built on it could never carve or repoint a flat tree.

THE ALGORITHM
    Every `import_roots` entry names a DIRECTORY that is (or, for a declared
    future layout, will be) a real `sys.path` entry — measured per project,
    never guessed. A root can therefore only ever claim a STRICT prefix of
    `path`'s segments, never all of them: the last segment is always the
    file itself, and a root is never rooted AT a file. `dotted_of` finds
    every root that is a strict, segment-wise PREFIX of `path` (a `*`
    segment in a root matches exactly one path segment, for a glob root like
    `packages/*/src`), keeps the LONGEST prefix among the matches, strips
    it, drops the file's suffix (`.py`, or the suffix of a project's own reader
    -- `adapter.book`), and drops a trailing `__init__`.

    LONGEST MATCH WINS because roots can nest: a flat tree's own repo root
    (`.`, zero segments) is a prefix of everything under it, but a more
    specific declared root two levels down (`harness/scripts`) names the
    REAL `sys.path` entry for the files under it, and must win. Proven by
    the case this function exists for: `harness/scripts/rops/ropslib/x.py`
    resolves through `harness/scripts` (an ACTUAL sys.path entry — nearly
    every hook/script in this tree inserts it) to `rops.ropslib.x`, not
    through `.` to `harness.scripts.rops.ropslib.x` — the position-based name
    a plain "shortest common ancestor" reading would produce.

WHAT IT CANNOT SEE
    * No default root, ever: an empty/absent `import_roots` means every path
      resolves to `None` — never a guessed `ob`/`gsm_agent`-style fallback.
    * A root is trusted, not verified against `sys.path` at runtime: a stale
      declaration (the tree moved, the root did not get updated) resolves
      silently to the WRONG dotted name rather than refusing — the same
      blind spot `check_repo()` covers for `layout.roots`, not this door.
    * A `*` segment matches exactly one path segment (a glob directory name,
      e.g. one dist under `packages/`) — it never spans `/`, so there is no
      `**` here. It can still only match a DIRECTORY segment, never the
      file itself (see `_match_len`) — a trailing `*` with nothing declared
      after it (`harness/tests/*`) names one bare subdirectory, not the
      files sitting directly in its parent.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any, Iterable, Sequence


def _root_segments(root: str) -> tuple[str, ...]:
    root = root.strip("/")
    if not root or root == ".":
        return ()
    return tuple(root.split("/"))


def _segment_matches(pattern: str, seg: str) -> bool:
    if "*" in pattern or "?" in pattern or "[" in pattern:
        return fnmatch.fnmatchcase(seg, pattern)
    return pattern == seg


def _match_len(root: str, path_parts: Sequence[str]) -> "int | None":
    """How many leading `path_parts` this ONE root's segments claim, or None.

    A root may only claim DIRECTORY segments — strictly FEWER segments than
    `path_parts` holds, since the last part is always the file itself, never
    a directory a root can be rooted at. Without this, a trailing `*` root
    (`harness/tests/*`, nothing declared after it) is "exactly one path
    segment" agnostic to file vs directory, and matches the file's own
    basename as if it were a subdirectory — `harness/tests/test_x.py` then
    resolves through `harness/tests/*` (length 3, `*` consuming `test_x.py`
    itself) instead of through the correct, shorter `harness/tests` (length
    2), leaving an EMPTY remainder and losing the module name entirely."""
    seg = _root_segments(root)
    if len(seg) >= len(path_parts):
        return None
    for pattern, actual in zip(seg, path_parts):
        if not _segment_matches(pattern, actual):
            return None
    return len(seg)


def spellings_of(path: "Path | str", import_roots: Sequence[str]) -> "list[str]":
    """Every dotted name `path` (repo-relative) is importable as: one per declared
    `import_roots` entry that is a prefix of it, sorted. `dotted_of` picks the
    longest of these as THE name; the others are how an import may still spell it
    (`harness.scripts.grid.loop` with the repo root on sys.path is `grid.loop`)."""
    parts = tuple(p for p in Path(path).as_posix().split("/") if p not in ("", "."))
    out: set[str] = set()
    for root in import_roots:
        n = _match_len(root, parts)
        if n is None:
            continue
        name = dotted_of("/".join(parts), [root])
        if name:
            out.add(name)
    return sorted(out)


def flat_leaf_index(flat_dirs: "dict[str, Path]",
                    modules: "Iterable[tuple[str, Path, str]]") -> "dict[str, list[str]]":
    """Bare-name index for `kind: flat` roots: leaf name -> sorted modules carrying it.
    `flat_dirs` maps each flat root's id to its directory; `modules` are
    `(root id, absolute file path, dotted name)`. A flat root's modules reach sys.path
    by DIRECTORY and import each other by leaf name; a workspace or src root never
    does, so indexing one would invent edges the code cannot make. Only a file sitting
    directly on a sys.path entry answers to its bare name: in the root's own
    directory, or in one that is no package. Inside a package (`grid/types.py` beside
    `grid/__init__.py`) it is `grid.types`, and reading `types` as it turned every
    stdlib `import types` into an edge. One carrier resolves; several do not."""
    out: "dict[str, list[str]]" = {}
    for root, path, module in modules:
        if root not in flat_dirs:
            continue
        home = path.parent.parent if path.name == "__init__.py" else path.parent
        if home == flat_dirs[root] or not (home / "__init__.py").is_file():
            out.setdefault(module.rsplit(".", 1)[-1], []).append(module)
    for v in out.values():
        v.sort()
    return out


def dotted_of(path: "Path | str", import_roots: Sequence[str]) -> "str | None":
    """`path` (repo-relative) -> dotted module name via the LONGEST matching
    `import_roots` entry, or `None` when no declared root is a prefix of it."""
    parts = tuple(p for p in Path(path).as_posix().split("/") if p not in ("", "."))
    if not parts:
        return None
    best: "int | None" = None
    for root in import_roots:
        n = _match_len(root, parts)
        if n is None:
            continue
        if best is None or n > best:
            best = n
    if best is None:
        return None
    remainder = list(parts[best:])
    if not remainder:
        return None
    last = remainder[-1]
    stem, dot, _suffix = last.rpartition(".")
    if dot and stem:
        remainder[-1] = stem
    if remainder[-1] == "__init__":
        remainder.pop()
    if not remainder:
        return None
    return ".".join(remainder)


def test_home_of(descriptor: Any, dist: "str | None" = None) -> str:
    """Repo-relative test-tree home a declared layout gives one dist, trailing
    `/` — the ONE place a `packages/<dist>/tests/`-vs-flat-`tests/` convention
    is decided (`refactor.namerepair.suite_roots` and `test.classify`'s home
    ladder both call this, never a second copy).

    `dist` given (truthy) -> `descriptor.layout.packages_dir` is REQUIRED
    (a dist without a packages layout to home it under is a caller bug, not
    a guess) -> `<packages_dir>/<dist>/<tests_subdir or "tests">/`.
    `dist` absent/falsy -> the repo-root `tests/`, UNCONDITIONALLY — the one
    shared test home every layout carries, packages-declaring or not (a flat
    root inside an otherwise `packages_dir` project homes there too).
    """
    if not dist:
        return "tests/"
    if not descriptor.packages_dir:
        raise ValueError(
            "test_home_of: a dist was given but the descriptor declares no "
            "layout.packages_dir to home it under")
    subdir = descriptor.tests_subdir or "tests"
    return f"{descriptor.packages_dir}/{dist}/{subdir}/"


def dist_root(descriptor: Any, dist: str) -> str:
    """Repo-relative root a dist's own tree lives under, no trailing `/` —
    the ONE join that replaces upstream's hard-coded `repo / "packages" /
    dist` for every caller that needs it (`hs-code port build`/`port
    tests-land`/`add-module`, sub3's redlist/leaks). `packages_dir` absent
    -> `ValueError`, the same caller-bug shape `test_home_of` raises above:
    a dist name was given with no packages layout to home it under, never a
    silent guess."""
    if not descriptor.packages_dir:
        raise ValueError(
            "dist_root: a dist was given but the descriptor declares no "
            "layout.packages_dir to home it under")
    return f"{descriptor.packages_dir}/{dist}"


def dist_of_path(descriptor: Any, rel_path: "Path | str") -> "str | None":
    """The dist (or root) a repo-relative path belongs to, off the declared
    layout — the inverse of `dist_root`, and the ONE place that answer is
    decided (`hs-code test redlist`/`test leaks` group by it; the opsbrain pack
    homes a file by it). Upstream guessed from the first path segment, or the
    one after a literal `packages/`, so a test under a nested root (this repo's
    `rops` inside `harness`) was counted as `harness`.

    Order: under `layout.packages_dir` -> the segment after it; else the
    DEEPEST declared `layout.roots` entry covering the path (the same
    longest-prefix rule `dotted_of` applies to `import_roots`) -> the package
    directory below it for a `workspace` root, the root's `id` otherwise;
    else `None` — never a guessed segment.

    WHAT IT CANNOT SEE: a root path with a `.` inside a segment cannot match a
    dotted name turned back into a path (a junit classname), and a path is
    matched by its segments only — nothing is read from disk."""
    parts = tuple(p for p in Path(rel_path).as_posix().split("/") if p not in ("", "."))
    if descriptor.packages_dir:
        n = _match_len(descriptor.packages_dir, parts)
        if n is not None and n + 1 < len(parts):
            return parts[n]
    hit = deepest_root(descriptor.roots, parts)
    if hit is None:
        return None
    root, n = hit
    if root.kind == "workspace":
        return parts[n] if n + 1 < len(parts) else None
    return root.id


def deepest_root(roots: "Iterable[Any]", parts: "tuple[str, ...]") -> "tuple[Any, int] | None":
    """(root, matched segment count) of the DEEPEST root covering `parts`;
    a root is anything with a `.path`."""
    best: "tuple[Any, int] | None" = None
    for root in roots:
        n = _match_len(root.path, parts)
        if n is not None and (best is None or n > best[1]):
            best = (root, n)
    return best


def dist_import_root(descriptor: Any, dist: str) -> str:
    """`dist_root(descriptor, dist)` plus the declared `src_subdir` — the
    dist's own `sys.path` entry, the SAME shape a workspace's `import_roots:
    ["packages/*/src"]` already declares per-dist as a glob, computed here
    for ONE named dist instead. `src_subdir` absent means NO src segment at
    all: a real flat-dist layout (`<packages_dir>/<dist>`), never an
    invented `"src"` default the way upstream's own hard-coded `packages/
    <dist>/src` join assumed for every dist unconditionally. The ONE place
    the src-or-no-src rule is decided — `dist_src_root` below and
    `codegraph_port_tree_verbs._resolve_dest` both build off this, rather
    than each re-deriving the same `if src_subdir` branch."""
    root = dist_root(descriptor, dist)
    if descriptor.src_subdir:
        return f"{root}/{descriptor.src_subdir}"
    return root


def dist_src_root(descriptor: Any, dist: str) -> str:
    """`dist_import_root(descriptor, dist)` plus `namespace_root` — where a
    dist's own package tree lives on disk. `add-module`'s own `_resolve_dest`
    does NOT call this: its `rel_path` already starts with the namespace
    segment (the target's dotted module name is namespace-qualified from
    the start), so appending `namespace_root` a second time here would
    double it — that caller uses `dist_import_root` directly instead."""
    return f"{dist_import_root(descriptor, dist)}/{descriptor.namespace_root}"


def target_descriptor(port_root: Path) -> "tuple[Any, str | None]":
    """`(descriptor, None)` off the target's `codemap.yaml`, or `(None, reason)`.

    Parsed with NO `repo` (`load(path, repo=None)`): a first-ever build runs
    before the dist tree exists on disk, so `check_repo`'s root-existence
    check has nothing to hold it to yet. `layout.packages_dir` is required —
    every port verb homes a dist under it — and a declared `packages_dir`
    already implies `namespace_root` (the descriptor refuses one without the
    other), so this one check covers both keys the layout door joins on."""
    from codegraph._core.descriptor import DescriptorError, load  # lazy: the path helpers above stay descriptor-free

    codemap_path = Path(port_root) / "codemap.yaml"
    if not codemap_path.is_file():
        return None, ("no `codemap.yaml` under %s -- onboard the target before porting "
                      "(the port verbs read the dist layout from it)" % port_root)
    try:
        descriptor = load(codemap_path, None).descriptor
    except DescriptorError as e:
        return None, str(e)
    if not descriptor.packages_dir:
        return None, ("%s declares no `layout.packages_dir` -- the port verbs need it "
                      "to home a dist's tree" % codemap_path)
    return descriptor, None


def package_code_dir(package: Path, src_subdir: "str | None" = None) -> Path:
    """Where one `workspace` package keeps its modules: `<package>/<src_subdir>`
    when that directory exists — the declared `layout.src_subdir`, `src` when
    none is declared — else the package directory itself. DETECTED per
    package, not assumed: code directly under the package directory is legal
    and common outside uv, and one tree may mix both shapes. Every reader of a
    workspace package's code asks here, so no two of them judge different
    trees."""
    sub = package / (src_subdir or "src")
    return sub if sub.is_dir() else package


def workspace_packages(root_dir: Path) -> list[Path]:
    """The package directories of one `workspace` root, sorted."""
    return sorted(x for x in root_dir.iterdir() if x.is_dir() and not x.name.startswith("."))


def root_owns(p: Path, r: Any, roots: "Sequence[Any]", repo: Path) -> bool:
    """True when `r` is the MOST SPECIFIC declared root (by path — longest
    match wins) covering `p` among `roots`. Reuses `dotted_of`'s own
    longest-prefix rule (never a second copy of that algorithm), applied to
    the declared roots' own PATHS instead of `import_roots` — so a file
    reachable through two overlapping roots (a namespace package's own root
    nested inside a coarser flat root, e.g. this repo's `rops` under
    `harness`) is scanned exactly once, by whichever root's path is the
    deepest match, never once per overlapping root. Without this, an
    overlapping pair produced two `SourceRef` rows for the SAME file which,
    once `import_roots` gave both the SAME correctly-resolved module name,
    silently collided in every dict keyed by module downstream — losing one
    root's data, not merely double-counting it (see the module docstring's
    own "cross-root overlap" note, which predates this rule and describes the
    OLDER, weaker symptom)."""
    try:
        rel = p.relative_to(repo).as_posix()
    except ValueError:
        return True
    all_paths = [x.path for x in roots]
    return dotted_of(rel, all_paths) == dotted_of(rel, [r.path])
