"""hs_run_facts.code_mentions — the `code.mentions` fact provider.

`provide(ctx) -> {value, basis, warning}`: where each named subject is still
WRITTEN in the tracked tree, each match typed by where it sits
(`code_mentions`: code / docstring / comment / string / data / doc / text).

A subject that resolves to a live symbol (`module::name`, `pkg.mod.name`, or a
bare name one definition carries -- `code_callers._qualify`, the same reading)
is searched by its bare name, and its `code` hits are COUNTED, never listed:
`code.callers`/`code.importers` already list those. Any other subject (an env
name, a config key, a message) is searched verbatim and every kind is listed,
since no other fact sees it. Each subject: `by_kind` counts every match, `rows`
stop at `ROWS` (`truncated` says so). An empty `by_kind` is a measured zero.
"""
from __future__ import annotations

import re
from pathlib import Path

import regex

import code_mentions
import codegraph_change_reader
from hs_run_facts import code_callers

ROWS = 20
_PER_FILE_SECONDS = 2.0

_BASIS = (
    "whole-word matches over every git-tracked file, typed by where each sits "
    "(code/docstring/comment/string by tokens+AST in .py; data with its key path "
    "in YAML/TOML/JSON; doc with heading path in Markdown; else text). A live "
    "symbol's `code` hits are counted, not listed (code.callers/code.importers "
    "list them); rows stop at %d per subject" % ROWS
)

_WARNING = (
    "code.mentions reads text: a name built at runtime (string concatenation, "
    "getattr with a computed name) is invisible, and a short common name matches "
    "every unrelated use of that word"
)


def _needle(text: str) -> str:
    """Whole-word where the subject's edge is a word character."""
    head = r"(?<![\w])" if re.match(r"\w", text) else ""
    tail = r"(?![\w])" if re.search(r"\w$", text) else ""
    return head + re.escape(text) + tail


def _module_of(idx: dict):
    by_path = {m["path"]: m["module"] for m in codegraph_change_reader._modules(idx)}
    return lambda p: by_path.get(p)


def mention_table(root: Path, idx: dict, subjects: "dict[str, tuple[str, bool]]") -> "dict[str, dict]":
    """`{label: {searched_as, live, by_kind, rows, truncated}}` for
    `{label: (text to find, live symbol?)}` -- one pass over the tree for all."""
    if not subjects:
        return {}
    texts = sorted({t for t, _ in subjects.values()}, key=len, reverse=True)
    pat = regex.compile("|".join(_needle(t) for t in texts))

    def find(_rel: str, text: str) -> list:
        try:
            return list(pat.finditer(text, timeout=_PER_FILE_SECONDS))
        except TimeoutError:
            return []   # escaped literals cannot backtrack; a pathological file is skipped

    hits, _notes = code_mentions.scan_with(root, find, module_of=_module_of(idx))
    by_text: "dict[str, list[dict]]" = {}
    for h in hits:
        by_text.setdefault(h["match"], []).append(h)
    out: "dict[str, dict]" = {}
    for label, (text, live) in sorted(subjects.items()):
        mine = by_text.get(text, [])
        by_kind: "dict[str, int]" = {}
        for h in mine:
            by_kind[h["kind"]] = by_kind.get(h["kind"], 0) + 1
        listed = [h for h in mine if not (live and h["kind"] == "code")]
        out[label] = {
            "searched_as": text, "live": live, "by_kind": by_kind,
            "rows": [{"file": h["file"], "line": h["line"], "kind": h["kind"],
                      "where": h["enclosing"] or h["context"]} for h in listed[:ROWS]],
            "truncated": len(listed) > ROWS,
        }
    return out


def provide(ctx: dict) -> dict:
    idx = ctx["snapshot"]
    defined = codegraph_change_reader.qualified_index(idx)
    modules = {info["module"] for info in defined.values()}
    by_bare: "dict[str, list[str]]" = {}
    for q in defined:
        by_bare.setdefault(q.partition("::")[2], []).append(q)
    subjects: "dict[str, tuple[str, bool]]" = {}
    for s in sorted(set(ctx.get("subjects") or ())):
        q, _shared = code_callers._qualify(s, defined, modules, by_bare)
        if q is not None:
            subjects[q] = (q.partition("::")[2].rsplit(".", 1)[-1], True)
        else:
            subjects[s] = (s, False)
    value = mention_table(Path(ctx["root"]), idx, subjects)
    return {"value": value, "basis": _BASIS, "warning": _WARNING}
