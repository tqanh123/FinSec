"""A project's reader for its own language: a command the snapshot runs per file.

THE CONTRACT
    The command (`codemap.yaml` `adapters[].command`, argv, run with the repo as
    cwd) gets ONE JSON object on stdin:

        {"path": "<repo-relative>", "module": "a.b", "container": ["a"], "text": "..."}

    and answers ONE JSON object on stdout:

        {"imports": [{"stmt": "import"|"from", "level": 0, "module": null|"x",
                      "names": [...], "resolved": [[part, ...], ...],
                      "in_function": false, "under_type_checking": false}, ...],
         "symbols": [{"name": "f", "kind": "func"|"class"|"const"|"method",
                      "lines": [first, last], "sha": "...", "refs": [...],
                      "bases": [...], "methods": [<symbol>, ...]}, ...]}

    `loc`, `nbytes` and `file_sha` are the kernel's, computed from the text the
    same way for every reader. Anything else on stdout, a non-zero exit, or a
    timeout is broken, named by the reader's id and the file.

TRUST
    The command is the project's code. It runs only when the repo is trusted
    (`trust_store.is_trusted`, the same store `hs-cli trust` writes); otherwise
    `AdapterRefused` -- a deliberate stop the snapshot reports as exit 3.

WHAT IT CANNOT SEE
    * Whatever the project's reader leaves out: the kernel checks the answer's
      shape, never whether it found every import or symbol in the file.
    * Cross-file facts: like every reader it sees one file; which import targets
      exist is the pipeline's question.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, ClassVar

from .base import ImportRecord, ModuleSymbols, SymbolInfo
from .book import AdapterError, AdapterRefused, AdapterRow

#: seconds one file may take
TIMEOUT = 60


class CommandAdapter:
    ext: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, row: AdapterRow, repo: Path):
        self.row, self.repo = row, repo
        self._trusted: "bool | None" = None

    def require_trust(self) -> None:
        if self._trusted is None:
            import trust_store
            try:
                self._trusted = trust_store.is_trusted(self.repo)
            except Exception:  # noqa: BLE001 -- fail closed: an unanswerable store is no trust
                self._trusted = False
        if not self._trusted:
            raise AdapterRefused(
                "reader %r is %s's own command (%s); it runs only in a trusted repo -- "
                "run `hs-cli trust %s` if you mean it"
                % (self.row.id, self.repo, " ".join(self.row.command), self.repo))

    def parse(self, path: Path, text: str, container: "tuple[str, ...]",
              module: str) -> ModuleSymbols:
        self.require_trust()
        try:
            rel = path.relative_to(self.repo).as_posix()
        except ValueError:
            rel = str(path)
        at = "reader %r on %s" % (self.row.id, rel)
        req = json.dumps({"path": rel, "module": module, "container": list(container),
                          "text": text})
        import process_group
        try:
            r = process_group.run(list(self.row.command), timeout=TIMEOUT, input=req,
                                  capture_output=True, text=True, cwd=str(self.repo))
        except subprocess.TimeoutExpired as e:
            raise AdapterError("%s: no answer in %ds" % (at, TIMEOUT)) from e
        except OSError as e:
            raise AdapterError("%s: cannot run %s: %s" % (at, self.row.command[0], e)) from e
        if r.returncode != 0:
            raise AdapterError("%s: exit %d: %s" % (at, r.returncode, (r.stderr or "").strip()[-300:]))
        try:
            doc = json.loads(r.stdout)
            imports = tuple(_import(i) for i in doc.get("imports") or ())
            symbols = tuple(_symbol(s) for s in doc.get("symbols") or ())
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            raise AdapterError("%s: answer is not the reader contract (%s): %r"
                               % (at, e, (r.stdout or "")[:200])) from e
        return ModuleSymbols(
            path=path, module=module, container=container, loc=text.count("\n"),
            nbytes=len(text.encode()), file_sha=hashlib.sha256(text.encode()).hexdigest()[:16],
            text=text, imports=imports, symbols=symbols)


def _strs(v: Any) -> "tuple[str, ...]":
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise TypeError("expected a list of strings, got %r" % (v,))
    return tuple(v)


def _import(d: Any) -> ImportRecord:
    if d["stmt"] not in ("import", "from") or not isinstance(d["level"], int):
        raise ValueError("import %r" % (d,))
    mod = d.get("module")
    if mod is not None and not isinstance(mod, str):
        raise TypeError("import module %r" % (mod,))
    return ImportRecord(stmt=d["stmt"], level=d["level"], module=mod, names=_strs(d["names"]),
                        resolved=tuple(_strs(p) for p in d["resolved"]),
                        in_function=bool(d.get("in_function")),
                        under_type_checking=bool(d.get("under_type_checking")))


def _symbol(d: Any, parent: "str | None" = None) -> SymbolInfo:
    lines = d["lines"]
    if (d["kind"] not in ("func", "class", "const", "method") or not isinstance(d["name"], str)
            or not isinstance(d["sha"], str) or len(lines) != 2
            or not all(isinstance(n, int) for n in lines)):
        raise ValueError("symbol %r" % (d,))
    return SymbolInfo(name=d["name"], kind=d["kind"], lines=(lines[0], lines[1]), sha=d["sha"],
                      refs=_strs(d.get("refs") or []), bases=_strs(d.get("bases") or []),
                      parent=parent,
                      methods=tuple(_symbol(m, d["name"]) for m in d.get("methods") or ()))
