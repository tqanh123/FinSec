"""ProjectDescriptor — one project is one declaration, not one code path.

Roots are either DECLARED or auto-discovered, the project has an arbitrary
name, and everything domain-specific rides in an opaque `domains` passthrough
that the kernel carries but never interprets.

WHAT IT CANNOT SEE
    * `kind` is trusted, not verified against the tree: declaring `src` for a
      workspace directory scans the wrong shape silently. `check_repo()` catches
      a missing directory, never a wrong shape.
    * Overlapping roots are RETURNED by `check_overlap`, not rejected — a narrow
      root nested inside a wide one to inspect just that slice is legitimate.
      The caller must print the pairs: a file under two roots is counted twice
      and the total still looks perfectly normal.

`duplicate_keys` (below) lives here rather than in `store.py`: `presets`
needs it too (a shipped preset file goes through the same duplicate-key
door user descriptors do), and `store.load()` needs `presets.resolve_doc`
for `extends:` resolution. Keeping the function on this leaf module — which
imports neither `store` nor `presets` — means that dependency stays
one-directional (`store` -> `presets` -> `model`) instead of a cycle
(`store` -> `presets` -> `store`).
"""
from __future__ import annotations

import fnmatch
import functools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import codegraph_snapshot_reader

#: Root kinds the kernel understands. A root without a `kind` is an error, not
#: a guess — there is no fourth kind, and `None` is not one.
ROOT_KINDS: frozenset[str] = frozenset({"src", "flat", "workspace"})

#: Descriptor schema this kernel writes and reads natively. A descriptor
#: without `schema_version` is LEGACY and only loads through a registered
#: legacy codec (see store.py) — no silent default, ever.
#:
#: SOURCE OF TRUTH lives in `codegraph_snapshot_reader` (a flat module under
#: `harness/scripts/`, not nested under `codegraph/`) — a caller like hs-run
#: that only needs to validate a snapshot's `schema_version` would otherwise
#: have to import this whole package to reach one int (measured: `import
#: codegraph._core.snapshot.store` alone pulls 150 modules into `sys.modules`,
#: `ruamel.yaml` among them; 2026-09-26, @4d201e849). Re-exported under the SAME name here so every
#: existing `from .model import KERNEL_SCHEMA_VERSION` call site in this
#: package keeps working unchanged — one constant, not two.
KERNEL_SCHEMA_VERSION = codegraph_snapshot_reader.KERNEL_SCHEMA_VERSION


class DescriptorError(Exception):
    """Honest cannot-run (maps to exit 2). Raised by the one-door loader."""


@dataclass(frozen=True)
class Root:
    """One source root: where a module tree starts.

    THREE fields, not two. `id` exists because two roots can produce the same
    unit name (`src/pkg_a/graph/` and `packages/pkg-b/`); it becomes a
    REAL directory layer inside the snapshot (`<snap>/<id>/<unit>.yaml`) and
    the prefix of the unit key (`<id>/<unit>`), so "which root owns this unit"
    is readable with `ls`, not by unpicking a filename. The layer ALWAYS
    exists, single-root repos included — one shape, no special cases.
    """

    id: str
    path: str           # relative to the repo root, e.g. "src/pkg", "packages"
    kind: str           # src | flat | workspace

    def dir(self, repo: Path) -> Path:
        return repo / self.path


@dataclass(frozen=True)
class ScanSpec:
    """What gets scanned and what is DELIBERATELY not.

    Defaults reproduce the reference implementation's own behavior exactly
    (`include: ["*.py"]`, no excludes) so a declaration that omits `scan:`
    measures the same tree.
    """

    include: tuple[str, ...] = ("*.py",)
    exclude: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"include": list(self.include), "exclude": list(self.exclude)}


@dataclass(frozen=True)
class GeneratedFile:
    """A file the path-driver scan must skip, and the reason it must.

    A generated inventory names every path in the tree, so it makes every module
    read as invoked and the scan says nothing while looking complete.

    `why` is not decoration. A bare list of filenames is indistinguishable from
    a list somebody once pasted, and the next reader cannot tell an exclusion
    that still holds from one that outlived its file.

    `regen` is the argv that rebuilds this file from the tree it describes —
    empty when the file has none. A move-flat that rewrote the old path INTO
    this file would be correct only until the generator ran again and put the
    old path back, so the fix here is regenerating, and `regen` is where that
    command is declared rather than guessed or hardcoded by any one caller.
    """

    path: str
    why: str
    regen: tuple[str, ...] = ()


@dataclass(frozen=True)
class DriverSpec:
    """How the path-driver stage reads the tree.

    `include` is the TEXT it searches — prose, config and shell, not just code,
    because that is where a script's callers live in a tree whose entry points
    are scripts. `generated` is what it refuses to believe.
    """

    include: tuple[str, ...] = ("*.md", "*.yaml", "*.yml", "*.json", "*.toml",
                                "*.sh", "*.py", "*.cfg", "*.ini", "*.txt")
    generated: tuple[GeneratedFile, ...] = ()
    #: Mapping KEYS whose YAML value is an execution context BY DECLARATION —
    #: `module: x` in a hook dispatcher, `command: …` in an installer's
    #: registry — regardless of whether the value trips the general exec-word
    #: rule. A key not declared here still counts as a citation (`drivers`),
    #: never a run (`invoked`).
    run_keys: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"include": list(self.include),
                "generated": [{"path": g.path, "why": g.why, "regen": list(g.regen)}
                              for g in self.generated],
                "run_keys": list(self.run_keys)}


#: The kinds of copy a `copies:` block may declare, in the order a path is judged.
COPY_KINDS: tuple[str, ...] = ("generated", "backup")


@functools.lru_cache(maxsize=64)
def gitignore_spec(patterns: tuple[str, ...]) -> Any:
    """`patterns` compiled once as a `pathspec.GitIgnoreSpec` -- imported here, on
    first use, so loading a descriptor that declares no copies never pulls it in."""
    import pathspec
    return pathspec.GitIgnoreSpec.from_lines(patterns)


@dataclass(frozen=True)
class CopiesSpec:
    """Files holding a copy of other text: `generated` (a view rebuilt from a source,
    a rendered preview) and `backup` (an old copy kept beside the live one).

    Patterns are gitignore-style (`pathspec.GitIgnoreSpec`, repo-relative): a pattern
    with a `/` is anchored at the repo root, `**` crosses directories, a trailing `/`
    names a directory's whole subtree. A hit there is fixed by regenerating or
    deleting, never by editing, so a prose sweep (`graph grep`) skips these unless
    asked -- and counts what it skipped. Empty when the block is absent.
    """

    generated: tuple[str, ...] = ()
    backup: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.generated or self.backup)

    def kind_of(self, rel: str) -> "str | None":
        """The first `COPY_KINDS` entry whose patterns match `rel`, else `None`."""
        for kind in COPY_KINDS:
            patterns = getattr(self, kind)
            if patterns and gitignore_spec(patterns).match_file(rel):
                return kind
        return None

    def as_dict(self) -> dict:
        return {k: list(getattr(self, k)) for k in COPY_KINDS}


@dataclass(frozen=True)
class EntrypointsSpec:
    """Declared exceptions to the default "is this module a CLI" heuristic.

    The heuristic (a `main` symbol, or an `argparse` import) stays the
    DEFAULT, so a project that declares nothing here classifies exactly as it
    always has. `cli` and `not_cli` are overrides for the heuristic's two
    failure modes — a real entrypoint it misses, a library it mistakes for
    one — never a full replacement list a project must keep exhaustive by
    hand.
    """

    cli: tuple[str, ...] = ()
    not_cli: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"cli": list(self.cli), "not_cli": list(self.not_cli)}


def as_entrypoints_spec(entrypoints: "EntrypointsSpec | dict | None") -> "EntrypointsSpec":
    """Normalize an `entrypoints:` declaration once, at the door.

    A module named in BOTH `cli` and `not_cli` is a self-contradicting
    declaration, not a coin flip decided by dict order — rejected here,
    naming every module caught both ways, rather than silently letting one
    list win.
    """
    if isinstance(entrypoints, EntrypointsSpec):
        return entrypoints
    d = entrypoints or {}
    cli = d.get("cli") or ()
    not_cli = d.get("not_cli") or ()
    for key, val in (("cli", cli), ("not_cli", not_cli)):
        if not all(isinstance(m, str) and m for m in val):
            raise DescriptorError(
                f"`entrypoints.{key}` must be a list of non-empty module "
                f"names, got {val!r}")
    overlap = sorted(set(cli) & set(not_cli))
    if overlap:
        raise DescriptorError(
            f"entrypoints: {', '.join(overlap)} declared in BOTH `cli` and "
            f"`not_cli` — one module cannot be both an entrypoint and "
            f"explicitly not one")
    return EntrypointsSpec(cli=tuple(cli), not_cli=tuple(not_cli))


def as_driver_spec(drivers: "DriverSpec | dict | None") -> "DriverSpec":
    """Normalize a drivers declaration once, at the door."""
    if isinstance(drivers, DriverSpec):
        return drivers
    d = drivers or {}
    gen = []
    for g in d.get("generated") or ():
        if not isinstance(g, dict):
            raise DescriptorError(
                "`drivers.generated[]` must be mappings of path + why, got %r" % (g,))
        regen = g.get("regen") or ()
        if not all(isinstance(tok, str) and tok for tok in regen):
            raise DescriptorError(
                "`drivers.generated[].regen` must be a list of non-empty "
                "strings, got %r" % (regen,))
        gen.append(GeneratedFile(path=str(g.get("path") or ""),
                                 why=str(g.get("why") or ""),
                                 regen=tuple(regen)))
    run_keys = d.get("run_keys") or ()
    if not all(isinstance(k, str) and k for k in run_keys):
        raise DescriptorError(
            "`drivers.run_keys` must be a list of non-empty strings, got %r" % (run_keys,))
    return DriverSpec(include=tuple(d.get("include") or DriverSpec().include),
                      generated=tuple(gen), run_keys=tuple(run_keys))


@dataclass(frozen=True)
class ProjectDescriptor:
    """The kernel's whole knowledge of a project. Immutable after load.

    `domains` is opaque payload for registered domain plugins: the kernel
    neither reads nor writes inside it. `snapshot` selects the snapshot
    format by registry name — `None` means the kernel's own v2 format.
    """

    schema_version: int
    project: str
    roots: tuple[Root, ...] = ()
    auto_discover: bool = False
    scan: ScanSpec = field(default_factory=ScanSpec)
    drivers: DriverSpec = field(default_factory=DriverSpec)
    #: Declared ownership channels + ledgers, read by the owners analyser.
    #: Kept as the raw mapping: the patterns are the analyser's vocabulary,
    #: not the descriptor's, and compiling them here would put one domain's
    #: rules in the kernel. None means the block is absent, which is not the
    #: same as declared-and-empty.
    owners: "dict[str, Any] | None" = None
    thresholds: dict[str, int] = field(default_factory=dict)
    rules: tuple[dict[str, Any], ...] = ()     # consumed by the rules engine
    #: Declared group ladder for trees WITHOUT dists: an ORDERED list of
    #: {name, match} where match holds path globs relative to the repo and the
    #: FIRST matching entry wins. Consumed by a graph builder, which fills
    #: a module's group only where the snapshot's `dist` is absent.
    groups: tuple[dict[str, Any], ...] = ()
    domains: dict[str, Any] = field(default_factory=dict)
    #: The shared top-level namespace package name for a `packages/<dist>/src`
    #: layout (F9) -- REQUIRED (refused, never defaulted) once `layout:`
    #: declares `packages_dir`; a `None` project (a flat or single-package
    #: tree) leaves this `None` too. No `"ob"`/`"gsm_agent"`-style fallback
    #: ships anywhere in this door: a silent default here is how a scaffold
    #: or a carve would write into the WRONG namespace and nobody would see it.
    namespace_root: str | None = None
    #: `layout.packages_dir` / `layout.src_subdir` / `layout.tests_subdir` --
    #: the `packages/<dist>/src` convention's three directory names, kept
    #: raw (no defaulted string) so a tree that never opted into this layout
    #: carries `None` in all three rather than an invented "packages"/"src".
    packages_dir: str | None = None
    src_subdir: str | None = None
    tests_subdir: str | None = None
    #: Repo-relative directories that are REAL `sys.path` entries (measured,
    #: one entry per genuine insertion -- a launcher, a conftest, a bootstrap
    #: -- never invented), consumed by `layout.dotted_of`. `.` names the repo
    #: root itself; a `*` path segment matches exactly one directory (e.g.
    #: `packages/*/src`, one entry per dist). Order does not matter: the
    #: LONGEST matching entry wins, not the first declared.
    import_roots: tuple[str, ...] = ()
    #: `scaffold.templates` -- key only in this schema; the VALUE is
    #: `hs-code scaffold`'s (`codegraph/scaffold/engine.py`), carried verbatim.
    scaffold: "dict[str, Any] | None" = None
    snapshot_format: str | None = None         # registry name, see snapshot/format.py
    #: How many of this project's snapshots `hs-code graph snapshot --apply`
    #: keeps by default (`snapshot.keep` in the yaml). None means the
    #: declaration is absent, which keeps every snapshot forever — the
    #: behavior before this field existed, not a default worth silently
    #: changing under a tree that never opted in.
    snapshot_keep: int | None = None
    #: Explicit exceptions to the default "is this a CLI" heuristic. See
    #: `EntrypointsSpec`.
    entrypoints: EntrypointsSpec = field(default_factory=EntrypointsSpec)
    #: `docs.source_ledgers` -- ({path, repos}, ...): the doc tables whose rows
    #: declare a source kind, and the repo names those rows may cite. Empty when
    #: the block is absent: the row check is then off, not defaulted.
    source_ledgers: tuple[dict[str, Any], ...] = ()
    #: `adapters:` -- the project's own readers (`adapter.book.AdapterRow`, origin
    #: "project"), each a command run only in a trusted repo. Empty when absent:
    #: the bin's book alone reads the tree.
    adapters: "tuple[Any, ...]" = ()
    #: `test_select:` -- ({kind, test, why}, ...), parsed by `codegraph_change_
    #: reader.parse_test_select`. `kind: whole_tree`: `test` (a path or a glob)
    #: is selected by `hs-code change test select` on every change. Empty when
    #: absent.
    test_select: "tuple[dict[str, Any], ...]" = ()
    #: `obligations:` -- ({id, path, how, pattern, note}, ...), parsed by
    #: `descriptor.obligations.parse`: what a dist owes the repo's own
    #: registries, read by `hs-code check dist-obligations`. Empty when absent.
    obligations: "tuple[dict[str, Any], ...]" = ()
    #: Which packs (`packs/<name>/`, living outside `harness/`, never in the
    #: release manifest) THIS project turned on -- one path per pack. Unlike
    #: every other path in this descriptor, these are relative to the
    #: HARNESS's OWN install root (where `packs/` physically lives -- under a
    #: global install that is the shared bin, not this project's tree), never
    #: to the project `repo` this descriptor describes -- a project opts a
    #: shared pack IN, it does not carry a private copy of it (one copy
    #: rule). `hs_code.py`'s `_pack_frag_dirs` resolves each path against its
    #: OWN install root and merges the pack's `hs-code-registry.d/` on top of
    #: the core's; this door only checks SHAPE (a list of non-empty strings)
    #: -- a path naming no real directory is the caller's refusal, not this
    #: door's, exactly like `layout.roots`' existence check lives in
    #: `check_repo`, not here.
    #:
    #: Distinct from `extends:` (`store.load`'s OWN preset resolution, a
    #: NAMED layout inherited from `codegraph/_core/presets/library/*.yaml`,
    #: resolved BEFORE this schema is even checked) -- two different
    #: questions happen to share the word "preset": `extends:` says WHERE
    #: this descriptor's OWN layout came from; `preset:` says WHICH shared
    #: packs this project turned on. Neither reads the other.
    preset: tuple[str, ...] = ()
    #: `copies:` -- generated views and backups. See `CopiesSpec`.
    copies: CopiesSpec = field(default_factory=CopiesSpec)
    path: Path | None = None                   # where this descriptor was loaded from


# ── root validation ───────────────────────────────────────────────────────────

def check_roots(roots: list[Root]) -> None:
    """Self-consistency of a root list. Every rejection blocks a lying scan:

    duplicate `id` (dict keyed by id silently overwrites), `id` containing `/`
    or empty (it is a real directory layer), unknown `kind`, duplicate `path`
    (every file under it counted twice — and the total still looks normal).
    """
    if not roots:
        raise DescriptorError("`layout.roots:` empty — nothing to scan")
    by_id: dict[str, Root] = {}
    by_path: dict[str, Root] = {}
    for r in roots:
        if not r.id or "/" in r.id or r.id.startswith("."):
            raise DescriptorError(
                f"root id {r.id!r} is unusable: it names a directory inside the "
                "snapshot, so it must be non-empty, contain no `/`, and not start with `.`")
        if r.kind not in ROOT_KINDS:
            raise DescriptorError(f"root {r.id!r} declares `kind: {r.kind}` — must be one of "
                                  + " / ".join(sorted(ROOT_KINDS)))
        if r.id in by_id:
            raise DescriptorError(f"two roots share id {r.id!r}: {by_id[r.id].path} and {r.path}")
        if r.path in by_path:
            raise DescriptorError(f"two roots share path {r.path!r}: ids {by_path[r.path].id!r} "
                                  f"and {r.id!r} — every file under it would be counted TWICE")
        by_id[r.id], by_path[r.path] = r, r


def root_paths(descriptor: "ProjectDescriptor") -> tuple[str, ...]:
    """Repo-relative paths of every declared root, in declaration order.

    Empty when nothing is statically declared (a pure auto-discover
    project) — never raises. This is the ONE door for "give me the
    declared roots as plain paths"; a caller that cannot function without
    a real scope wants `require_roots` instead, not its own empty check.
    """
    return tuple(r.path for r in descriptor.roots)


def require_roots(descriptor: "ProjectDescriptor", *, why: str) -> tuple[str, ...]:
    """Like `root_paths`, but an empty answer is an ERROR here, not a value
    the caller must remember to check.

    `why` is the caller's own reason it needs a real scope — the refusal
    names it, so "why did this cannot-run" reads from the message alone,
    not from re-deriving which verb raised it.
    """
    paths = root_paths(descriptor)
    if not paths:
        raise DescriptorError(
            f"no `layout.roots:` declared — {why} needs a known scope; "
            f"an auto-discover project has none to narrow to")
    return paths


def check_overlap(roots: list[Root]) -> list[tuple[Root, Root]]:
    """Pairs of roots where one contains the other -> [(outer, inner)]. Empty is clean.

    Returns instead of raising: only the caller knows whether an overlap here is
    a mistake or a deliberate narrow-inspect root. This function only DETECTS
    the nest, for a human to confirm; the actual prevention of double-counting
    a file is `_root_owns`'s longest-path-match rule (`snapshot/pipeline.py`),
    which runs independently of whether this function is even consulted.
    """
    res: list[tuple[Root, Root]] = []
    for a in roots:
        for b in roots:
            if a is b:
                continue
            if (b.path + "/").startswith(a.path.rstrip("/") + "/"):
                res.append((a, b))
    return res


def select_roots(roots: list[Root], want: list[str] | None = None,
                 snapshot_ids: list[str] | None = None) -> tuple[list[Root], list[str]]:
    """Which roots this pass actually scans -> (kept, [dropped ids, with reason]).

    Two independent cut sources the caller must distinguish: `want` (a typed
    `--root`, intentional, visible in the command line) and `snapshot_ids`
    (a previous filtered snapshot; nobody typed anything NOW). The second is
    the dangerous one: scanning the full tree against a root-filtered snapshot
    turns every symbol of the missing root into NEW — a wall of red for a
    completely wrong reason.
    """
    keep = list(roots)
    why: list[str] = []
    if snapshot_ids is not None:
        gone = [r.id for r in keep if r.id not in snapshot_ids]
        keep = [r for r in keep if r.id in snapshot_ids]
        why += [f"{i} (snapshot does not contain this root)" for i in gone]
    if want:
        gone = [r.id for r in keep if r.id not in want]
        keep = [r for r in keep if r.id in want]
        why += [f"{i} (--root)" for i in gone]
    return keep, why


def roots_banner(kept: list[Root], total: int, why: list[str]) -> list[str]:
    """Lines to print for a FILTERED pass. Empty on a full pass — silence there
    is CORRECT silence. A filtered exit-0 looks exactly like a full one in CI
    logs; an unprinted forgotten `--root x` reports green forever about half a
    tree nobody is looking at."""
    if not why:
        return []
    return [f"⚠ FILTERED PASS — {len(kept)}/{total} roots",
            *(f"  dropped: {w}" for w in why),
            "  cross-root edges TO those roots do NOT appear in this pass.", ""]


# ── scanning ───────────────────────────────────────────────────────────────────

def path_matches(rel: str, pattern: str) -> bool:
    """fnmatch on the relative path, plus `prefix/**` meaning a whole subtree."""
    if pattern.endswith("/**"):
        return rel == pattern[:-3] or rel.startswith(pattern[:-2])
    return fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(Path(rel).name, pattern)


def as_scan_spec(scan: ScanSpec | dict | None) -> ScanSpec:
    """Normalize a scan declaration (descriptor dict or ScanSpec) once, at the door."""
    if isinstance(scan, ScanSpec):
        return scan
    d = scan or {}
    return ScanSpec(include=tuple(d.get("include") or ("*.py",)),
                    exclude=tuple(d.get("exclude") or ()))


def scan_py(root: Path, scan: ScanSpec | dict | None = None) -> list[Path]:
    """Source files under `root` per the scan declaration. Sorted.

    WHY NOT BARE rglob: a repo-scoped `rglob("*.py")` drags `migrations/`,
    `*_pb2.py`, `vendor/`, `.venv/` into every measurement — size gates go red
    on generated files nobody hand-writes. Both directions lie: no excludes
    makes the tree look dirtier than it is; silent excludes make it look
    cleaner. Call `scan_py_counted` and PRINT the dropped count.
    """
    spec = as_scan_spec(scan)
    inc = spec.include or ("*.py",)
    exc = spec.exclude
    res: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if not any(path_matches(rel, m) for m in inc):
            continue
        if any(path_matches(rel, m) for m in exc):
            continue
        res.append(p)
    return res


def scan_py_counted(root: Path, scan: ScanSpec | dict | None = None) -> tuple[list[Path], int]:
    """Like `scan_py`, plus the count of EXCLUDED files — the caller prints it.
    A silent exclusion is a way for a tree to look cleaner than it is, and it
    reads exactly like a genuinely clean tree."""
    all_files = scan_py(root, ScanSpec(include=as_scan_spec(scan).include or ("*.py",)))
    kept = scan_py(root, scan)
    return kept, len(all_files) - len(kept)


def duplicate_keys(p: Path) -> list[tuple[str, int, int]]:
    """Keys declared TWICE in the same YAML mapping -> [(key, first, later line)].

    A YAML-1.1 (PyYAML-style) loader silently keeps the LAST key on a repeat —
    measured on a real project descriptor once: 20 of 43 dists had `slices:`
    declared twice, 20 domain questions vanished while sitting in the file 130
    lines apart, and every gate stayed green. That is exactly the shape this
    module exists to catch: an empty cell reads exactly like a clean one.

    Deliberately NOT a YAML parse — parsing is the thing that eats the
    evidence. Line-scope tracking only.
    """
    res: list[tuple[str, int, int]] = []
    seq_at: dict[int, int] = {}                     # indent -> `- ` sequence counter
    seen_at: dict[str, int] = {}                    # "scope/key" -> line
    stack: list[tuple[int, str]] = []               # (indent, scope name)

    def scope_at(level: int) -> str:
        while stack and stack[-1][0] >= level:
            stack.pop()
        return "/".join(x[1] for x in stack)

    for i, ln in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        dash = re.match(r"^(\s*)-\s", ln)
        if dash:
            level = len(dash.group(1))
            seq_at[level] = seq_at.get(level, 0) + 1
            scope_at(level)
            stack.append((level, f"[{seq_at[level]}]"))
            # a key written right after `- ` belongs to this element
            ln = " " * (level + 2) + ln.lstrip()[2:]
        m = re.match(r"^(\s*)([A-Za-z_][\w.-]*):", ln)
        if not m:
            continue
        level = len(m.group(1))
        scope = scope_at(level)
        scoped_key = f"{scope}/{m.group(2)}"
        if scoped_key in seen_at:
            res.append((m.group(2), seen_at[scoped_key], i))
        seen_at[scoped_key] = i
        stack.append((level, m.group(2)))
    return res
