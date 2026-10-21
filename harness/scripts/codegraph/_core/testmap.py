"""Test map: which unit each test FILE and test FUNCTION touches, computed
straight from a loaded snapshot — no second scan of the tree beyond reading
patch-string TEXT for the working copy of a test file (see WHAT IT CANNOT
SEE). This is the merged engine behind `hs-code graph tests`,
replacing two frozen doors that answered a NARROWER question each: the
ported tool's `query tests` (file level only, read a `_tests.yaml` sidecar a
separate generator produced — no generator exists in this tree, so that
reader was dead code) and the reference `testkit/analysis/testmap.py`
(function level, its own `<src>/<pkg>` unit notion, unrelated to a
snapshot's `<root>/<unit>` keys). Both levels here are computed from the
SAME snapshot index the other `graph` query verbs read; unit keys are the
snapshot's own, resolved by the caller through
`codegraph._core.units.resolve_unit`.

A test FILE is a snapshot record satisfying `codegraph._core.testfiles.
is_test_file` (in a test-tree ancestor directory AND a basename the target's
own `python_files` patterns match — see that module for why this is a
NARROWER question than "is this test code") — OR a record `codegraph._core.
snapshot_bridge.bridge_records` builds for a git-tracked test file the
snapshot itself never measured (outside every declared root; see that
module's own docstring). A test FUNCTION is a top-level symbol named
`test*`, or a method (`kind == "method"`, stored `"<Class>.<method>"`) of a
top-level symbol `kind == "class"` whose name starts with `Test`.

Function touches unit U iff EITHER:
  (a) one of the function's `refs` (`<module>::<symbol>`) names a module
      that is exactly one of U's modules, or
  (b) a monkeypatch-style dotted string literal (`"a.b.c"`, 2+ dotted
      segments) inside the function's line range resolves, by LONGEST
      matching module prefix, to a module of U — a string can name an
      attribute path (`pkg.mod.Class.attr`), not just a bare module, so (b)
      strips trailing segments until a known module is found; (a) never
      needs that, a ref's module part is exact.

File touches unit U iff EITHER: a function of the file touches U, or one of
the file's `imports_module_level`/`imports_function_local` targets names a
module of U (exact match, same as `importers`'s own `base_module` — a
`from pkg.mod import x` target is stored `pkg.mod::x`), or a patch string
OUTSIDE every top-level func/class span names U. Function-level touch is a
SUBSET of file-level touch by this construction — every signal that credits
a function also credits its file.

WHAT IT CANNOT SEE
    * Dynamic imports (`importlib.import_module(name)`, or any module name
      built at runtime) — invisible; a test touching a unit only that way
      reads as untouched.
    * conftest.py fixtures pulling further units in transitively — an
      otherwise single-unit test can still depend on half the tree through
      a fixture. Not analyzed.
    * A helper module living inside the tests tree: its own imports are NOT
      credited to the test functions that use it.
    * `pytest.mark.parametrize` — one row per function, never per case id.
    * Patch strings are read from the WORKING TREE text at `root / path`,
      not from the snapshot: with `--allow-stale` the file on disk may
      already differ from what the snapshot measured. A file the working
      tree no longer holds contributes no patch strings, not an error.
    * Test-file detection reads `root`'s own `python_files` config
      (`testfiles.python_files_patterns`) — pytest's own precedence,
      resolved ONCE per `build_test_map` call, not per file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from codegraph._core.testfiles import FilesLayout, build_test_layout
from codegraph._core.units import module_unit_map, unit_of_dotted

#: A dotted string literal with 2+ segments — the same shape a monkeypatch
#: target or an attribute path takes. Not anchored to any one package name:
#: resolution against the snapshot's own module set is what decides whether
#: it means anything (see `_unit_of_patch_string`).
_PATCH_RE = re.compile(r"""["']([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)["']""")


def _is_test_func(name: str) -> bool:
    return name.startswith("test")


def _is_test_class(name: str) -> bool:
    return name.startswith("Test")


@dataclass(frozen=True)
class TestFunc:
    """One test function/method. `id` is `"<file>::<name>"` — `name` is the
    bare function name, or `"<Class>.<method>"` for a class method."""

    id: str
    file: str
    name: str
    lines: "tuple[int, int]"
    refs_touch: "tuple[str, ...]"     # units via ref module (exact match)
    patch_touch: "tuple[str, ...]"    # units via a monkeypatch string in range

    @property
    def touch(self) -> "frozenset[str]":
        return frozenset(self.refs_touch) | frozenset(self.patch_touch)


@dataclass
class TestFile:
    path: str
    funcs: "list[TestFunc]" = field(default_factory=list)
    import_touch: "tuple[str, ...]" = ()          # module-level/function-local imports
    outside_patch_touch: "tuple[str, ...]" = ()   # patch strings outside every func/class
    #: the pack that named it ("" for Python): its test functions are read only for Python
    lang: str = ""

    def touch(self) -> "frozenset[str]":
        out: set = set(self.import_touch) | set(self.outside_patch_touch)
        for f in self.funcs:
            out |= f.touch
        return frozenset(out)


@dataclass
class TestMap:
    files: "list[TestFile]"

    def all_funcs(self) -> "list[TestFunc]":
        return [f for tf in self.files for f in tf.funcs]

    def function_level_unavailable(self, unit: "str | None" = None) -> "list[dict[str, str]]":
        """The test files whose functions this build does not read (another language's),
        touching `unit` when given: listed, so a function-level count never reads 0
        for them in silence."""
        return [{"path": tf.path, "reason": "%s test functions are not read in this build" % tf.lang}
                for tf in sorted(self.files, key=lambda t: t.path)
                if tf.lang and (unit is None or unit in tf.touch())]


# Membership lookups live in `units` (the `graph unit` edges read them too);
# rebound, not reimplemented — one definition.
_module_unit_map = module_unit_map
_unit_of_patch_string = unit_of_dotted


def _build_func(path: str, name: str, lines: "tuple[int, int]", refs: "list[str]",
                line_patches: "dict[int, set[str]]", mod_unit: "dict[str, str]") -> TestFunc:
    refs_touch: set = set()
    for r in refs:
        mod = r.split("::", 1)[0]
        u = mod_unit.get(mod)
        if u:
            refs_touch.add(u)
    patch_touch: set = set()
    for ln in range(lines[0], lines[1] + 1):
        patch_touch |= line_patches.get(ln, set())
    return TestFunc(id=f"{path}::{name}", file=path, name=name, lines=lines,
                    refs_touch=tuple(sorted(refs_touch)),
                    patch_touch=tuple(sorted(patch_touch)))


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _build_test_file(m: dict, mod_unit: "dict[str, str]", root: Path,
                     layout: FilesLayout) -> "TestFile | None":
    """One snapshot-shaped record `m` -> its `TestFile`, or `None` when `m`
    is not a test record at all. Shared by both the snapshot's own records
    and `snapshot_bridge.bridge_records`' — a bridged record is built to the
    SAME shape (see that module's docstring), so it needs no branch here."""
    path = m["path"]
    if not layout.is_test_file(path):
        return None
    if m.get("lang"):
        touch = {mod_unit[t.split("::", 1)[0]] for t in list(m.get("imports_module_level", []))
                 + list(m.get("imports_function_local", [])) if t.split("::", 1)[0] in mod_unit}
        return TestFile(path=path, import_touch=tuple(sorted(touch)), lang=m["lang"])
    text = _read_text(root / path)

    line_patches: "dict[int, set[str]]" = {}
    for pm in _PATCH_RE.finditer(text):
        u = _unit_of_patch_string(pm.group(1), mod_unit)
        if u:
            line = text.count("\n", 0, pm.start()) + 1
            line_patches.setdefault(line, set()).add(u)

    import_touch: set = set()
    for tgt in list(m.get("imports_module_level", [])) + list(m.get("imports_function_local", [])):
        u = mod_unit.get(tgt.split("::", 1)[0])
        if u:
            import_touch.add(u)

    spans: "list[tuple[int, int]]" = []
    funcs: "list[TestFunc]" = []
    for s in m.get("symbols", []):
        kind = s.get("kind")
        if kind in ("func", "class"):
            spans.append((s["lines"][0], s["lines"][1]))
        if kind == "func" and _is_test_func(s["name"]):
            funcs.append(_build_func(path, s["name"], tuple(s["lines"]),
                                     list(s.get("refs", [])), line_patches, mod_unit))
        elif kind == "class" and _is_test_class(s["name"]):
            for meth in s.get("methods", []):
                bare = meth["name"].split(".", 1)[-1]
                if _is_test_func(bare):
                    funcs.append(_build_func(path, meth["name"], tuple(meth["lines"]),
                                             list(meth.get("refs", [])), line_patches, mod_unit))

    outside_patch: set = set()
    for line, units in line_patches.items():
        if not any(a <= line <= b for a, b in spans):
            outside_patch |= units

    return TestFile(path=path, funcs=funcs,
                    import_touch=tuple(sorted(import_touch)),
                    outside_patch_touch=tuple(sorted(outside_patch)))


def build_test_map(idx: dict, root: Path) -> TestMap:
    """Every test-file record in `idx`, PLUS every record
    `codegraph._core.snapshot_bridge.bridge_records` builds for a
    git-tracked test file `idx` never measured (see that module's
    docstring) — its functions, and their touch sets. `root`'s own
    `FilesLayout` (`tests_subdir`/`python_files`) is resolved ONCE here, not
    per file, and passed through to `bridge_records` so one call self-loads
    `codemap.yaml` at most once, not twice."""
    from codegraph._core.snapshot_bridge import bridge_records

    mod_unit = _module_unit_map(idx)
    layout = build_test_layout(root).with_roles(idx)
    files: "list[TestFile]" = []
    for _unit, mods in idx.get("units", {}).items():
        for m in mods:
            tf = _build_test_file(m, mod_unit, root, layout)
            if tf is not None:
                files.append(tf)
    # measured by the snapshot pass itself, outside every root; never in
    # `mod_unit` -- a test helper is no unit's code
    for m in idx.get("test_modules") or ():
        tf = _build_test_file(m, mod_unit, root, layout)
        if tf is not None:
            files.append(tf)
    for m in bridge_records(idx, root, layout):
        tf = _build_test_file(m, mod_unit, root, layout)
        if tf is not None:
            files.append(tf)
    return TestMap(files=files)


# ── unit-level view ──────────────────────────────────────────────────────────

def owned_and_shared(tmap: TestMap, unit: str) -> "tuple[list[str], list[str]]":
    """Files touching `unit`, split by whether they ALSO touch another unit."""
    owned: "list[str]" = []
    shared: "list[str]" = []
    for tf in tmap.files:
        touched = tf.touch()
        if unit not in touched:
            continue
        (shared if touched - {unit} else owned).append(tf.path)
    return sorted(owned), sorted(shared)


def unit_stats(tmap: TestMap, all_units: "set[str]") -> dict:
    owned = shared = 0
    orphans: "list[str]" = []
    touched_any: set = set()
    for tf in tmap.files:
        touched = tf.touch()
        touched_any |= touched
        if not touched:
            orphans.append(tf.path)
        elif len(touched) == 1:
            owned += 1
        else:
            shared += 1
    return {
        "total_test_files": len(tmap.files),
        "owned_files": owned,
        "shared_files": shared,
        "orphan_files": len(orphans),
        "orphan_paths": sorted(orphans),
        "units_without_test_file": sorted(all_units - touched_any),
    }


# ── function-level view ──────────────────────────────────────────────────────

def functions_touching(tmap: TestMap, unit: str) -> "list[str]":
    return sorted(f.id for f in tmap.all_funcs() if unit in f.touch)


def function_stats(tmap: TestMap, all_units: "set[str]") -> dict:
    funcs = tmap.all_funcs()
    one = [f for f in funcs if len(f.touch) == 1]
    many = [f for f in funcs if len(f.touch) > 1]
    blind = [f for f in funcs if not f.touch]
    patch_only = [f for f in funcs if f.patch_touch and not f.refs_touch]
    touched_any = {u for f in funcs for u in f.touch}
    return {
        "total_test_files": len(tmap.files),
        "total_test_funcs": len(funcs),
        "one_unit_funcs": len(one),
        "shared_funcs": len(many),
        "blind_funcs": len(blind),
        "blind_func_ids": sorted(f.id for f in blind),
        "patch_only_funcs": len(patch_only),
        "units_without_direct_test": sorted(all_units - touched_any),
    }
