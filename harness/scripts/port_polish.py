"""port_polish.py — idempotent hand-polish steps for `hs-code port build`'s
`hand_polish` spec field, as one FLAT module under `harness/scripts/`
(matching every sibling `port_*.py`).

Ported (algorithm-faithful) from `ob-tools@60075cb:packages/portkit/src/
portkit/build/polish.py` (`PolishStrategy`/`_ReplaceStrategy`/
`_ImportStrategy`/`_NoteStrategy`/`apply_polish`/`extend_spec_with_polish`).
Reached from two callers, both extending the same closed set rather than
each growing a private copy: `port_spec.Spec.load` (`extend_spec_with_polish`,
so a spec's `hand_polish:` list normalises prose strings into `{"note": ...}`
steps at load time, never storing a raw string a strategy dispatch would
choke on) and `codegraph_port_build_verbs.build_dist` (`apply_polish`, run
against the just-written dist tree, BEFORE the py_compile self-check).

Each step is an independent strategy. A failing step reds the build with the
step's own name; a `note:` step is prose and is skipped — old specs keep
working.

WHAT IT CANNOT SEE
    * `replace:` matches PLAIN STRINGS (no regex, no AST). A string appearing
      twice in a file is NOT applied — "not unique" is returned; split the
      step into narrower ones.
    * `import:` after: inserts after a line matching EXACTLY ONCE; an already
      present target import makes the step succeed silently (idempotent).
    * Steps apply in declared order; one failure stops the chain.
    * It does not know whether the file still COMPILES after a step — the
      builder's py_compile self-check runs right after.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class PolishStrategy(Protocol):
    """One polish step — must be idempotent: re-applying yields the same tree."""

    step_type: str

    def apply(self, root: Path, step: "Dict[str, Any]") -> "Optional[str]": ...
    def is_idempotent(self, root: Path, step: "Dict[str, Any]") -> bool: ...


class _ReplaceStrategy:
    step_type = "replace"

    def is_idempotent(self, root: Path, step: "Dict[str, Any]") -> bool:
        p = root / step["file"]
        return p.is_file() and step["old"] not in p.read_text(encoding="utf-8")

    def apply(self, root: Path, step: "Dict[str, Any]") -> "Optional[str]":
        p = root / step["file"]
        if not p.is_file():
            return "replace: no such file %s" % step["file"]
        text = p.read_text(encoding="utf-8")
        old, new = step["old"], step["new"]
        if old not in text:
            return None            # idempotent: already applied or not needed
        count = text.count(old)
        if count > 1:
            return ("replace: %s: target string occurs %dx — cannot apply "
                    "safely; split into narrower steps" % (step["file"], count))
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return None


class _ImportStrategy:
    step_type = "import"

    def _import_line(self, step: "Dict[str, Any]") -> str:
        names = step["names"]
        names_str = ", ".join(names) if isinstance(names, list) else str(names)
        return "from %s import %s" % (step["from"], names_str)

    def is_idempotent(self, root: Path, step: "Dict[str, Any]") -> bool:
        p = root / step["file"]
        if not p.is_file():
            return False
        return self._import_line(step) in p.read_text(encoding="utf-8")

    def apply(self, root: Path, step: "Dict[str, Any]") -> "Optional[str]":
        p = root / step["file"]
        if not p.is_file():
            return "import: no such file %s" % step["file"]
        text = p.read_text(encoding="utf-8")
        imp = self._import_line(step)
        if imp in text:
            return None            # idempotent
        after = step.get("after", "")
        if after and after in text:
            idx = text.index(after) + len(after)
            newline = text.find("\n", idx)
            insert_at = newline + 1 if newline >= 0 else len(text)
            text = text[:insert_at] + imp + "\n" + text[insert_at:]
        else:
            lines = text.splitlines(keepends=True)
            last_import = 0
            for i, ln in enumerate(lines):
                # MODULE level only: an indented `from x import y` is a lazy
                # import inside a function body — appending after it once
                # broke the file's indentation.
                if ln.startswith("import ") or ln.startswith("from "):
                    last_import = i
            lines.insert(last_import + 1, imp + "\n")
            text = "".join(lines)
        p.write_text(text, encoding="utf-8")
        return None


class _NoteStrategy:
    step_type = "note"

    def is_idempotent(self, root: Path, step: "Dict[str, Any]") -> bool:
        del root, step
        return True

    def apply(self, root: Path, step: "Dict[str, Any]") -> "Optional[str]":
        del root, step
        return None


_STRATEGIES: "Dict[str, PolishStrategy]" = {
    "replace": _ReplaceStrategy(),
    "import": _ImportStrategy(),
    "note": _NoteStrategy(),
}


def apply_polish(root: Path, steps: "List[Dict[str, Any]]", *,
                 findings: "Optional[List[Dict[str, Any]]]" = None) -> "List[str]":
    """Apply the whole chain; return errors (empty = success). A failing step
    stops the chain and is named; idempotent steps apply silently. A step
    that is neither a known type nor a note REDS — silently skipping it would
    let a porter believe an unapplied seam was applied.

    `findings`, when given, gets ONE `{"file": <step's own "file" key or
    None>, "why": <the same message appended to the returned list>}` per
    failure — a STRUCTURAL record of which step's own declared file failed,
    for a caller (`codegraph_port_build_verbs.build_dist`) that needs the
    file path without parsing it back out of the prose message."""
    errs: "List[str]" = []
    for i, step in enumerate(steps):
        step_type = next((k for k in step if k in _STRATEGIES), None)
        if step_type is None:
            if set(step) - {"note"}:
                msg = ("polish step %d: unrecognized step type (keys %s); "
                      "shape: replace: {file, old, new}" % (i + 1, sorted(step)))
                errs.append(msg)
                if findings is not None:
                    findings.append({"file": None, "why": msg})
                break
            continue
        body = step[step_type] if isinstance(step[step_type], dict) else {}
        strategy = _STRATEGIES[step_type]
        if strategy.is_idempotent(root, body):
            continue
        err = strategy.apply(root, body)
        if err:
            msg = "polish step %d (%s): %s" % (i + 1, step_type, err)
            errs.append(msg)
            if findings is not None:
                findings.append({"file": body.get("file"), "why": msg})
            break           # stop the chain
    return errs


def extend_spec_with_polish(spec_data: "Dict[str, Any]") -> "List[Dict[str, Any]]":
    """Read `hand_polish:` from parsed YAML into a step list. Prose strings
    from old specs are wrapped as `note:` — no crash. The ONE normaliser
    `port_spec.Spec.load` calls for this field — never a second, private
    wrap-if-not-a-dict copy living beside it."""
    raw = spec_data.get("hand_polish") or []
    result: "List[Dict[str, Any]]" = []
    for step in raw:
        result.append(step if isinstance(step, dict) else {"note": str(step)})
    return result
