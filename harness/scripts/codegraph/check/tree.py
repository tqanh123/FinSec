"""One measurement of the target tree, read by every `hs-code check` door that asks
about its code: the descriptor, a snapshot, and the workspace packages found by the
snapshot's own probe (`package_code_dir`).

The snapshot is a fresh one of the working tree unless the door is PINNED
(`--snap REF [--out BASE]`) to a stored one, read under the query verbs' own law
(`resolve_snapshot` + `checked_idx`): a snapshot whose commit is not HEAD over the
declared roots is refused unless `--allow-stale`; `--fresh` with `--snap` is a usage
error. Many doors over one tree then cost one measurement, not one each.

A door that walked the filesystem by a rule of its own would judge a different tree
from the one the next door measured — a package without `src/` seen by one and
skipped by the other, green at the moment it is being built.
"""
from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import attrs


class CheckError(Exception):
    """The door cannot run honestly (exit 2)."""


@attrs.define(frozen=True, kw_only=True)
class Package:
    name: str
    dir: Path
    code: Path
    unit: str


@attrs.define(frozen=True, kw_only=True)
class Tree:
    repo: Path
    descriptor: Any
    idx: Dict[str, Any]
    packages: Tuple[Package, ...]

    def records(self) -> List[Dict[str, Any]]:
        return [m for ms in self.idx["units"].values() for m in ms]

    def package_of(self) -> Dict[str, str]:
        """module -> package name, for modules inside a workspace package."""
        by_unit = {p.unit: p.name for p in self.packages}
        return {mod: by_unit[u] for mod, u in self.idx.get("mod_unit", {}).items() if u in by_unit}

    def ownership_clashes(self) -> List[str]:
        """Names two units both provide, by the name an import REACHES
        (`dotted_of` over `import_roots`): the snapshot renames a colliding pair
        to their full paths, so its own module names never clash."""
        from codegraph._core.layout import dotted_of
        roots = list(self.descriptor.import_roots or ())
        owners: Dict[str, List[str]] = {}
        for u, ms in sorted(self.idx["units"].items()):
            for m in ms:
                name = (dotted_of(m["path"], roots) if roots and m.get("path") else None) or m["module"]
                owners.setdefault(name, []).append(u)
        return ["%s: owned by %s" % (mod, " and ".join(us))
                for mod, us in sorted(owners.items()) if len(us) > 1]

    def import_paths(self) -> List[Path]:
        """`import_roots` as directories, in declaration order (globs expanded, sorted)."""
        out: List[Path] = []
        for entry in self.descriptor.import_roots or ():
            hits = sorted(p for p in self.repo.glob(entry) if p.is_dir()) if any(
                c in entry for c in "*?[") else [self.repo / entry]
            out.extend(p for p in hits if p.is_dir() and p not in out)
        return out


def module_graph(records: List[Dict[str, Any]], keep: Optional[set] = None) -> Dict[str, set]:
    """module -> modules it imports (module-level and function-local; `mod::name`
    counts as `mod`; a target outside the snapshot is not a node)."""
    mods = {m["module"] for m in records if keep is None or m["module"] in keep}
    g: Dict[str, set] = {m: set() for m in mods}
    for m in records:
        if m["module"] not in g:
            continue
        for tgt in list(m.get("imports_module_level") or []) + list(m.get("imports_function_local") or []):
            base = tgt.split("::", 1)[0]
            if base in g and base != m["module"]:
                g[m["module"]].add(base)
    return g


def import_counts(records: List[Dict[str, Any]], owner: Dict[str, str]) -> Dict[str, Counter]:
    """owner -> Counter{owner: import statements}, self edges dropped."""
    out: Dict[str, Counter] = {}
    for m in records:
        src = owner.get(m["module"])
        if src is None:
            continue
        c = out.setdefault(src, Counter())
        for tgt in list(m.get("imports_module_level") or []) + list(m.get("imports_function_local") or []):
            dst = owner.get(tgt.split("::", 1)[0])
            if dst is not None and dst != src:
                c[dst] += 1
    return out


@attrs.define(frozen=True, kw_only=True)
class Pin:
    """Which snapshot a door reads: `snap` None measures the working tree now."""
    snap: Optional[str] = None
    out: Optional[str] = None
    allow_stale: bool = False
    quiet: bool = False


def add_pin_args(ap: Any) -> None:
    ap.add_argument("--snap", metavar="REF", help="read this stored snapshot (id, prefix or hash) "
                    "instead of measuring the tree; refused when its commit is not HEAD")
    ap.add_argument("--out", metavar="DIR", help="the snapshot store --snap resolves in "
                    "(default: this project's under the data root)")


def pin_of(args: Any, opts: Any) -> Pin:
    if getattr(args, "snap", None) and getattr(opts, "fresh", False):
        raise CheckError("--fresh and --snap are mutually exclusive — --fresh measures the tree on disk "
                         "as it is now, --snap pins an explicit one")
    return Pin(snap=getattr(args, "snap", None), out=getattr(args, "out", None),
               allow_stale=bool(getattr(opts, "allow_stale", False)), quiet=bool(getattr(opts, "json", False)))


def snapshot_idx(repo: Path, descriptor: Any, pin: Optional[Pin], verb: str) -> Dict[str, Any]:
    """Unpinned: measure the tree on disk now. Pinned: the stored snapshot, refused unless it is HEAD's.
    Through the query verbs' own resolve, so a store that cannot resolve answers here in
    the same sentence it answers there."""
    from types import SimpleNamespace

    from codegraph_graph_freshness import SnapshotResolveError, resolve_idx
    pin = pin or Pin()
    opts = SimpleNamespace(allow_stale=pin.allow_stale, json=pin.quiet)
    try:
        _, _, idx, scratch = resolve_idx(repo, descriptor, opts, pin.snap, pin.out, verb,
                                         fresh=pin.snap is None, need_store=False)
    except SnapshotResolveError as e:
        raise CheckError(str(e)) from e
    if scratch is not None:
        shutil.rmtree(scratch, ignore_errors=True)
    return idx


def workspace(repo: Path) -> Tuple[Any, Tuple[Package, ...]]:
    """(descriptor, the packages of its `workspace` roots) — no snapshot taken."""
    from codegraph._core.descriptor import DescriptorError, load
    from codegraph._core.snapshot.pipeline import package_code_dir, workspace_packages
    repo = Path(repo).resolve()
    try:
        descriptor = load(repo / "codemap.yaml", repo).descriptor
    except DescriptorError as e:
        raise CheckError(str(e)) from e
    packages = tuple(
        Package(name=d.name, dir=d, code=package_code_dir(d, descriptor.src_subdir),
                unit="%s/%s" % (r.id, d.name))
        for r in descriptor.roots if r.kind == "workspace" and r.dir(repo).is_dir()
        for d in workspace_packages(r.dir(repo)))
    return descriptor, packages


def measure(repo: Path, pin: Optional[Pin] = None) -> Tree:
    repo = Path(repo).resolve()
    descriptor, packages = workspace(repo)
    idx = snapshot_idx(repo, descriptor, pin, "check")
    return Tree(repo=repo, descriptor=descriptor, idx=idx, packages=packages)
