"""A JavaScript workspace: its manager, its packages, which package owns a file.

The workspace root is the nearest directory at or above where the walk starts that
holds a `pnpm-workspace.yaml` (pnpm -- its `packages:`, never package.json's
`workspaces`) or a `package.json` with `workspaces` (an array, or `{packages}` /
yarn 1's `{packages, nohoist}`; yarn berry takes the array only). None found, the
nearest `package.json` is one package alone (`manager: none`). The manager of a
`workspaces` root is its `packageManager` field, else its lockfile (`yarn.lock` with
`.yarnrc.yml` is berry, alone yarn 1), else npm.

A package is a directory the globs select (`globs.selected`, `**/node_modules/**`
never) that holds a readable `package.json`, the root's own included; packages are
keyed by directory, since a name two packages share is listed in `duplicates`, not
resolved. `ts_version` is the `typescript` spec the root declares, else the first
package's -- the string as written, nothing is installed or read from node_modules.

WHAT IT CANNOT SEE
    * pnpm `catalog:` specs and `.pnpmfile.cjs` hooks: specs are kept as written.
    * A package outside the repo (`link:../elsewhere`).
"""
from __future__ import annotations

import json
import os
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml_io

from . import globs, pkgjson
from .pkgjson import Pkg

_ALWAYS_OUT = "**/node_modules/**"
_SKIP_DIRS = frozenset({"node_modules", ".git"})


@dataclass(frozen=True)
class WorkspaceMap:
    root: str                                   # repo-relative, "." for the repo root
    manager: str                                # pnpm | npm | yarn1 | yarn-berry | none
    packages: "tuple[Pkg, ...]" = ()
    ts_version: "str | None" = None
    duplicates: "dict[str, tuple[str, ...]]" = field(default_factory=dict)
    notes: "tuple[str, ...]" = ()
    #: the manifests this map was read from (repo-relative)
    inputs: "tuple[str, ...]" = ()

    def owner(self, rel_path: str) -> "Pkg | None":
        """The package whose directory is the longest prefix of `rel_path`."""
        best: "Pkg | None" = None
        for p in self.packages:
            if p.dir == "." or rel_path == p.dir or rel_path.startswith(p.dir + "/"):
                if best is None or best.dir == "." or (p.dir != "." and len(p.dir) > len(best.dir)):
                    best = p
        return best

    def by_name(self) -> "dict[str, Pkg]":
        out: "dict[str, Pkg]" = {}
        for p in self.packages:
            if p.name:
                out.setdefault(p.name, p)
        return out


def _rel(d: str, sub: str) -> str:
    return posixpath.normpath(posixpath.join(d, sub))


def _workspace_globs(repo: Path, d: str) -> "tuple[str, Any, list[str]] | None":
    """(manager, globs, notes) when `d` is a workspace root, else None."""
    yml = repo / d / "pnpm-workspace.yaml"
    if yml.is_file():
        try:
            doc = yaml_io.yaml_load(yml.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001 -- a broken file is a note, never a crash
            return "pnpm", [], ["pnpm_workspace_unreadable: %s (%s)" % (_rel(d, "pnpm-workspace.yaml"), e)]
        pats = doc.get("packages") if isinstance(doc, dict) else None
        return "pnpm", [str(p) for p in pats or [] if isinstance(p, str)], []
    raw = _raw_manifest(repo, d)
    if "workspaces" not in raw:
        return None
    manager = _manager(repo, d, raw)
    ws = raw["workspaces"]
    if isinstance(ws, list):
        return manager, [str(p) for p in ws if isinstance(p, str)], []
    if isinstance(ws, dict) and manager != "yarn-berry":
        return manager, [str(p) for p in ws.get("packages") or [] if isinstance(p, str)], []
    return manager, [], ["workspaces_shape_unsupported: %s (%s takes an array)"
                         % (pkgjson.manifest(d), manager)]


def _raw_manifest(repo: Path, d: str) -> "dict[str, Any]":
    try:
        raw = json.loads((repo / pkgjson.manifest(d)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _manager(repo: Path, d: str, raw: "dict[str, Any]") -> str:
    pm = raw.get("packageManager")
    if isinstance(pm, str) and "@" in pm:
        name, version = pm.split("@", 1)
        if name == "yarn":
            return "yarn1" if version.startswith("1.") else "yarn-berry"
        if name in ("npm", "pnpm"):
            return name
    if (repo / d / "yarn.lock").is_file():
        return "yarn-berry" if (repo / d / ".yarnrc.yml").is_file() else "yarn1"
    if (repo / d / "pnpm-lock.yaml").is_file():
        return "pnpm"
    return "npm"


def _dirs(repo: Path, root: str) -> "list[str]":
    base = repo / root
    out = []
    for cur, dirs, _files in os.walk(base):
        dirs[:] = sorted(x for x in dirs if x not in _SKIP_DIRS)
        rel = Path(cur).relative_to(base).as_posix()
        if rel != ".":
            out.append(rel)
    return out


def _ts_version(packages: "list[Pkg]") -> "str | None":
    for p in packages:
        dep = p.deps.get("typescript")
        if dep and dep[1]:
            return dep[1]
    return None


def discover(repo: Path, start: str = ".") -> WorkspaceMap:
    """The workspace holding `start` (repo-relative), searched upward to the repo root."""
    d = posixpath.normpath(start)
    nearest_pkg: "str | None" = None
    while True:
        if nearest_pkg is None and (repo / pkgjson.manifest(d)).is_file():
            nearest_pkg = d
        found = _workspace_globs(repo, d)
        if found is not None:
            return _from_root(repo, d, *found)
        if d == ".":
            break
        d = posixpath.dirname(d) or "."
    if nearest_pkg is None:
        return WorkspaceMap(root=posixpath.normpath(start), manager="none")
    pkg, notes = pkgjson.load(repo, nearest_pkg)
    return WorkspaceMap(root=nearest_pkg, manager="none", packages=(pkg,) if pkg else (),
                        ts_version=_ts_version([pkg] if pkg else []), notes=tuple(notes),
                        inputs=(pkgjson.manifest(nearest_pkg),))


def _from_root(repo: Path, root: str, manager: str, pats: "list[str]", notes: "list[str]") -> WorkspaceMap:
    notes = list(notes)
    inputs = [pkgjson.manifest(root)] + ([_rel(root, "pnpm-workspace.yaml")] if manager == "pnpm" else [])
    pkgs: "list[Pkg]" = []
    root_pkg, n = pkgjson.load(repo, root)
    notes += n
    if root_pkg is not None:
        pkgs.append(root_pkg)
    for rel in _dirs(repo, root):
        if globs.match(_ALWAYS_OUT, rel + "/x") or not globs.selected(rel, pats):
            continue
        if not (repo / root / rel / "package.json").is_file():
            continue
        d = _rel(root, rel)
        inputs.append(pkgjson.manifest(d))
        pkg, n = pkgjson.load(repo, d)
        notes += n
        if pkg is not None:
            pkgs.append(pkg)
    names: "dict[str, list[str]]" = {}
    for p in pkgs:
        if p.name:
            names.setdefault(p.name, []).append(p.dir)
    dups = {k: tuple(v) for k, v in sorted(names.items()) if len(v) > 1}
    return WorkspaceMap(root=root, manager=manager, packages=tuple(pkgs), ts_version=_ts_version(pkgs),
                        duplicates=dups, notes=tuple(notes), inputs=tuple(inputs))


def owner_of(maps: "list[WorkspaceMap]", rel_path: str) -> "Pkg | None":
    """The owning package across several workspaces (the deepest directory wins)."""
    hits = [p for p in (m.owner(rel_path) for m in maps) if p is not None]
    return max(hits, key=lambda p: (p.dir != ".", len(p.dir)), default=None)
