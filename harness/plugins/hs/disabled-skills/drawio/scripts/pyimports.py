#!/usr/bin/env python3
"""Extract a Python project's module-import graph as autolayout graph JSON.

Walks a package/project directory, parses each module with `ast`, builds the
intra-project import edges, and (by default) applies transitive reduction so
the diagram stays readable instead of becoming a hairball. The output feeds
autolayout.py:

  python3 pyimports.py myproject -o graph.json
  python3 autolayout.py graph.json -o diagram.drawio

Transitive reduction uses Graphviz `tred` (drops edges implied by a longer
path); pass --no-reduce to keep every import edge. Only intra-project imports
are kept — third-party and stdlib imports are ignored.

Usage: python3 pyimports.py <project_dir> [-o graph.json] [--direction TB|LR] [--no-reduce]
"""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def _project_root_for(path):
    """Nearest ancestor of `path` holding a `codemap.yaml` the shared codegraph
    door can read, or None. Most projects this tool is pointed at carry no such
    file at all -- "no ancestor descriptor" is the ordinary case, not an error."""
    cur = Path(path).resolve()
    while True:
        if (cur / "codemap.yaml").is_file():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


def _under(path, base):
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except ValueError:
        return False


def snapshot_records(project):
    """The shared codegraph snapshot's module records for the files under
    `project`, or None when the door cannot answer for it -- which the caller
    reads as "fall back to the direct scan, unchanged".

    The door only answers when `project` is EXACTLY one of the nearest
    ancestor project's declared `import_roots`: that is the one case where
    the snapshot's own dotted module names already match these tools'
    project-relative naming (bare, no package prefix) -- any other directory
    keeps the AST resolver, which has no equivalent "absolute dotted name" to
    compare its own project-relative one against.

    Returns None, never raises, on every one of: no ancestor `codemap.yaml`
    (the ordinary case for a project the harness is not installed into), the
    vendored `codegraph` package missing beside that descriptor, `project`
    itself a package (`__init__.py` present -- the base-prefix case
    `discover()` already handles), `project` not an exact `import_roots`
    entry, or the snapshot measurement itself failing (no git checkout, or
    any other `CheckError`)."""
    project = Path(project).resolve()
    if (project / "__init__.py").is_file():
        return None
    repo = _project_root_for(project)
    if repo is None:
        return None
    codegraph_scripts = repo / "harness" / "scripts"
    if not (codegraph_scripts / "codegraph").is_dir():
        return None
    if str(codegraph_scripts) not in sys.path:
        sys.path.insert(0, str(codegraph_scripts))
    try:
        from codegraph._core.descriptor import DescriptorError, load
        from codegraph.check.tree import CheckError, measure
    except ImportError:
        return None
    try:
        descriptor = load(repo / "codemap.yaml", repo).descriptor
    except DescriptorError:
        return None
    rel = "." if project == repo else project.relative_to(repo).as_posix()
    if rel not in (descriptor.import_roots or ()):
        return None
    try:
        tree = measure(repo)
    except CheckError:
        return None
    records = tree.records()
    under = [rec for rec in records if _under(repo / rec["path"], project)]
    return (records, under) if under else None


def snapshot_graph(project):
    """(module names, edges) read off the shared codegraph snapshot instead of
    this file's own `discover()`/`edges_of()` AST resolver -- None when
    `snapshot_records` cannot answer for `project`."""
    got = snapshot_records(project)
    if got is None:
        return None
    from codegraph.check.tree import module_graph
    records, under = got
    graph = module_graph(records, keep={rec["module"] for rec in under})
    edges = sorted({(src, dst) for src, dsts in graph.items() for dst in dsts})
    return sorted(graph), edges


def discover(root):
    """Map dotted module name -> file path for every .py under root, plus the
    package prefix. If root is itself a package (has __init__.py), module names
    are qualified with its name so the project's own absolute imports resolve."""
    root = os.path.abspath(root)
    base = os.path.basename(root) if os.path.exists(os.path.join(root, "__init__.py")) else ""
    modules = {}
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root)[:-3]  # strip .py
            parts = rel.split(os.sep)
            if parts[-1] == "__init__":
                parts = parts[:-1]                                       # package = its dir
            parts = ([base] + parts) if base else parts
            if parts:
                modules[".".join(parts)] = os.path.join(dirpath, fn)
    return modules, base


def resolve(name, current, modules):
    """Resolve a dotted name to the longest known module prefix (or None)."""
    parts = name.split(".") if name else []
    while parts:
        cand = ".".join(parts)
        if cand in modules and cand != current:
            return cand
        parts = parts[:-1]
    return None


def edges_of(name, path, modules):
    """Intra-project modules imported by `name`."""
    pkg = name if path.endswith("__init__.py") else name.rsplit(".", 1)[0] if "." in name else ""
    found = set()
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except SyntaxError:
        return found
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):                     # import a.b.c
            for alias in node.names:
                target = resolve(alias.name, name, modules)
                if target:
                    found.add(target)
        elif isinstance(node, ast.ImportFrom):               # from a.b import c
            if node.level:                                   # relative: climb level-1 packages
                base = pkg.split(".") if pkg else []
                base = base[: len(base) - (node.level - 1)]
                prefix = ".".join(base)
                mod = f"{prefix}.{node.module}" if prefix and node.module else (node.module or prefix)
            else:
                mod = node.module or ""
            target = resolve(mod, name, modules)
            if target:
                found.add(target)
            for alias in node.names:                         # `from pkg import submodule`
                sub = f"{mod}.{alias.name}" if mod else alias.name
                target = resolve(sub, name, modules)
                if target:
                    found.add(target)
    return found


def transitive_reduce(nodes, edges):
    """Drop edges implied by a longer path, via Graphviz `tred`."""
    idx = {n: i for i, n in enumerate(nodes)}
    dot = "digraph{" + "".join(f"{idx[s]}->{idx[t]};" for s, t in edges) + "}"
    try:
        out = subprocess.run(["tred"], input=dot, capture_output=True,
                             text=True, check=True).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        sys.stderr.write(f"warning: tred unavailable, keeping all edges ({exc})\n")
        return edges
    rev = {i: n for n, i in idx.items()}
    return [(rev[int(a)], rev[int(b)]) for a, b in re.findall(r"(\d+)\s*->\s*(\d+)", out)]


def main():
    ap = argparse.ArgumentParser(description="Python import graph -> autolayout graph JSON.")
    ap.add_argument("project", help="package or project directory")
    ap.add_argument("-o", "--output", help="output JSON path (default: stdout)")
    ap.add_argument("--direction", default="TB", choices=["TB", "LR"])
    ap.add_argument("--group", action="store_true",
                    help="group nodes into containers by sub-package")
    ap.add_argument("--no-reduce", action="store_true",
                    help="keep every edge (skip transitive reduction)")
    args = ap.parse_args()

    via_door = snapshot_graph(args.project)
    if via_door is not None:
        node_names, edges = via_door
        modules, base = {m: None for m in node_names}, ""
    else:
        modules, base = discover(args.project)
        if not modules:
            sys.exit(f"error: no .py modules found under {args.project}")
        edges = sorted({(name, t) for name, path in modules.items()
                        for t in edges_of(name, path, modules)})
    raw = len(edges)
    if not args.no_reduce:
        edges = transitive_reduce(list(modules), edges)
    # Drop the shared package prefix from labels for readability; ids stay full.
    strip = base + "." if base else ""
    label = lambda m: m[len(strip):] if strip and m.startswith(strip) else m

    def node(m):
        d = {"id": m, "label": label(m)}
        if args.group:
            rest = label(m).split(".")
            if len(rest) > 1:                            # nested under a sub-package
                d["group"] = "/".join(rest[:-1])         # full sub-package path -> nested boxes
        return d

    graph = {
        "direction": args.direction,
        "nodes": [node(m) for m in modules],
        "edges": [{"source": s, "target": t} for s, t in edges],
    }
    text = json.dumps(graph, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        sys.stderr.write(f"wrote {args.output}\n")
    else:
        sys.stdout.write(text)
    note = "" if args.no_reduce else f" (reduced from {raw})"
    source = " [snapshot]" if via_door is not None else ""
    sys.stderr.write(f"{len(modules)} modules, {len(edges)} edges{note}{source}\n")


if __name__ == "__main__":
    main()
