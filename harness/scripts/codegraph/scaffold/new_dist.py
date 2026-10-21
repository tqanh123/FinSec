"""hs-code scaffold new-dist -- a new distribution rendered from a declared
`scaffold.templates` entry (`kind: dist`). Same doctrine as `new-module`:
a DRY RUN by default (`data.dry_run: true`), written only under hs-code's
shared `--apply`, into `--target DIR` (default: the repo root); no file is
ever overwritten, a collision refuses the WHOLE plan.

Exit: 0 planned (and written, under --apply) clean · 1 a planned file
already exists under the target (nothing written) · 2 no codemap.yaml, no
such template, wrong template kind, or an unknown placeholder in the
template.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import hs_family

from ._layout import load_descriptor
from ._apply import apply_plan, file_rows, print_report
from .dist_docs import dist_doc_templates, doc_substitutions, render_dist_docs
from .engine import Content, declared_templates, plan_dist, substitutions
from .inferred import prose_warning, infer_dist_plan
from .errors import ScaffoldError
from hs_family.cli import Command


_EPILOG = """\
per-dist doc files: every codemap.yaml scaffold.templates.<key> of kind: dist-doc
renders beside the new dist, whichever way the dist itself is planned:
  agents-md: {kind: dist-doc, path: "{dist_dir}/AGENTS.md",
              source: docs/standards/slice-doc.md,     # a file of this repo
              anchor: "<!-- template: dist -->",       # optional: first fenced block after it
              placeholders: {"<dist-name>": "{name}"}} # optional: literal -> value
  claude-md: {kind: dist-doc, path: "{dist_dir}/CLAUDE.md", link_to: AGENTS.md}
values may use {name} {tier} {question} {namespace} {dist_dir} {module} {module_path}.
"""


def _new_dist_args(ap: argparse.ArgumentParser) -> None:
    ap.epilog = _EPILOG
    ap.formatter_class = argparse.RawDescriptionHelpFormatter
    ap.add_argument("name", help="dist name, e.g. acme-core")
    ap.add_argument("--template", default=None, metavar="KEY",
                    help="a key under codemap.yaml scaffold.templates (kind: dist); "
                         "none declared: the template is read off the tree, see warnings")
    ap.add_argument("--tier", default="",
                    help="declared tier for this dist (free-form; available as {tier})")
    ap.add_argument("--question", default="",
                    help="the one question this dist answers (available as {question})")
    ap.add_argument("--module", default=None, metavar="DOTTED",
                    help="the dist's import path ({module} in a kind: dist-doc template); "
                         "when no kind: dist template is declared, default: spelled from "
                         "the name the way the exemplar's is")
    ap.add_argument("--set", dest="assign", action="append", default=[], metavar="KEY=VALUE",
                    help="a [tool.<namespace_root>] value for the new dist (repeatable; VALUE is "
                         "TOML: true, 3, or a bare string) -- picks the group a template is read "
                         "off when no kind: dist template is declared")
    ap.add_argument("--target", default=None, metavar="DIR",
                    help="where --apply writes (default: the repo root; refuses to overwrite); "
                         "without --apply nothing is written, whatever --target says")


def _assignments(raw: "list[str]") -> dict:
    import tomllib
    out = {}
    for item in raw:
        key, sep, val = item.partition("=")
        if not sep or not key.strip():
            raise ScaffoldError(f"--set {item!r}: expected KEY=VALUE")
        try:
            out[key.strip()] = tomllib.loads(f"v = {val}")["v"]
        except tomllib.TOMLDecodeError:
            out[key.strip()] = val
    return out


def _new_dist(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts

    repo = Path(root).resolve()
    try:
        descriptor = load_descriptor(repo)
        warnings: "list[str]" = []
        exemplar = None
        carried = None
        assign = _assignments(args.assign)
        module = args.module
        prose: "list[str]" = []
        docs = dist_doc_templates(descriptor)
        declared = {k: t for k, t in declared_templates(descriptor).items() if k not in docs}
        if declared:
            if args.template is None:
                raise ScaffoldError("pass --template KEY (declared: %s)" % sorted(declared))
            files = plan_dist(descriptor, args.name, template=args.template,
                              tier=args.tier, question=args.question)
        else:
            plan = infer_dist_plan(repo, descriptor, args.name, tier=args.tier,
                                   question=args.question, module=args.module, assign=assign)
            files, warnings, exemplar = plan["files"], plan["warnings"], plan["exemplar"]
            carried = plan["carried"]
            module, prose = plan["module"], plan["prose"]
            if args.template:
                warnings.insert(0, "--template %r ignored: codemap.yaml declares no "
                                   "scaffold.templates of kind dist" % args.template)
        subs = doc_substitutions(
            substitutions(args.name, namespace=descriptor.namespace_root or "",
                          question=args.question, tier=args.tier), files, module)
        doc_files, doc_warnings = render_dist_docs(repo, descriptor, subs)
        planned: "list[tuple[str, Content]]" = [*files, *doc_files]
        warnings += doc_warnings
        rendered = {Path(p).name for p, _ in doc_files}
        left = [n for n in prose if n not in rendered]
        if left:
            warnings.append(prose_warning(Path(exemplar or "").name, left))
        if descriptor.obligations:
            warnings.append(
                "this dist owes %d repo obligation(s) codemap.yaml declares (%s): after --apply, "
                "`hs-code check dist-obligations %s` says which are met"
                % (len(descriptor.obligations),
                   ", ".join(r["id"] for r in descriptor.obligations), args.name))
    except ScaffoldError as e:
        raise hs_family.UsageError(str(e)) from e

    data = {"name": args.name, "template": args.template,
            **apply_plan(repo, planned, target=args.target,
                         apply=bool(getattr(opts, "apply", False))),
            "files": file_rows(planned),
            "inferred_from": exemplar, "carried": carried, "warnings": warnings}
    if bool(getattr(opts, "json", False)):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print_report("new-dist", data, planned)
    return 1 if data["refused"] else 0


NEW_DIST = Command(add_args=_new_dist_args, run=_new_dist)

