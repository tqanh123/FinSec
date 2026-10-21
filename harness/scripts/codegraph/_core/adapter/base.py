"""Language adapter seam — the kernel's only door into language specifics.

WHY THIS EXISTS
    The kernel measures code trees. Python is the first language it reads, not the
    only one it can ever read: everything language-specific (how to find symbols,
    how to hash a body, how imports resolve inside one file) lives behind the
    `LanguageAdapter` protocol. A second language is a new adapter registered in
    `adapter.registry` — the pipeline, query engine and gates never change; extend
    by data and registration, not by editing the engine.

    The seam is deliberately drawn at FILE level: `parse()` sees one file and its
    container path, nothing else. Cross-file work (which import targets exist in
    the tree, unit attribution) is pipeline-level by nature and stays in
    `codegraph._core.snapshot.pipeline`.

WHAT IT CANNOT SEE (contract shared by every adapter — a blind spot listed here
is inherited by every consumer of the kernel):
    * Runtime-defined symbols (`globals()[name] = ...`, decorators that synthesize
      functions, `type()` called at runtime) do not exist in static form, so they
      are not in any snapshot. An empty result is never proof of absence.
    * `import *` cannot be resolved to names; such imports contribute no symbol
      references.
    * Anything behind dynamic dispatch (`getattr(x, name)`, dict-based dispatch,
      callbacks passed as arguments) is invisible to name resolution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable


@dataclass(frozen=True)
class ImportRecord:
    """One import statement, pre-resolved to dotted parts — file-level facts only.

    `resolved` is the raw dotted-path decomposition (relative imports resolved
    against `container`); deciding whether a target EXISTS in the measured tree
    needs the whole tree and is done by the pipeline, not here.
    """

    stmt: str                 # "from" | "import"
    level: int                # 0 = absolute; >0 = relative depth
    module: str | None        # `from X import ...` -> X; plain import -> None
    names: tuple[str, ...]    # imported names ("*" kept verbatim)
    resolved: tuple[tuple[str, ...], ...]   # dotted parts per resolve()
    in_function: bool         # import sits inside a function body ("lazy")
    under_type_checking: bool # import sits under `if TYPE_CHECKING:`


@dataclass(frozen=True)
class SymbolInfo:
    """One module-level symbol or one method, with the normalized-body hash.

    `sha` is a hash of the UNPARSED body (comments stripped, whitespace
    normalized): moving a function within a file, reformatting, or editing only
    comments does NOT change it. Line numbers are for human navigation only,
    never for matching.
    """

    name: str
    kind: str                 # func | class | const | method
    lines: tuple[int, int]
    sha: str
    refs: tuple[str, ...] = ()
    dist: str | None = None   # domain payload, CARRIED not interpreted (see pipeline)
    methods: tuple["SymbolInfo", ...] = ()
    parent: str | None = None # set for methods: the class name
    bases: tuple[str, ...] = ()  # classes: each base expression, unparsed, in order


@dataclass(frozen=True)
class ModuleSymbols:
    """Everything the adapter extracts from ONE file — symbols, imports, hashes.

    `container` is the dotted path of the enclosing package (the file's own parts
    minus its stem); `module` is the file's full dotted path. Both are needed by
    the pipeline to resolve relative imports and to key the graph.
    """

    path: Path
    module: str
    container: tuple[str, ...]
    loc: int
    nbytes: int
    file_sha: str
    text: str = field(repr=False, default="")
    #: the reader's import payload: `ImportRecord`s from the Python reader and a
    #: project's command reader; a language pack's own records (`lang.typescript.
    #: facts.TsImport`) from its reader -- read only by that pack's resolver
    imports: "tuple[Any, ...]" = ()
    symbols: tuple[SymbolInfo, ...] = ()
    dup_module_assign: tuple[str, ...] = ()
    #: where the file did not parse cleanly: `{kind, line, col}` per region (a
    #: region may hide symbols). The Python reader raises `SyntaxError` instead.
    problems: "tuple[dict[str, Any], ...]" = ()


@runtime_checkable
class LanguageAdapter(Protocol):
    """Reads one source file into `ModuleSymbols`. Pure: no I/O beyond that file."""

    ext: ClassVar[frozenset[str]]  # see PythonAdapter for a concrete value

    def parse(self, path: Path, text: str, container: tuple[str, ...],
              module: str) -> ModuleSymbols: ...
