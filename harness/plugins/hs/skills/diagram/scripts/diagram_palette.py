#!/usr/bin/env python3
"""diagram_palette.py — role -> visual family -> color, across presets and themes.

Every archetype's own role/kind vocabulary (architecture's `gateway`, workflow's
`decision`, lifecycle's `failure-fatal`, ...) maps onto one shared set of
FAMILIES, so a reader who learns the legend once reads every diagram type with
it. Two presets (`classic`, `blueprint`) x two themes (`light`, `dark`) = four
renderable combinations; a preset changes background/surface/font, never the
family-to-color mapping, so the legend never has to explain itself twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PRESETS = ("classic", "blueprint")
THEMES = ("light", "dark")

#: raw IR role/kind string -> shared visual family
FAMILY_OF = {
    # architecture components.role
    "frontend": "client", "backend": "service", "database": "data", "cache": "data",
    "queue": "queue", "storage": "data", "gateway": "gateway", "auth": "security",
    "external": "external", "worker": "worker", "person": "person", "system": "system",
    # workflow steps.kind
    "start": "start", "action": "process", "decision": "decision", "wait": "waiting",
    "subprocess": "process", "terminal-success": "success", "terminal-failure": "failure",
    # dataflow nodes.role
    "source": "start", "transform": "transform", "store": "data", "sink": "success",
    "consumer": "worker", "filter": "filter", "governance": "governance",
    # lifecycle states.kind
    "initial": "start", "active": "process", "waiting": "waiting",
    "failure-recoverable": "failure", "failure-fatal": "failure",
    "terminal-cancelled": "failure",
    # sequence participants.role
    "client": "client", "service": "service",
}

FAMILY_COLORS = {
    "client": "#2563eb", "service": "#0ea5e9", "data": "#059669", "queue": "#d97706",
    "gateway": "#7c3aed", "security": "#dc2626", "external": "#6b7280", "worker": "#0891b2",
    "start": "#16a34a", "process": "#2563eb", "decision": "#d97706", "waiting": "#94a3b8",
    "success": "#16a34a", "failure": "#dc2626", "transform": "#7c3aed", "filter": "#0ea5e9",
    "governance": "#334155", "person": "#1e3a8a", "system": "#1d4ed8",
}
DEFAULT_FAMILY = "process"


def family_for(role: str | None) -> str:
    if not role:
        return DEFAULT_FAMILY
    return FAMILY_OF.get(role, DEFAULT_FAMILY)


def color_for(role: str | None) -> str:
    return FAMILY_COLORS.get(family_for(role), FAMILY_COLORS[DEFAULT_FAMILY])


_THEME_BASE = {
    "light": {"bg": "#ffffff", "surface": "#f8fafc", "text": "#0f172a",
              "edge": "#475569", "group_stroke": "#94a3b8", "group_fill": "#f1f5f9"},
    "dark": {"bg": "#0b1220", "surface": "#111827", "text": "#e2e8f0",
             "edge": "#94a3b8", "group_stroke": "#475569", "group_fill": "#1e293b"},
}

#: per-preset overrides layered on top of _THEME_BASE (only the keys that differ).
#: Values are heterogeneous (nested color dicts under "light"/"dark", a bare str
#: under "font"), so this is typed as `dict[str, dict]` rather than left for mypy
#: to infer -- an inferred `Collection[str]` union would flag every real read below.
_PRESET_OVERRIDES: dict[str, dict] = {
    "classic": {"light": {}, "dark": {}, "font": "system-ui, sans-serif"},
    "blueprint": {
        "light": {"bg": "#eef2ff", "surface": "#e0e7ff", "group_stroke": "#6366f1"},
        "dark": {"bg": "#020617", "surface": "#0f172a", "edge": "#38bdf8",
                 "group_stroke": "#1d4ed8"},
        "font": "'JetBrains Mono', 'Courier New', monospace",
    },
}


@dataclass
class Palette:
    preset: str
    theme: str
    bg: str
    surface: str
    text: str
    edge: str
    group_stroke: str
    group_fill: str
    font_family: str
    family_colors: dict = field(default_factory=lambda: dict(FAMILY_COLORS))


def resolve_palette(preset: str, theme: str) -> Palette:
    if preset not in PRESETS:
        raise ValueError("unknown preset %r -- use one of %s" % (preset, PRESETS))
    if theme not in THEMES:
        raise ValueError("unknown theme %r -- use one of %s" % (theme, THEMES))
    base = dict(_THEME_BASE[theme])
    base.update(_PRESET_OVERRIDES[preset].get(theme, {}))
    return Palette(
        preset=preset, theme=theme,
        bg=base["bg"], surface=base["surface"], text=base["text"], edge=base["edge"],
        group_stroke=base["group_stroke"], group_fill=base["group_fill"],
        font_family=_PRESET_OVERRIDES[preset]["font"],
    )
