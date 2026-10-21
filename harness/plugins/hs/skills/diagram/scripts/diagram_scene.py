#!/usr/bin/env python3
"""diagram_scene.py — flatten a typed IR document into one generic node/edge/
group scene the layout backends and the renderer both consume.

architecture, dataflow, workflow and lifecycle all share one shape once the
field names are normalised: a set of nodes, an optional set of grouping frames
(boundaries / stages / lanes) each owning a member list, and a set of directed
edges. Pulling that shape out here means the layout code (diagram_layout.py)
and the SVG renderer (diagram_render.py) each read ONE representation instead
of re-deriving "what is a node" five times. `sequence` keeps its own shape
(ordered participants + timed messages) — it is not a rank/group graph at all,
so it is read directly by diagram_layout_sequence instead of going through
this module.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SceneNode:
    id: str
    label: str
    role: str | None = None
    rank_hint: int | None = None
    group: str | None = None
    technology: str | None = None
    description: str | None = None


@dataclass
class SceneGroup:
    id: str
    label: str
    role: str | None
    members: list = field(default_factory=list)


@dataclass
class SceneEdge:
    source: str
    target: str
    label: str | None = None
    kind: str | None = None


@dataclass
class Scene:
    diagram_type: str
    title: str
    rankdir: str  # "LR" or "TB" -- the dominant reading direction for this archetype
    nodes: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    edges: list = field(default_factory=list)


def _architecture_scene(ir: dict) -> Scene:
    nodes = [
        SceneNode(id=c["id"], label=c.get("label", c["id"]), role=c.get("role"),
                   rank_hint=c.get("layer"), technology=c.get("technology"),
                   description=c.get("description"))
        for c in ir.get("components", [])
    ]
    groups = [
        SceneGroup(id=b["id"], label=b.get("label", b["id"]), role=b.get("role"),
                    members=list(b.get("components", [])))
        for b in ir.get("boundaries", [])
    ]
    edges = [
        SceneEdge(source=c["from"], target=c["to"], label=c.get("label"), kind=c.get("kind"))
        for c in ir.get("connections", [])
    ]
    return Scene(diagram_type="architecture", title=ir.get("meta", {}).get("title", ""),
                 rankdir="LR", nodes=nodes, groups=groups, edges=edges)


def _dataflow_scene(ir: dict) -> Scene:
    stage_order = {s["id"]: s.get("order", idx) for idx, s in enumerate(ir.get("stages", []))}
    nodes = [
        SceneNode(id=n["id"], label=n.get("label", n["id"]), role=n.get("role"),
                   rank_hint=stage_order.get(n.get("stage")), group=n.get("stage"))
        for n in ir.get("nodes", [])
    ]
    groups = [
        SceneGroup(id=s["id"], label=s.get("label", s["id"]), role=None,
                    members=[n["id"] for n in ir.get("nodes", []) if n.get("stage") == s["id"]])
        for s in ir.get("stages", [])
    ]
    edges = [
        SceneEdge(source=f["from"], target=f["to"], label=f.get("label"))
        for f in ir.get("flows", [])
    ]
    return Scene(diagram_type="dataflow", title=ir.get("meta", {}).get("title", ""),
                 rankdir="LR", nodes=nodes, groups=groups, edges=edges)


def _workflow_scene(ir: dict) -> Scene:
    nodes = [
        SceneNode(id=s["id"], label=s.get("label", s["id"]), role=s.get("kind"),
                   group=s.get("lane"))
        for s in ir.get("steps", [])
    ]
    groups = [
        SceneGroup(id=lane["id"], label=lane.get("label", lane["id"]), role=lane.get("role"),
                    members=[s["id"] for s in ir.get("steps", []) if s.get("lane") == lane["id"]])
        for lane in ir.get("lanes", [])
    ]
    edges = [
        SceneEdge(source=t["from"], target=t["to"], label=t.get("label"), kind=t.get("kind"))
        for t in ir.get("transitions", [])
    ]
    return Scene(diagram_type="workflow", title=ir.get("meta", {}).get("title", ""),
                 rankdir="TB", nodes=nodes, groups=groups, edges=edges)


def _lifecycle_scene(ir: dict) -> Scene:
    nodes = [
        SceneNode(id=s["id"], label=s.get("label", s["id"]), role=s.get("kind"),
                   rank_hint=0 if s.get("kind") == "initial" else None)
        for s in ir.get("states", [])
    ]
    edges = [
        SceneEdge(source=t["from"], target=t["to"], label=t.get("event"), kind=t.get("kind"))
        for t in ir.get("transitions", [])
    ]
    return Scene(diagram_type="lifecycle", title=ir.get("meta", {}).get("title", ""),
                 rankdir="TB", nodes=nodes, groups=[], edges=edges)


_BUILDERS = {
    "architecture": _architecture_scene,
    "dataflow": _dataflow_scene,
    "workflow": _workflow_scene,
    "lifecycle": _lifecycle_scene,
}


def build_scene(ir: dict) -> Scene:
    diagram_type = ir.get("diagram_type")
    builder = _BUILDERS.get(diagram_type) if isinstance(diagram_type, str) else None
    if builder is None:
        raise ValueError(
            "build_scene does not handle diagram_type %r (sequence has its own "
            "layout path — see diagram_layout_sequence.py)" % (diagram_type,))
    return builder(ir)
