#!/usr/bin/env python3
"""diagram_layout.py — turn a Scene (or, for sequence, the raw IR) into pixel geometry.

Three backends share one output shape (NodeBox / GroupBox / EdgeRoute / Layout):

  - graphviz: feeds a generated DOT description to the real `dot` binary
    (`splines=ortho` for orthogonal routing) and parses its `-Tplain` output.
    The orthogonal router ignores edge labels ("Orthogonal edges do not currently
    handle edge labels"), so a labelled edge goes through a LABEL NODE sized to its
    text — dot then routes around it like any other box, which is what keeps a label
    off every box, line and other label. `newrank=true` keeps a `layer` hint from
    pulling a boundary's member out of its cluster.
    Chosen over `-Tjson` because the plain-text format is a small, stable,
    line-oriented contract documented in the graphviz manual and unchanged
    across the 2.43 -> 14.1 releases probed on this box and on homelab,
    instead of a nested object graph whose shape has moved between releases.
  - fallback-grid: pure-Python longest-path ranking plus a plain grid
    placement, used only when diagram_engine.select_engine's whole preference
    chain answers unavailable, or the `dot` invocation itself fails at
    runtime. Degrade honestly, never crash and never silently draw nothing.
  - sequence-linear: participants across the top, a lifeline under each, one row
    per message going down the page, spaced so every label fits its span. There is
    no ranking problem to solve for a sequence diagram, so no external engine is
    invoked at all.

Every backend's output is moved onto the canvas (`_normalize`) and checked
(`diagram_check.check_layout`); a finding is a warning on the layout.
"""
from __future__ import annotations

import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from diagram_check import check_layout
from diagram_engine import select_engine
from diagram_scene import build_scene
from diagram_text import text_width, truncate_to_width, wrap_to_width

POINTS_PER_INCH = 72.0
NODE_PAD_X = 24.0
NODE_MIN_W = 90.0
NODE_H = 44.0
GROUP_PAD = 20.0
GROUP_LABEL_H = 18.0
LABEL_FONT_SIZE = 10.5
LABEL_PAD_X = 6.0
LABEL_H = 16.0
DETAIL_FONT_SIZE = 10.5
DETAIL_LINE_H = 14.0
DETAIL_MAX_W = 200.0
CANVAS_MARGIN = 16.0
#: dot's arrowhead length at arrowsize=1; an edge's last point is that far from its box.
ARROW_LEN = 10.0
#: Outside the IR id alphabet (`^[a-zA-Z0-9_-]+$`), so no component can share the name.
LABEL_NODE = "label.%d"
LABEL_PREFIX = LABEL_NODE.split("%")[0]

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
    details: list = field(default_factory=list)


@dataclass
class GroupBox:
    id: str
    label: str
    role: str | None
    x: float
    y: float
    w: float
    h: float
    members: list = field(default_factory=list)


@dataclass
class EdgeRoute:
    source: str
    target: str
    label: str | None
    kind: str | None
    points: list
    label_box: tuple | None = None


@dataclass
class Lifeline:
    id: str
    x: float
    y0: float
    y1: float


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
    lifelines: list = field(default_factory=list)


def node_details(node) -> list:
    """A C4-style box's lines under its name: `[technology]`, then the description
    wrapped to the box. Empty for a plain box."""
    lines = []
    tech = getattr(node, "technology", None)
    if tech:
        lines.append(truncate_to_width("[%s]" % tech, DETAIL_MAX_W, DETAIL_FONT_SIZE))
    desc = getattr(node, "description", None)
    if desc:
        lines.extend(wrap_to_width(desc, DETAIL_MAX_W, DETAIL_FONT_SIZE))
    return lines


def _node_size(label: str, details: list | None = None) -> tuple:
    w = max(NODE_MIN_W, text_width(label, 13.0, 600) + NODE_PAD_X * 2)
    if not details:
        return w, NODE_H
    w = max([w] + [text_width(d, DETAIL_FONT_SIZE) + NODE_PAD_X * 2 for d in details])
    return w, NODE_H + DETAIL_LINE_H * len(details)


def label_size(label: str) -> tuple:
    return text_width(label, LABEL_FONT_SIZE) + LABEL_PAD_X * 2, LABEL_H


def _group_box(group, node_lookup: dict, extra: "tuple | list" = ()) -> GroupBox | None:
    members = [node_lookup[m] for m in group.members if m in node_lookup]
    if not members:
        return None
    rects = [(b.x, b.y, b.x + b.w, b.y + b.h) for b in members] + list(extra)
    x0 = min(r[0] for r in rects) - GROUP_PAD
    y0 = min(r[1] for r in rects) - GROUP_PAD - GROUP_LABEL_H
    x1 = max(r[2] for r in rects) + GROUP_PAD
    y1 = max(r[3] for r in rects) + GROUP_PAD
    return GroupBox(id=group.id, label=group.label, role=group.role,
                     x=x0, y=y0, w=x1 - x0, h=y1 - y0, members=[b.id for b in members])


def _label_box_at(points, label: str | None) -> tuple | None:
    """A label centred on the halfway point of `points`, measured by length."""
    if not label or not points:
        return None
    segs = list(zip(points, points[1:]))
    total = sum(abs(q[0] - p[0]) + abs(q[1] - p[1]) for p, q in segs)
    at, cx, cy = total / 2, points[0][0], points[0][1]
    for p, q in segs:
        n = abs(q[0] - p[0]) + abs(q[1] - p[1])
        if n and at <= n:
            cx, cy = p[0] + (q[0] - p[0]) * at / n, p[1] + (q[1] - p[1]) * at / n
            break
        at -= n
    w, h = label_size(label)
    return (cx - w / 2, cy - h / 2, w, h)


def _to_box(points: list, rect: tuple, reach: float = ARROW_LEN * 2) -> list:
    """Carry the last segment on to `rect`'s edge: dot ends an edge where its own
    arrowhead begins, and the SVG marker draws the head back from the path's end."""
    if len(points) < 2:
        return points
    (px, py), (qx, qy) = points[-2], points[-1]
    for i in range(len(points) - 2, -1, -1):
        px, py = points[i]
        if (px, py) != (qx, qy):
            break
    dx, dy = qx - px, qy - py
    norm = (dx * dx + dy * dy) ** 0.5
    if not norm:
        return points
    dx, dy = dx / norm, dy / norm
    x0, y0, x1, y1 = rect
    best = None
    for t in ((x0 - qx) / dx if dx else None, (x1 - qx) / dx if dx else None,
              (y0 - qy) / dy if dy else None, (y1 - qy) / dy if dy else None):
        if t is None or t < -0.01 or t > reach:
            continue
        hx, hy = qx + dx * t, qy + dy * t
        if x0 - 0.5 <= hx <= x1 + 0.5 and y0 - 0.5 <= hy <= y1 + 0.5:
            best = t if best is None else min(best, t)
    if not best or best < 0.01:
        return points
    return points + [(qx + dx * best, qy + dy * best)]


def _normalize(layout: Layout) -> Layout:
    """Shift everything so the drawing starts at CANVAS_MARGIN and size the canvas to
    hold it: a frame's label strip and a participant centred near x=0 were drawn at
    negative coordinates and cut off."""
    xs, ys = [], []
    for o in list(layout.nodes) + list(layout.groups):
        xs += [o.x, o.x + o.w]
        ys += [o.y, o.y + o.h]
    for e in layout.edges:
        xs += [p[0] for p in e.points]
        ys += [p[1] for p in e.points]
        if e.label_box:
            bx, by, bw, bh = e.label_box
            xs += [bx, bx + bw]
            ys += [by, by + bh]
    for ln in layout.lifelines:
        xs.append(ln.x)
        ys += [ln.y0, ln.y1]
    if not xs:
        return layout
    dx, dy = CANVAS_MARGIN - min(xs), CANVAS_MARGIN - min(ys)
    for o in list(layout.nodes) + list(layout.groups):
        o.x += dx
        o.y += dy
    for e in layout.edges:
        e.points = [(x + dx, y + dy) for x, y in e.points]
        if e.label_box:
            bx, by, bw, bh = e.label_box
            e.label_box = (bx + dx, by + dy, bw, bh)
    for ln in layout.lifelines:
        ln.x += dx
        ln.y0 += dy
        ln.y1 += dy
    layout.width = max(xs) + dx + CANVAS_MARGIN
    layout.height = max(ys) + dy + CANVAS_MARGIN
    return layout


# --------------------------------------------------------------- graphviz

def _dot_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace('"', '\\"')


def _fixed_label(w: float, h: float) -> str:
    """An HTML-like label of exactly w x h points: what dot reserves for a frame's title."""
    return ('<<TABLE BORDER="0" CELLPADDING="0" CELLSPACING="0"><TR><TD FIXEDSIZE="TRUE" '
            'WIDTH="%d" HEIGHT="%d"> </TD></TR></TABLE>>' % (round(w), round(h)))


def _label_nodes(scene) -> list:
    """(edge index, label node id, group id or None) for every labelled edge; the label
    node sits in a frame only when both ends of its edge do."""
    group_of: dict = {}
    for g in scene.groups:
        for m in g.members:
            group_of.setdefault(m, g.id)
    out = []
    for i, e in enumerate(scene.edges):
        if e.label:
            g = group_of.get(e.source)
            out.append((i, LABEL_NODE % i, g if g and g == group_of.get(e.target) else None))
    return out


def build_dot(scene, splines: str = "ortho") -> str:
    """Render `scene` as a DOT source. Public so tests can assert on structure
    without shelling out to `dot`."""
    lines = [
        "digraph G {",
        "  rankdir=%s;" % scene.rankdir,
        "  splines=%s;" % splines,
        "  newrank=true;",
        "  nodesep=0.45;",
        "  ranksep=0.5;",
        '  node [shape=box, style=filled, fontsize=11, fillcolor=white, fixedsize=true];',
        "  edge [arrowsize=1];",
    ]
    label_nodes = _label_nodes(scene)
    for g in scene.groups:
        lines.append('  subgraph "cluster_%s" {' % g.id)
        lines.append("    label=%s;" % _fixed_label(
            text_width(g.label, 11.0) + 20, GROUP_LABEL_H))
        lines.append("    labeljust=l;")
        lines.append("    margin=%d;" % GROUP_PAD)
        lines.append("    style=dashed;")
        for m in g.members:
            lines.append('    "%s";' % m)
        for _, lid, gid in label_nodes:
            if gid == g.id:
                lines.append('    "%s";' % lid)
        lines.append("  }")
    for n in scene.nodes:
        w, h = _node_size(n.label, node_details(n))
        lines.append('  "%s" [label="%s", width=%.3f, height=%.3f];'
                      % (n.id, _dot_escape(n.label), w / POINTS_PER_INCH, h / POINTS_PER_INCH))
    rank_of = {n.id: n.rank_hint for n in scene.nodes}
    by_hint: dict = {}
    for n in scene.nodes:
        if n.rank_hint is not None:
            by_hint.setdefault(n.rank_hint, []).append(n.id)
    for i, lid, _ in label_nodes:
        e = scene.edges[i]
        w, h = label_size(e.label)
        lines.append('  "%s" [label="", shape=box, style=invis, width=%.3f, height=%.3f];'
                      % (lid, w / POINTS_PER_INCH, h / POINTS_PER_INCH))
        hint = rank_of.get(e.source)
        if hint is not None and hint == rank_of.get(e.target):
            by_hint[hint].append(lid)
    for hint in sorted(by_hint):
        ids = by_hint[hint]
        if len(ids) > 1:
            lines.append("  { rank=same; %s; }" % "; ".join('"%s"' % i for i in ids))
    via = {i: lid for i, lid, _ in label_nodes}
    for i, e in enumerate(scene.edges):
        style = ", style=dashed" if e.kind in _DASHED_KINDS else ""
        if i in via:
            lines.append('  "%s" -> "%s" [arrowhead=none%s];' % (e.source, via[i], style))
            lines.append('  "%s" -> "%s" [%s];' % (via[i], e.target, style.lstrip(", ")))
        else:
            lines.append('  "%s" -> "%s"%s;' % (e.source, e.target,
                                               " [%s]" % style.lstrip(", ") if style else ""))
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
                raw = graphviz.Source(dot_source).pipe(format="plain", encoding="utf-8", quiet=True)
            except graphviz.backend.ExecutableNotFound as exc:
                raise RuntimeError("dot binary not found (%s)" % exc) from exc
            except subprocess.CalledProcessError as exc:
                raise RuntimeError("dot exited %s: %s" % (
                    exc.returncode, " ".join((exc.stderr or "").split())[:200])) from exc
            return raw
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "scene.dot"
        src.write_text(dot_source, encoding="utf-8")
        proc = subprocess.run([dot_path, "-Tplain", str(src)],
                               capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError("dot exited %d: %s" % (proc.returncode, proc.stderr.strip()))
    return proc.stdout


def _join(a: list, b: list) -> list:
    """Two halves of a labelled edge meet inside the label node; an elbow keeps the
    joint orthogonal (it is drawn under the label)."""
    if not a or not b:
        return a + b
    p, q = a[-1], b[0]
    mid = [] if p[0] == q[0] or p[1] == q[1] else [(q[0], p[1])]
    return a + mid + b


def _take(halves: dict, key: tuple) -> list:
    got = halves.get(key)
    return list(got.pop(0)) if got else []


def layout_with_graphviz(scene, dot_path: str = "dot", splines: str = "ortho") -> Layout:
    dot_source = build_dot(scene, splines)
    parsed = _parse_plain(_run_dot(dot_source, dot_path))

    src_by_id = {n.id: n for n in scene.nodes}
    nodes, label_geo = [], {}
    for name, geo in parsed["nodes"].items():
        x, y = geo["x"] - geo["w"] / 2, geo["y"] - geo["h"] / 2
        if name.startswith(LABEL_PREFIX):
            label_geo[name] = (x, y, geo["w"], geo["h"])
            continue
        src_node = src_by_id.get(name)
        nodes.append(NodeBox(
            id=name, label=src_node.label if src_node else geo["label"],
            x=x, y=y, w=geo["w"], h=geo["h"], role=src_node.role if src_node else None,
            details=node_details(src_node) if src_node else [],
        ))
    node_lookup = {n.id: n for n in nodes}
    rect = {n.id: (n.x, n.y, n.x + n.w, n.y + n.h) for n in nodes}

    halves: dict = {}
    for pe in parsed["edges"]:
        halves.setdefault((pe["tail"], pe["head"]), []).append(pe["points"])
    routes = []
    for i, e in enumerate(scene.edges):
        lid = LABEL_NODE % i
        if lid in label_geo:
            points = _join(_take(halves, (e.source, lid)), _take(halves, (lid, e.target)))
            box = label_geo[lid]
        else:
            points, box = _take(halves, (e.source, e.target)), None
        if e.target in rect:
            points = _to_box(points, rect[e.target])
        routes.append(EdgeRoute(source=e.source, target=e.target, label=e.label, kind=e.kind,
                                points=points, label_box=box))

    inner: dict = {}
    for i, lid, gid in _label_nodes(scene):
        if gid and lid in label_geo:
            bx, by, bw, bh = label_geo[lid]
            inner.setdefault(gid, []).append((bx, by, bx + bw, by + bh))
    groups = [b for b in (_group_box(g, node_lookup, inner.get(g.id, ())) for g in scene.groups)
              if b is not None]
    return Layout(engine="graphviz", width=0.0, height=0.0, nodes=nodes, groups=groups,
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
            details = node_details(n)
            w, h = _node_size(n.label, details)
            if horizontal:
                x, y = 40 + col_idx * col_gap, 40 + row_idx * row_gap
            else:
                x, y = 40 + row_idx * col_gap, 40 + col_idx * row_gap
            box = NodeBox(id=n.id, label=n.label, x=x, y=y, w=w, h=h, role=n.role,
                          details=details)
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
                                 points=points, label_box=_label_box_at(points, e.label)))

    return Layout(engine="fallback-grid", width=0.0, height=0.0, nodes=nodes, groups=groups,
                  edges=routes,
                  warnings=["no external layout engine was available -- used a plain grid"])


# ------------------------------------------------------------ sequence

LIFELINE_TOP = 60.0
LIFELINE_GAP = 190.0
SEQ_ROW_H = 50.0
PARTICIPANT_W = 150.0
PARTICIPANT_H = 44.0
SELF_LOOP_W = 46.0
LABEL_CLEARANCE = 24.0


def _participant_xs(participants: list, widths: list, messages: list) -> list:
    """Lifeline x per participant: at least LIFELINE_GAP apart and wide enough for both
    boxes, then pushed apart wherever a message's label is wider than its span."""
    xs: list = []
    for i, w in enumerate(widths):
        xs.append(w / 2 if i == 0 else
                  xs[-1] + max(LIFELINE_GAP, (widths[i - 1] + w) / 2 + LABEL_CLEARANCE))
    idx = {p["id"]: i for i, p in enumerate(participants)}
    for m in messages:
        a, b = idx.get(m.get("from")), idx.get(m.get("to"))
        if a is None or b is None or not m.get("label"):
            continue
        need = label_size(m["label"])[0] + LABEL_CLEARANCE
        if a == b:
            a, b, need = a, a + 1, need + SELF_LOOP_W
            if b >= len(xs):
                continue
        lo, hi = min(a, b), max(a, b)
        short = need - (xs[hi] - xs[lo])
        if short > 0:
            xs[hi:] = [x + short for x in xs[hi:]]
    return xs


def layout_sequence(ir: dict) -> Layout:
    participants = ir.get("participants", [])
    messages = ir.get("messages", [])
    widths = [max(PARTICIPANT_W, _node_size(p.get("label", p["id"]))[0]) for p in participants]
    xs = _participant_xs(participants, widths, messages)
    x_by_id, nodes = {}, []
    for p, cx, w in zip(participants, xs, widths):
        x_by_id[p["id"]] = cx
        nodes.append(NodeBox(id=p["id"], label=p.get("label", p["id"]),
                              x=cx - w / 2, y=LIFELINE_TOP - PARTICIPANT_H,
                              w=w, h=PARTICIPANT_H, role=p.get("role")))

    routes = []
    y = LIFELINE_TOP + SEQ_ROW_H
    for m in messages:
        if m.get("from") not in x_by_id or m.get("to") not in x_by_id:
            y += SEQ_ROW_H
            continue
        label, box = m.get("label"), None
        lw, lh = label_size(label) if label else (0.0, 0.0)
        if m["from"] == m["to"]:
            cx = x_by_id[m["from"]]
            points = [(cx, y), (cx + SELF_LOOP_W, y), (cx + SELF_LOOP_W, y + SEQ_ROW_H * 0.6),
                      (cx, y + SEQ_ROW_H * 0.6)]
            if label:
                box = (cx + SELF_LOOP_W + 6, y + SEQ_ROW_H * 0.3 - lh / 2, lw, lh)
        else:
            x1, x2 = x_by_id[m["from"]], x_by_id[m["to"]]
            points = [(x1, y), (x2, y)]
            if label:
                box = ((x1 + x2) / 2 - lw / 2, y - 3 - lh, lw, lh)
        routes.append(EdgeRoute(source=m["from"], target=m["to"], label=label,
                                 kind=m.get("kind"), points=points, label_box=box))
        y += SEQ_ROW_H

    bottom = y - SEQ_ROW_H / 2
    lifelines = [Lifeline(id=n.id, x=x_by_id[n.id], y0=n.y + n.h, y1=bottom) for n in nodes]
    return Layout(engine="sequence-linear", width=0.0, height=0.0, nodes=nodes, groups=[],
                  edges=routes, lifelines=lifelines)


# -------------------------------------------------------------- dispatch

def compile_layout(ir: dict, probes: dict | None = None) -> Layout:
    """Validate-free entry point: caller runs diagram_ir.validate_ir first.
    Picks the engine (diagram_engine.select_engine), lays the diagram out, and
    degrades to the pure-Python grid on any real invocation failure -- a
    layout engine crashing on unusual input must never take the whole compile
    down with it. The result is moved onto its canvas and checked; every
    collision the check names is a `layout check:` warning."""
    layout = _place(ir, probes)
    _normalize(layout)
    layout.warnings.extend("layout check: %s" % f for f in check_layout(layout))
    return layout


def _place(ir: dict, probes: dict | None) -> Layout:
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
            # dot's orthogonal router aborts on some graphs (2.43: maze.c chkSgraph);
            # the same scene still lays out with straight segments.
            try:
                layout = layout_with_graphviz(scene, splines="polyline")
                layout.warnings.append("dot's orthogonal router failed (%s) -- routed with "
                                       "straight segments instead" % exc)
            except (RuntimeError, OSError) as exc2:
                layout = layout_fallback_grid(scene)
                layout.warnings.append(
                    "dot invocation failed (%s) -- used the grid fallback" % exc2)
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
