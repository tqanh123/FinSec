#!/usr/bin/env python3
"""diagram_layout.py — turn a Scene (or, for sequence, the raw IR) into pixel geometry.

Three backends share one output shape (NodeBox / GroupBox / EdgeRoute / Layout):

  - graphviz: feeds a generated DOT description to the real `dot` binary
    (`splines=ortho` for orthogonal routing) and parses its `-Tplain` output.
    Chosen over `-Tjson` because the plain-text format is a small, stable,
    line-oriented contract documented in the graphviz manual and unchanged
    across the 2.43 -> 14.1 releases probed on this box and on homelab,
    instead of a nested object graph whose shape has moved between releases.
  - fallback-grid: pure-Python longest-path ranking plus a plain grid
    placement, used only when diagram_engine.select_engine's whole preference
    chain answers unavailable, or the `dot` invocation itself fails at
    runtime. Degrade honestly, never crash and never silently draw nothing.
  - sequence-linear: participants across the top, one row per message going
    down the page. There is no ranking problem to solve for a sequence
    diagram, so no external engine is invoked at all.
"""
from __future__ import annotations

import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from diagram_engine import select_engine
from diagram_scene import build_scene
from diagram_text import text_width

POINTS_PER_INCH = 72.0
NODE_PAD_X = 24.0
NODE_MIN_W = 90.0
NODE_H = 44.0
GROUP_PAD = 20.0
GROUP_LABEL_H = 18.0

_DASHED_KINDS = {"async", "fallback", "retry", "async-signal", "branch-false", "exception",
                 "timeout", "cancel"}


@dataclass
class NodeBox:
    id: str
    label: str
    x: float
    y: float
    w: float
    h: float
    role: str | None = None


@dataclass
class GroupBox:
    id: str
    label: str
    role: str | None
    x: float
    y: float
    w: float
    h: float


@dataclass
class EdgeRoute:
    source: str
    target: str
    label: str | None
    kind: str | None
    points: list


@dataclass
class Layout:
    engine: str
    width: float
    height: float
    nodes: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    edges: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    engine_trace: list = field(default_factory=list)


def _node_size(label: str) -> tuple:
    w = max(NODE_MIN_W, text_width(label, 13.0, 600) + NODE_PAD_X * 2)
    return w, NODE_H


def _group_box(group, node_lookup: dict) -> GroupBox | None:
    members = [node_lookup[m] for m in group.members if m in node_lookup]
    if not members:
        return None
    x0 = min(b.x for b in members) - GROUP_PAD
    y0 = min(b.y for b in members) - GROUP_PAD - GROUP_LABEL_H
    x1 = max(b.x + b.w for b in members) + GROUP_PAD
    y1 = max(b.y + b.h for b in members) + GROUP_PAD
    return GroupBox(id=group.id, label=group.label, role=group.role,
                     x=x0, y=y0, w=x1 - x0, h=y1 - y0)


# --------------------------------------------------------------- graphviz

def _dot_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace('"', '\\"')


def build_dot(scene) -> str:
    """Render `scene` as a DOT source. Public so tests can assert on structure
    without shelling out to `dot`."""
    lines = [
        "digraph G {",
        "  rankdir=%s;" % scene.rankdir,
        "  splines=ortho;",
        "  nodesep=0.45;",
        "  ranksep=0.65;",
        '  node [shape=box, style=filled, fontsize=11, fillcolor=white];',
    ]
    for g in scene.groups:
        lines.append('  subgraph "cluster_%s" {' % g.id)
        lines.append('    label="%s";' % _dot_escape(g.label))
        lines.append("    style=dashed;")
        for m in g.members:
            lines.append('    "%s";' % m)
        lines.append("  }")
    for n in scene.nodes:
        w, h = _node_size(n.label)
        lines.append('  "%s" [label="%s", width=%.3f, height=%.3f, fixedsize=true];'
                      % (n.id, _dot_escape(n.label), w / POINTS_PER_INCH, h / POINTS_PER_INCH))
    by_hint: dict = {}
    for n in scene.nodes:
        if n.rank_hint is not None:
            by_hint.setdefault(n.rank_hint, []).append(n.id)
    for hint in sorted(by_hint):
        ids = by_hint[hint]
        if len(ids) > 1:
            lines.append("  { rank=same; %s; }" % "; ".join('"%s"' % i for i in ids))
    for e in scene.edges:
        attrs = []
        if e.label:
            attrs.append('label="%s"' % _dot_escape(e.label))
        if e.kind in _DASHED_KINDS:
            attrs.append("style=dashed")
        attr_str = " [%s]" % ", ".join(attrs) if attrs else ""
        lines.append('  "%s" -> "%s"%s;' % (e.source, e.target, attr_str))
    lines.append("}")
    return "\n".join(lines)


def _parse_plain(text: str) -> dict:
    """Parse `dot -Tplain` output. Returns points already converted to POINTS
    and with y flipped so it increases downward (SVG convention; graphviz's
    native coordinate space is y-up, origin bottom-left)."""
    graph_h = 0.0
    nodes: dict = {}
    edges: list = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        tokens = shlex.split(raw)
        if not tokens:
            continue
        kind = tokens[0]
        if kind == "graph":
            graph_h = float(tokens[3]) * POINTS_PER_INCH
        elif kind == "node":
            name = tokens[1]
            x, y, w, h = (float(t) * POINTS_PER_INCH for t in tokens[2:6])
            nodes[name] = {"x": x, "y": y, "w": w, "h": h, "label": tokens[6]}
        elif kind == "edge":
            tail, head = tokens[1], tokens[2]
            n = int(tokens[3])
            coords = [float(t) for t in tokens[4:4 + 2 * n]]
            points = [(coords[i] * POINTS_PER_INCH, coords[i + 1] * POINTS_PER_INCH)
                      for i in range(0, len(coords), 2)]
            rest = tokens[4 + 2 * n:]
            label = rest[0] if len(rest) == 5 else None
            edges.append({"tail": tail, "head": head, "points": points, "label": label})
    for n in nodes.values():
        n["y"] = graph_h - n["y"]
    for e in edges:
        e["points"] = [(x, graph_h - y) for x, y in e["points"]]
    return {"height": graph_h, "nodes": nodes, "edges": edges}


def _run_dot(dot_source: str, dot_path: str = "dot") -> str:
    """Invoke `dot` on `dot_source`, returning its `-Tplain` stdout.

    Uses the OPTIONAL `graphviz` package (harness/requirements.txt) when it is
    importable and no explicit `dot_path` override is in play: it resolves the
    binary correctly across platforms and raises a specific
    `ExecutableNotFound` instead of a bare `FileNotFoundError`. Falls back to a
    direct subprocess call otherwise -- the package is a nicer invocation of
    the same binary, never the only way to reach it, so its absence never
    blocks a diagram that has `dot` on PATH.
    """
    if dot_path == "dot":
        try:
            import graphviz
        except ImportError:
            graphviz = None
        if graphviz is not None:
            try:
                raw = graphviz.Source(dot_source).pipe(format="plain", encoding="utf-8")
            except graphviz.backend.ExecutableNotFound as exc:
                raise RuntimeError("dot binary not found (%s)" % exc) from exc
            return raw
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "scene.dot"
        src.write_text(dot_source, encoding="utf-8")
        proc = subprocess.run([dot_path, "-Tplain", str(src)],
                               capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError("dot exited %d: %s" % (proc.returncode, proc.stderr.strip()))
    return proc.stdout


def layout_with_graphviz(scene, dot_path: str = "dot") -> Layout:
    dot_source = build_dot(scene)
    parsed = _parse_plain(_run_dot(dot_source, dot_path))

    src_by_id = {n.id: n for n in scene.nodes}
    nodes = []
    for name, geo in parsed["nodes"].items():
        src_node = src_by_id.get(name)
        nodes.append(NodeBox(
            id=name, label=src_node.label if src_node else geo["label"],
            x=geo["x"] - geo["w"] / 2, y=geo["y"] - geo["h"] / 2,
            w=geo["w"], h=geo["h"], role=src_node.role if src_node else None,
        ))
    node_lookup = {n.id: n for n in nodes}

    groups = [b for b in (_group_box(g, node_lookup) for g in scene.groups) if b is not None]

    pending: dict = {}
    for e in scene.edges:
        pending.setdefault((e.source, e.target), []).append(e)
    routes = []
    for pe in parsed["edges"]:
        candidates = pending.get((pe["tail"], pe["head"]), [])
        src_edge = candidates.pop(0) if candidates else None
        routes.append(EdgeRoute(
            source=pe["tail"], target=pe["head"],
            label=src_edge.label if src_edge else pe["label"],
            kind=src_edge.kind if src_edge else None,
            points=pe["points"],
        ))

    width = max([n.x + n.w for n in nodes] + [g.x + g.w for g in groups] + [0.0]) + GROUP_PAD
    height = max([n.y + n.h for n in nodes] + [g.y + g.h for g in groups] + [0.0]) + GROUP_PAD
    return Layout(engine="graphviz", width=width, height=height, nodes=nodes, groups=groups,
                  edges=routes)


# ----------------------------------------------------------- fallback grid

def _longest_path_ranks(scene) -> dict:
    node_ids = [n.id for n in scene.nodes]
    preds: dict = {i: [] for i in node_ids}
    for e in scene.edges:
        if e.source in preds and e.target in preds and e.source != e.target:
            preds[e.target].append(e.source)
    rank: dict = {}
    visiting: set = set()

    def resolve(i):
        if i in rank:
            return rank[i]
        if i in visiting:
            return 0  # back edge -- do not recurse into a cycle
        visiting.add(i)
        value = 0
        for p in preds[i]:
            value = max(value, resolve(p) + 1)
        visiting.discard(i)
        rank[i] = value
        return value

    for i in node_ids:
        resolve(i)
    for n in scene.nodes:  # an authored rank_hint always wins over the derived one
        if n.rank_hint is not None:
            rank[n.id] = n.rank_hint
    return rank


def layout_fallback_grid(scene) -> Layout:
    ranks = _longest_path_ranks(scene)
    by_rank: dict = {}
    for n in scene.nodes:
        by_rank.setdefault(ranks.get(n.id, 0), []).append(n)
    horizontal = scene.rankdir == "LR"
    col_gap, row_gap = 170.0, 70.0

    nodes, node_lookup = [], {}
    for col_idx, col in enumerate(sorted(by_rank)):
        for row_idx, n in enumerate(by_rank[col]):
            w, h = _node_size(n.label)
            if horizontal:
                x, y = 40 + col_idx * col_gap, 40 + row_idx * row_gap
            else:
                x, y = 40 + row_idx * col_gap, 40 + col_idx * row_gap
            box = NodeBox(id=n.id, label=n.label, x=x, y=y, w=w, h=h, role=n.role)
            nodes.append(box)
            node_lookup[n.id] = box

    groups = [b for b in (_group_box(g, node_lookup) for g in scene.groups) if b is not None]

    routes = []
    for e in scene.edges:
        a, b = node_lookup.get(e.source), node_lookup.get(e.target)
        if not a or not b:
            continue
        if horizontal:
            ax, ay = a.x + a.w, a.y + a.h / 2
            bx, by = b.x, b.y + b.h / 2
            mid = (ax + bx) / 2
            points = [(ax, ay), (bx, by)] if abs(ay - by) < 1 else [
                (ax, ay), (mid, ay), (mid, by), (bx, by)]
        else:
            ax, ay = a.x + a.w / 2, a.y + a.h
            bx, by = b.x + b.w / 2, b.y
            mid = (ay + by) / 2
            points = [(ax, ay), (bx, by)] if abs(ax - bx) < 1 else [
                (ax, ay), (ax, mid), (bx, mid), (bx, by)]
        routes.append(EdgeRoute(source=e.source, target=e.target, label=e.label, kind=e.kind,
                                 points=points))

    width = max((n.x + n.w for n in nodes), default=0.0) + 40
    height = max((n.y + n.h for n in nodes), default=0.0) + 40
    return Layout(engine="fallback-grid", width=width, height=height, nodes=nodes, groups=groups,
                  edges=routes,
                  warnings=["no external layout engine was available -- used a plain grid"])


# ------------------------------------------------------------ sequence

LIFELINE_TOP = 60.0
LIFELINE_GAP = 190.0
SEQ_ROW_H = 50.0
PARTICIPANT_W = 150.0
PARTICIPANT_H = 44.0


def layout_sequence(ir: dict) -> Layout:
    participants = ir.get("participants", [])
    messages = ir.get("messages", [])
    x_by_id, nodes = {}, []
    for idx, p in enumerate(participants):
        cx = 60 + idx * LIFELINE_GAP
        x_by_id[p["id"]] = cx
        nodes.append(NodeBox(id=p["id"], label=p.get("label", p["id"]),
                              x=cx - PARTICIPANT_W / 2, y=LIFELINE_TOP - PARTICIPANT_H,
                              w=PARTICIPANT_W, h=PARTICIPANT_H, role=p.get("role")))

    routes = []
    y = LIFELINE_TOP + SEQ_ROW_H
    for m in messages:
        if m.get("from") not in x_by_id or m.get("to") not in x_by_id:
            y += SEQ_ROW_H
            continue
        if m["from"] == m["to"]:
            cx = x_by_id[m["from"]]
            loop = 46.0
            points = [(cx, y), (cx + loop, y), (cx + loop, y + SEQ_ROW_H * 0.6),
                      (cx, y + SEQ_ROW_H * 0.6)]
        else:
            points = [(x_by_id[m["from"]], y), (x_by_id[m["to"]], y)]
        routes.append(EdgeRoute(source=m["from"], target=m["to"], label=m.get("label"),
                                 kind=m.get("kind"), points=points))
        y += SEQ_ROW_H

    width = (60 + max(len(participants) - 1, 0) * LIFELINE_GAP) + PARTICIPANT_W / 2 + 60
    height = y + 20
    return Layout(engine="sequence-linear", width=width, height=height, nodes=nodes, groups=[],
                  edges=routes)


# -------------------------------------------------------------- dispatch

def compile_layout(ir: dict, probes: dict | None = None) -> Layout:
    """Validate-free entry point: caller runs diagram_ir.validate_ir first.
    Picks the engine (diagram_engine.select_engine), lays the diagram out, and
    degrades to the pure-Python grid on any real invocation failure -- a
    layout engine crashing on unusual input must never take the whole compile
    down with it."""
    diagram_type = ir["diagram_type"]
    if diagram_type == "sequence":
        layout = layout_sequence(ir)
        layout.engine_trace = []
        return layout

    scene = build_scene(ir)
    choice = select_engine(diagram_type, probes=probes)
    engine = choice["engine"]

    if engine == "graphviz":
        try:
            layout = layout_with_graphviz(scene)
        except (RuntimeError, OSError) as exc:
            layout = layout_fallback_grid(scene)
            layout.warnings.append("dot invocation failed (%s) -- used the grid fallback" % exc)
        layout.engine_trace = choice["tried"]
        return layout

    if engine == "elk":
        # select_engine only returns this when a real probe resolves elkjs;
        # nothing implements the ELK bridge itself yet (references/
        # engine-selection.md), so route straight to the honest fallback.
        layout = layout_fallback_grid(scene)
        layout.warnings.append(
            "elk was detected but the elk bridge is not implemented yet -- used the grid fallback")
        layout.engine_trace = choice["tried"]
        return layout

    layout = layout_fallback_grid(scene)
    layout.engine_trace = choice["tried"]
    return layout
