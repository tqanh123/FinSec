#!/usr/bin/env python3
"""pack_config.py — pack (`packs/<name>/`) resolution and manifest reading.

A pack lives at the repo root, outside `harness/` — it never enters the
release manifest. WHICH packs a project turns on is a per-project decision,
declared in that project's own `codemap.yaml` (`preset:`, a v2 top-level key
— see `codegraph/_core/descriptor/store.py`'s `V2_KEYS`), never a
harness-global data file: under a global install one shared binary serves
many projects, and a bin-global config would turn a pack on for ALL of them
at once. `hs_code.py`'s `_pack_frag_dirs` reads the descriptor of the
project it is invoked against (`--repo`, or the resolved project root) and
calls `resolve_frag_dirs` here to turn that project's declared `preset:`
list into real fragment directories — relative to the HARNESS's OWN install
root (where `packs/` physically lives), never to the project's own tree; see
`preset`'s own field docstring in `descriptor/model.py` for why.

This module also reads a pack's own `pack.yaml` self-declaration
(`load_pack_manifest`), and `resolve_frag_dirs` holds every `preset:` entry
to it: a directory named there that carries no valid manifest is not a pack,
so it is refused by name and its fragments never load.

This file carries NO `codegraph` import, anywhere, even nested inside a
function: `harness/scripts/hs_family/gate_registry.py` (part of the
hs-run/hs_family family, forbidden from reaching `codegraph` at all —
`test_no_hs_run_or_hs_family_module_imports_codegraph`) imports this module
at the top, and that gate reads a FILE as "touches codegraph" the moment the
text contains the import, regardless of which function it sits in or
whether that function ever runs. `read_enabled_preset` below reads `preset:`
with a plain YAML parse for exactly that reason — no descriptor schema
validation, no `codegraph._core.descriptor.DescriptorError`. `hs_code.py`
(the codegraph-side CLI, free to import codegraph) validates the REST of
`codemap.yaml` itself, through its own direct descriptor load, before it
ever calls here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

import yaml_io

#: The fragment directory a pack contributes to hs-code's registry, relative
#: to the pack's own root — mirrors `harness/data/hs-code-registry.d/`'s name
#: one level down, so a pack's own fragment authors the exact same shape a
#: core domain fragment already does.
FRAG_DIR_NAME = "hs-code-registry.d"

PACK_MANIFEST_NAME = "pack.yaml"
ALLOWED_MANIFEST_KEYS = frozenset({"name", "schema_version"})


class PackConfigError(Exception):
    """`codemap.yaml` does not parse as YAML, does not parse to a mapping, or
    its `preset:` value is not a list of strings. Never `codegraph`'s own
    `DescriptorError` — see this module's docstring for why."""


def load_pack_manifest(pack_dir: Path):
    """Read and validate `<pack_dir>/pack.yaml` — a pack's own self-
    declaration. A directory named in `preset:` that carries no manifest is
    not a pack yet, just a directory — absence here IS a problem. Returns
    `(manifest, problems)`."""
    path = pack_dir / PACK_MANIFEST_NAME
    if not path.is_file():
        return None, ["no %s at %s" % (PACK_MANIFEST_NAME, pack_dir)]
    try:
        value = yaml_io.yaml_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — any parse failure is one problem
        return None, ["%s does not parse as YAML (%s)" % (path, exc)]
    if not isinstance(value, dict):
        return None, ["%s does not parse to a mapping (got %s)"
                      % (path, type(value).__name__)]
    problems = []
    unknown = sorted(set(value) - ALLOWED_MANIFEST_KEYS)
    for key in unknown:
        problems.append("unknown top-level key `%s`" % key)
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("`name` is missing or not a non-empty string")
    return (value if not problems else None), problems


def read_enabled_preset(target_root: Path) -> tuple:
    """The `preset:` list a project declares in its OWN `codemap.yaml`. No
    `codemap.yaml` at all -> no packs enabled (opt-in; most projects never
    touch this file), not an error. Raises `PackConfigError` for a file that
    does not parse as YAML, does not parse to a mapping, or whose `preset:`
    is not a list of strings — a NARROWER check than the full V2 schema
    (unrelated keys are not this reader's concern; see the module docstring
    for why it stops here instead of reusing the descriptor loader).
    `hs_code.py`'s own pack-verb seam additionally full-validates
    `codemap.yaml` itself before calling here, since it is free to import
    `codegraph` and this reader deliberately is not."""
    codemap_path = Path(target_root) / "codemap.yaml"
    if not codemap_path.is_file():
        return ()
    try:
        doc = yaml_io.yaml_load(codemap_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — any parse failure is one problem
        raise PackConfigError("%s does not parse as YAML (%s)" % (codemap_path, exc)) from exc
    if not isinstance(doc, dict):
        raise PackConfigError("%s does not parse to a mapping (got %s)"
                              % (codemap_path, type(doc).__name__))
    preset = doc.get("preset")
    if preset is None:
        return ()
    if not isinstance(preset, list) or not all(isinstance(p, str) for p in preset):
        raise PackConfigError("%s: `preset` must be a list of strings" % codemap_path)
    return tuple(preset)


def enabled_pack_dirs(root: Path, preset: Sequence[str]) -> tuple:
    """Every pack `preset` enables, as its resolved directory under `root` (the
    HARNESS's OWN install root, never the project `--repo` `preset` was read from
    -- see this module's docstring), in `preset`'s own declared order. `root / p`
    must be a real directory for every `p` in `preset`, because a typo there must
    read as a real refusal, not as "this pack contributes nothing" -- and it must
    declare itself in a valid `pack.yaml` (`load_pack_manifest`); each manifest
    problem is reported as `<p>: <problem>` and that pack is not enabled.

    Returns `(pack_dirs, problems)` -- never raises; a caller turns a non-empty
    `problems` into its own named usage error."""
    pack_dirs = []
    problems = []
    for rel in preset:
        pack_dir = (root / rel).resolve()
        if not pack_dir.is_dir():
            problems.append("preset path does not exist: %s" % rel)
            continue
        _manifest, manifest_problems = load_pack_manifest(pack_dir)
        if manifest_problems:
            problems.extend("%s: %s" % (rel, p) for p in manifest_problems)
            continue
        pack_dirs.append(pack_dir)
    return tuple(pack_dirs), problems


def resolve_frag_dirs(root: Path, preset: Sequence[str]) -> tuple:
    """Every enabled pack's own `hs-code-registry.d/` (only the ones that
    actually have one — a pack with no registry fragments yet is not an
    error), over `enabled_pack_dirs` — the same refusals, the same order.

    Returns `(frag_dirs, problems)` — never raises; the caller (hs_code.py)
    turns a non-empty `problems` into its own named usage error."""
    pack_dirs, problems = enabled_pack_dirs(root, preset)
    return tuple(d / FRAG_DIR_NAME for d in pack_dirs if (d / FRAG_DIR_NAME).is_dir()), problems
