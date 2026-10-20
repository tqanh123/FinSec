"""The schema-driven scaffolding engine `new-module` and `new-dist` both plan
through. `template_for` below is also the door `slice-docs` (`kind:
slice-doc`) reads its own declared template through -- that verb's OWN
prose/path fields are not `{path, content}` file lists, so it does not call
`render_template`/`plan_module`/`plan_dist`, only `template_for`.

WHY NOT PORT UPSTREAM'S CONTENT GENERATORS
    Upstream (`devkit.docs.scaffold`, ob-tools @`60075cb89c`) bakes the
    emitted TEXT into Python functions (`_pyproject`/`_door`/
    `_module_docstring`/`_module_body`): PEP 420 layout, `[tool.ob] tier`,
    and OpsBrain gate names (`check_pkg_door`, `check_code_shape`) are
    literal strings in the tool. A harness with no namespace of its own
    cannot ship that -- it would be either wrong for every project that
    is not OpsBrain, or a second copy of the same hard-coding aimed at a
    different name.

    Instead the TEXT lives in `codemap.yaml`'s `scaffold.templates` key
    (`ProjectDescriptor.scaffold` -- a raw passthrough the kernel never
    interprets): a `kind: module`/`kind: dist` template is `{kind, files:
    [{path, content}, ...]}`, `path`/`content` are `str.format` strings
    against a FIXED, small placeholder vocabulary (`substitutions`,
    below), and this module supplies no project-specific word anywhere.
    The harness's own default (a flat one-file template) is a
    `codemap.yaml` DATA entry, not code; a pack (OpsBrain's own template)
    is another data entry beside it, never a second engine.
    A codemap declaring NO templates reads one off the tree instead
    (`inferred`) -- still data, never a word of this package's own.

WHAT IT CANNOT SEE
    * A template's own correctness: a template that emits invalid Python
      is rendered exactly as declared -- this engine checks placeholders
      resolve, never that the result parses.
    * A single-segment `name` (no dot): `moddir`/`moddir_dotted` are then
      empty strings, and a template built for a nested `pkg.leaf` shape
      renders a doubled or missing separator. The two shipped fixtures
      (`docs/STANDARDIZE.md`) both use a nested name; a template author
      choosing to support bare top-level names writes for that case.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, Union


@dataclass(frozen=True)
class Link:
    """A planned file that is a symlink to `target` (relative to its own
    directory) rather than text -- a `kind: dist-doc` entry's `link_to`."""

    target: str


#: What one planned file holds: its text, or the symlink it is.
Content = Union[str, Link]


def substitutions(name: str, *, namespace: str = "", question: str = "",
                  tier: str = "") -> "dict[str, str]":
    """The fixed placeholder vocabulary every template may use.

    `moddir` carries a trailing `/` (or is empty) so a template can write
    `{moddir}__init__.py` without a caller-side conditional slash; the same
    split is offered dotted (`moddir_dotted`, no trailing `.`) for prose.
    `tier` is free-form text a `kind: dist` template may cite (`new-dist
    --tier`); this engine validates nothing about it -- no TIERS enum lives
    in this package, matching the standing "no hard-coded namespace/tier
    literal" constraint. A single-segment `name` (a typical dist id, no dot)
    naturally gives `moddir=""`, collapsing a `{moddir}__init__.py` path to
    the package root -- no dist-specific branch needed here."""
    parts = name.split(".")
    return {
        "name": name,
        "leaf": parts[-1],
        "modpath": "/".join(parts),
        "moddir": "/".join(parts[:-1]) + "/" if len(parts) > 1 else "",
        "moddir_dotted": ".".join(parts[:-1]),
        "namespace": namespace,
        "question": question,
        "tier": tier,
    }


def declared_templates(descriptor: Any) -> "dict[str, Any]":
    """`scaffold.templates` as declared -- `{}` when the codemap has none, the
    case `inferred` reads a template off the tree for."""
    return (descriptor.scaffold or {}).get("templates") or {}


def template_for(descriptor: Any, key: str, *, kind: str) -> "dict[str, Any]":
    """The declared `scaffold.templates.<key>`, checked against the `kind`
    this verb needs. Missing key and wrong kind are both named in the
    message -- never a bare KeyError, never a silent pick of "the first
    template of the right kind"."""
    from .errors import ScaffoldError

    templates = declared_templates(descriptor)
    if key not in templates:
        raise ScaffoldError(
            "no scaffold template named %r declared in codemap.yaml "
            "scaffold.templates (have: %s)" % (key, sorted(templates)))
    tmpl = templates[key]
    if not isinstance(tmpl, dict) or tmpl.get("kind") != kind:
        raise ScaffoldError(
            "scaffold template %r declares kind=%r, this verb needs kind=%r"
            % (key, (tmpl or {}).get("kind"), kind))
    return tmpl


def render_template(tmpl: "dict[str, Any]", subs: "dict[str, str]") -> "list[tuple[str, str]]":
    """`(path, content)` for every file the template declares, both run
    through `str.format(**subs)`. An unknown placeholder raises naming it,
    rather than the raw KeyError a bare `.format()` call would surface."""
    from .errors import ScaffoldError

    out: "list[tuple[str, str]]" = []
    for i, f in enumerate(tmpl.get("files") or ()):
        try:
            path = str(f["path"]).format(**subs)
            content = str(f["content"]).format(**subs)
        except KeyError as e:
            raise ScaffoldError(
                "template file entry [%d] uses unknown placeholder %s" % (i, e)) from None
        out.append((path, content))
    return out


def plan_module(descriptor: Any, name: str, *, template: str, question: str = "") -> "list[tuple[str, str]]":
    """`(path, content)` for `new-module <name> --template <key>` -- the
    plan only, nothing written (see `write_plan`)."""
    tmpl = template_for(descriptor, template, kind="module")
    subs = substitutions(name, namespace=descriptor.namespace_root or "", question=question)
    return render_template(tmpl, subs)


def plan_dist(descriptor: Any, name: str, *, template: str, tier: str = "",
             question: str = "") -> "list[tuple[str, str]]":
    """`(path, content)` for `new-dist <name> --template <key>` -- the plan
    only, nothing written (see `write_plan`)."""
    tmpl = template_for(descriptor, template, kind="dist")
    subs = substitutions(name, namespace=descriptor.namespace_root or "", question=question, tier=tier)
    return render_template(tmpl, subs)


def write_plan(target: Path, files: "Sequence[tuple[str, Content]]") -> "tuple[list[str], list[str]]":
    """Write `files` under `target` -> `(written, refused)`.

    All-or-nothing on the CHECK, not on the write: every destination is
    tested for existence first (a dangling symlink counts), so a collision
    leaves `target` completely untouched rather than half-scaffolded.
    `refused` is the sorted list of colliding relative paths; a non-empty
    `refused` means `written` is always `[]`. A `Link` is written as a
    symlink, never as a copy of what it points at."""
    target = Path(target)
    collisions = collisions_under(target, files)
    if collisions:
        return [], collisions
    written: "list[str]" = []
    for p, content in files:
        dest = target / p
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Link):
            dest.symlink_to(content.target)
        else:
            dest.write_text(content, encoding="utf-8")
        written.append(p)
    return sorted(written), []


def collisions_under(target: Path, files: "Sequence[tuple[str, Content]]") -> "list[str]":
    """The planned paths that already exist under `target`, sorted."""
    return sorted(p for p, _ in files if os.path.lexists(Path(target) / p))
