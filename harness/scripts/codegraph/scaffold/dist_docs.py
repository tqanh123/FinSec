"""The per-dist doc files `new-dist` renders from `scaffold.templates.<key>`
entries of `kind: dist-doc` -- the repo's OWN doc template, never words of
this package.

    agents-md:
      kind: dist-doc
      path: "{dist_dir}/AGENTS.md"           # where it lands
      source: docs/code-standards/slice-doc.md
      anchor: "<!-- template: dist -->"      # optional: the first fenced block after this line
      placeholders: {"<dist-name>": "{name}"} # optional: literal text -> value
    claude-md:
      kind: dist-doc
      path: "{dist_dir}/CLAUDE.md"
      link_to: AGENTS.md                     # a symlink beside it instead

`path` and every `placeholders` value are `str.format` strings over the
vocabulary `engine.substitutions` offers plus `dist_dir` (the directory the
planned `pyproject.toml` sits in), `module` and `module_path` (the dist's
import path, dotted and slashed: read off the tree, or `--module`). The
source text itself is never `.format`ed: a markdown file holds braces of its
own, so only the declared literals are replaced, in one pass.

The entry's SHAPE is checked when `codemap.yaml` loads
(`descriptor/store.py`); what only the tree can answer -- the source file,
the anchor in it, a placeholder value naming nothing -- refuses here, naming
the key, before anything is written.

WHAT IT CANNOT SEE
    * Whether the rendered doc satisfies the repo's own doc gate: it is the
      template the repo declares, filled in, never checked against a gate.
    * A placeholder literal the template does not contain: named in
      `warnings`, the text is rendered without it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

from .engine import Content, Link, declared_templates
from .errors import ScaffoldError

KIND = "dist-doc"

_WHY_ABSENT = {
    "dist_dir": "no planned pyproject.toml to place it beside",
    "module": "pass --module DOTTED",
    "module_path": "pass --module DOTTED",
}


def dist_doc_templates(descriptor: Any) -> "dict[str, dict]":
    return {k: t for k, t in declared_templates(descriptor).items()
            if isinstance(t, dict) and t.get("kind") == KIND}


def doc_substitutions(base: "dict[str, str]", files: "Sequence[tuple[str, Content]]",
                      module: "str | None") -> "dict[str, str]":
    """`base` plus `dist_dir` (when exactly one planned `pyproject.toml` says
    where the dist is) and `module`/`module_path` (when known)."""
    subs = dict(base)
    homes = {str(Path(p).parent.as_posix()) for p, _ in files if Path(p).name == "pyproject.toml"}
    if len(homes) == 1:
        subs["dist_dir"] = homes.pop()
    if module:
        subs["module"] = module
        subs["module_path"] = module.replace(".", "/")
    return subs


def _fill(text: str, subs: "dict[str, str]", key: str, field: str) -> str:
    try:
        return text.format(**subs)
    except KeyError as e:
        name = e.args[0]
        why = _WHY_ABSENT.get(name)
        raise ScaffoldError(
            "scaffold.templates.%s %s: placeholder {%s} %s (have: %s)"
            % (key, field, name, "has no value here -- " + why if why else "is unknown",
               ", ".join(sorted(subs)))) from None
    except (IndexError, ValueError) as e:
        raise ScaffoldError("scaffold.templates.%s %s: %s" % (key, field, e)) from None


def _block_after(text: str, anchor: str, key: str, source: str) -> str:
    """The first fenced block after the line that reads `anchor`."""
    lines = text.splitlines()
    want = anchor.strip()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == want)
    except StopIteration:
        raise ScaffoldError("scaffold.templates.%s: anchor %r is not a line of %s"
                            % (key, anchor, source)) from None
    fence = None
    body: "list[str]" = []
    for ln in lines[start + 1:]:
        if fence is None:
            m = re.match(r"\s*(`{3,}|~{3,})", ln)
            if m:
                fence = m.group(1)
            continue
        if ln.strip().startswith(fence) and set(ln.strip()) == {fence[0]}:
            return "\n".join(body) + "\n"
        body.append(ln)
    raise ScaffoldError("scaffold.templates.%s: no closed fenced block after anchor %r in %s"
                        % (key, anchor, source))


def render_dist_docs(repo: Path, descriptor: Any, subs: "dict[str, str]"
                     ) -> "tuple[list[tuple[str, Content]], list[str]]":
    """`(files, warnings)` for every `kind: dist-doc` template, in declared order."""
    files: "list[tuple[str, Content]]" = []
    warnings: "list[str]" = []
    for key, tmpl in dist_doc_templates(descriptor).items():
        path = _fill(tmpl["path"], subs, key, "path")
        if "link_to" in tmpl:
            files.append((path, Link(tmpl["link_to"])))
            continue
        source = tmpl["source"]
        src = Path(repo) / source
        if not src.is_file():
            raise ScaffoldError("scaffold.templates.%s: source %s is not a file of this repo"
                                % (key, source))
        text = src.read_text(encoding="utf-8")
        if tmpl.get("anchor"):
            text = _block_after(text, tmpl["anchor"], key, source)
        values = {lit: _fill(v, subs, key, "placeholders[%r]" % lit)
                  for lit, v in (tmpl.get("placeholders") or {}).items()}
        missing = sorted(lit for lit in values if lit not in text)
        if missing:
            warnings.append("scaffold.templates.%s: placeholder(s) %s not found in the template "
                            "read from %s" % (key, ", ".join(missing), source))
        if values:
            pattern = "|".join(re.escape(lit) for lit in sorted(values, key=len, reverse=True))
            text = re.sub(pattern, lambda m: values[m.group(0)], text)
        files.append((path, text))
    return files, warnings
