"""The project gate layer, `<data home>/gates.yaml`: read and judged in one place.

An entry turns ONE gate off on the record:

    gates:
      STD-X-001: {enabled: false, reason: "...", actor: "user:...", ts: "..."}

`layer_problem` is the one judgement both sides use — the reader that applies the layer
(`rule_view.load_rules`) honours exactly the entries the checker (`hs-cli gates`)
accepts. `known` maps each gate name to `{page, layer, floor}`: a name missing from it,
a page, a hook, a page whose reader ignores the layer, and a floor rule are refused —
a project layer never outranks the standards layer.

Stdlib plus `yaml_io`, because the reader sits on the hook path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping, Optional

import off_record

GATES_LAYER = "gates.yaml"
HOOKS_LAYER = "harness-hooks.yaml"


class LayerFileError(ValueError):
    """The layer file cannot be read as `gates: {name: entry}`."""


def read(path: Path) -> Dict[str, object]:
    """The `gates:` mapping of a layer file, unjudged; {} when absent."""
    import yaml_io
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        doc = yaml_io.yaml_load(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — named, path-carrying
        raise LayerFileError("%s: %s" % (path, e)) from e
    gates = doc.get("gates") if isinstance(doc, dict) else None
    if gates is None:
        return {}
    if not isinstance(gates, dict):
        raise LayerFileError("%s: `gates:` must be a mapping of gate name to entry" % path)
    return {str(k): v for k, v in gates.items()}


def entry_problem(name: str, entry) -> Optional[str]:
    """Why one entry is not an on-the-record off of one gate, by its shape alone."""
    if any(ch in name for ch in "*?[]"):
        return "%s is a pattern; an entry switches one gate, never a family" % name
    if not isinstance(entry, dict):
        return "%s: entry must be a mapping" % name
    extra = sorted(set(entry) - {"enabled", *off_record.RECORD_KEYS})
    if extra:
        return ("%s: key(s) %s not accepted — an entry only turns a gate off "
                "(enabled: false + reason, actor, ts); a project never loosens a threshold"
                % (name, ", ".join(extra)))
    if entry.get("enabled") is not False:
        return "%s: enabled must be false — a project layer only turns gates off" % name
    missing = off_record.missing(entry)
    if missing:
        return "%s: missing %s — every off says who, when and why" % (name, ", ".join(missing))
    return None


def layer_problem(name: str, entry, known: Mapping[str, Mapping], pages=()) -> Optional[str]:
    """Why `name: entry` is not an off this layer may carry, or None."""
    if name in pages:
        return "%s is a page; an entry switches one gate, never a family" % name
    problem = entry_problem(name, entry)
    if problem:
        return problem
    info = known.get(name)
    if info is None:
        return "no gate named %s in the registry" % name
    if info.get("layer") == HOOKS_LAYER:
        return ("%s is a hook — hooks are switched in %s (hs-cli gates --off writes there)"
                % (name, HOOKS_LAYER))
    if info.get("layer") != GATES_LAYER:
        return ("%s is on page %s, whose reader does not honour a project layer"
                % (name, info.get("page")))
    if info.get("floor"):
        return "%s is a floor rule — no project layer turns it off" % name
    return None


def offs(layer: Mapping[str, object], known: Mapping[str, Mapping], pages=()) -> Dict[str, dict]:
    """The entries of `layer` this layer may carry — what a reader applies."""
    return {n: e for n, e in layer.items()  # type: ignore[misc]
            if layer_problem(n, e, known, pages) is None}
