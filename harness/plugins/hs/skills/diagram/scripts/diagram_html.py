#!/usr/bin/env python3
"""diagram_html.py — wrap a rendered diagram (diagram_render.py) into one
self-contained, interactive HTML document: no CDN, no network fetch, no build
step. Both themes' CSS already live in the SVG (diagram_render's `<style>`
carries light AND dark), so the toggle button only flips a `data-theme`
attribute -- no re-layout, no re-render.

Interactions, deliberately kept to three (real, not a stub, but not chasing
feature parity with a heavier reader): type-to-search dims every node whose
label does not match, clicking a node isolates it and its direct neighbours
(built from the compiled edge list -- grounded in the diagram's own topology,
never guessed), and the theme button flips light/dark.
"""
from __future__ import annotations

import json
from xml.sax.saxutils import escape as _xml_escape

from diagram_render import render_svg_markup

_SCRIPT_TEMPLATE = """
(function () {
  var root = document.getElementById("%(elem_id)s");
  if (!root) return;
  var adjacency = JSON.parse(document.getElementById("%(elem_id)s-adjacency").textContent);
  var nodes = Array.prototype.slice.call(root.querySelectorAll(".hsd-node"));
  var edges = Array.prototype.slice.call(root.querySelectorAll(".hsd-edge"));
  var search = document.getElementById("%(elem_id)s-search");
  var themeBtn = document.getElementById("%(elem_id)s-theme-toggle");
  var focused = null;

  function clearHighlights() {
    nodes.forEach(function (n) { n.classList.remove("hsd-dim", "hsd-match"); });
    edges.forEach(function (e) { e.classList.remove("hsd-dim"); });
  }

  if (search) {
    search.addEventListener("input", function () {
      var q = search.value.trim().toLowerCase();
      clearHighlights();
      focused = null;
      if (!q) return;
      nodes.forEach(function (n) {
        var label = n.getAttribute("data-label") || "";
        if (label.indexOf(q) === -1) { n.classList.add("hsd-dim"); }
        else { n.classList.add("hsd-match"); }
      });
    });
  }

  nodes.forEach(function (n) {
    n.addEventListener("click", function () {
      var id = n.getAttribute("data-id");
      if (search) { search.value = ""; }
      if (focused === id) { focused = null; clearHighlights(); return; }
      focused = id;
      var neighbours = adjacency[id] || [];
      clearHighlights();
      nodes.forEach(function (m) {
        var mid = m.getAttribute("data-id");
        if (mid !== id && neighbours.indexOf(mid) === -1) { m.classList.add("hsd-dim"); }
      });
      edges.forEach(function (e) {
        if (e.getAttribute("data-source") !== id && e.getAttribute("data-target") !== id) {
          e.classList.add("hsd-dim");
        }
      });
    });
  });

  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      document.body.setAttribute("data-page-theme", next);
    });
  }
})();
"""

_PAGE_STYLE = """
  html, body { margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0b1220; color: #e2e8f0; }
  body[data-page-theme="light"] { background: #f8fafc; color: #0f172a; }
  .hs-diagram-toolbar {
    display: flex; gap: 10px; align-items: center; padding: 10px 14px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.35);
  }
  .hs-diagram-toolbar input, .hs-diagram-toolbar button {
    padding: 6px 10px; border-radius: 6px; border: 1px solid rgba(148, 163, 184, 0.5);
    background: transparent; color: inherit; font: inherit;
  }
  .hs-diagram-toolbar button { cursor: pointer; }
  .hs-diagram-body { padding: 14px; overflow: auto; }
"""


def _build_adjacency(layout) -> dict:
    adjacency: dict = {}
    for e in layout.edges:
        adjacency.setdefault(e.source, set()).add(e.target)
        adjacency.setdefault(e.target, set()).add(e.source)
    return {k: sorted(v) for k, v in adjacency.items()}


def render_html(ir: dict, layout, preset: str = "classic", theme: str = "light",
                 elem_id: str = "hsd") -> str:
    """The `html` output format: one self-contained interactive document."""
    if theme not in ("light", "dark"):
        raise ValueError("unknown theme %r" % (theme,))
    svg = render_svg_markup(ir, layout, preset, theme, elem_id=elem_id)
    title = ir.get("meta", {}).get("title") or ir.get("diagram_type", "diagram")
    adjacency_json = json.dumps(_build_adjacency(layout))
    script = _SCRIPT_TEMPLATE % {"elem_id": elem_id}
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />\n'
        "<title>%s</title>\n"
        "<style>%s</style>\n"
        "</head>\n"
        '<body data-page-theme="%s">\n'
        '<div class="hs-diagram-toolbar">\n'
        '<input type="search" id="%s-search" placeholder="Search nodes" '
        'aria-label="Search diagram nodes" />\n'
        '<button type="button" id="%s-theme-toggle">Toggle theme</button>\n'
        "</div>\n"
        '<div class="hs-diagram-body">\n%s\n</div>\n'
        '<script type="application/json" id="%s-adjacency">%s</script>\n'
        "<script>%s</script>\n"
        "</body>\n"
        "</html>\n"
    ) % (_xml_escape(title), _PAGE_STYLE, theme, elem_id, elem_id, svg, elem_id,
         adjacency_json, script)
