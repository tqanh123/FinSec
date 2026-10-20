"""hs-code test fixture-shadow -- a fixture name defined twice along one
conftest chain. Port of ob-tools' `testkit.analysis.fixture_shadow` (pinned
commit `60075cb`).

pytest resolves a fixture by NAME, nearest definition wins, and a second
definition of the same name never produces a warning. The loser stops
existing. A test that meant to use it still runs, against someone else's
tree, and passes — so the failure mode is a green test asserting nothing it
intended.

TWO SHAPES, and they are not the same finding:

  same_file  Two definitions in one module. Python itself keeps only the last;
             there is no case where this is intended, and no reason to allow it.
  chain      A child conftest redefining a parent's name. pytest calls this
             legal and it IS a real technique — narrowing a fixture for one
             subtree. So it is REPORTED, and cleared by an allow-list entry
             that states why, rather than banned.

RED BOTH WAYS. A new shadow reds. An allow-list entry whose shadow no longer
exists also reds: a list nobody prunes stops describing the tree, and then
the first direction is asserting against fiction.

DIRECTORIES, GENERALISED
    Upstream took one caller-typed `TESTS_DIR`. This repo's own
    `codemap.yaml` declares SIX flat `layout.roots` and no single repo-root
    `tests/` at all. With `tests_dir` omitted, `find_shadows_multi` fans the
    SAME tree-scoped scan (`_shadows_in_tree`) over every declared root
    (`codegraph.test.clone_scan.default_scan_roots` — one door, not a second
    copy of "declared roots as directories") independently: none of this
    repo's roots has a conftest.py ABOVE it, so a conftest CHAIN never
    crosses a root boundary here, and scanning each root on its own is
    exactly what pytest would see. Passing `tests_dir` explicitly keeps
    upstream's literal single-tree behaviour.

WHAT IT CANNOT SEE
    * `request.getfixturevalue(name)` and parametrised indirection resolve by
      string at runtime; a name built that way is invisible here.
    * Fixtures injected by a pytest PLUGIN (entry-point, `pytest_plugins`) are
      not in this tree's conftest files, so a plugin fixture shadowed by a local
      one reads as no shadow at all.
    * A fixture defined inside a test MODULE (not a conftest) is visible only
      there; those are scanned too, but a module never shadows a sibling module,
      so only its own file and its conftest ancestors can collide with it.
    * The decorator is matched by NAME (`pytest.fixture`, `fixture`). A fixture
      produced by a helper that wraps the decorator is not seen.
    * The multi-root default treats each declared root as its OWN conftest
      universe: a project that puts a conftest.py ABOVE its declared roots
      (this repo does not) would have a real chain this scan cannot see —
      pass `tests_dir` explicitly to scan that boundary directly.

USAGE
    hs-code test fixture-shadow
    hs-code test fixture-shadow TESTS_DIR [--allow allow.yaml]
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

import hs_family
import yaml_io

from codegraph.test.clone_scan import default_scan_roots
from hs_family.cli import Command

#: Decorator spellings that make a function a fixture.
_FIXTURE_ATTR = "fixture"


def _is_fixture(node: "ast.FunctionDef | ast.AsyncFunctionDef") -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if (isinstance(target, ast.Attribute) and target.attr == _FIXTURE_ATTR) \
                or (isinstance(target, ast.Name) and target.id == _FIXTURE_ATTR):
            return True
    return False


def _fixture_defs(path: Path) -> "list[tuple[str, int, str]]":
    """(name, lineno, scope) for every @pytest.fixture in this file, in file
    order. `scope` is the dotted class/function path the definition sits in
    ("" at module level): two classes each defining `cfg` keep both, so only
    a name repeated inside ONE scope is a same-file loss."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    out: "list[tuple[str, int, str]]" = []

    def walk(parent: ast.AST, scope: str) -> None:
        for node in ast.iter_child_nodes(parent):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if _is_fixture(node):
                    out.append((node.name, node.lineno, scope))
                walk(node, f"{scope}.{node.name}" if scope else node.name)
            elif isinstance(node, ast.ClassDef):
                walk(node, f"{scope}.{node.name}" if scope else node.name)
            else:
                walk(node, scope)

    walk(tree, "")
    return sorted(out, key=lambda row: row[1])


def _chain(path: Path, root: Path) -> "list[Path]":
    """The conftest files pytest would consult for `path`, outermost first."""
    out: "list[Path]" = []
    cur = path.parent
    while True:
        cand = cur / "conftest.py"
        if cand.is_file() and cand != path:
            out.append(cand)
        if cur == root:
            break
        cur = cur.parent
        if root not in cur.parents and cur != root:
            break
    return list(reversed(out))


def _shadows_in_tree(root: Path) -> "list[dict]":
    """Every same_file/chain shadow under `root`, sites relative to `root`
    itself and UNRESOLVED against any allow-list — the tree-scoped half both
    `find_shadows` (one tree) and `find_shadows_multi` (many, sites re-based
    afterwards) build on."""
    files = sorted(p for p in root.rglob("*.py") if p.is_file())
    defs = {p: _fixture_defs(p) for p in files}

    shadows: "list[dict]" = []
    for path, rows in defs.items():
        rel = path.relative_to(root).as_posix()
        # (a) same file, same scope: Python already dropped every definition
        # but the last.
        by_name: "dict[tuple[str, str], list[int]]" = {}
        for name, line, scope in rows:
            by_name.setdefault((name, scope), []).append(line)
        for (name, _scope), lines in by_name.items():
            if len(lines) > 1:
                shadows.append({"name": name, "kind": "same_file",
                                "sites": [f"{rel}:{ln}" for ln in lines]})
        # (b) the chain: an ancestor conftest defining the same name. Every
        # (ancestor, file) pair is its own entry — the allow-list reason for a
        # name has to hold at each of them.
        mine: "dict[str, int]" = {}
        for name, line, _scope in rows:
            mine.setdefault(name, line)
        for anc in _chain(path, root):
            for name, line, _scope in defs.get(anc, []):
                if name in mine:
                    shadows.append({"name": name, "kind": "chain",
                                    "sites": [f"{anc.relative_to(root).as_posix()}:{line}",
                                              f"{rel}:{mine[name]}"]})

    shadows.sort(key=lambda s: (s["name"], s["kind"], s["sites"]))
    return shadows


def _apply_allow(shadows: "list[dict]", allow: "dict[str, str] | None") -> dict:
    """The allow-list verdict over an already-computed shadow list — the
    root-agnostic half of `find_shadows`, reused by `find_shadows_multi` so
    one shared allow-list is judged once, over the MERGED shadow set, never
    once per root (a root where a name is not found would otherwise mark a
    perfectly-live allow entry `stale`)."""
    allow = allow or {}
    found = {s["name"] for s in shadows}
    # An allow entry only counts when it says WHY. A blank reason is a name on
    # a list, which is what this gate exists to stop being enough.
    reasoned = {k for k, v in allow.items() if (v or "").strip()}
    unreasoned = sorted(set(allow) - reasoned)
    new = [s for s in shadows
           if s["name"] not in reasoned or s["kind"] == "same_file"]
    stale = sorted(k for k in allow if k not in found)
    return {
        "shadows": shadows, "new": new, "stale": stale,
        "unreasoned": unreasoned,
        "green": not new and not stale and not unreasoned,
        "blind": ("request.getfixturevalue and parametrised indirection resolve "
                  "by string at runtime; plugin-provided fixtures are not in "
                  "this tree; a decorator produced by a helper is not matched"),
    }


def find_shadows(tests_dir: "Path | str", allow: "dict[str, str] | None" = None) -> dict:
    """Shadowed fixture names under `tests_dir`, plus the allow-list verdict."""
    root = Path(tests_dir).resolve()
    return _apply_allow(_shadows_in_tree(root), allow)


def find_shadows_multi(repo_root: Path, scan_roots: "list[Path]",
                       allow: "dict[str, str] | None" = None) -> dict:
    """The same door, fanned over every one of `scan_roots` independently —
    sites are re-based to be relative to `repo_root` (prefixed with the
    scanning root's own repo-relative path) so a merged report names each
    site unambiguously even when two roots both happen to hold a
    `conftest.py:1`."""
    repo_root = repo_root.resolve()
    shadows: "list[dict]" = []
    for base in scan_roots:
        if not base.is_dir():
            continue
        base = base.resolve()
        prefix = base.relative_to(repo_root).as_posix()
        for s in _shadows_in_tree(base):
            shadows.append({**s, "sites": [f"{prefix}/{site}" for site in s["sites"]]})
    shadows.sort(key=lambda s: (s["name"], s["kind"]))
    return _apply_allow(shadows, allow)


def render(result: dict) -> str:
    lines = []
    for s in result["shadows"]:
        mark = "RED " if s in result["new"] else "allow"
        lines.append(f"  [{mark}] {s['kind']:<9} {s['name']}  —  "
                     + " , ".join(s["sites"]))
    for name in result["stale"]:
        lines.append(f"  [RED ] stale     {name}  —  allowed, but no such shadow")
    for name in result["unreasoned"]:
        lines.append(f"  [RED ] no-reason {name}  —  allow entry states no reason")
    lines.append(f"fixture-shadow: {len(result['shadows'])} shadow(s) · "
                 f"{len(result['new'])} RED new · {len(result['stale'])} stale · "
                 f"{'green' if result['green'] else 'RED'}")
    lines.append(f"  blind: {result['blind']}")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("tests_dir", nargs="?", default=None,
                    help="tests root to scan, repo-relative (default: every declared "
                         "layout.roots directory, scanned independently)")
    sp.add_argument("--allow", default=None,
                    help="yaml mapping fixture name -> reason it may shadow")


def _fixture_shadow(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    root = Path(root).resolve()
    as_json = bool(getattr(opts, "json", False))

    allow: dict = {}
    if args.allow:
        try:
            allow = yaml_io.yaml_load(Path(args.allow).read_text(encoding="utf-8")) or {}
        except (OSError, yaml_io.YAMLError) as e:
            raise hs_family.UsageError(f"fixture-shadow: {e}") from e

    if args.tests_dir is None:
        from codegraph.check.tree import workspace
        descriptor, _packages = workspace(root)
        scan_roots = default_scan_roots(root, descriptor)
        result = find_shadows_multi(root, scan_roots, allow)
    else:
        tdir = Path(args.tests_dir)
        tdir = tdir if tdir.is_absolute() else root / tdir
        if not tdir.is_dir():
            raise hs_family.UsageError(f"fixture-shadow: not a directory: {tdir}")
        result = find_shadows(tdir, allow)

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    else:
        print(render(result))
    return 0 if result["green"] else 1


FIXTURE_SHADOW = Command(add_args=add_arguments, run=_fixture_shadow)
