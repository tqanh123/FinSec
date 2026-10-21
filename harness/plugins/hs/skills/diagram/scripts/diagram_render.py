#!/usr/bin/env python3
"""diagram_render.py — render a Layout (diagram_layout.py) into SVG.

Every selector in the emitted `<style>` is prefixed with the diagram's element
id (`#<elem_id> ...`) rather than left bare, because a `fragment` output is
meant to be pasted into a page this module knows nothing about: a bare
`.hsd-node { fill: ... }` would restyle an unrelated element the host page
already had. `svg`/`fragment`/`html` (diagram_html.py) all call
`render_svg_markup` and differ only in what wraps it.
"""
from __future__ import annotations

from xml.sax.saxutils import escape as _xml_escape

from diagram_layout import DETAIL_FONT_SIZE, DETAIL_LINE_H, LABEL_FONT_SIZE, NODE_H, label_size
from diagram_palette import color_for, resolve_palette
from diagram_text import truncate_to_width

_DASHED_KINDS = {"async", "fallback", "retry", "async-signal", "branch-false", "exception",
                 "timeout", "cancel", "return"}
NODE_FONT_SIZE = 12.5
GROUP_LABEL_FONT_SIZE = 11.0


def _esc(text) -> str:
    return _xml_escape(str(text if text is not None else ""))


def _path_d(points) -> str:
    if not points:
        return ""
    cmds = ["M%.1f,%.1f" % points[0]]
    cmds.extend("L%.1f,%.1f" % p for p in points[1:])
    return " ".join(cmds)


def _node_markup(node) -> str:
    color = color_for(node.role)
    label = truncate_to_width(node.label, node.w - 16, NODE_FONT_SIZE, 600)
    details = list(getattr(node, "details", None) or ())
    cx = node.x + node.w / 2
    cy = node.y + (NODE_H / 2 if details else node.h / 2)
    lines = "".join(
        '<text x="%.1f" y="%.1f" class="hsd-node-detail" text-anchor="middle" '
        'dominant-baseline="middle">%s</text>'
        % (cx, node.y + NODE_H - 6 + DETAIL_LINE_H * i, _esc(d)) for i, d in enumerate(details))
    return (
        '<g class="hsd-node" data-id="%s" data-label="%s">'
        '<title>%s</title>'
        '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="8" '
        'class="hsd-node-box" style="fill:%s22;stroke:%s" />'
        '<text x="%.1f" y="%.1f" class="hsd-node-text" text-anchor="middle" '
        'dominant-baseline="middle">%s</text>%s'
        '</g>'
    ) % (_esc(node.id), _esc(node.label.lower()), _esc(node.label),
         node.x, node.y, node.w, node.h, color, color,
         cx, cy, _esc(label), lines)


def _group_markup(group) -> str:
    return (
        '<g class="hsd-group" data-id="%s">'
        '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="10" class="hsd-group-box" />'
        '<text x="%.1f" y="%.1f" class="hsd-group-text">%s</text>'
        '</g>'
    ) % (_esc(group.id), group.x, group.y, group.w, group.h,
         group.x + 10, group.y + 14, _esc(group.label))


def _edge_markup(edge) -> str:
    dashed = 'stroke-dasharray="6,4"' if edge.kind in _DASHED_KINDS else ""
    d = _path_d(edge.points)
    label_markup = ""
    box = getattr(edge, "label_box", None)
    if edge.label and edge.points and box is None:
        mid = edge.points[len(edge.points) // 2]
        w, h = label_size(edge.label)
        box = (mid[0] - w / 2, mid[1] - h / 2, w, h)
    if edge.label and box is not None:
        bx, by, bw, bh = box
        label_markup = (
            '<g class="hsd-edge-label">'
            '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" class="hsd-edge-label-bg" />'
            '<text x="%.1f" y="%.1f" text-anchor="middle" dominant-baseline="middle" '
            'class="hsd-edge-label-text">%s</text>'
            '</g>'
        ) % (bx, by, bw, bh, bx + bw / 2, by + bh / 2, _esc(edge.label))
    return (
        '<g class="hsd-edge" data-source="%s" data-target="%s">'
        '<path d="%s" class="hsd-edge-path" %s />'
        '%s</g>'
    ) % (_esc(edge.source), _esc(edge.target), d, dashed, label_markup)


def _lifeline_markup(line) -> str:
    return ('<line class="hsd-lifeline" data-id="%s" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" '
            'stroke-dasharray="4,4" />' % (_esc(line.id), line.x, line.y0, line.x, line.y1))


def _style_block_for_theme(elem_id: str, theme: str, palette) -> str:
    """CSS scoped by BOTH the diagram's element id and `[data-theme="<theme>"]`
    on the `<svg>` root, so both themes' rules can live in the same document
    at once and a JS theme toggle is just flipping that one attribute --
    no re-layout, no re-fetch, nothing to regenerate."""
    p = '#%s[data-theme="%s"]' % (elem_id, theme)
    return (
        "%(p)s .hsd-bg { fill: %(bg)s; }\n"
        "%(p)s text { font-family: %(font)s; fill: %(text)s; }\n"
        "%(p)s .hsd-node-text { font-size: %(node_fs)spx; font-weight: 600; }\n"
        "%(p)s .hsd-node-detail { font-size: %(detail_fs)spx; }\n"
        "%(p)s .hsd-lifeline { stroke: %(group_stroke)s; stroke-width: 1.2px; }\n"
        "%(p)s .hsd-node-box { stroke-width: 1.6px; }\n"
        "%(p)s .hsd-group-box { fill: %(group_fill)s; stroke: %(group_stroke)s; }\n"
        "%(p)s .hsd-group-text { font-size: %(group_fs)spx; fill: %(edge)s; }\n"
        "%(p)s .hsd-edge-path { stroke: %(edge)s; stroke-width: 1.6px; fill: none; "
        "marker-end: url(#hsd-arrow-%(theme)s); }\n"
        "%(p)s .hsd-edge-label-text { font-size: %(label_fs)spx; }\n"
        "%(p)s .hsd-edge-label-bg { fill: %(surface)s; opacity: 0.92; }\n"
    ) % {
        "p": p, "theme": theme, "bg": palette.bg, "font": palette.font_family,
        "text": palette.text, "edge": palette.edge, "surface": palette.surface,
        "group_fill": palette.group_fill, "group_stroke": palette.group_stroke,
        "node_fs": NODE_FONT_SIZE, "group_fs": GROUP_LABEL_FONT_SIZE,
        "label_fs": LABEL_FONT_SIZE, "detail_fs": DETAIL_FONT_SIZE,
    }


def _shared_style_block(elem_id: str) -> str:
    p = "#%s" % elem_id
    return (
        "%(p)s .hsd-node.hsd-dim, %(p)s .hsd-edge.hsd-dim { opacity: 0.18; }\n"
        "%(p)s .hsd-node.hsd-match .hsd-node-box { stroke-width: 3px; }\n"
    ) % {"p": p}


def render_svg_markup(ir: dict, layout, preset: str = "classic", theme: str = "light",
                       elem_id: str = "hsd") -> str:
    """One self-contained `<svg>` for `layout`: `<defs>` (one arrowhead marker
    per theme), a `<style>` carrying BOTH themes of `preset` (so a later theme
    toggle needs no re-render), background, groups, edges, then nodes (edges
    under nodes so a route never visually crosses over the boxes it names)."""
    if theme not in ("light", "dark"):
        raise ValueError("unknown theme %r" % (theme,))
    light, dark = resolve_palette(preset, "light"), resolve_palette(preset, "dark")
    width = max(layout.width, 40.0)
    height = max(layout.height, 40.0)
    title = ir.get("meta", {}).get("title") or ir.get("diagram_type", "diagram")

    style = (
        "<style><![CDATA[\n" + _style_block_for_theme(elem_id, "light", light)
        + _style_block_for_theme(elem_id, "dark", dark) + _shared_style_block(elem_id)
        + "]]></style>\n"
    )
    parts = [
        '<svg id="%s" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %.1f %.1f" '
        'width="%.1f" height="%.1f" role="img" aria-label="%s" data-preset="%s" '
        'data-theme="%s">'
        % (elem_id, width, height, width, height, _esc(title), preset, theme),
        "<title>%s</title>" % _esc(title),
        style,
        '<defs>'
        '<marker id="hsd-arrow-light" viewBox="0 0 10 10" refX="10" refY="5" '
        'markerUnits="userSpaceOnUse" markerWidth="10" markerHeight="10" '
        'orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" style="fill:%s" /></marker>'
        '<marker id="hsd-arrow-dark" viewBox="0 0 10 10" refX="10" refY="5" '
        'markerUnits="userSpaceOnUse" markerWidth="10" markerHeight="10" '
        'orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" style="fill:%s" /></marker>'
        '</defs>' % (light.edge, dark.edge),
        '<rect class="hsd-bg" x="0" y="0" width="%.1f" height="%.1f" />' % (width, height),
    ]
    for g in layout.groups:
        parts.append(_group_markup(g))
    for ln in getattr(layout, "lifelines", ()):
        parts.append(_lifeline_markup(ln))
    for e in layout.edges:
        parts.append(_edge_markup(e))
    for n in layout.nodes:
        parts.append(_node_markup(n))
    parts.append("</svg>")
    return "\n".join(parts)


def render_svg(ir: dict, layout, preset: str = "classic", theme: str = "light") -> str:
    """The `svg` output format: a bare, standalone `<svg>` document."""
    return render_svg_markup(ir, layout, preset, theme, elem_id="hsd")


def render_fragment(ir: dict, layout, preset: str = "classic", theme: str = "light",
                     elem_id: str = "hsd") -> str:
    """The `fragment` output format: one `<div>` scoped by `elem_id`, safe to
    paste into a page this module has never seen."""
    svg = render_svg_markup(ir, layout, preset, theme, elem_id=elem_id)
    return '<div class="hs-diagram" id="%s-wrap">\n%s\n</div>' % (elem_id, svg)
