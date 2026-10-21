"""The TypeScript/JavaScript pack's import payload: what one statement imports -- and
`Refs`, what one file references (`symbol_uses`).

The kernel carries a reader's imports opaquely (`ModuleSymbols.imports`); this pack's
reader fills them with `TsImport` and its resolver is their only reader.

    kind      static | export_from | export_all | side_effect | dynamic | require |
              import_equals
    spec      the module specifier when it is a string literal, else None
    spec_expr {kind, text} of a specifier that is not a literal (`import(x)`)
    bindings  (imported, local, type_only) per name: `imported` is a name, `default`
              or `*`; `local` what this file calls it
    type_only the whole statement is type-only (`import type`, `export type`, an
              `import()` in a type position); a single binding says so in its own slot
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

KINDS = ("static", "export_from", "export_all", "side_effect", "dynamic", "require", "import_equals")


@dataclass(frozen=True)
class TsImport:
    kind: str
    spec: "str | None"
    bindings: "tuple[tuple[str, str, bool], ...]" = ()
    type_only: bool = False
    line: int = 0
    in_function: bool = False
    spec_expr: "dict[str, Any] | None" = None


@dataclass(frozen=True)
class Refs:
    """One file's references: `plain` (line, name) for an identifier that reads a name,
    `member` (line, object, property) for `object.property` / `Object.Type` /
    `<object.Tag>`, `requires` (line, spec, ((imported, local), ...)) for `const k =
    require(spec)` (`("*", k)`) and `const {a, b: c} = require(spec)`. Identifiers inside an
    import or re-export statement are not references."""

    plain: "tuple[tuple[int, str], ...]" = ()
    member: "tuple[tuple[int, str, str], ...]" = ()
    requires: "tuple[tuple[int, str, tuple[tuple[str, str], ...]], ...]" = ()
