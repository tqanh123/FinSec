"""`codemap.yaml` `obligations:` -- what a new dist owes the repo's OWN
registries (a root pyproject key, a roles block, a ledger in a script, a
durations file), one row each:

    obligations:
    - id: uv-source                       # optional; default obligations[<n>]
      path: pyproject.toml                # placeholders allowed
      contains: "{dist} = { workspace = true }"
      note: root [tool.uv.sources]        # optional
    - id: mypy-path
      path: pyproject.toml
      regex: 'mypy_path\\s*=.*"{dist_dir}/src"'

Exactly one of `contains` (a substring) or `regex` (searched, multiline). The
placeholders are `PLACEHOLDERS`; in a `regex` each value is inserted escaped.
Any other `{word}` is refused when the codemap loads (a typo would otherwise
read as a row nobody can meet); `{{`/`}}` spell a literal brace. Every other
brace -- a TOML inline table, a regex quantifier -- is left as written.

`hs-code check dist-obligations` reads these rows; the descriptor loader
parses them here, so a malformed row is refused before any verb runs.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

PLACEHOLDERS: "tuple[str, ...]" = ("dist", "dist_dir", "module", "module_path")
KEYS: "frozenset[str]" = frozenset({"id", "path", "contains", "regex", "note"})

_TOKEN = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ObligationError(ValueError):
    """A row a dist cannot be checked against, or a value it cannot be spelled with."""


def render(template: str, values: "Mapping[str, str]", *, escape: bool = False) -> str:
    """`template` with every placeholder filled (`re.escape`d when `escape`);
    a placeholder `values` lacks raises naming it."""
    def sub(m: "re.Match[str]") -> str:
        tok = m.group(0)
        if tok in ("{{", "}}"):
            return re.escape(tok[0]) if escape else tok[0]
        name = m.group(1)
        if name not in values:
            raise ObligationError("{%s} has no value for this dist" % name)
        return re.escape(values[name]) if escape else values[name]
    return _TOKEN.sub(sub, template)


def _unknown(template: str) -> "list[str]":
    return sorted({m.group(1) for m in _TOKEN.finditer(template)
                   if m.group(1) and m.group(1) not in PLACEHOLDERS})


def parse(raw: Any) -> "tuple[dict[str, Any], ...]":
    """`obligations:` -> `({id, path, how, pattern, note}, ...)`; ObligationError
    names the row and what is wrong with it."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ObligationError("`obligations:` must be a list of rows")
    out: "list[dict[str, Any]]" = []
    seen: "set[str]" = set()
    for i, row in enumerate(raw, 1):
        where = f"obligations[{i}]"
        if not isinstance(row, dict):
            raise ObligationError(f"{where} must be a mapping")
        unknown = sorted(set(row) - KEYS)
        if unknown:
            raise ObligationError(f"{where}: unknown keys {', '.join(unknown)} "
                                  f"(valid: {', '.join(sorted(KEYS))})")
        hows = [k for k in ("contains", "regex") if k in row]
        if len(hows) != 1:
            raise ObligationError(f"{where}: needs exactly one of `contains` or `regex`")
        how = hows[0]
        for key in ("path", how):
            if not isinstance(row.get(key), str) or not row[key]:
                raise ObligationError(f"{where}: `{key}` must be a non-empty string")
            bad = _unknown(row[key])
            if bad:
                raise ObligationError(
                    f"{where}: `{key}` uses {', '.join('{%s}' % b for b in bad)} -- known: "
                    f"{', '.join('{%s}' % p for p in PLACEHOLDERS)} ({{{{ }}}} for a literal brace)")
        if row["path"].startswith("/") or ".." in row["path"].split("/"):
            raise ObligationError(f"{where}: `path` must be repo-relative")
        if how == "regex":
            try:
                re.compile(render(row["regex"], {p: p for p in PLACEHOLDERS}, escape=True))
            except re.error as e:
                raise ObligationError(f"{where}: `regex` does not compile: {e}") from None
        rid = row.get("id", where)
        if not isinstance(rid, str) or not rid or rid in seen:
            raise ObligationError(f"{where}: `id` must be a unique non-empty string")
        seen.add(rid)
        note = row.get("note")
        if note is not None and not isinstance(note, str):
            raise ObligationError(f"{where}: `note` must be a string")
        out.append({"id": rid, "path": row["path"], "how": how, "pattern": row[how],
                    "note": note})
    return tuple(out)
