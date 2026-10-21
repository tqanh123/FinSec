#!/usr/bin/env python3
"""diagram_check.py — name every collision in a laid-out diagram.

A layout engine's own guarantees stop at what it was told: graphviz never saw an edge
label under `splines=ortho`, and a frame drawn around its members' bounding box is not
the cluster dot placed. So the geometry is checked AFTER layout, on the shapes the
renderer will draw, and `compile_layout` carries every finding as a warning — a picture
with a collision is still drawn, never silently passed as clean.
"""
from __future__ import annotations

#: How far an arrow tip may sit from its target box before it reads as falling short.
TIP_SLACK = 1.5
#: Boxes this close are touching, not overlapping (rounding in the engine's output).
EPS = 0.5


def _rect(o) -> tuple:
    return (o.x, o.y, o.x + o.w, o.y + o.h)


def _overlap(a: tuple, b: tuple) -> bool:
    return (a[0] + EPS < b[2] and b[0] + EPS < a[2]
            and a[1] + EPS < b[3] and b[1] + EPS < a[3])


def _segment_hits(p, q, r: tuple) -> bool:
    """Liang-Barsky: does segment p-q pass through the interior of rect r?"""
    x0, y0, x1, y1 = r[0] + EPS, r[1] + EPS, r[2] - EPS, r[3] - EPS
    if x0 >= x1 or y0 >= y1:
        return False
    dx, dy = q[0] - p[0], q[1] - p[1]
    t0, t1 = 0.0, 1.0
    for pk, qk in ((-dx, p[0] - x0), (dx, x1 - p[0]), (-dy, p[1] - y0), (dy, y1 - p[1])):
        if pk == 0:
            if qk < 0:
                return False
            continue
        t = qk / pk
        if pk < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return t1 - t0 > 1e-9


def _dist_to_rect(p, r: tuple) -> float:
    dx = max(r[0] - p[0], 0.0, p[0] - r[2])
    dy = max(r[1] - p[1], 0.0, p[1] - r[3])
    inside = r[0] < p[0] < r[2] and r[1] < p[1] < r[3]
    return 0.0 if inside else (dx * dx + dy * dy) ** 0.5


def _name(e) -> str:
    return "%s→%s" % (e.source, e.target)


def _segments(e):
    return list(zip(e.points, e.points[1:]))


def check_layout(layout) -> list:
    """Every collision in `layout`, one sentence each; empty means none was found."""
    found: list = []
    nodes = {n.id: _rect(n) for n in layout.nodes}
    labels = [(e, tuple(e.label_box[:2]) + (e.label_box[0] + e.label_box[2],
                                            e.label_box[1] + e.label_box[3]))
              for e in layout.edges if e.label_box]

    ids = list(nodes)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if _overlap(nodes[a], nodes[b]):
                found.append("box %s overlaps box %s" % (a, b))

    for g in layout.groups:
        frame = _rect(g)
        members = set(getattr(g, "members", None) or ())
        for nid, r in nodes.items():
            if nid not in members and _overlap(frame, r):
                found.append("box %s is not in frame %s but is drawn inside it" % (nid, g.id))

    for i, (e, r) in enumerate(labels):
        for nid, nr in nodes.items():
            if _overlap(r, nr):
                found.append("label %r (%s) lies on box %s" % (e.label, _name(e), nid))
        for f, fr in labels[i + 1:]:
            if _overlap(r, fr):
                found.append("label %r (%s) overlaps label %r (%s)"
                             % (e.label, _name(e), f.label, _name(f)))
        for f in layout.edges:
            if f is e:
                continue
            if any(_segment_hits(p, q, r) for p, q in _segments(f)):
                found.append("label %r (%s) lies on edge %s" % (e.label, _name(e), _name(f)))

    lifeline_x = {ln.id: ln.x for ln in getattr(layout, "lifelines", ())}
    for e in layout.edges:
        ends = {e.source, e.target}
        for nid, r in nodes.items():
            if nid in ends:
                continue
            if any(_segment_hits(p, q, r) for p, q in _segments(e)):
                found.append("edge %s runs through box %s" % (_name(e), nid))
        target = nodes.get(e.target)
        if target is not None and e.points and e.source != e.target:
            if e.target in lifeline_x:
                gap = abs(e.points[-1][0] - lifeline_x[e.target])
            else:
                gap = _dist_to_rect(e.points[-1], target)
            if gap > TIP_SLACK:
                found.append("edge %s stops %.0fpx short of box %s" % (_name(e), gap, e.target))

    for ln in getattr(layout, "lifelines", ()):
        for nid, r in nodes.items():
            if nid != ln.id and _segment_hits((ln.x, ln.y0), (ln.x, ln.y1), r):
                found.append("lifeline %s runs through box %s" % (ln.id, nid))

    shapes = [("box " + n.id, _rect(n)) for n in layout.nodes]
    shapes += [("frame " + g.id, _rect(g)) for g in layout.groups]
    shapes += [("label %r" % e.label, r) for e, r in labels]
    shapes += [("edge " + _name(e), (min(p[0] for p in e.points), min(p[1] for p in e.points),
                                    max(p[0] for p in e.points), max(p[1] for p in e.points)))
               for e in layout.edges if e.points]
    for what, (x0, y0, x1, y1) in shapes:
        if x0 < -EPS or y0 < -EPS or x1 > layout.width + EPS or y1 > layout.height + EPS:
            found.append("%s leaves the canvas (0,0)-(%.0f,%.0f)"
                         % (what, layout.width, layout.height))
    return found
