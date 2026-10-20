"""Preset library — a descriptor's `extends:` resolves through here.

A preset is a PARTIAL v2 descriptor shipped as data (`library/*.yaml`), never
code: it may carry `layout`, `scan`, `thresholds` — everything except identity
(`schema_version`, `project`), which the extending file must declare itself.
The generator `hs-code graph init` merges through this same door
(`codegraph/graph/rules.py`, `resolve_doc`), so its written file and a
hand-written `extends:` cannot drift apart.

MERGE SEMANTICS (deterministic, one rule)
    dict  -> merged recursively
    other -> replaced (lists REPLACE, they never concatenate: appending root
             lists across presets would build a layout nobody declared)
    Order: presets apply in declaration order, later wins; the extending file
    applies last and wins over everything.

WHAT IT CANNOT SEE
    * A preset is trusted once its FILE passes `duplicate_keys` — its layout
      is never compared against any real tree here (that is `check_repo` and
      the snapshot pass's job).
    * `extends` inside a preset resolves too (chains, depth-capped) — a cycle
      or a too-deep chain is an ERROR naming the chain, never a silent floor.
    * There is no registry of "who extends me": renaming a preset id breaks
      extending descriptors at THEIR next load, not here.

`duplicate_keys` is imported from `..descriptor.model` rather than
`..descriptor.store` on purpose: `store.load()` needs `resolve_doc` (below)
for `extends:` resolution, so a `presets -> descriptor.store` edge here would
close a cycle with `descriptor.store`'s own (function-local) import of this
module. `descriptor.model` imports neither `.store` nor `presets`, so the
dependency stays one-directional.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml_io

from ..descriptor.model import DescriptorError, duplicate_keys

LIBRARY_DIR = Path(__file__).parent / "library"

#: Guard against preset-chains that loop or merely never bottom out.
MAX_CHAIN_DEPTH = 8


def available_presets() -> list[str]:
    """Preset ids on disk (sorted). Data, not a hardcoded list."""
    return sorted(p.stem for p in LIBRARY_DIR.glob("*.yaml"))


def load_preset(preset_id: str) -> dict[str, Any]:
    """One preset's raw document, through the same duplicate-key door as user
    descriptors — a shipped file with a doubled key is OUR bug, caught here."""
    p = LIBRARY_DIR / f"{preset_id}.yaml"
    if not p.is_file():
        raise DescriptorError(
            f"unknown preset {preset_id!r}; available: "
            f"{', '.join(available_presets()) or '(none)'}")
    dupes = duplicate_keys(p)
    if dupes:
        detail = "\n".join(f"    `{k}` twice (lines {a}, {b})" for k, a, b in dupes)
        raise DescriptorError(f"preset {preset_id!r} has duplicate keys:\n{detail}")
    doc = yaml_io.yaml_load(p.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise DescriptorError(f"preset {preset_id!r}: root must be a mapping")
    return doc


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """The one merge rule (see module docstring). Pure: returns a new dict."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_doc(doc: dict[str, Any], _depth: int = 0,
                _chain: tuple[str, ...] = ()) -> dict[str, Any]:
    """Resolve `extends:` to a flat v2 document (the `extends` key is GONE).

    `schema_version`/`project` may ride in a preset for a generator's
    convenience, but `resolve_doc` never INJECTS them: a file that did not
    declare its own schema_version stays without one (the store rejects it —
    no silent default, same rule as an un-extended file).
    """
    if _depth > MAX_CHAIN_DEPTH:
        chain = " -> ".join(_chain) or "(top)"
        raise DescriptorError(
            f"preset chain deeper than {MAX_CHAIN_DEPTH}: {chain} — a cycle or a "
            f"tower; neither measures anything")
    extends = doc.get("extends")
    if extends is None:
        return {k: v for k, v in doc.items() if k != "extends"}
    ids = [extends] if isinstance(extends, str) else list(extends)
    if not ids or not all(isinstance(x, str) and x for x in ids):
        raise DescriptorError(
            f"`extends:` must be one preset id or a list of ids, got {extends!r}")
    merged: dict[str, Any] = {}
    for pid in ids:
        if pid in _chain:
            raise DescriptorError(
                f"preset cycle: {' -> '.join(_chain + (pid,))}")
        preset = load_preset(pid)
        resolved = resolve_doc(preset, _depth + 1, _chain + (pid,))
        merged = deep_merge(merged, resolved)
    own = {k: v for k, v in doc.items() if k != "extends"}
    return deep_merge(merged, own)


def dump_yaml(doc: dict[str, Any]) -> str:
    """Deterministic, hand-editable YAML (insertion order kept, unicode kept).
    The ONE writer so generated output and tests agree on bytes."""
    return yaml_io.safe_dump(doc, sort_keys=False, default_flow_style=False,
                             allow_unicode=True, width=100)
