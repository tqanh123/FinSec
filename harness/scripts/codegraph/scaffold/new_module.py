"""hs-code scaffold new-module -- a new module rendered from a declared
`scaffold.templates` entry (`kind: module`). A DRY RUN by default
(`data.dry_run: true`, the rendered paths printed): writing is a separate,
explicit act, hs-code's shared `--apply`, into `--target DIR` (default: the
repo root). No file is ever overwritten: a collision refuses the WHOLE plan
(exit 1), never a partial write.

Exit: 0 planned (and written, under --apply) clean · 1 a planned file
already exists under the target (nothing written) · 2 no codemap.yaml, no
such template, wrong template kind, or an unknown placeholder in the template.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import hs_family

from ._layout import load_descriptor
from ._apply import apply_plan, print_report
from .engine import declared_templates, plan_module
from .inferred import infer_module
from .errors import ScaffoldError
from hs_family.cli import Command


def _new_module_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("name", help="dotted module path, e.g. pkg.sub.leaf")
    ap.add_argument("--template", default=None, metavar="KEY",
                    help="a key under codemap.yaml scaffold.templates (kind: module); "
                         "none declared: the template is read off the tree, see warnings")
    ap.add_argument("--question", default="",
                    help="the one question this module answers (available as {question})")
    ap.add_argument("--target", default=None, metavar="DIR",
                    help="where --apply writes (default: the repo root; refuses to overwrite); "
                         "without --apply nothing is written, whatever --target says")


def _new_module(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts

    repo = Path(root).resolve()
    try:
        descriptor = load_descriptor(repo)
        warnings: "list[str]" = []
        declared = declared_templates(descriptor)
        if declared:
            if args.template is None:
                raise ScaffoldError("pass --template KEY (declared: %s)" % sorted(declared))
            files = plan_module(descriptor, args.name, template=args.template, question=args.question)
        else:
            files, warnings = infer_module(repo, descriptor, args.name, question=args.question)
            if args.template:
                warnings.insert(0, "--template %r ignored: codemap.yaml declares no "
                                   "scaffold.templates" % args.template)
    except ScaffoldError as e:
        raise hs_family.UsageError(str(e)) from e

    data = {"name": args.name, "template": args.template,
            **apply_plan(repo, files, target=args.target,
                         apply=bool(getattr(opts, "apply", False))),
            "files": [{"path": p} for p, _ in files], "warnings": warnings}
    if bool(getattr(opts, "json", False)):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print_report("new-module", data, files)
    return 1 if data["refused"] else 0


NEW_MODULE = Command(add_args=_new_module_args, run=_new_module)

