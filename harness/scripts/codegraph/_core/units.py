"""Which unit of a snapshot a name refers to.

A snapshot groups its records by unit — a root-relative directory — and more
than one caller needs to turn a name a person typed into one of those keys.
Written once here because the two spellings drifted the moment there were two:
one accepted a bare `scripts` when it was unique, the other refused anything
but the full key, and both were reachable from the same command line.

Why a name and not a dotted prefix, measured rather than assumed: on one tree
`--prefix harness.scripts` and `--unit harness/scripts` returned the identical
115 modules, so the dotted filter CAN express a directory. What it cannot do is
notice a typo — `--prefix harness.scrips` returned 0 modules at exit 0, which
reads exactly like a directory holding nothing. That asymmetry, not
expressiveness, is what this exists for.

WHAT IT CANNOT SEE: whether the unit a name resolves to is the one the caller
MEANT. A bare name that is unique today stops being unique the moment a second
root grows a directory of that name, and the refusal is the only warning.
"""

from __future__ import annotations

from ambiguous_input import Ambiguous


def resolve_unit(index: dict, name: str) -> str:
    """The full `<root>/<unit>` key for `name`.

    A bare unit name resolves only when it is UNIQUE: two roots can both hold a
    `scripts` unit, and picking one silently would measure the wrong half of
    the tree. Both failures raise `ValueError` naming what was available, so a
    typo can never read as an empty directory — which is what the dotted-prefix
    filter does instead, measured: 0 modules, exit 0, no complaint.
    """
    units = index.get("units", {})
    if name in units:
        return name
    hits = sorted(u for u in units if u.split("/", 1)[-1] == name)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise Ambiguous("unit", name, hits, "use the full '<root>/<unit>' key")
    raise ValueError(
        f"no unit {name!r}. Available: {', '.join(sorted(units))}")


def select_units(index: dict, unit: "str | None") -> list:
    """Every record in `index`, or only the ones in `unit`."""
    if unit is None:
        return [rec for recs in index.get("units", {}).values() for rec in recs]
    return list(index["units"][resolve_unit(index, unit)])


def module_unit_map(index: dict) -> "dict[str, str]":
    """dotted module -> the unit key it is LISTED under in `index`. Membership,
    not a guessed dotted segment: a namespace package (`vsf.di.<x>` split over
    dists) gives every module the same second segment and no unit that name."""
    return {m["module"]: unit
            for unit, mods in index.get("units", {}).items() for m in mods}


def unit_of_dotted(dotted: str, mod_unit: "dict[str, str]", lang: "str | None" = None) -> "str | None":
    """Unit of the longest module prefix of `dotted` — the string may carry
    trailing attribute segments (`pkg.mod.Class.attr`) or a `::symbol` tail a
    module name never has; a path id (`a/b.ts`) is matched whole, never by a
    prefix (`lang.ids.lookup_keys`), and so is a target of a record of `lang` other than
    Python. None = no measured module matched."""
    from .lang.ids import lookup_keys
    for key in lookup_keys(dotted, lang):
        unit = mod_unit.get(key)
        if unit is not None:
            return unit
    return None
