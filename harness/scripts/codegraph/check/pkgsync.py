"""hs-code check pkgsync — each workspace package declares exactly what it imports.

A package's `[project.dependencies]` is a contract, red BOTH ways: importing what it
does not declare breaks an install from its wheel; declaring what it never imports
drags a ghost into every install. The packages are the workspace roots' (found by the
snapshot's own probe); the project's section is `[tool.<namespace_root>]`.

LAWS (each red on its own)
    missing            imports a workspace package it does not declare
    stale              declares a workspace package no edge reaches (a TYPE_CHECKING-only
                       import is declared typing intent: it exempts, and never makes `missing`)
    test_only          declares, as a runtime dependency, something only the package's tests
                       import — it rides into every install; move it to a test group
    third_missing      imports a non-stdlib third-party module it does not declare
    third_stale        declares a third-party dependency nothing imports
    ns_unknown         declares `<ns>-x` that is neither a workspace package nor listed in
                       `[tool.<ns>] external_deps` (the listing exempts from this law only:
                       an external dependency nothing imports is still `third_stale`)
    ext_stale          `external_deps` names no declared dependency (a dead exception)
    version_conflict   one workspace package declared with differing specs across packages
    plugin_undeclared  `[tool.<ns>] plugins` names a package that is not declared (a plugin
                       loaded through ITS entry point also exempts from `stale`)
    dev_unused         an optional dependency or `[dependency-groups]` entry nothing imports,
                       code or tests — WARN only, never red

Names compare PEP 503-normalized. A package IS its `[project].name`, never its directory
(a dir `core` shipping `acme-core` is what `acme-core` in a dependency list means). An import maps to its PyPI name by the longest dotted
owner the repo's `.venv` reports (its installed files, longest dotted prefix), else the
shipped table — `codegraph._core.dist_names`; a package's `[tool.<ns>] import_names` wins
over both. An import some builds share (`cv2`) is satisfied by declaring any of them.
`[project.scripts]` / `[project.entry-points]` targets count as edges. A package's tests
are the files under its directory in a test tree (`layout.tests_subdir`, else the
`tests?(_*)` convention), wherever its code dir is: they are not package modules, and
their imports count only toward a test group or `test_only`.

WHAT IT CANNOT SEE
    * `importlib.import_module`, `__import__`, string-built names: a dependency used only
      that way shows as stale.
    * Transitive needs; whether `[build-system]` actually builds.

`--dist NAME` keeps the rows of one package (its `[project].name`, PEP 503, or its
directory) -- every package is still measured, since `stale` and `version_conflict` read
the whole workspace; a `version_conflict` row stays when the package takes part in it.
A name no package answers to refuses with the near ones.

EXIT  0 clean · 1 red · 2 cannot run (no workspace, no package, TOML unreadable, a
      `--dist` naming no package)
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import attrs

from ambiguous_input import Ambiguous
from codegraph._core import dist_names
from codegraph.check.tree import CheckError, Package, workspace
from hs_family.cli import Command

LAWS = ("missing", "stale", "test_only", "third_missing", "third_stale", "ns_unknown",
        "ext_stale", "version_conflict", "plugin_undeclared")


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def dep_name(spec: str) -> str:
    m = re.match(r"[A-Za-z0-9._-]+", spec.strip())
    return m.group(0) if m else ""


@attrs.define(kw_only=True)
class Dist:
    pkg: Package
    name: str
    deps: Dict[str, str] = attrs.Factory(dict)
    dev: Set[str] = attrs.Factory(set)
    external: Set[str] = attrs.Factory(set)
    plugins: Set[str] = attrs.Factory(set)
    names: Dict[str, str] = attrs.Factory(dict)
    modules: Set[str] = attrs.Factory(set)
    targets: Set[str] = attrs.Factory(set)
    typing: Set[str] = attrs.Factory(set)
    test_targets: Set[str] = attrs.Factory(set)
    unread: List[str] = attrs.Factory(list)


def declare(pkg: Package, ns: str) -> Dist:
    pp = pkg.dir / "pyproject.toml"
    try:
        toml = tomllib.loads(pp.read_text(encoding="utf-8"))
    except OSError as e:
        raise CheckError("%s: %s" % (pp, e))
    except tomllib.TOMLDecodeError as e:
        raise CheckError("%s: unparseable TOML: %s" % (pp, e))
    proj = toml.get("project") or {}
    if not isinstance(proj.get("name"), str) or not proj["name"].strip():
        raise CheckError("%s: no [project].name — a package is known by that name, not its directory" % pp)
    tool = (toml.get("tool") or {}).get(ns) or {}
    d = Dist(pkg=pkg, name=proj["name"].strip(), external=set(tool.get("external_deps") or ()), plugins=set(tool.get("plugins") or ()),
             names={str(k): str(v) for k, v in (tool.get("import_names") or {}).items()})
    for raw in proj.get("dependencies") or []:
        if dep_name(raw):
            d.deps[dep_name(raw)] = raw
    groups = []
    for table in (proj.get("optional-dependencies"), toml.get("dependency-groups")):
        if isinstance(table, dict):
            groups += [g for g in table.values() if isinstance(g, list)]
    for group in groups:
        # a `{include-group = ...}` entry names another group, not a dependency
        d.dev.update(dep_name(x) for x in group if isinstance(x, str) and dep_name(x))
    eps = dict(proj.get("entry-points") or {})
    eps["console-scripts"] = dict(proj.get("scripts") or {})
    d.targets.update(t.split(":", 1)[0].strip() for g in eps.values() for t in g.values() if t.split(":", 1)[0].strip())
    return d


def measure(d: Dist, layout=None, listed: "Optional[Set[Path]]" = None) -> None:
    """Every import of every module under the package's code dir, both levels, and of
    every file in a test tree under the package dir (`layout`: a `FilesLayout`; None
    reads the undeclared convention). A file is read when the snapshot would read it:
    in `listed` (what git tracks or would track — None outside git) and under no
    directory a graph walk skips. Test-tree membership is judged below the code dir
    for a file inside it, so a code dir that itself sits under a test-shaped name is
    still code. A test file that does not parse is skipped and named, never fatal.
    The parse is the snapshot's own adapter, so relative imports resolve the same way."""
    from codegraph._core.adapter import adapter_for
    from codegraph._core.testfiles import in_test_tree
    from codegraph._core.walk import skipped
    is_test = layout.in_test_tree if layout is not None else in_test_tree
    code, top = d.pkg.code, d.pkg.dir
    for path in sorted(top.rglob("*.py")):
        rel = path.relative_to(top)
        if skipped(rel.parts) or any(x.startswith(".") for x in rel.parts[:-1]):
            continue
        if listed is not None and path.resolve() not in listed:
            continue
        in_code = code == top or code in path.parents
        test = is_test((path.relative_to(code) if in_code else rel).as_posix())
        if not test and not in_code:
            continue
        parts = (rel if test else path.relative_to(code)).with_suffix("").parts
        is_init = parts[-1] == "__init__"
        mod = parts[:-1] if is_init else parts
        if not mod:
            continue
        if not test:
            d.modules.add(".".join(mod))
        try:
            text = path.read_bytes().decode("utf-8", errors="strict" if not test else "replace")
            parsed = adapter_for(path).parse(path, text,
                                             tuple(mod if is_init else mod[:-1]), ".".join(mod))
        except (SyntaxError, UnicodeDecodeError) as e:
            if test:
                d.unread.append("%s  test file %s does not parse (%s); its imports are not "
                                "read" % (d.name, rel.as_posix(), type(e).__name__))
                continue
            raise CheckError("%s does not parse: %s" % (path, e))
        for rec in parsed.imports:
            for r in rec.resolved:
                if r:
                    sink = (d.test_targets if test else
                            d.typing if rec.under_type_checking else d.targets)
                    sink.add(".".join(r))


def edges(d: Dist, targets: Set[str], provided: Dict[str, str],
          installed: "Optional[dist_names.Installed]" = None) -> Tuple[Set[str], Set[Tuple[str, ...]]]:
    """(workspace owners, third-party candidate tuples) the targets reach."""
    ws: Set[str] = set()
    third: Set[Tuple[str, ...]] = set()
    for t in targets:
        parts = t.split(".")
        owner = next((provided[".".join(parts[:i])] for i in range(len(parts), 0, -1)
                      if ".".join(parts[:i]) in provided), None)
        if owner is not None:
            if owner != norm(d.name):
                ws.add(owner)
        elif parts[0] not in sys.stdlib_module_names and parts[0] != "__future__":
            third.add(dist_names.candidates(t, d.names, installed))
    return ws, third


def _names(cands: Set[Tuple[str, ...]]) -> Set[str]:
    return {norm(c) for cs in cands for c in cs}


def judge(dists: List[Dist], ns: str, only: Optional[str] = None,
          installed: "Optional[dist_names.Installed]" = None) -> Tuple[Dict[str, List[str]], List[str]]:
    """`only` (a normalized package name) keeps that package's rows; every package is
    still judged, so what counts as stale or conflicting is the whole workspace's."""
    red: Dict[str, List[str]] = {k: [] for k in LAWS}
    warn: List[str] = []
    # every workspace-side comparison is on the normalized name; messages keep the spelling
    names = {norm(d.name) for d in dists}
    provided: Dict[str, str] = {}
    for d in dists:
        for m in d.modules:
            provided.setdefault(m, norm(d.name))
    specs: Dict[str, List[Tuple[str, str]]] = {}
    for d in sorted(dists, key=lambda x: norm(x.name)):
        n = d.name
        ws, third = edges(d, d.targets, provided, installed)
        tws, tthird = edges(d, d.typing, provided, installed)
        test_ws, test_third = edges(d, d.test_targets, provided, installed)
        test_used = test_ws | _names(test_third)
        have = {norm(x) for x in d.deps}
        declared_ws = {x for x in d.deps if norm(x) in names}
        declared_third = set(d.deps) - declared_ws
        for dep in declared_ws:
            specs.setdefault(norm(dep), []).append((n, d.deps[dep]))
        if only is not None and norm(n) != only:
            continue
        plugins = {norm(x) for x in d.plugins}
        red["missing"] += ["%s  imports `%s` but does not declare it" % (n, x) for x in sorted(ws - have)]
        unreached = [x for x in sorted(declared_ws) if norm(x) not in ws | tws | plugins]
        red["stale"] += ["%s  declares `%s`; no edge reaches it" % (n, x)
                         for x in unreached if norm(x) not in test_used]
        red["test_only"] += [_test_only(n, x) for x in unreached if norm(x) in test_used]
        red["plugin_undeclared"] += ["%s  plugins lists `%s`, which it does not declare" % (n, x)
                                     for x in sorted(d.plugins) if norm(x) not in have]
        red["third_missing"] += ["%s  imports `%s` without declaring it%s"
                                 % (n, cs[0], " (or any of: %s)" % ", ".join(cs[1:]) if cs[1:] else "")
                                 for cs in sorted(third) if not any(norm(c) in have for c in cs)]
        used = _names(third | tthird)
        unused = [x for x in sorted(declared_third) if norm(x) not in used and x not in d.dev]
        red["third_stale"] += ["%s  declares `%s`; nothing imports it" % (n, x)
                               for x in unused if norm(x) not in test_used]
        red["test_only"] += [_test_only(n, x) for x in unused if norm(x) in test_used]
        red["ext_stale"] += ["%s  external_deps lists `%s`, which is no dependency" % (n, x)
                             for x in sorted(d.external) if norm(x) not in have]
        external = {norm(x) for x in d.external}
        if ns:
            red["ns_unknown"] += ["%s  declares `%s`: prefix `%s-`, yet no workspace package has that "
                                  "name (list it in external_deps if it is external)" % (n, x, ns)
                                  for x in sorted(declared_third) if norm(x).startswith(norm(ns) + "-") and norm(x) not in external]
        warn += ["%s  optional dependency `%s` unused" % (n, x)
                 for x in sorted(d.dev) if norm(x) not in used | ws | test_used]
        warn += d.unread
    for dep, got in sorted(specs.items()):
        if only is not None and only != dep and only not in {norm(pkg) for pkg, _ in got}:
            continue
        # the spec after the name: `Acme_Core>=1` and `acme-core>=1` are one spec
        if len({re.sub(r"\s+", "", raw.split(";")[0].strip()[len(dep_name(raw)):]) for _, raw in got}) > 1:
            red["version_conflict"].append("`%s` declared with differing specs: %s"
                                           % (dep, ", ".join("%s:%r" % g for g in sorted(got))))
    return {k: v for k, v in red.items() if v}, warn


def _test_only(pkg: str, dep: str) -> str:
    return ("%s  declares `%s` as a runtime dependency; only its tests import it — move it "
            "to a test group ([project.optional-dependencies] or [dependency-groups])" % (pkg, dep))


def pick_dist(dists: List[Dist], ask: str) -> Dist:
    """The package `ask` names: its `[project].name` (PEP 503) or its directory. A name
    no package answers to is a refusal listing the near ones -- never an empty report
    that reads as a green package."""
    hits = [d for d in dists if norm(d.name) == norm(ask) or d.pkg.dir.name == ask]
    if len(hits) == 1:
        return hits[0]
    names = sorted(d.name for d in dists)
    if hits:
        raise Ambiguous("dist", ask, (d.name for d in hits), "pass its [project].name")
    near = [n for n in names if norm(ask) in norm(n)]
    near += difflib.get_close_matches(ask, names, n=5, cutoff=0.6)
    raise Ambiguous("no workspace package named %r; near" % ask, "", near or names,
                    "--dist takes a package's [project].name or its directory name")


def _pkgsync_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--list", action="store_true", help="print every violation")
    ap.add_argument("--dist", default=None, metavar="NAME",
                    help="only this package's rows: its [project].name or its directory "
                         "(every package is still measured)")


def _pkgsync(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    try:
        descriptor, packages = workspace(Path(root))
        ns = descriptor.namespace_root or ""
        pkgs = [p for p in packages if (p.dir / "pyproject.toml").is_file()]
        if not pkgs:
            raise CheckError("no workspace package with a pyproject.toml (a `workspace` root in "
                             "codemap.yaml `layout.roots`): zero packages is not zero violations")
        dists = [declare(p, ns) for p in pkgs]
        from codegraph._core.snapshot.pipeline import git_listed
        from codegraph._core.testfiles import build_test_layout
        layout = build_test_layout(Path(root), descriptor)
        rels = git_listed(Path(root))
        listed = None if rels is None else {(Path(root) / r).resolve() for r in rels}
        for d in dists:
            measure(d, layout, listed)
        installed, source = dist_names.installed_modules(Path(root))
    except CheckError as e:
        print("hs-code check pkgsync: %s" % e)
        return 2
    picked: Optional[Dist] = None
    if args.dist is not None:
        try:
            picked = pick_dist(dists, args.dist)
        except Ambiguous as e:
            return e.refuse(opts)
    red, warn = judge(dists, ns, only=norm(picked.name) if picked else None, installed=installed)
    if picked is not None:
        warn = [w for w in warn if w.startswith(picked.name + "  ")]
    if getattr(opts, "json", False):
        print(json.dumps({"schema": "hs-code.check.pkgsync/1", "packages": len(dists), "red": red,
                          "warn": warn, "dist": picked.name if picked else None,
                          "import_names": source}, sort_keys=True))
        return 1 if red else 0
    print("pkgsync — %s" % ("%s (1 of %d package(s))" % (picked.name, len(dists)) if picked
                             else "%d package(s)" % len(dists)))
    print("  import names: %s" % source)
    for law in LAWS:
        items = red.get(law, [])
        print("  %4d  %s" % (len(items), law))
        for x in items if args.list else items[:5]:
            print("          %s" % x)
        if not args.list and len(items) > 5:
            print("          … %d more (--list)" % (len(items) - 5))
    for x in warn:
        print("  WARN  %s" % x)
    print("RED: %d violation(s)." % sum(map(len, red.values())) if red
          else "GREEN: every package declares exactly what it imports.")
    return 1 if red else 0


PKGSYNC = Command(add_args=_pkgsync_args, run=_pkgsync)
