"""DescriptorStore — the ONE door every descriptor read goes through.

One-door descriptor reading: before this discipline, a lookalike hand-rolled
reader let one extra space in a `tiers:` block empty the tier ladder and turn
a red layer gate into exit 0. One door, a real YAML parser, duplicate-key
detection before anything else, unknown top-level keys rejected.

SCHEMA VERSIONS
    `schema_version: 2`  — the kernel's own schema (below).
    absent               — LEGACY. Loads only through a registered
                           `LegacyDescriptorCodec` that CLAIMS the document;
                           no codec claims it -> honest exit 2, naming what
                           was tried. No implicit default: a silent default
                           is how an earlier schema drifted. The seam's one
                           real registrant is the port domain's own codec
                           (`harness/scripts/port_legacy_snapshot.py`,
                           `register_all()`) — called by `hs-code port
                           snapshot` before it loads a pre-`schema_version`
                           source descriptor (e.g. an old porting
                           `index.yaml`).

WHAT IT CANNOT SEE
    * No semantic checks: two roots with the same `kind` at the same depth, a
      `thresholds` value that is not a number — this door returns exactly what
      is written. Catching semantics belongs to the rules engine / gates.
    * It reads the DECLARATION, never the tree — `check_repo()` compares them.
    * Every re-read is a re-read of the file. No cache: a script that edits a
      descriptor mid-run sees the new one on the next load.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import codegraph_change_reader
import yaml_io
from hs_family.versioning import int_version_problem

from . import obligations as _obligations
from .model import (COPY_KINDS, CopiesSpec, DescriptorError, ProjectDescriptor, Root,
                    KERNEL_SCHEMA_VERSION, gitignore_spec, ScanSpec, as_driver_spec,
                    as_entrypoints_spec, check_roots, duplicate_keys)

#: v2 top-level keys. An unknown key is an ERROR, not freedom: `thresholds`
#: typed as `threshold` silently switches every gate to defaults nobody chose.
V2_KEYS: frozenset[str] = frozenset({
    "schema_version", "project", "layout", "scan", "drivers", "thresholds",
    "rules", "groups", "domains", "snapshot", "owners", "entrypoints",
    "namespace_root", "import_roots", "scaffold", "docs", "preset", "adapters",
    "copies", "test_select", "obligations",
})

#: `layout:` sub-keys (F9). Unchecked before this door: a `layout:` typo
#: (`packages_dr` for `packages_dir`) silently read as absent rather than
#: refusing -- the same silent-default failure `V2_KEYS` exists to catch,
#: one level down.
LAYOUT_KEYS: frozenset[str] = frozenset({
    "roots", "auto_discover", "packages_dir", "src_subdir", "tests_subdir",
})

#: `scaffold:` sub-keys. `templates` is the only key this schema recognizes;
#: its VALUE is `hs-code scaffold`'s (`codegraph/scaffold/engine.py`), carried through unread.
SCAFFOLD_KEYS: frozenset[str] = frozenset({"templates"})

#: A `scaffold.templates.<key>` of `kind: dist-doc` -- one per-dist doc file
#: `new-dist` renders. The one template kind this schema reads the shape of: its
#: `source` is a FILE of the repo, so a typo there surfaces here rather than as a
#: half-scaffolded dist. Exactly one of `source` (a repo-relative file; `anchor`
#: picks the first fenced block after that line, `placeholders` maps literal text
#: of it to a value) or `link_to` (a symlink beside it).
DIST_DOC_KEYS: frozenset[str] = frozenset({"kind", "path", "source", "anchor", "placeholders",
                                           "link_to"})

#: `docs:` sub-keys. `source_ledgers` lists the doc tables whose rows each declare
#: where a borrowed piece came from, and the repos a row may name; the docs
#: reference gate (`docs_source_ledger`) checks those rows, and only those.
DOCS_KEYS: frozenset[str] = frozenset({"source_ledgers"})
LEDGER_KEYS: frozenset[str] = frozenset({"path", "repos"})


@runtime_checkable
class LegacyDescriptorCodec(Protocol):
    """Claims and converts a legacy (pre-schema_version) descriptor document.

    A domain that owns a legacy format registers one codec; the kernel stays
    ignorant of the legacy key set — it only enforces that SOMEBODY claims
    the document and that unclaimed keys are rejected.
    """

    name: str

    def claims(self, doc: dict[str, Any]) -> bool: ...
    def to_descriptor(self, doc: dict[str, Any], path: Path) -> ProjectDescriptor: ...
    def claimed_keys(self) -> frozenset[str]: ...


_LEGACY_CODECS: "list[LegacyDescriptorCodec]" = []


def register_legacy_codec(codec: LegacyDescriptorCodec) -> None:
    """Register a legacy-format codec (a domain does this at plugin load —
    this build's one real registrant is `port_legacy_snapshot.register_all`).
    """
    _LEGACY_CODECS.append(codec)


def legacy_codecs() -> "list[LegacyDescriptorCodec]":
    return list(_LEGACY_CODECS)


@dataclass(frozen=True)
class LoadedDescriptor:
    """A descriptor plus everything the door learned while loading it."""

    descriptor: ProjectDescriptor
    legacy: bool
    claimed_by: str | None      # codec name when legacy
    passthrough: dict[str, Any] # legacy keys the codec routed to its domain


def check_python(descriptor_python: str | None, path: Path) -> None:
    """The RUNNING interpreter must be able to parse the measured tree:
    `ast.parse` uses the interpreter's grammar, not the tree's. A 3.12 tree
    read by 3.10 yields SyntaxError and every gate blames the tree for an
    environment problem. Blind spot: the reverse direction is unchecked — a
    NEWER interpreter passes, correctly, so removed syntax slips through.
    """
    if not descriptor_python:
        return
    try:
        need = tuple(int(x) for x in descriptor_python.split(".")[:2])
    except ValueError:
        raise DescriptorError(f'{path}: `python: {descriptor_python!r}" unparseable — need "3.12" form')
    if sys.version_info[:2] < need:
        running = ".".join(str(x) for x in sys.version_info[:2])
        raise DescriptorError(
            f"tree declares python {descriptor_python}, running interpreter is {running}.\n"
            f"  ast.parse uses the interpreter's grammar: anything newer than {running} raises\n"
            f"  SyntaxError and gets misread as a broken file. Re-run with python{descriptor_python}.")


def check_repo(descriptor: ProjectDescriptor, repo: Path) -> list[Root]:
    """The passed repo IS the declared tree? -> resolved roots.

    WHY: running one project's descriptor against a wrong-but-similar tree
    made a gate print 17 orphan verdicts and go red for the WRONG reason — it
    thought files were missing, never that it was reading the wrong tree. A
    red gate for a wrong reason is worse than a green one: people fix what is
    not broken.

    Blind spots: only directory EXISTENCE is checked — a modified copy of the
    right tree passes; content parity is a different gate's job. Shape
    (`kind` matching reality) is not checked either.
    """
    roots = list(descriptor.roots)
    if not repo.is_dir():
        raise DescriptorError(f"no such repo {repo}")
    missing = [r for r in roots if not r.dir(repo).is_dir()]
    if missing:
        lines = "\n".join(f"    {r.id:12} {r.path}" for r in missing)
        raise DescriptorError(
            f"repo {repo} does not belong to this descriptor — "
            f"{len(missing)}/{len(roots)} roots missing:\n{lines}\n"
            f"  running one descriptor's gates on another tree gives red for the wrong reason.")
    return roots


# ── the one door ─────────────────────────────────────────────────────────────

def load(path: Path, repo: Path | None = None) -> LoadedDescriptor:
    """Load a descriptor file through every check, exactly once per read."""
    if not path.is_file():
        if path.name == "codemap.yaml":
            import codemap_adoption
            raise DescriptorError(f"no descriptor at {path} — {codemap_adoption.REMEDY}")
        raise DescriptorError(f"no descriptor at {path}")
    dupes = duplicate_keys(path)
    if dupes:
        detail = "\n".join(f"    `{k}` declared TWICE (lines {a} and {b})" for k, a, b in dupes)
        raise DescriptorError(
            f"{path}: {len(dupes)} duplicate keys — a repeat-key-tolerant loader "
            f"keeps the LAST one and the\n"
            f"  earlier half disappears silently:\n{detail}")
    try:
        doc = yaml_io.yaml_load(path.read_text(encoding="utf-8"))
    except yaml_io.YAMLError as e:
        raise DescriptorError(f"{path}: could not parse: {e}") from e
    if not isinstance(doc, dict):
        raise DescriptorError(f"{path}: root must be a mapping, got {type(doc).__name__}")

    if "extends" in doc:
        # Preset resolution happens INSIDE the one door: every reader sees
        # the merged document, so no consumer can diverge on what `extends`
        # means. Identity stays the FILE's own — inheriting a schema_version
        # from a preset would be exactly the silent default this door
        # exists to refuse.
        #
        # Not to be confused with the v2 `preset:` KEY (below, in V2_KEYS):
        # `extends:` here says where THIS descriptor's own layout came from
        # (a named preset from `codegraph/_core/presets/library/*.yaml`,
        # resolved before schema validation even starts); `preset:` is an
        # ordinary field of the resolved document, checked like any other —
        # it says which packs (`packs/<name>/`) this project turned ON. Two
        # unrelated questions that happen to share the word "preset".
        if "schema_version" not in doc:
            raise DescriptorError(
                f"{path}: uses `extends:` but declares no `schema_version` of its own —\n"
                f"  a preset may carry one for a generator's convenience, but loading must\n"
                f"  never inherit it: declare `schema_version: {KERNEL_SCHEMA_VERSION}` here.")
        from ..presets import resolve_doc  # local: model->presets is one-way
        doc = resolve_doc(doc)

    if "schema_version" in doc:
        return _load_v2(doc, path, repo)
    return _load_legacy(doc, path, repo)


def _load_v2(doc: dict[str, Any], path: Path, repo: Path | None) -> LoadedDescriptor:
    version = doc["schema_version"]
    if int_version_problem(version, frozenset({KERNEL_SCHEMA_VERSION})) is not None:
        raise DescriptorError(
            f"{path}: schema_version {version!r} — this kernel reads "
            f"{KERNEL_SCHEMA_VERSION} only; no legacy-codec fallback ships in this build")
    unknown = sorted(set(doc) - V2_KEYS)
    if unknown:
        raise DescriptorError(
            f"{path}: unknown top-level keys: {', '.join(unknown)}\n"
            f"  A mistyped key must NOT be silently ignored — it switches off exactly the\n"
            f"  declaration you just wrote. Valid keys: {', '.join(sorted(V2_KEYS))}")
    project = doc.get("project")
    if not project or not isinstance(project, str):
        raise DescriptorError(f"{path}: `project:` (non-empty string) is required")

    layout = doc.get("layout") or {}
    if not isinstance(layout, dict):
        raise DescriptorError(f"{path}: `layout:` must be a mapping")
    if "kind" in layout or "workspace_dir" in layout:
        raise DescriptorError(
            f"{path}: `layout.kind` / `layout.workspace_dir` are DEAD keys.\n"
            f"  Use `layout.roots:` — one line per root, `path` RELATIVE to the repo root.")
    unknown_layout = sorted(set(layout) - LAYOUT_KEYS)
    if unknown_layout:
        raise DescriptorError(
            f"{path}: unknown `layout.` keys: {', '.join(unknown_layout)}\n"
            f"  Valid: {', '.join(sorted(LAYOUT_KEYS))}")
    packages_dir = layout.get("packages_dir")
    src_subdir = layout.get("src_subdir")
    tests_subdir = layout.get("tests_subdir")
    for key, val in (("packages_dir", packages_dir), ("src_subdir", src_subdir),
                     ("tests_subdir", tests_subdir)):
        if val is not None and (not isinstance(val, str) or not val):
            raise DescriptorError(f"{path}: `layout.{key}:` must be a non-empty string")
    raw = layout.get("roots") or []
    auto = bool(layout.get("auto_discover", False))
    roots: list[Root] = []
    for i, r in enumerate(raw, 1):
        if not isinstance(r, dict):
            raise DescriptorError(f"{path}: layout.roots[{i}] is not a mapping")
        miss = [k for k in ("id", "path", "kind") if not r.get(k)]
        if miss:
            raise DescriptorError(f"{path}: layout.roots[{i}] missing {', '.join(miss)}")
        roots.append(Root(id=str(r["id"]), path=str(r["path"]).strip("/"), kind=str(r["kind"])))
    if roots:
        check_roots(roots)
    elif not auto:
        raise DescriptorError(
            f"{path}: no `layout.roots:` and no `layout.auto_discover: true` —\n"
            f"  nothing says where scanning starts. Declare roots, or opt into discovery.")

    scan_doc = doc.get("scan") or {}
    scan = ScanSpec(include=tuple(scan_doc.get("include") or ("*.py",)),
                    exclude=tuple(scan_doc.get("exclude") or ()))

    drivers_doc = doc.get("drivers") or {}
    if not isinstance(drivers_doc, dict):
        raise DescriptorError(f"{path}: `drivers:` must be a mapping")
    drivers = as_driver_spec(drivers_doc)
    owners_doc = doc.get("owners")
    if owners_doc is not None and not isinstance(owners_doc, dict):
        raise DescriptorError(f"{path}: `owners:` must be a mapping")
    stay_raw = (owners_doc or {}).get("stay")
    if stay_raw is not None:
        if not isinstance(stay_raw, list):
            raise DescriptorError(f"{path}: `owners.stay:` must be a list of "
                                  f"{{match, why}} entries")
        for i, s in enumerate(stay_raw, 1):
            if (not isinstance(s, dict)
                    or not isinstance(s.get("match"), str) or not s.get("match")
                    or not isinstance(s.get("why"), str) or not s.get("why")):
                raise DescriptorError(
                    f"{path}: owners.stay[{i}] needs non-empty string `match` "
                    f"(a path glob) and `why` (why the file must not move)")

    thresholds = doc.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        raise DescriptorError(f"{path}: `thresholds:` must be a mapping")

    rules_raw = doc.get("rules") or []
    if not isinstance(rules_raw, list):
        raise DescriptorError(f"{path}: `rules:` must be a list")
    rules: list[dict[str, Any]] = []
    for i, r in enumerate(rules_raw, 1):
        if not isinstance(r, dict) or not r.get("id") or not r.get("kind"):
            raise DescriptorError(f"{path}: rules[{i}] needs at least `id` and `kind`")
        rules.append(dict(r))

    groups_raw = doc.get("groups") or []
    if not isinstance(groups_raw, list):
        raise DescriptorError(f"{path}: `groups:` must be a list — order IS the "
                              f"rule (first matching entry wins)")
    groups: list[dict[str, Any]] = []
    group_names: set[str] = set()
    for i, g in enumerate(groups_raw, 1):
        if not isinstance(g, dict) or not g.get("name") or not g.get("match"):
            raise DescriptorError(
                f"{path}: groups[{i}] needs non-empty `name` and `match` "
                f"(a list of path globs, repo-relative)")
        if not isinstance(g["match"], list) or not all(
                isinstance(p, str) and p for p in g["match"]):
            raise DescriptorError(
                f"{path}: groups[{i}] `match` must be a list of path globs, "
                f"got {g['match']!r}")
        if g["name"] in group_names:
            raise DescriptorError(
                f"{path}: group name {g['name']!r} declared twice — first match "
                f"wins by list order, so a repeated name silently swallows the "
                f"second entry's paths")
        group_names.add(g["name"])
        groups.append({"name": str(g["name"]),
                       "match": [str(p) for p in g["match"]]})

    domains = doc.get("domains") or {}
    if not isinstance(domains, dict):
        raise DescriptorError(f"{path}: `domains:` must be a mapping (opaque domain payload)")

    snap = doc.get("snapshot") or {}
    if not isinstance(snap, dict):
        raise DescriptorError(f"{path}: `snapshot:` must be a mapping")
    fmt = snap.get("format")
    keep = snap.get("keep")
    if keep is not None and (not isinstance(keep, int) or isinstance(keep, bool) or keep < 1):
        raise DescriptorError(f"{path}: `snapshot.keep:` must be a positive integer")

    entrypoints_doc = doc.get("entrypoints") or {}
    if not isinstance(entrypoints_doc, dict):
        raise DescriptorError(f"{path}: `entrypoints:` must be a mapping")
    try:
        entrypoints = as_entrypoints_spec(entrypoints_doc)
    except DescriptorError as e:
        raise DescriptorError(f"{path}: {e}") from None

    namespace_root = doc.get("namespace_root")
    if namespace_root is not None and (not isinstance(namespace_root, str) or not namespace_root):
        raise DescriptorError(f"{path}: `namespace_root:` must be a non-empty string")
    if packages_dir is not None and not namespace_root:
        raise DescriptorError(
            f"{path}: `layout.packages_dir: {packages_dir}` declares a "
            f"`packages/<dist>/src` layout, which needs `namespace_root:` — "
            f"there is no default (an invented one would scaffold or carve "
            f"into the wrong namespace silently). Declare it.")

    import_roots_raw = doc.get("import_roots") or []
    if not isinstance(import_roots_raw, list) or not all(
            isinstance(r, str) and r for r in import_roots_raw):
        raise DescriptorError(f"{path}: `import_roots:` must be a list of non-empty strings")

    preset_raw = doc.get("preset") or []
    if not isinstance(preset_raw, list) or not all(
            isinstance(p, str) and p for p in preset_raw):
        raise DescriptorError(f"{path}: `preset:` must be a list of non-empty strings")

    scaffold_doc = doc.get("scaffold")
    if scaffold_doc is not None:
        if not isinstance(scaffold_doc, dict):
            raise DescriptorError(f"{path}: `scaffold:` must be a mapping")
        unknown_scaffold = sorted(set(scaffold_doc) - SCAFFOLD_KEYS)
        if unknown_scaffold:
            raise DescriptorError(
                f"{path}: unknown `scaffold.` keys: {', '.join(unknown_scaffold)}\n"
                f"  Valid: {', '.join(sorted(SCAFFOLD_KEYS))}")
        _check_dist_docs(scaffold_doc.get("templates"), path)

    source_ledgers = _source_ledgers(doc.get("docs"), path)
    copies = _copies(doc.get("copies"), path)

    try:
        test_select = codegraph_change_reader.parse_test_select(doc.get("test_select"))
    except ValueError as e:
        raise DescriptorError(f"{path}: {e}") from None
    try:
        obligations = _obligations.parse(doc.get("obligations"))
    except _obligations.ObligationError as e:
        raise DescriptorError(f"{path}: {e}") from None

    adapters: "tuple[Any, ...]" = ()
    if doc.get("adapters") is not None:
        from ..adapter.book import AdapterError
        from ..adapter.registry import project_rows
        try:
            adapters = project_rows(doc["adapters"], str(path))
        except AdapterError as e:
            raise DescriptorError(str(e)) from None

    descriptor = ProjectDescriptor(
        schema_version=KERNEL_SCHEMA_VERSION, project=project,
        roots=tuple(roots), auto_discover=auto, scan=scan, drivers=drivers,
        thresholds=dict(thresholds), rules=tuple(rules), groups=tuple(groups),
        domains=dict(domains), namespace_root=namespace_root,
        packages_dir=packages_dir, src_subdir=src_subdir, tests_subdir=tests_subdir,
        import_roots=tuple(import_roots_raw), scaffold=scaffold_doc,
        snapshot_format=fmt, snapshot_keep=keep,
        entrypoints=entrypoints, preset=tuple(preset_raw),
        path=path, owners=owners_doc, source_ledgers=source_ledgers, adapters=adapters,
        copies=copies, test_select=test_select, obligations=obligations,
    )
    if repo is not None and not descriptor.auto_discover:
        check_repo(descriptor, repo)
    return LoadedDescriptor(descriptor=descriptor, legacy=False,
                            claimed_by=None, passthrough={})


def _load_legacy(doc: dict[str, Any], path: Path, repo: Path | None) -> LoadedDescriptor:
    """A doc with no `schema_version` -> the first registered codec that
    CLAIMS it, or an honest exit 2 naming who was asked."""
    claimer = next((c for c in _LEGACY_CODECS if _safe_claims(c, doc)), None)
    if claimer is None:
        names = ", ".join(c.name for c in _LEGACY_CODECS) or "none registered"
        raise DescriptorError(
            f"{path}: descriptor lacks `schema_version` and no registered legacy codec "
            f"claims it (registered: {names}).\n"
            f"  No implicit default: loading an unclaimed legacy document would guess its\n"
            f"  schema. Register the owning domain's codec, or migrate to schema_version: "
            f"{KERNEL_SCHEMA_VERSION}.")
    descriptor = claimer.to_descriptor(doc, path)
    if descriptor.schema_version >= KERNEL_SCHEMA_VERSION:
        raise DescriptorError(
            f"{path}: legacy codec {claimer.name!r} returned schema_version "
            f"{descriptor.schema_version} — legacy codecs must map DOWN, not forward")
    claimed = claimer.claimed_keys()
    unknown = sorted(set(doc) - claimed - {"schema_version"})
    if unknown:
        raise DescriptorError(
            f"{path}: keys not claimed by codec {claimer.name!r}: {', '.join(unknown)}\n"
            f"  A mistyped key must not be silently ignored — it switches off the\n"
            f"  declaration you just wrote.")
    if repo is not None and not descriptor.auto_discover:
        check_repo(descriptor, repo)
    return LoadedDescriptor(descriptor=descriptor, legacy=True,
                            claimed_by=claimer.name,
                            passthrough={k: doc[k] for k in doc if k not in ("schema_version",)})


def _safe_claims(codec: LegacyDescriptorCodec, doc: dict[str, Any]) -> bool:
    try:
        return bool(codec.claims(doc))
    except Exception:  # noqa: BLE001 — a broken codec must not brick the door
        return False


def _repo_relative(value: Any) -> bool:
    return (isinstance(value, str) and bool(value) and not value.startswith("/")
            and ".." not in Path(value).parts)


def _check_dist_docs(templates: Any, path: Path) -> None:
    """Refuse a malformed `kind: dist-doc` entry of `scaffold.templates`."""
    if not isinstance(templates, dict):
        return
    for key, tmpl in templates.items():
        if not isinstance(tmpl, dict) or tmpl.get("kind") != "dist-doc":
            continue
        where = f"{path}: scaffold.templates.{key} (kind: dist-doc)"
        unknown = sorted(set(tmpl) - DIST_DOC_KEYS)
        if unknown:
            raise DescriptorError(f"{where}: unknown keys: {', '.join(unknown)}\n"
                                  f"  Valid: {', '.join(sorted(DIST_DOC_KEYS))}")
        if not isinstance(tmpl.get("path"), str) or not tmpl["path"]:
            raise DescriptorError(f"{where}: `path` (where the file lands, placeholders "
                                  "allowed) is required")
        if ("source" in tmpl) == ("link_to" in tmpl):
            raise DescriptorError(f"{where}: needs exactly one of `source` (the file the doc "
                                  "is read from) or `link_to` (a symlink beside it)")
        if "link_to" in tmpl:
            extra = sorted({"anchor", "placeholders"} & set(tmpl))
            if not _repo_relative(tmpl["link_to"]) or extra:
                raise DescriptorError(f"{where}: `link_to` is a relative path, with no "
                                      f"{', '.join(extra) or 'absolute or ../ part'}")
            continue
        if not _repo_relative(tmpl["source"]):
            raise DescriptorError(f"{where}: `source` {tmpl['source']!r} must be a repo-relative "
                                  "file path")
        if "anchor" in tmpl and (not isinstance(tmpl["anchor"], str) or not tmpl["anchor"]):
            raise DescriptorError(f"{where}: `anchor` must be a non-empty string")
        ph = tmpl.get("placeholders", {})
        if not isinstance(ph, dict) or not all(
                isinstance(k, str) and k and isinstance(v, str) for k, v in ph.items()):
            raise DescriptorError(f"{where}: `placeholders` maps literal template text to a "
                                  "value string")


def _source_ledgers(docs_doc: Any, path: Path) -> tuple[dict[str, Any], ...]:
    """`docs.source_ledgers` as ({path, repos}, ...); absent block -> ()."""
    if docs_doc is None:
        return ()
    if not isinstance(docs_doc, dict):
        raise DescriptorError(f"{path}: `docs:` must be a mapping")
    unknown = sorted(set(docs_doc) - DOCS_KEYS)
    if unknown:
        raise DescriptorError(f"{path}: unknown `docs.` keys: {', '.join(unknown)}\n"
                              f"  Valid: {', '.join(sorted(DOCS_KEYS))}")
    raw = docs_doc.get("source_ledgers") or []
    if not isinstance(raw, list):
        raise DescriptorError(f"{path}: `docs.source_ledgers:` must be a list")
    out: list[dict[str, Any]] = []
    for i, led in enumerate(raw, 1):
        repos = led.get("repos") if isinstance(led, dict) else None
        if (not isinstance(led, dict) or set(led) != LEDGER_KEYS
                or not isinstance(led["path"], str) or not led["path"]
                or not isinstance(repos, list) or not repos
                or not all(isinstance(r, str) and r for r in repos)):
            raise DescriptorError(
                f"{path}: docs.source_ledgers[{i}] needs exactly `path` (a repo-relative doc) "
                f"and `repos` (a non-empty list of the repo names its rows may cite)")
        if any(o["path"] == led["path"] for o in out):
            raise DescriptorError(f"{path}: docs.source_ledgers declares {led['path']!r} twice")
        out.append({"path": led["path"].strip("/"), "repos": tuple(str(r) for r in repos)})
    return tuple(out)


def _copies(doc: Any, path: Path) -> CopiesSpec:
    """`copies:` as a `CopiesSpec`; absent block -> an empty one. Each kind is a list of
    non-empty gitignore-style patterns; an unknown kind is refused, never ignored."""
    if doc is None:
        return CopiesSpec()
    if not isinstance(doc, dict):
        raise DescriptorError(f"{path}: `copies:` must be a mapping of "
                              f"{' / '.join(COPY_KINDS)} to pattern lists")
    unknown = sorted(set(doc) - set(COPY_KINDS))
    if unknown:
        raise DescriptorError(f"{path}: unknown `copies.` keys: {', '.join(map(str, unknown))}\n"
                              f"  Valid: {', '.join(COPY_KINDS)}")
    out: dict[str, tuple[str, ...]] = {}
    for kind in COPY_KINDS:
        raw = doc.get(kind) or []
        if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
            raise DescriptorError(f"{path}: `copies.{kind}` must be a list of non-empty "
                                  f"gitignore-style patterns")
        out[kind] = tuple(x.strip() for x in raw)
    try:
        for patterns in out.values():
            if patterns:
                gitignore_spec(patterns)
    except Exception as e:  # pathspec raises its own pattern errors; any of them is a bad declaration
        raise DescriptorError(f"{path}: `copies:` holds a pattern that does not compile: {e}") from None
    return CopiesSpec(**out)
