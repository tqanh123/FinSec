"""Auto-discovery of roots — declare `auto_discover: true` instead of listing.

A deterministic ladder, first match wins. The point: a project without
`src/` or `packages/` — a flat package at the repo root — is a FIRST-CLASS
root, not an error.

WHAT IT CANNOT SEE
    * No branch is verified against reality beyond "this file/dir exists":
      a pyproject that LIES about its layout produces a wrong-but-plausible
      root list. Declare `layout.roots:` when the tree matters.
    * Namespace packages spanning several top-level dirs (PEP 420 trees
      without a workspace declaration) come out as SEPARATE flat roots —
      cross-package edges still resolve (one pass, many roots), but the unit
      naming follows the directory, not the package.
    * Non-Python markers (Cargo/Go-style manifests) are invisible. Register a
      language adapter AND declare roots for such trees.
"""
from __future__ import annotations

import re
from pathlib import Path

from .model import Root


def _py_project_parents(repo: Path) -> dict[Path, list[Path]]:
    """Parent dirs -> their pyproject-bearing children, at depth 1..2.

    Depth 1: `repo/<pkg>/pyproject.toml` (sibling-packages monorepo).
    Depth 2: `repo/packages/<pkg>/pyproject.toml` (the uv-workspace shape
    WITHOUT a workspace declaration in the root pyproject).
    """
    groups: dict[Path, list[Path]] = {}
    for pat in ("*/pyproject.toml", "*/*/pyproject.toml"):
        for p in repo.glob(pat):
            if p.is_file():
                groups.setdefault(p.parent.parent, []).append(p)
    return groups


def _uv_workspace_members(repo: Path) -> list[str] | None:
    """`[tool.uv.workspace] members` globs, read with a fixed-position regex —
    a wrong regex returns None (visible: no workspace root), a full TOML parse
    of a stranger's file would fail the whole discovery on one odd file."""
    pp = repo / "pyproject.toml"
    if not pp.is_file():
        return None
    m = re.search(r"^\s*members\s*=\s*\[(.*?)\]", pp.read_text(encoding="utf-8"), re.S | re.M)
    if not m:
        return None
    return re.findall(r'"([^"]+)"', m.group(1)) or None


def _src_packages(repo: Path) -> list[Path]:
    """Package dirs under `src/` — each becomes its own `src` root."""
    src = repo / "src"
    if not src.is_dir():
        return []
    return sorted(d for d in src.iterdir()
                  if d.is_dir() and not d.name.startswith((".", "_"))
                  and any(d.rglob("*.py")))


def _flat_packages(repo: Path) -> list[Path]:
    """Importable package dirs sitting directly at the repo root (no src/)."""
    return sorted(d for d in repo.iterdir()
                  if d.is_dir() and (d / "__init__.py").is_file()
                  and not d.name.startswith((".", "_")))


def discover_roots(repo: Path) -> tuple[tuple[Root, ...], str]:
    """-> (roots, which-rule-fired). Deterministic ladder:

    1. uv workspace members  -> one `workspace` root per member glob's parent
       (`packages/*` -> root `packages` at `packages`).
    2. `src/<pkg>/` present  -> one `src` root per package dir under src/.
    3. sibling pyprojects     -> one `workspace` root at the repo root
       (two or more `*/pyproject.toml` — a monorepo without a workspace file).
    4. flat `__init__.py` dirs -> one `flat` root per top-level package.
    5. otherwise              -> a single `flat` root at the repo root
       (loose modules; noisy by nature — tighten with `scan.exclude`).
    """
    if not repo.is_dir():
        return (), "repo-missing"

    members = _uv_workspace_members(repo)
    if members:
        parents = sorted({Path(m).parts[0] for m in members
                          if not m.startswith((".", "/")) and Path(m).parts})
        roots = tuple(Root(id=p, path=p, kind="workspace") for p in parents)
        if roots:
            return roots, "uv-workspace"

    pkgs = _src_packages(repo)
    if pkgs:
        return tuple(Root(id=p.name, path=(Path("src") / p.name).as_posix(), kind="src")
                     for p in pkgs), "src-layout"

    groups = _py_project_parents(repo)
    best = max(groups.items(), key=lambda kv: len(kv[1]), default=None)
    if best and len(best[1]) >= 2:
        parent, pps = best
        rel = parent.relative_to(repo).as_posix() if parent != repo else "."
        rid = parent.name if parent != repo else "workspace"
        return (Root(id=rid, path=rel, kind="workspace"),), "sibling-pyprojects"

    flat = _flat_packages(repo)
    if flat:
        return tuple(Root(id=p.name, path=p.name, kind="flat") for p in flat), "flat-packages"

    return (Root(id=repo.name, path=".", kind="flat"),), "repo-flat-fallback"
