"""port_spec.py — the port domain's per-dist SPEC reader
(`.port/batches/<b>/specs/<dist>.yaml`), as one FLAT module under
`harness/scripts/` (sibling of `port_batch_reader.py`: same dot-free-module
reason, and `port-batch` is this reader's first caller).

`Spec` is ported (algorithm-faithful) from `ob-tools@60075cb:packages/
portkit/src/portkit/build/spec.py` (`Spec`/`Spec.load`), trimmed to exactly
the fields `port-batch`'s own algorithm reads (`module_map`, `renames`,
`born_new`, `placeholder`) plus `dist` (load-time typo-catch only — no
caller on this path reads it back off the loaded `Spec`). `tier`/`question`
stay REQUIRED-key checks only (upstream's own typo-catch, `spec.py:79-82`),
never stored — nothing on this path reads them either. `add-module` already
extends this SAME closed set with `name_moves` below, rather than growing a
second `Spec` reader beside this one. `hs-code port build` is now ported (`codegraph_port_build_verbs.py`); it
extends this same closed set the same way, rather than growing a second
`Spec` reader beside this one — including upstream's own hand-edit-step list
and the helper that parses it (`build/polish.py`, ported here as
`port_polish.py`): `hand_polish` is normalised through
`port_polish.extend_spec_with_polish` at load time (a prose string from an
old spec becomes a `{"note": ...}` step, never a bare string a strategy
dispatch would choke on), and `build_dist`'s slice pass reads `slice_sources`
back off the loaded `Spec` for real now, not for presence only.

`name_moves` is added to `Spec` here: `add-module`'s own closure reaches it
through `import_rewrite.rewrite_imports`'s name-level house-split branch —
a name split across dists after the fact, e.g. `contracts.py` ->
`ob.contract` + `ob.contract.provenance` — the SAME extend-the-closed-set
precedent this module's own docstring already names for `port-batch`.

`hs-code port build`'s own closure extends the set again, the same way:
`tier`/`question` (checked but not stored before — the docstring above named
this exact gap) plus `description`/`version`/`package`/`unit_sources`/
`file_sources`/`slice_sources`/`handbuilt`/`hand_polish`/`deps_extra`/
`door_extra` — every field `build_dist` reads off a dist spec, ported
(algorithm-faithful) from `ob-tools@60075cb:packages/portkit/src/portkit/
build/spec.py`'s own `Spec`/`_file_entry`. `slice_sources` needed NO shape
change to go from "read for presence only" to "read for real": the parsed
shape (`{module, to?, symbols?, docstring?, keep_source_docstring?,
imports?}`) already matches exactly what `build_dist`'s slice pass consumes.
`hand_polish` DOES change shape: it is normalised through
`port_polish.extend_spec_with_polish` at load time now, instead of a bare
`list(d.get("hand_polish") or [])` — see the field's own comment below.

`merged_spec_and_unit_dist` is ported (algorithm-faithful) from `ob-tools@
60075cb:packages/portkit/src/portkit/tests_move/test_move.py`'s own function
of the same name — `add-module`'s closure reaches only `merged`/`multi`;
nothing in this tree calls the `primary`/`spans` halves yet, but a future
port of `tests`/`tests-land` would reach the SAME function for those, so the
full 5-tuple stays together here rather than splitting into two versions
that could drift apart. Upstream's own `_OLD_PREFIX` (a hard-coded
`"gsm_agent."` literal, `move_rename.py:16`) is replaced by a required
`old_prefix` argument — the same "no hard-coded package" rule applied
everywhere else in this port domain (`port.yaml sources[].package`).

Reads through `yaml_io.yaml_load`, matching every other config reader in
this tree (`port_config.py`'s own docstring says why a second `import yaml`
here would read the file a shade differently from every other reader).
"""
from __future__ import annotations

import dataclasses
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import port_polish
import yaml_io
from hs_family.errors import UsageError

#: Upstream's own load-time typo-catch (`spec.py:79-82`): a spec missing any
#: of these is refused outright, never silently treated as "declares
#: nothing". `tier`/`question` are checked for presence only — nothing on
#: `port-batch`'s path stores or reads their values.
_REQUIRED_KEYS = ("dist", "tier", "question", "module_map")


def _file_entry(s: dict) -> dict:
    """One `sources[]` `file:` entry, both spellings the upstream loader
    accepts: flat (`{file: path, to: x}` at the entry level) and nested
    (`{file: {file: path, to: x}}`). Ported (algorithm-faithful) from
    `ob-tools@60075cb:packages/portkit/src/portkit/build/spec.py:_file_entry`
    — the flat spelling's extra keys merge in via `setdefault`, never
    overwriting a value the nested spelling already carries; flattening the
    two shapes into one dict WITHOUT this merge once silently ate
    `exclude_ported` (a real upstream regression)."""
    f = s["file"]
    ent = dict(f) if isinstance(f, dict) else {"file": f}
    for k in ("to", "from_module", "exclude_ported", "exclude_foreign"):
        if k in s:
            ent.setdefault(k, s[k])
    return ent


class SpecError(UsageError):
    """`<dist>.yaml` could not be read, does not parse, or fails the
    load-time typo-catch (a required key missing) — exit 2, the same
    `hs_family` error every other config reader in this tree raises for an
    unusable file, never a bare `SystemExit` the way upstream's own `die()`
    does."""


@dataclass(frozen=True)
class Spec:
    """One dist's port spec — trimmed to the fields `port-batch` reads (see
    module docstring)."""

    dist: str
    module_map: "dict[str, str] | None"
    renames: "dict[str, str]"
    born_new: "list[dict]"
    placeholder: bool = False
    #: "<src module>::<name>" -> destination MODULE — a from-import whose
    #: names were split across dists after the fact. The name itself is
    #: untouched (`renames` owns name changes); this owns HOUSE changes.
    name_moves: "dict[str, str]" = field(default_factory=dict)
    #: Required-key checks only until `build` (see module docstring); now
    #: stored too — `build_dist`'s pyproject/door text reads both back.
    tier: str = ""
    question: str = ""
    description: str = ""
    version: str = "0.1.0"
    #: The directory under `src/<namespace_root>/` this dist's files land
    #: under — `build_dist` refuses a spec with none declared.
    package: "str | None" = None
    #: Whole packages to copy, one dotted unit per entry.
    unit_sources: "list[str]" = field(default_factory=list)
    #: `{file, to?, from_module?, exclude_ported?, exclude_foreign?}` — the
    #: flat spelling (`{file: path, ...}` at the source-list entry level)
    #: already merged in by `_file_entry` at load time.
    file_sources: "list[dict]" = field(default_factory=list)
    #: `{module, to?, symbols?, docstring?, keep_source_docstring?,
    #: imports?}` — `build_dist`'s slice pass reads every one of these keys
    #: back off the loaded spec (see `codegraph_port_build_verbs.build_dist`).
    slice_sources: "list[dict]" = field(default_factory=list)
    #: Hand-made destinations, existence-checked only.
    handbuilt: "list[str]" = field(default_factory=list)
    #: Idempotent hand-polish steps, normalised through
    #: `port_polish.extend_spec_with_polish` at load time (a prose string
    #: from an old spec becomes a `{"note": ...}` step) — `build_dist` runs
    #: `port_polish.apply_polish` over the built tree with this list, right
    #: after the leftover-import sweep and before the py_compile self-check.
    hand_polish: "list[dict]" = field(default_factory=list)
    #: AST-blind pyproject requirements declared by hand.
    deps_extra: "list[str]" = field(default_factory=list)
    #: Extra imports for the package door — read for presence only; nothing
    #: on this path writes them into a door yet.
    door_extra: "list[str]" = field(default_factory=list)

    @staticmethod
    def load(path: Path) -> "Spec":
        """The ONE door onto a dist spec."""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SpecError("%s could not be read (%s)" % (path, exc)) from exc
        try:
            d: Any = yaml_io.yaml_load(text)
        except Exception as exc:  # noqa: BLE001 — any parse failure is one problem
            raise SpecError("%s does not parse as YAML (%s)" % (path, exc)) from exc
        if not isinstance(d, dict):
            raise SpecError(
                "%s does not parse to a mapping (got %s)" % (path, type(d).__name__))
        missing = [k for k in _REQUIRED_KEYS if k not in d]
        if missing:
            raise SpecError(
                "%s is missing key(s): %s" % (path, ", ".join(missing)))
        sources = [s for s in (d.get("sources") or []) if isinstance(s, dict)]
        return Spec(
            dist=d["dist"],
            module_map=d["module_map"],
            renames=d.get("renames") or {},
            born_new=d.get("born_new") or [],
            placeholder=bool(d.get("placeholder", False)),
            name_moves=d.get("name_moves") or {},
            tier=str(d["tier"]),
            question=str(d["question"]),
            description=str(d.get("description") or ""),
            version=str(d.get("version") or "0.1.0"),
            package=d.get("package"),
            unit_sources=[s["unit"] for s in sources if "unit" in s],
            file_sources=[_file_entry(s) for s in sources if "file" in s],
            slice_sources=[s["slice"] if isinstance(s["slice"], dict) else {"module": s["slice"]}
                          for s in sources if "slice" in s],
            handbuilt=list(d.get("handbuilt") or []),
            hand_polish=port_polish.extend_spec_with_polish(d),
            deps_extra=list(d.get("deps_extra") or []),
            door_extra=(d.get("door") or {}).get("extra_imports", [])
            if isinstance(d.get("door"), dict) else [],
        )


def merged_spec_and_unit_dist(specs_dir: Path, old_prefix: str):
    """Every `*.yaml` spec under `specs_dir` merged into ONE `Spec` + four
    tables: `(merged, unit -> PRIMARY dist, unit -> dists it spans, old
    module -> the several houses it maps to when specs disagree, root
    catch-all key -> house)`.

    A module mapped to several houses is not an error — `contracts.py`
    really split (`ob.contract` + `ob.contract.provenance`); the decision
    moves to NAME level (`Spec.name_moves`), never guessed at module level.
    Without this, every import of a multi-home module would be refused.

    A ROOT key (no dot — the bare old package, e.g. a spec carrying
    `<old_prefix>: ob.prompts`) is pulled OUT of the map: as a longest-
    prefix entry it would win for EVERY old path, silently swallowing every
    other mapping under it. It is reported (`catch_all`) so a caller can
    warn; the lines it used to swallow surface as ordinary leftovers.

    Raises `SpecError` when `specs_dir` carries no `*.yaml` spec at all —
    the request is well-formed but there is nothing to merge."""
    old_prefix = old_prefix.rstrip(".") + "."
    module_map: "dict[str, str]" = {}
    renames: "dict[str, str]" = {}
    name_moves: "dict[str, str]" = {}
    unit_dists: "dict[str, Counter]" = {}
    multi: "dict[str, list[str]]" = {}
    catch_all: "dict[str, str]" = {}
    base: "Spec | None" = None
    for p in sorted(specs_dir.glob("*.yaml")):
        spec = Spec.load(p)
        base = spec if base is None else base
        for old, new in (spec.module_map or {}).items():
            if "." not in old:
                catch_all[old] = new
                continue
            if old in module_map and module_map[old] != new:
                houses = multi.setdefault(old, [module_map[old]])
                if new not in houses:
                    houses.append(new)
                continue
            module_map[old] = new
            unit = old[len(old_prefix):].split(".")[0] if old.startswith(old_prefix) else None
            if unit:
                unit_dists.setdefault(unit, Counter())[spec.dist] += 1
        renames.update(spec.renames)
        name_moves.update(spec.name_moves)
    for old in multi:
        module_map.pop(old, None)
    if base is None:
        raise SpecError("no *.yaml spec under %s" % specs_dir)
    merged = dataclasses.replace(
        base, dist="(merged)", module_map=module_map, renames=renames,
        name_moves=name_moves)
    primary = {u: c.most_common(1)[0][0] for u, c in unit_dists.items()}
    spans = {u: sorted(c) for u, c in unit_dists.items() if len(c) > 1}
    return merged, primary, spans, multi, catch_all
