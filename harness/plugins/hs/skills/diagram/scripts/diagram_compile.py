#!/usr/bin/env python3
"""diagram_compile.py — hs:diagram's CLI: validate -> layout -> render, one command.

Kept as three composable calls (diagram_ir.validate_ir, diagram_layout.compile_layout,
one of diagram_render/diagram_html) rather than a single do-everything function, so a
caller that already validated upstream (or wants its own error reporting) can call
`compile_diagram` directly and skip re-validating.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import json

from diagram_html import render_html
from diagram_ir import DIAGRAM_TYPES, load_ir, load_schema, validate_ir
from diagram_layout import compile_layout
from diagram_render import render_fragment, render_svg

FORMATS = ("svg", "fragment", "html")
PRESETS = ("classic", "blueprint")
THEMES = ("light", "dark")


def compile_diagram(ir: dict, fmt: str, preset: str = "classic", theme: str = "light",
                     elem_id: str = "hsd"):
    """Returns (markup, layout) for an ALREADY-VALID `ir` document."""
    if fmt not in FORMATS:
        raise ValueError("unknown format %r -- use one of %s" % (fmt, FORMATS))
    layout = compile_layout(ir)
    if fmt == "svg":
        markup = render_svg(ir, layout, preset, theme)
    elif fmt == "fragment":
        markup = render_fragment(ir, layout, preset, theme, elem_id=elem_id)
    else:
        markup = render_html(ir, layout, preset, theme, elem_id=elem_id)
    return markup, layout


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compile an hs:diagram IR document")
    parser.add_argument("--input", required=False)
    parser.add_argument("--format", choices=FORMATS, default="svg")
    parser.add_argument("--preset", choices=PRESETS, default="classic")
    parser.add_argument("--theme", choices=THEMES, default="light")
    parser.add_argument("--out")
    parser.add_argument("--id", dest="elem_id", default="hsd")
    parser.add_argument("--schema", choices=DIAGRAM_TYPES,
                         help="print diagram_type's merged JSON schema and exit "
                              "(no --input needed) -- the contract an editor or "
                              "external tool can validate a document against")
    args = parser.parse_args(argv)

    if args.schema:
        print(json.dumps(load_schema(args.schema), indent=2))
        return 0

    if not args.input:
        parser.error("--input is required unless --schema is given")

    ir = load_ir(args.input)
    errors = validate_ir(ir)
    if errors:
        for e in errors:
            print("%s: %s -- %s" % (e.path, e.message, e.hint), file=sys.stderr)
        print("%d validation error(s) -- not compiled" % len(errors), file=sys.stderr)
        return 2

    markup, layout = compile_diagram(ir, args.format, args.preset, args.theme, args.elem_id)

    for w in layout.warnings:
        print("warning: %s" % w, file=sys.stderr)
    print("engine: %s" % layout.engine, file=sys.stderr)

    if args.out:
        Path(args.out).write_text(markup, encoding="utf-8")
        print(args.out)
    else:
        print(markup)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
