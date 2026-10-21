"""hs_family.gate_registry — one book for every gate: a page per kind, a row per gate.

A page is one file, `harness/data/gate-registry.d/<page>.yaml` (or
`packs/<pack>/gate-registry.d/<page>.yaml` for a pack's own rows):

    page: hook                                         # must match the file name
    source: {module: hs_cli, entry: hook_gates}        # optional
    attach_to: hook                                    # optional, see below
    project_layer: harness-hooks.yaml                  # optional, see below
    run: {module: codegraph.check.gates, entry: run_tests}     # or `runtime`; see below
    gates:                                             # optional literal rows
      some_gate: {module: some_module, why: "..."}

`source` names the function that already owns this kind of gate; it is called
with the repo root and returns `{name: {field: value}}`, one entry per gate, so
no gate is written down in two places. `gates:` holds rows no source can see; a
row that names a `module:` must name one that exists.

A name is one row across the whole book. Two rows with one name is an error,
with one exception a page must ask for: `attach_to: <page>` makes a source row
whose name is already a row on that page an attribute of it (`row.attached[page]`)
instead of a second row; the attaching page's own rows carry the same
attribute. Guards use it: four of the nine are hooks, and a hook that is also a
guard is still one gate.

`project_layer` names the file under the project's data home that the page's
reader honours: `harness-hooks.yaml` for hooks (their own layer), `gates.yaml`
for the rest. A page without it cannot be switched from a project. A
`gates.yaml` entry turns ONE gate off and says who, when and why:

    gates:
      STD-X-001: {enabled: false, reason: "...", actor: "user:...", ts: "..."}

Anything else — a threshold key, a family (a page name or a glob), an entry that
turns a gate on, a name the book does not know — is refused: a project may turn
a gate off on the record, never loosen one.

`run` says how `hs-code check gates` runs the page's rows: a runner `{module, entry}`
called with the target root and the enabled rows, or `runtime` for gates a hook
enforces when its event fires. A page without `run` cannot run its rows.
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import attrs
import off_record
import pack_config
import project_gate_layer

from . import registry

PAGES_DIR = Path("harness") / "data" / "gate-registry.d"
GATES_LAYER = project_gate_layer.GATES_LAYER
HOOKS_LAYER = project_gate_layer.HOOKS_LAYER
PROJECT_LAYERS = (GATES_LAYER, HOOKS_LAYER)
RECORD_KEYS = off_record.RECORD_KEYS
PACKS_DIR = Path("packs")
RUNTIME = "runtime"
_MODULE_HOMES = (Path("harness") / "scripts", Path("harness") / "hooks")


class GateRegistryError(registry.RegistryError):
    """The book cannot be read as one row per gate."""


@attrs.define(frozen=True, kw_only=True)
class GateRow:
    name: str
    page: str
    path: Path
    source: Optional[str] = None
    module: Optional[str] = None
    pack: Optional[str] = None
    why: Optional[str] = None
    fields: dict = attrs.field(factory=dict)
    attached: dict = attrs.field(factory=dict)
    layer: Optional[str] = None

    @property
    def core(self) -> bool:
        """Hard core: switched off only with --force and a reason (the source says so)."""
        return self.fields.get("core") is True

    def as_rule(self) -> dict:
        """The row as a standards rule — its fields plus `id` — the shape a detector
        runner reads."""
        return dict(self.fields, id=self.name)

    @property
    def whole_tree(self) -> bool:
        """A change anywhere in the tree can redden it, so a narrow run adds it."""
        return self.fields.get("whole_tree") is True


@attrs.define(frozen=True, kw_only=True)
class GateBook:
    rows: List[GateRow]
    files: List[Path]
    runs: dict = attrs.field(factory=dict)

    def whole_tree(self) -> List[GateRow]:
        return [r for r in self.rows if r.whole_tree]

    def whole_tree_files(self) -> List[str]:
        """The `file:` of every whole-tree row: the tests a change anywhere can redden."""
        return sorted({str(r.fields["file"]) for r in self.whole_tree() if r.fields.get("file")})

    def by_page(self) -> Dict[str, List[GateRow]]:
        out: Dict[str, List[GateRow]] = {}
        for r in self.rows:
            out.setdefault(r.page, []).append(r)
        return out


def registry_files(root: Path, target: Optional[Path] = None) -> List[Path]:
    """Every page file the loader reads: the core pages, then each pack the
    TARGET enables — never every pack physically present under `root`.
    `target` is the project whose own `codemap.yaml` `preset:` says which
    packs it turns on (`pack_config.read_enabled_preset` — a plain YAML read,
    deliberately not the full descriptor loader `hs_code.py`'s own pack-verb
    seam uses, since this module must never reach `codegraph`); `root` is
    only where a pack's directory (and so its `gate-registry.d/`) physically
    lives. They differ under a global install:
    many projects share one `root` (the bin), each with its own `target`. A
    project declaring no preset (or no `codemap.yaml` at all) gets NONE of a
    pack's rows — a pack merely SHIPPING in the bin must never leak its gates
    into a project that never opted in; `target` defaults to `root` for the
    self-hosting case, where the two are the same tree."""
    root = Path(root)
    target = Path(target) if target is not None else root
    return _core_page_files(root) + _enabled_pack_files(root, target)


def _core_page_files(root: Path) -> List[Path]:
    """`harness/data/gate-registry.d/*.yaml` — the core pages every project
    shares regardless of preset. The one glob both `registry_files` above
    (preset-filtered, via `_enabled_pack_files`) and `declared_modules`'s
    preset-independent whole-tree walk (`harness/tests/gate_registry_
    declared_modules.py`, moved out of this module — its only callers are
    their own tests) start from; a second copy of this glob is what used to
    drift the two."""
    root = Path(root)
    return sorted((root / PAGES_DIR).glob("*.yaml")) if (root / PAGES_DIR).is_dir() else []


def _pack_dirs(root: Path) -> List[Path]:
    """Every pack directory under `PACKS_DIR`, preset-independent — the one
    enumeration both the preset-filtered walk below and `declared_modules`'s
    whole-tree walk share."""
    packs = root / PACKS_DIR
    return sorted(p for p in packs.iterdir() if p.is_dir()) if packs.is_dir() else []


def _pack_registry_files(pack: Path) -> List[Path]:
    """One pack directory's own page files under its `gate-registry.d/`."""
    return sorted((pack / "gate-registry.d").glob("*.yaml"))


def _enabled_pack_files(root: Path, target: Path) -> List[Path]:
    if not (root / PACKS_DIR).is_dir():
        return []
    try:
        preset = pack_config.read_enabled_preset(target)
    except pack_config.PackConfigError as e:
        raise GateRegistryError("%s: %s" % (target / "codemap.yaml", e)) from e
    enabled = {(root / rel).resolve() for rel in preset}
    files: List[Path] = []
    for pack in _pack_dirs(root):
        if pack.resolve() in enabled:
            files += _pack_registry_files(pack)
    return files


def _pack_of(root: Path, path: Path) -> Optional[str]:
    try:
        rel = path.relative_to(root / PACKS_DIR)
    except ValueError:
        return None
    return rel.parts[0]


def _module_exists(root: Path, module: str, pack: Optional[str]) -> bool:
    rel = Path(*module.split("."))
    homes = [root / PACKS_DIR / pack] if pack else [root / h for h in _MODULE_HOMES]
    return any((h / rel).with_suffix(".py").is_file() or (h / rel / "__init__.py").is_file()
               for h in homes)


def _read_page(root: Path, path: Path) -> dict:
    for bad in path.parent.glob("*.yml"):
        raise registry.RegistryExtensionError(bad)
    doc = registry.read_registry_file(path)
    if not isinstance(doc, dict):
        raise GateRegistryError("%s is not a mapping" % path)
    page = doc.get("page")
    if page != path.stem:
        raise GateRegistryError("%s declares page %r; a page is named by its file (%s.yaml)"
                                % (path, page, page))
    unknown = set(doc) - {"page", "source", "attach_to", "gates", "project_layer", "run"}
    if unknown:
        raise GateRegistryError("%s has unknown key(s): %s" % (path, ", ".join(sorted(unknown))))
    if doc.get("project_layer") not in (None, *PROJECT_LAYERS):
        raise GateRegistryError("%s: project_layer must be one of %s"
                                % (path, ", ".join(PROJECT_LAYERS)))
    run = doc.get("run")
    if run is not None and run != RUNTIME:
        if not (isinstance(run, dict) and set(run) == {"module", "entry"}):
            raise GateRegistryError("%s: run must be `%s` or {module, entry}, got %r"
                                    % (path, RUNTIME, run))
        # A runner is harness code imported at run time, not a file of the target repo.
        # A pack's own page names a module living under its OWN packs/<pack>/ dir —
        # not on sys.path yet at load time (the check door adds it before dispatch,
        # `codegraph.check.gates._pack_sys_path`) — so this checks the pack's module
        # HOME the same way a literal row's `module:` already does (`_module_exists`),
        # never `find_spec`, which would look on the wrong path and always miss.
        pack = _pack_of(root, path)
        ok = (_module_exists(root, str(run["module"]), pack) if pack
              else importlib.util.find_spec(str(run["module"])) is not None)
        if not ok:
            raise GateRegistryError("%s: run module %r cannot be imported" % (path, run["module"]))
    return doc


def _call_source(root: Path, path: Path, source) -> Dict[str, dict]:
    if not (isinstance(source, dict) and source.get("module") and source.get("entry")):
        raise GateRegistryError("%s: source must be {module, entry}, got %r" % (path, source))
    label = "%s.%s" % (source["module"], source["entry"])
    try:
        fn = getattr(importlib.import_module(str(source["module"])), str(source["entry"]))
    except (ImportError, AttributeError) as e:
        raise GateRegistryError("%s: source %s cannot be loaded: %s" % (path, label, e)) from e
    try:
        rows = fn(root)
    except Exception as e:  # noqa: BLE001 — a source's own defect is a broken page, named
        raise GateRegistryError("%s: source %s failed: %s: %s"
                                % (path, label, type(e).__name__, e)) from e
    if not isinstance(rows, dict):
        raise GateRegistryError("%s: source %s returned %s, not {name: row}"
                                % (path, label, type(rows).__name__))
    return rows


def _carried(root: Path, spec: dict) -> bool:
    """False for a row whose `file:` lives in a directory this tree does not carry — a
    test suite an install drops is not a gate of the installed tree."""
    return "file" not in spec or (root / str(spec["file"])).parent.is_dir()


def _check_fields(root: Path, path: Path, name, spec: dict) -> None:
    if "whole_tree" in spec and not isinstance(spec["whole_tree"], bool):
        raise GateRegistryError("%s: gate %s: whole_tree must be true or false, got %r"
                                % (path, name, spec["whole_tree"]))
    if "file" in spec and not (root / str(spec["file"])).is_file():
        raise GateRegistryError("%s: gate %s names file %s, which does not exist"
                                % (path, name, spec["file"]))


def _literal_rows(root: Path, path: Path, doc: dict) -> List[GateRow]:
    pack = _pack_of(root, path)
    out = []
    for name, spec in (doc.get("gates") or {}).items():
        spec = dict(spec or {})
        if not _carried(root, spec):
            continue
        module = spec.pop("module", None)
        why = spec.pop("why", None)
        if module is not None and not _module_exists(root, str(module), pack):
            raise GateRegistryError("%s: gate %s names module %r, which does not exist"
                                    % (path, name, module))
        _check_fields(root, path, name, spec)
        out.append(GateRow(name=str(name), page=doc["page"], path=path, module=module,
                           pack=pack, why=why, fields=spec, layer=doc.get("project_layer")))
    return out


# `declared_modules` (the whole-tree, preset-independent reader over these
# same pages) used to live here, but its only two callers are their own
# tests (test_module_reachability.py, test_the_gate_registry_is_one_book.py)
# — no shipped caller. Moved to harness/tests/gate_registry_declared_
# modules.py, same pattern state_path_assembly_scan.py already used: test
# support wearing a production path, moved next to the tests that call it
# so harness/tests/ (excluded from the production reachability index by
# design) is where it lives instead of reading as an orphan module here.


def load(root: Path, pages: Optional[tuple] = None, target: Optional[Path] = None) -> GateBook:
    """The whole book, or only `pages` — a door that needs one kind of gate does not
    pay for expanding the others. Names are checked unique across what is loaded.
    `target` is the project whose `preset:` gates which pack pages are even in
    scope (`registry_files`'s own docstring) — defaults to `root` for the
    self-hosting case."""
    root = Path(root)
    files = [p for p in registry_files(root, target) if pages is None or p.stem in pages]
    docs = [(p, _read_page(root, p)) for p in files]
    rows: Dict[str, GateRow] = {}

    def put(row: GateRow):
        if row.name in rows:
            raise GateRegistryError("gate %s is declared on page %s (%s) and page %s (%s)"
                                    % (row.name, rows[row.name].page, rows[row.name].path,
                                       row.page, row.path))
        rows[row.name] = row

    attaching = []
    for path, doc in docs:
        for row in _literal_rows(root, path, doc):
            put(row)
        if doc.get("source") is None:
            continue
        src = doc["source"]
        label = "%s.%s" % (src.get("module"), src.get("entry")) if isinstance(src, dict) else src
        found = _call_source(root, path, src)
        if doc.get("attach_to"):
            attaching.append((path, doc, label, found))
            continue
        pack = _pack_of(root, path)
        for name, spec in found.items():
            spec = dict(spec or {})
            if not _carried(root, spec):
                continue
            module = spec.pop("module", None)
            if module is not None and not _module_exists(root, str(module), pack):
                raise GateRegistryError("%s: source %s names module %r for gate %s, which "
                                        "does not exist" % (path, label, module, name))
            _check_fields(root, path, name, spec)
            put(GateRow(name=str(name), page=doc["page"], path=path, source=label,
                        module=module, pack=pack, fields=spec,
                        layer=doc.get("project_layer")))

    for path, doc, label, found in attaching:
        host = doc["attach_to"]
        for name, spec in found.items():
            prior = rows.get(str(name))
            if prior is not None and prior.page == host:
                rows[str(name)] = attrs.evolve(
                    prior, attached={**prior.attached, doc["page"]: dict(spec or {})})
            else:
                put(GateRow(name=str(name), page=doc["page"], path=path, source=label,
                            pack=_pack_of(root, path),
                            attached={doc["page"]: dict(spec or {})}))

    return GateBook(rows=sorted(rows.values(), key=lambda r: (r.page, r.name)), files=files,
                    runs={doc["page"]: doc["run"] for _p, doc in docs if doc.get("run") is not None})


def read_project_layer(path: Path) -> Dict[str, dict]:
    """The `gates:` mapping of a project layer file, unjudged; {} when absent."""
    try:
        return project_gate_layer.read(path)  # type: ignore[return-value]
    except project_gate_layer.LayerFileError as e:
        raise GateRegistryError(str(e)) from e


entry_problem = project_gate_layer.entry_problem


def known(book: GateBook) -> Dict[str, dict]:
    """What `project_gate_layer.layer_problem` needs to know about each gate."""
    return {r.name: {"page": r.page, "layer": r.layer, "floor": r.fields.get("floor") is True}
            for r in book.rows}


def apply_project_layer(book: GateBook, data_home: Path) -> Dict[str, dict]:
    """Validate `<data_home>/gates.yaml` against the book; `{gate: entry}` of its offs.
    Raises GateRegistryError naming the first entry that is not a valid off — the same
    judgement the reader applies (`project_gate_layer.layer_problem`)."""
    path = Path(data_home) / GATES_LAYER
    layer = read_project_layer(path)
    rows, pages = known(book), {r.page for r in book.rows}
    for name, entry in layer.items():
        problem = project_gate_layer.layer_problem(name, entry, rows, pages)
        if problem:
            raise GateRegistryError("%s: %s" % (path, problem))
    return layer


def loosened(book: GateBook, hook_runtime) -> List[dict]:
    """`{state, name, layer, core, reason, who}` for every gate that is not fully on: a
    hook off or a core hook made advisory, as `hook_runtime` (the module the hooks
    themselves import) resolves it, and any other row with `enabled: false` — written on
    the page, or reported by its source with `off_layer`/`off_reason` (a review rule
    off by the repo's override). `who` is `(actor, ts)` when the layer recorded them."""
    hook_runtime._reset_config_cache()
    hooks = hook_runtime.hook_config_document().get("hooks") or {}
    out: List[dict] = []
    for r in book.rows:
        if r.page != "hook":
            if r.fields.get("enabled") is False:
                out.append({"state": "off", "name": r.name,
                            "layer": r.fields.get("off_layer") or "shipped", "core": r.core,
                            "reason": r.fields.get("off_reason") or "shipped default",
                            "who": None})
            continue
        cls = r.fields.get("class") or "nudge"
        try:
            if hook_runtime.hook_enabled(r.name, cls):
                if not (r.core and hook_runtime.hook_mode(r.name, cls) == "advisory"):
                    continue
                state, layer = "advisory", hook_runtime.hook_mode_source(r.name, cls)
            else:
                state, layer = "off", hook_runtime.hook_enabled_source(r.name, cls)
        except ValueError:
            continue
        raw = hooks.get(r.name)
        entry: dict = raw if isinstance(raw, dict) else {}
        out.append({"state": state, "name": r.name, "layer": layer, "core": r.core,
                    "reason": (entry.get("force_reason") or entry.get("reason")
                               or ("shipped default" if layer == "shipped" else "-")),
                    "who": ((entry["actor"], entry["ts"])
                            if entry.get("actor") and entry.get("ts") else None)})
    return out


@contextlib.contextmanager
def _hook_overlay(path: Optional[Path]):
    """Point the hook config's project layer at `path` for one read (the variable
    `hook_runtime._overlay_path` already honours), then restore it."""
    if path is None:
        yield
        return
    old = os.environ.get("HARNESS_HOOK_OVERLAY")
    os.environ["HARNESS_HOOK_OVERLAY"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("HARNESS_HOOK_OVERLAY", None)
        else:
            os.environ["HARNESS_HOOK_OVERLAY"] = old


def gates_off(book: GateBook, root: Path, data_home: Optional[Path]) -> List[dict]:
    """`{state, name, layer, core, reason, who}` for every gate that is not fully on — the
    ONE answer `hs-cli gates` and `hs-code check gates` both print. Three sources: a hook
    off or a core hook advisory, or a row shipped off (`loosened`); the write guard's own
    switch; and the project's `gates.yaml` in `data_home`. `root` is the bin tree the hooks
    and the switch live in. The hook layer is `hook_runtime`'s own when `data_home` is this
    process's project (an overlay the session bound still applies), and `data_home`'s when
    it names ANOTHER project. `data_home` None (unresolved) reads no project layer. Raises GateRegistryError for an off that is not
    on the record: an invalid project entry, or a guard switch off without who/when/why."""
    hooks = str(Path(root) / "harness" / "hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    import harness_paths
    import hook_runtime
    import write_guard
    own = harness_paths.data_root()
    other = data_home is not None and (harness_paths.data_root_unresolved(own)
                                       or Path(data_home).resolve() != own.resolve())
    with _hook_overlay(Path(data_home) / "harness-hooks.yaml" if other and data_home is not None else None):
        out = loosened(book, hook_runtime)
    hook_runtime._reset_config_cache()
    switch = write_guard.switch_state(root=root)
    if switch["missing"]:
        raise GateRegistryError(
            "%s at %s says enabled: false but is missing %s — an off says who, when and why; "
            "the guard stays armed until it does"
            % (switch["layer"], switch["path"], ", ".join(switch["missing"])))
    if switch["off"]:
        cfg = switch["config"]
        out.append({"state": "off", "name": "write_guard", "layer": switch["layer"], "core": True,
                    "reason": cfg["reason"], "who": (cfg["actor"], cfg["ts"])})
    if data_home is not None:
        for name, e in sorted(apply_project_layer(book, data_home).items()):
            out.append({"state": "off", "name": name, "layer": "project", "core": False,
                        "reason": e["reason"], "who": (e["actor"], e["ts"])})
    return out


def off_line(g: dict) -> str:
    """`<off|advisory> <name> <layer> [core] reason: <why> [(actor, ts)]`."""
    return "%s %s %s%s reason: %s%s" % (g["state"], g["name"], g["layer"], " core" if g["core"] else "",
                                        g["reason"], " (%s, %s)" % g["who"] if g["who"] else "")
