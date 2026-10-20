"""The flow tracer: read a run's flow out of the code — the node graph, from
the BUILD calls, never from names. Pure stdlib over one snapshot index and the
tree it measured. ONE door for both readers: `hs-code port flow-trace` and the
Flows section of `hs-code scaffold slice-docs` (two readers are how a figure
and a printed number drift apart with no gate catching it).

Ported from `ob-tools@60075cb:packages/portkit/src/portkit/doc/flow_trace.py`.
Design and invariants carry over; one change: the CALL NAMES that declare a
flow are data (`harness/data/flow-builders.yaml`, extendable per declaration
under `flow_builders:`), not the four LangGraph method names hard-coded in the
reader — a second builder library with the same calling shape is a table row,
not a second reader. A flow names its builder (`builder:`, default
`langgraph`).

WHY
    A dependency map answers "what depends on what", not "where does one run
    pass through". A graph builder's flow is DECLARED in code — entry, node,
    direct edge, branching edge — a readable fact, not inferred from names.

DESTINATION TABLES — two shapes, both resolve: an inline dict, or a module
constant in the SAME file. A constant in another file does not resolve and is
printed under `unresolved`, never dropped: a missing edge reads exactly like a
nonexistent one. A framework sentinel (`END`, `START`) is a node only when the
file imports it from the builder's own framework.

WHAT IT CANNOT SEE
    * Only the STATICALLY DECLARED graph: a node added in a loop over a
      runtime list does not show (a `for name, fn in REG.items()` loop over a
      same-file or imported dict comprehension does).
    * Which edge is actually taken — every declared destination is drawn.
    * Order inside a stage.
    * Flows outside the graph (cron jobs, webhooks) need their own entry.
    * A home by NAME lookup across the repo: there is none on purpose — a name
      matching two modules yields a dist that looks right and is wrong; a `?`
      is readable.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Optional

_DATA = Path(__file__).resolve().parent.parent / "data" / "flow-builders.yaml"
_ROLES = ("entry", "node", "edge", "branch")
DEFAULT_BUILDER = "langgraph"
SENTINEL_NODES = frozenset({"END", "START"})


class Missing(Exception):
    """The declaration itself is wrong or cannot be read — never zero flows."""


def load_builders(extra: "Optional[dict]" = None) -> "dict[str, dict]":
    """The shipped builder table, with `extra` (a declaration's own
    `flow_builders:`) laid over it by name. Every row names the four roles
    and its framework; a malformed row is `Missing`."""
    import yaml_io
    table = dict((yaml_io.safe_load(_DATA.read_text(encoding="utf-8")) or {}).get("builders") or {})
    table.update(extra or {})
    for name, row in table.items():
        if not isinstance(row, dict) or any(not isinstance(row.get(r), str) for r in _ROLES) \
                or not isinstance(row.get("framework"), str):
            raise Missing("flow builder %r needs string `framework`, %s" % (
                name, ", ".join("`%s`" % r for r in _ROLES)))
    return table


class Flow:
    """One extracted flow: nodes, edges, entry, and what did NOT resolve."""

    def __init__(self, fid: str, name: str) -> None:
        self.id, self.name = fid, name
        self.entry: "str | None" = None
        self.impl: "dict[str, str]" = {}
        self.edges: "list[tuple[str, str, str]]" = []
        self.routers: "dict[str, str]" = {}
        self.unresolved: "list[str]" = []
        #: registry dicts whose `.items()` loop registers nodes
        self.registrars: "set[str]" = set()

    def miss(self, line: int, what: str, expr: str) -> None:
        self.unresolved.append("line %d: %s — `%s`" % (line, what, expr))

    @property
    def nodes(self) -> "list[str]":
        seen = list(self.impl)
        for a, b, _ in self.edges:
            for x in (a, b):
                if x not in seen:
                    seen.append(x)
        return seen


class Names:
    """Resolve an expression to a STRING: an inline constant, a same-file
    module constant, or a framework sentinel the file really imports."""

    def __init__(self, tree: ast.Module, framework: str) -> None:
        self.strs: "dict[str, str]" = {}
        self.dicts: "dict[str, dict]" = {}
        self.sentinels: "set[str]" = set()
        for n in tree.body:
            if isinstance(n, ast.ImportFrom) and (n.module or "").startswith(framework):
                for al in n.names:
                    if al.name in SENTINEL_NODES and not al.asname:
                        self.sentinels.add(al.name)
            if not isinstance(n, ast.Assign):
                continue
            targets = [t.id for t in n.targets if isinstance(t, ast.Name)]
            if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                for t in targets:
                    self.strs[t] = n.value.value
            elif isinstance(n.value, ast.Dict):
                pairs = {k.value: v for k, v in zip(n.value.keys, n.value.values)
                         if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if pairs:
                    for t in targets:
                        self.dicts[t] = pairs

    def string(self, n: "ast.AST | None") -> "str | None":
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in self.sentinels:
                return n.id
            return self.strs.get(n.id)
        return None

    def table(self, n: ast.AST) -> "dict | None":
        if isinstance(n, ast.Dict):
            return {k.value: v for k, v in zip(n.keys, n.values)
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if isinstance(n, ast.Name):
            return self.dicts.get(n.id)
        return None


def trace_builder(tree: ast.Module, spec: dict, builder: dict) -> Flow:
    """Read the builder's entry / node / edge / branch calls off one module."""
    f = Flow(str(spec.get("id") or "?"), str(spec.get("name") or ""))
    nm = Names(tree, builder["framework"])
    role = {builder[r]: r for r in _ROLES}
    key_vars: "dict[str, str]" = {}
    for n in ast.walk(tree):
        if (isinstance(n, ast.For) and isinstance(n.target, ast.Tuple)
                and len(n.target.elts) == 2 and isinstance(n.target.elts[0], ast.Name)
                and isinstance(n.iter, ast.Call)
                and isinstance(n.iter.func, ast.Attribute) and n.iter.func.attr == "items"
                and isinstance(n.iter.func.value, ast.Name)):
            key_vars[n.target.elts[0].id] = n.iter.func.value.id
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
            continue
        kind, a, ln = role.get(n.func.attr), n.args, n.lineno
        if kind == "entry" and a:
            f.entry = nm.string(a[0])
            if f.entry is None:
                f.miss(ln, "entry point", ast.unparse(a[0]))
        elif kind == "node" and len(a) >= 2:
            node = nm.string(a[0])
            if node is None and isinstance(a[0], ast.Name) and a[0].id in key_vars:
                f.registrars.add(key_vars[a[0].id])
                continue
            if node is None:
                f.miss(ln, "node name", ast.unparse(a[0]))
            else:
                f.impl[node] = ast.unparse(a[1])
        elif kind == "edge" and len(a) >= 2:
            src_n, dst_n = nm.string(a[0]), nm.string(a[1])
            if src_n is None or dst_n is None:
                f.miss(ln, "direct edge", ast.unparse(n))
            else:
                f.edges.append((src_n, dst_n, "direct"))
        elif kind == "branch" and len(a) >= 3:
            src_n, tbl = nm.string(a[0]), nm.table(a[2])
            if src_n is None:
                f.miss(ln, "branch edge source", ast.unparse(a[0]))
                continue
            if tbl is None:
                f.miss(ln, "destination table of `%s`" % src_n, ast.unparse(a[2]))
                continue
            f.routers[src_n] = ast.unparse(a[1])
            for key, val in tbl.items():
                dst_n = nm.string(val)
                if dst_n is None:
                    f.miss(ln, "destination `%s`/`%s`" % (src_n, key), ast.unparse(val))
                else:
                    f.edges.append((src_n, dst_n, "branch"))
    return f


class Homes:
    """module -> dist (its unit key's last segment), path <-> module, and a
    per-symbol dist when the snapshot records one."""

    def __init__(self, idx: dict) -> None:
        self.mod_dist: "dict[str, str]" = {}
        self.by_path: "dict[str, str]" = {}
        self.mod_path: "dict[str, str]" = {}
        self.sym: "dict[tuple[str, str], str]" = {}
        for unit_key, mods in (idx.get("units") or {}).items():
            dist = unit_key.rsplit("/", 1)[-1]
            for m in mods:
                self.mod_dist[m["module"]] = dist
                self.by_path[m["path"]] = m["module"]
                self.mod_path[m["module"]] = m["path"]
                for sy in m.get("symbols") or []:
                    for h in [sy] + (sy.get("methods") or []):
                        if h.get("dist"):
                            self.sym[(m["module"], h["name"])] = h["dist"]

    def paths_matching(self, rel: str) -> "list[str]":
        return sorted(p for p in self.by_path if p == rel or p.endswith("/" + rel))


class Resolver:
    """A node's implementation expression -> dist, by READING it: a method of a
    bound instance of an imported class, a factory call (its home is the
    factory's), a name imported from a module, a same-file symbol, or a def
    nested in a function (home = the declaring file). Anything else is `?`."""

    def __init__(self, tree: ast.Module, self_mod: str, homes: Homes) -> None:
        self.homes, self.self_mod = homes, self_mod
        self.mod_alias: "dict[str, str]" = {}
        self.from_alias: "dict[str, tuple[str, str]]" = {}
        self.bindings: "dict[str, str]" = {}
        self.top_level: "set[str]" = set()
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.top_level.add(n.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = self._abs(node.module, node.level)
                for al in node.names:
                    name = al.asname or al.name
                    self.from_alias[name] = (base, al.name)
                    self.mod_alias.setdefault(name, "%s.%s" % (base, al.name))
            elif isinstance(node, ast.Import):
                for al in node.names:
                    self.mod_alias[al.asname or al.name.split(".")[0]] = al.name
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                fn = node.value.func
                cls = fn.id if isinstance(fn, ast.Name) else None
                if cls:
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            self.bindings[t.id] = cls

    def _abs(self, mod: "str | None", level: int) -> str:
        if not level:
            return mod or ""
        parts = self.self_mod.split(".")[:-level]
        return ".".join(parts + ([mod] if mod else []))

    def _sym(self, module: str, name: str) -> "str | None":
        return self.homes.sym.get((module, name))

    def of(self, expr: ast.AST) -> "tuple[str, str]":
        d: "str | None"
        if isinstance(expr, ast.Call):
            d, how = self.of(expr.func)
            return d, "factory · %s" % how
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            holder = expr.value.id
            cls = self.bindings.get(holder)
            if cls and cls in self.from_alias:
                mod, orig = self.from_alias[cls]
                d = self._sym(mod, "%s.%s" % (orig, expr.attr))
                if d:
                    return d, "method `%s.%s` in `%s`" % (orig, expr.attr, mod)
            if holder in self.mod_alias:
                mod = self.mod_alias[holder]
                d = self._sym(mod, expr.attr) or self.homes.mod_dist.get(mod)
                if d:
                    return d, "`%s` in module `%s`" % (expr.attr, mod)
            return "?", "cannot resolve `%s.%s`" % (holder, expr.attr)
        if isinstance(expr, ast.Name):
            if expr.id in self.from_alias:
                mod, orig = self.from_alias[expr.id]
                d = self._sym(mod, orig) or self.homes.mod_dist.get(mod)
                if d:
                    return d, "`%s` imported from `%s`" % (orig, mod)
            if expr.id in self.top_level:
                d = self._sym(self.self_mod, expr.id)
                if d:
                    return d, "same file"
            d = self.homes.mod_dist.get(self.self_mod)
            if d:
                return d, "def nested in a function — home is the file declaring the flow"
        return "?", "cannot resolve `%s`" % ast.unparse(expr)


class Stage:
    """One strongly connected component of the flow graph: a multi-node (or
    self-looping) stage IS a loop; its name is the node entered from outside."""

    def __init__(self, nodes: "list[str]", entry: str,
                 edges: "list[tuple[str, str, str]]") -> None:
        self.entry = entry
        self.nodes = self._ring(nodes, entry, edges)
        self.self_loop = any(a == b and a in self.nodes for a, b, _ in edges)

    @staticmethod
    def _ring(nodes: "list[str]", entry: str, edges: "list[tuple[str, str, str]]") -> "list[str]":
        """Walk order along the ring's own edges, not alphabetical."""
        inside = set(nodes)
        adj = {n: sorted({b for a, b, _ in edges if a == n and b in inside and b != n})
               for n in nodes}
        order: "list[str]" = []
        seen: "set[str]" = set()
        stack = [entry]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            order.append(n)
            stack += [v for v in reversed(adj[n]) if v not in seen]
        return order + [n for n in sorted(nodes) if n not in seen]

    @property
    def is_loop(self) -> bool:
        return len(self.nodes) > 1 or self.self_loop


def condense(f: Flow) -> "tuple[list[Stage], list[tuple[int, int]]]":
    """(stages in walk order, edges between stages) — Kosaraju, then topo."""
    nodes = f.nodes
    fwd: "dict[str, set[str]]" = {n: set() for n in nodes}
    rev: "dict[str, set[str]]" = {n: set() for n in nodes}
    for a, b, _ in f.edges:
        fwd[a].add(b)
        rev[b].add(a)

    def walk(start: str, adj: "dict[str, set[str]]", seen: "set[str]") -> "list[str]":
        acc: "list[str]" = []
        stack = [(start, iter(sorted(adj[start])))]
        seen.add(start)
        while stack:
            n, it = stack[-1]
            for v in it:
                if v not in seen:
                    seen.add(v)
                    stack.append((v, iter(sorted(adj[v]))))
                    break
            else:
                acc.append(n)
                stack.pop()
        return acc

    seen: "set[str]" = set()
    order: "list[str]" = []
    for n in nodes:
        if n not in seen:
            order += walk(n, fwd, seen)
    seen, comps = set(), []
    for n in reversed(order):
        if n not in seen:
            comps.append(sorted(walk(n, rev, seen)))
    owner = {n: i for i, c in enumerate(comps) for n in c}
    ce = {(owner[a], owner[b]) for a, b, _ in f.edges if owner[a] != owner[b]}
    indeg = {i: 0 for i in range(len(comps))}
    for _, d in ce:
        indeg[d] += 1
    ready = sorted(i for i, d in indeg.items() if d == 0)
    topo: "list[int]" = []
    while ready:
        i = ready.pop(0)
        topo.append(i)
        for x, y in sorted(ce):
            if x == i:
                indeg[y] -= 1
                if indeg[y] == 0:
                    ready.append(y)
        ready.sort()
    stages = []
    for i in topo:
        c = comps[i]
        outside = [n for n in c if n == f.entry or any(
            a not in c and b == n for a, b, _ in f.edges)]
        stages.append(Stage(c, outside[0] if outside else c[0], f.edges))
    pos = {old: new for new, old in enumerate(topo)}
    return stages, sorted({(pos[a], pos[b]) for a, b in ce})


def registry_nodes(f: Flow, res: Resolver, homes: Homes, repo: Path) -> "dict[str, str]":
    """node -> dist for nodes a `for name, fn in REG.items()` loop registers,
    `REG = {name: getattr(Cls, name) for name in ("a", ...)}` read where it
    lives; the home is the registry's module (coarser, and said so)."""
    out: "dict[str, str]" = {}
    for reg in sorted(f.registrars):
        mod, reg_name = res.from_alias.get(reg, (res.self_mod, reg))
        path = homes.mod_path.get(mod) if mod else None
        if not path or not (repo / path).is_file():
            continue
        try:
            rtree = ast.parse((repo / path).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for n in rtree.body:
            if not (isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == reg_name for t in n.targets)):
                continue
            v = n.value
            if not (isinstance(v, ast.DictComp) and v.generators and isinstance(v.key, ast.Name)):
                continue
            it = v.generators[0].iter
            names = ([e.value for e in it.elts
                      if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                     if isinstance(it, ast.Tuple) else [])
            gv = v.value
            cls = None
            if (isinstance(gv, ast.Call) and isinstance(gv.func, ast.Name)
                    and gv.func.id == "getattr" and len(gv.args) >= 2):
                cls = ast.unparse(gv.args[0])
            elif isinstance(gv, ast.Attribute):
                cls = ast.unparse(gv)
            dist = homes.mod_dist.get(mod) if names and cls else None
            if dist:
                out.update({x: dist for x in names})
    return out


def node_homes(f: Flow, res: Resolver, homes: Homes, repo: Path) -> "dict[str, str]":
    """node -> dist; framework sentinels are not nodes (`—`)."""
    reg = registry_nodes(f, res, homes, repo)
    out: "dict[str, str]" = {}
    for node in f.nodes:
        if node in SENTINEL_NODES:
            out[node] = "—"
            continue
        expr = f.impl.get(node)
        if expr is None:
            if node in reg:
                out[node] = reg[node]
                continue
            f.unresolved.append("node `%s` appears only in an edge, no node declaration" % node)
            out[node] = "?"
            continue
        d, how = res.of(ast.parse(expr, mode="eval").body)
        out[node] = d
        if d == "?":
            f.unresolved.append("home of node `%s`: %s" % (node, how))
    return out


class Traced:
    """A fully extracted flow: graph, per-node homes, stage decomposition."""

    def __init__(self, spec: dict, flow: Flow, homes: "dict[str, str]", at: str) -> None:
        self.spec, self.flow, self.homes, self.at = spec, flow, homes, at
        self.stages, self.stage_edges = condense(flow)

    @property
    def dists(self) -> "list[str]":
        return sorted({d for d in self.homes.values() if d not in ("—", "?")})

    def as_dict(self) -> "dict[str, Any]":
        f = self.flow
        return {"id": f.id, "name": f.name, "at": self.at,
                "builder": self.spec.get("builder") or DEFAULT_BUILDER, "entry": f.entry,
                "nodes": f.nodes, "homes": self.homes, "dists": self.dists,
                "edges": [{"from": a, "to": b, "kind": k, "router": f.routers.get(a)
                           if k == "branch" else None} for a, b, k in f.edges],
                "stages": [{"entry": s.entry, "nodes": s.nodes, "loop": s.is_loop}
                           for s in self.stages],
                "stage_edges": [list(e) for e in self.stage_edges],
                "unresolved": list(f.unresolved)}


def trace_all(flows: "list[dict] | None", repo: Path, idx: dict, builders: "dict[str, dict]",
              only: "str | None" = None) -> "list[Traced]":
    """Trace every declared flow against one snapshot index and the tree it
    measured. No `flows:` is `Missing` — zero flows reads exactly like "this
    repo has no flows", which is a different claim."""
    if not flows:
        raise Missing("the declaration carries no `flows:` — this reads flows from the "
                      "builder calls a declaration points at; with none it would measure 0 "
                      "flows, and 0 reads exactly like 'this repo has no flows'")
    homes = Homes(idx)
    out: "list[Traced]" = []
    for spec in flows:
        if not isinstance(spec, dict) or not spec.get("id") or not spec.get("at"):
            raise Missing("every `flows:` entry needs `id` and `at`, not %r" % (spec,))
        if only and spec.get("id") != only:
            continue
        bname = spec.get("builder") or DEFAULT_BUILDER
        if bname not in builders:
            raise Missing("`flows[%s].builder: %s` is no declared builder (known: %s)"
                          % (spec["id"], bname, ", ".join(sorted(builders))))
        rel = str(spec["at"])
        hits = homes.paths_matching(rel)
        if len(hits) != 1:
            raise Missing("`flows[%s].at: %s` matches %s in the snapshot" % (
                spec["id"], rel, "no file" if not hits else "%d files %s" % (len(hits), hits)))
        at = repo / hits[0]
        if not at.is_file():
            raise Missing("`%s` is in the snapshot but not in %s" % (hits[0], repo))
        try:
            tree = ast.parse(at.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as e:
            raise Missing("%s:%s: does not parse — %s" % (at, e.lineno, e.msg)) from None
        self_mod = homes.by_path[hits[0]]
        f = trace_builder(tree, spec, builders[bname])
        out.append(Traced(spec, f, node_homes(f, Resolver(tree, self_mod, homes), homes, repo),
                          hits[0]))
    if only and not out:
        raise Missing("no flow carries id `%s`" % only)
    return out


def flow_homes(traced: "list[Traced]") -> "dict[str, list[tuple[str, str, int]]]":
    """dist -> [(flow id, the declared `at`, how many of its nodes live there)]
    — what a slice doc points at instead of copying a node list that rots."""
    acc: "dict[str, list[tuple[str, str, int]]]" = {}
    for t in traced:
        per: "dict[str, int]" = {}
        for _node, dist in t.homes.items():
            if dist not in ("—", "?"):
                per[dist] = per.get(dist, 0) + 1
        for dist, n in per.items():
            acc.setdefault(dist, []).append((t.flow.id, str(t.spec.get("at")), n))
    for v in acc.values():
        v.sort()
    return acc
