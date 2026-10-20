"""hs-code's `port` domain — the batch write verb, as one FLAT module under
`harness/scripts/` (sibling of `codegraph_port_ledger_verbs.py`: same
`hs-code-registry.d` reachability reason — a bare, dot-free `module:` name).

`PORT_BATCH` is ported (algorithm-faithful) from `ob-tools@60075cb:
packages/portkit/src/portkit/batch/port_batch.py` (`port_batch`,
`_map_module`, `_rows_for`) — one ledger line per symbol/method of a landed
dist, read off the batch's own FROZEN snapshot, plus one `born-new` line per
`spec.born_new` entry. Upstream's positional `dist`/`new_hash8` CLI args and
`--plan DIR` are replaced by this domain's batch concept: the dist stays a
positional, `new_hash8` (upstream's own landed-tree anchor) is resolved from
`--batch NAME`'s own `new_snapshot` (`port_batch_reader.new_batch_anchor`,
the SAME door `port map`'s `new@batch` shorthand resolves through), and the
frozen snapshot comes from that batch's own `frozen_snapshot` — never a
caller-typed `--plan`.

`src.id`/`src.sha` on every line this verb writes are the batch's OWN
PINNED source: `src.sha` is `port_batch_reader.pinned_frozen_commit`'s own
return value (the frozen snapshot's commit, checked against `find_source`'s
`pinned_sha` through the ONE anchor/pin door every port verb shares,
`port_batch_reader.check_anchor_pinned`) — a frozen snapshot taken at a
different commit than the batch's own pin is refused outright (exit 3,
before any listing/dry-run output), never silently trusted. Never a live
git resolve — a batch is a promise that every line written under it agrees
on ONE commit (the same rule `MAP`'s `--batch` mode and `INIT`
already hold to).

WRITE ORDER: the whole plan through `write_all` (`append_events`), in
upstream's own row order. `port_batch` can write TWO lines for the SAME node
in one call — a node found `dropped` gets a transitional `in-progress` line
immediately before its `ported` line (a dist relanding after an older batch
split it) — and the second line's legality depends on the first: the batch
door judges each line against a state the earlier lines of the plan already
joined, so that pair is legal in the dry-run as it is on `--apply`. One
refused line writes nothing (upstream stops at the first refusal and leaves
what already landed, `port_batch.py:139,163,178`: a half-written dist).

DRY-RUN PREVIEW (no `--apply`): the same law-by-law verdict `--apply` would
reach, then a listing of what WOULD be written.

GIT: none. Every commit this verb writes into a ledger line comes from the
batch's own `pinned_sha` or a snapshot's own stamped `commit` — never a live
resolve — so this module carries no `codegraph._core.git` import at all
(unlike `codegraph_port_ledger_verbs.py`'s live-resolve path for a
non-batch `port map`).

GHOST CHECK: after a real `--apply` write, every OLD-tree key the ledger's
own replayed state carries that the frozen snapshot no longer claims is
reported (never overwritten) — ported (algorithm-faithful) from `ob-tools@
60075cb:packages/portkit/src/portkit/batch/port_batch.py:174-179`, reusing
`codegraph_port_ledger_verbs._snapshot_node_keys` (the same door `INIT`
already opens onto a snapshot's own key set) rather than a second copy of
it. Dry-run prints no ghost check — there is nothing on disk yet for a
ledger-vs-snapshot comparison to mean anything against.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hook_runtime import resolve_actor  # noqa: E402

import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_ledger_reader  # noqa: E402
import port_spec  # noqa: E402
import port_workspace  # noqa: E402
import yaml_io  # noqa: E402
from codegraph._core.pathtail import tail_matches  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from codegraph_port_ledger_verbs import _snapshot_node_keys, write_all  # noqa: E402
from hs_family.cli import Command  # noqa: E402
from hs_family.versioning import int_version_problem  # noqa: E402
from port_ledger_writer import BrokenLedgerError, Ledger, now_ts  # noqa: E402


def _port_batch_args(ap: argparse.ArgumentParser) -> None:
    """`dist` is the sole positional; `--batch` is required (this verb has
    no walk-up default — a batch is always named)."""
    ap.add_argument("dist")
    ap.add_argument("--batch", required=True, dest="batch_name")


def _map_module(mod: str, mmap: "dict[str, str] | None") -> str:
    """LONGEST prefix wins. Ported (algorithm-faithful) from `ob-tools@
    60075cb:packages/portkit/src/portkit/batch/port_batch.py:47-54`
    (`_map_module`) — the absolute-match lesson upstream's own docstring
    names: a plain-prefix match without the longest-wins tiebreak silently
    lost child modules to a shorter, unrelated `module_map` entry."""
    best_src: "str | None" = None
    best_dst: str = ""
    for src, dst in (mmap or {}).items():
        if mod == src or mod.startswith(src + "."):
            if best_src is None or len(src) > len(best_src):
                best_src, best_dst = src, dst
    return mod if best_src is None else best_dst + mod[len(best_src):]


def _rows_for(idx: dict, dist: str,
              assigned: "dict[str, str] | None" = None) -> "list[tuple[str, str]]":
    """`(module, name)` of every symbol/method carrying `dist` — a method
    with no dist of its own INHERITS its class's. Ported (algorithm-
    faithful) from `ob-tools@60075cb:packages/portkit/src/portkit/batch/
    port_batch.py:57-72` (`_rows_for`) — the dist-lookup kept in this one
    function so `port assign` and the `map` rule kind can both reuse it
    against the same snapshot shape rather than re-deriving it.

    `assigned` is the SAME `module::name -> dist` sidecar `port assign`
    writes (never the snapshot itself — the frozen snapshot stays
    immutable, see that verb's own module docstring): it WINS over a
    symbol's own `dist` field (always
    absent, in this tree, until a sidecar exists) when both are present,
    which is exactly how upstream's post-`assign` snapshot would have
    answered this same lookup (upstream wrote `dist` straight into the
    snapshot; this tree writes it beside it instead)."""
    return [(mod, sym["name"]) for mod, sym, d in port_batch_reader.node_dists(idx, assigned)
            if d == dist]


_ASSIGNMENT_FILENAME = "assignment.yaml"
_ASSIGNMENT_KEYS = frozenset({"version", "by_symbol", "by_module", "by_unit", "applies_to"})
_ASSIGNMENT_VERSIONS = frozenset({1})


def load_assignment_rules(path: Path) -> dict:
    """The one door onto a batch's `assignment.yaml` — ported (shape only;
    upstream's own `load_rules` is a two-line `yaml.safe_load`) with the
    closed-key validation every other config reader in this tree already
    holds to (`port_batch_reader._validate_batch_config`, `port_config.py`).
    Raises `hs_family.UsageError` (exit 2) for anything short of a valid,
    closed-key mapping — never a silent "declares nothing"."""
    if not path.is_file():
        raise hs_family.UsageError("no assignment rules at %s" % path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise hs_family.UsageError("%s could not be read (%s)" % (path, exc)) from exc
    try:
        doc = yaml_io.yaml_load(text) or {}
    except Exception as exc:  # noqa: BLE001 — any parse failure is one problem
        raise hs_family.UsageError("%s does not parse as YAML (%s)" % (path, exc)) from exc
    if not isinstance(doc, dict):
        raise hs_family.UsageError(
            "%s does not parse to a mapping (got %s)" % (path, type(doc).__name__))
    unknown = sorted(set(doc) - _ASSIGNMENT_KEYS)
    if unknown:
        raise hs_family.UsageError(
            "%s has unknown key(s): %s (known: %s)"
            % (path, ", ".join(unknown), ", ".join(sorted(_ASSIGNMENT_KEYS))))
    if "version" in doc:
        problem = int_version_problem(doc["version"], _ASSIGNMENT_VERSIONS)
        if problem:
            raise hs_family.UsageError("%s: `version` %s" % (path, problem))
    return doc


def resolve_dist(path: str, unit: str, symbol: "str | None",
                  rules: dict) -> "tuple[str, str]":
    """`(dist, why)` — specific wins. Ported (algorithm-faithful) from
    `ob-tools@60075cb:packages/portkit/src/portkit/ledger/assign.py:99-127`
    (`resolve`): exact `by_symbol` line first, then a trailing-`.*` line
    (the longest stem wins among stars), then the longest-tail `by_module`
    match, then `by_unit` (full key, then bare), else UNRULED. One change from
    upstream: a path tail matches whole segments (`tail_matches`, the matcher
    `graph file` uses), so `c.py` no longer claims `pkg/bc.py`."""
    if symbol:
        by_sym = rules.get("by_symbol") or {}
        for key, dist in by_sym.items():
            tail, _, name = key.partition("::")
            if name == symbol and tail_matches(path, tail):
                return dist, "by_symbol %s" % key
        best_star: "tuple[int, str, str] | None" = None
        for key, dist in by_sym.items():
            tail, _, name = key.partition("::")
            if not name.endswith(".*") or not tail_matches(path, tail):
                continue
            stem = name[:-2]
            if symbol == stem or symbol.startswith(stem + "."):
                cand = (len(stem), dist, "by_symbol %s" % key)
                if best_star is None or cand[0] > best_star[0]:
                    best_star = cand
        if best_star:
            return best_star[1], best_star[2]
    best: "tuple[int, str, str] | None" = None
    for tail, dist in (rules.get("by_module") or {}).items():
        if tail_matches(path, tail) or (tail.endswith("/") and ("/%s" % tail) in path + "/"):
            cand = (len(tail), dist, "by_module %s" % tail)
            if best is None or cand[0] > best[0]:
                best = cand
    if best:
        return best[1], best[2]
    by_unit = rules.get("by_unit") or {}
    for key in (unit, unit.split("/", 1)[-1]):
        if key in by_unit:
            return by_unit[key], "by_unit %s" % key
    return "UNRULED", "no rule"


def _check_unit_rules(rules: dict, units: "list[str]") -> "list[str]":
    """Which `by_unit` keys are unusable. Ported (algorithm-faithful) from
    `ob-tools@60075cb:...assign.py:130-153` (`check_unit_rules`): a key
    pointing at a unit that does not exist, or a bare key ambiguous across
    two roots — both a typo nobody would notice on their own."""
    errs: "list[str]" = []
    bare: "dict[str, list[str]]" = {}
    for u in units:
        bare.setdefault(u.split("/", 1)[-1], []).append(u)
    for key in (rules.get("by_unit") or {}):
        if key in units:
            continue
        hits = bare.get(key, [])
        if not hits:
            errs.append("by_unit `%s` points at a unit that does not exist" % key)
        elif len(hits) > 1:
            errs.append("by_unit `%s` is AMBIGUOUS — exists as %s. Spell "
                        "the full key." % (key, ", ".join(hits)))
    return errs


def _dead_law_warnings(rules: dict, idx: dict) -> "list[str]":
    """Advisory only, never a red — a rule matching 0 nodes in the snapshot
    is a law nobody deleted after its subject moved. Ported (algorithm-
    faithful) from `ob-tools@60075cb:...assign.py:156-181`
    (`warn_dead_laws`)."""
    units = idx.get("units") or {}
    all_paths = [m["path"] for mods in units.values() for m in mods]
    sym_pairs: "set[tuple[str, str]]" = set()
    for mods in units.values():
        for m in mods:
            for s in m.get("symbols") or []:
                sym_pairs.add((m["path"], s["name"]))
                for mm in s.get("methods") or []:
                    sym_pairs.add((m["path"], mm["name"]))
    out: "list[str]" = []
    for tail in sorted((rules.get("by_module") or {})):
        if not any(tail_matches(p, tail) or (tail.endswith("/") and ("/%s" % tail) in p + "/")
                  for p in all_paths):
            out.append("by_module `%s` matches 0 paths in the snapshot — a "
                      "dead law?" % tail)
    for key in sorted((rules.get("by_symbol") or {})):
        tail, _, name = key.partition("::")
        star = name.endswith(".*")
        stem = name[:-2] if star else name
        hit = False
        for path, sname in sym_pairs:
            if not tail_matches(path, tail):
                continue
            if (star and (sname == stem or sname.startswith(stem + "."))) or \
                    (not star and sname == name):
                hit = True
                break
        if not hit:
            out.append("by_symbol `%s` matches no node in the snapshot — a "
                      "dead law?" % key)
    return out


class _NoWorkspaceRootError(hs_family.UsageError):
    """The target snapshot declares no `workspace`-kind root at ALL — a
    structural gap in the target's own layout declaration, never a
    per-module "not landed yet" state (that stays `None`, no error)."""


def _target_module_dist(idx: dict) -> "dict[str, str]":
    """`module -> dist` for every module the TARGET tree's own snapshot
    records under a `workspace`-kind root — the dist it ALREADY sits in.
    Ported (algorithm-faithful) from `ob-tools@60075cb:packages/portkit/
    src/portkit/legacy.py:59-66` (`WorkspacePayloadProvider.source_payload`):
    "dist IS the subdirectory name (a fact read off the path)". This
    tree's own kernel snapshot writes no `payload.dist` at all (every
    module's own `payload` dict is empty — measured live against a real
    `hs-code graph snapshot` run over a `packages/<dist>/src/...` fixture),
    so this reads `idx["roots"]`'s own `kind` plus each unit key's own
    shape (`<root id>/<package dir name>`, `codegraph._core.snapshot.
    pipeline.discover`'s own workspace branch) instead of a payload key a
    provider had already filled in at snapshot-build time.

    Raises `_NoWorkspaceRootError` when `idx` declares NO `workspace`-kind
    root anywhere — nothing this derivation could ever answer from, a
    structural gap worth surfacing loudly rather than reading as "nothing
    ported yet"."""
    root_kind = {r.get("id"): r.get("kind") for r in (idx.get("roots") or ())}
    if "workspace" not in root_kind.values():
        raise _NoWorkspaceRootError(
            "the target snapshot %r declares no `workspace` root — a "
            "module's dist cannot be derived from it" % idx.get("snapshot"))
    out: "dict[str, str]" = {}
    for unit, mods in (idx.get("units") or {}).items():
        for m in mods:
            if root_kind.get(m.get("root")) == "workspace":
                out[m["module"]] = unit.split("/", 1)[-1]
    return out


def _ported_destination(led_state: dict, commit_tag: str, module: str,
                        name: str) -> "str | None":
    """The TARGET module a source `(module, name)` entry already ported
    to, read off the ledger's own `to` — `None` when the entry carries no
    `ported` line yet (a legitimate "not landed" state, never an error).
    `to`'s own shape is `"<new_anchor>::<module>::<name>"` (see
    `_plan_events`'s own `new_to` construction, below) — only the module
    component is wanted here."""
    node_key = "%s::%s::%s" % (commit_tag, module, name)
    prev = led_state.get(node_key)
    if prev is None or prev.status != "ported":
        return None
    to = tuple(prev.extra.get("to") or ())
    if not to:
        return None
    _anchor, newmod, _newname = to[0].split("::", 2)
    return newmod


def _assign_entries(idx: dict) -> "list[tuple[str, str, str, str]]":
    """`(unit, path, module, name)` for every symbol/method in `idx` — the
    exact traversal `_rows_for`'s own `visit` walks, flattened (assign
    resolves every entry independently, never by class inheritance — see
    `ASSIGN`'s own docstring)."""
    out: "list[tuple[str, str, str, str]]" = []
    for unit, mods in (idx.get("units") or {}).items():
        for m in mods:
            for s in m.get("symbols") or []:
                for h in [s] + list(s.get("methods") or []):
                    out.append((unit, m.get("path") or "", m["module"], h["name"]))
    return out


def _assign_args(ap: argparse.ArgumentParser) -> None:
    """`assign` takes no positional argument, only `--batch`."""
    ap.add_argument("--batch", required=True, dest="batch_name")


def _assign(args: argparse.Namespace) -> int:
    """`hs-code port assign --batch <name> [--apply]` — fill/cross-check the
    dist assignment for one batch's frozen SOURCE snapshot against its own
    `.../map/assignment.yaml` rules, writing the result to an out-of-band
    sidecar, one `module::name -> dist` entry per symbol/method.

    Ported (algorithm-faithful) from `ob-tools@60075cb:packages/portkit/
    src/portkit/ledger/assign.py` (`resolve`/`check_unit_rules`/
    `warn_dead_laws`/the fill+cross-check loop of `apply_assignment`) —
    ADAPTED for the same reason the `map` rule kind is: upstream overwrites
    the snapshot's `_index.json` plus every unit YAML's `dist:` lines IN
    PLACE; a batch's frozen snapshot is a promise every line written under
    it agrees on one commit (`port_batch_reader.py`'s own module
    docstring), so this verb writes ONLY the sidecar — the snapshot on
    disk is never touched, byte for byte, by this verb (the immutability a
    hash test pins). Upstream's file-rewrite job also has a second half
    this tree has no analogue for (a unit/module-level `dist:` line at
    every YAML indent level) — this tree's own idx already carries `dist`
    at symbol/method granularity, so only that granularity is written.

    "TRUTH" for the cross-check is where a source entry ALREADY sits in the
    TARGET tree — never the frozen SOURCE tree, which never moves (a
    check against it could never redden a module ported into the wrong
    dist, the exact bug this cross-check exists to catch). For each
    source `(module, name)`: the ledger's own replayed state (one read
    door, `Ledger.load_checked`) says whether it carries a `ported` line
    yet; if it does, `_ported_destination` reads the TARGET module name
    off that line's own `to`, and `_target_module_dist` (over the batch's
    own `new_snapshot` — the target tree's own snapshot, never the
    frozen source one: a snapshot is only ever compared with the tree it
    was taken of) resolves that module's dist from the target's own
    `workspace`-kind root layout. No `ported` line yet, no `new_snapshot`
    declared yet, or one declared but not built yet (the lane builds it after
    `build`), is `None` — a legitimate "not landed" state, never an error; `new_snapshot` declared but carrying NO
    `workspace` root anywhere IS an error (a module's dist can never be
    derived from a tree with none).

    Exit: 2 for anything malformed (unknown batch, no `frozen_snapshot`, no
    `assignment.yaml`, a bad `by_unit` key, an `applies_to` that disagrees
    with this batch's own `frozen_snapshot`, a broken ledger line, a
    target snapshot with no `workspace` root); 1 when the cross-check
    finds a MISMATCH or a ported-with-no-rule entry (upstream's own
    "stale rules" signal — the request ran fine, the assignment itself
    needs attention); 0 otherwise. Dry-run by default; `--apply` writes.
    """
    root, opts, batch_name = args.ctx.root, args.opts, args.batch_name

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    try:
        batch = port_batch_reader.load_batch(port_root, batch_name)
    except (port_batch_reader.BatchNotFoundError,
            port_batch_reader.BatchConfigError) as e:
        return Verdict.unresolved(e).exit

    if not batch.frozen_snapshot:
        return Verdict.broken(
            "batch %r has no `frozen_snapshot` declared — nothing to "
            "assign against" % batch.name).exit

    try:
        rules = load_assignment_rules(batch.map_dir / _ASSIGNMENT_FILENAME)
    except hs_family.UsageError as e:
        return Verdict.broken(str(e)).exit

    applies_to = rules.get("applies_to")
    if applies_to and applies_to != batch.frozen_snapshot:
        return Verdict.broken(
            "assignment.yaml declares applies_to: %r, but batch %r's own "
            "frozen_snapshot is %r" % (applies_to, batch.name, batch.frozen_snapshot)).exit

    try:
        frozen_commit, idx = port_batch_reader.read_snapshot(port_root, batch.frozen_snapshot)
    except hs_family.UsageError as e:
        return Verdict.broken(str(e)).exit

    unit_errs = _check_unit_rules(rules, sorted(idx.get("units") or {}))
    if unit_errs:
        return Verdict.broken(
            "assignment.yaml is invalid:\n" + "\n".join("  · %s" % e for e in unit_errs)).exit

    for w in _dead_law_warnings(rules, idx):
        print("WARNING: %s" % w)

    # target-tree truth: where a source entry ALREADY sits, if it has
    # landed — a batch that has not built a target snapshot yet has none
    # of that yet either, a real state, not an error (see module docstring).
    new_module_dist: "dict[str, str]" = {}
    if batch.new_snapshot and not port_batch_reader.snapshot_path(port_root, batch.new_snapshot).exists():
        # Declared but not built: the lane builds the target snapshot after
        # `build`, so at assign time nothing has landed to cross-check yet.
        print("new_snapshot %r not built yet — nothing landed to cross-check"
              % batch.new_snapshot)
    elif batch.new_snapshot:
        try:
            _new_commit, new_idx = port_batch_reader.read_snapshot(port_root, batch.new_snapshot)
        except hs_family.UsageError as e:
            return Verdict.broken(str(e)).exit
        try:
            new_module_dist = _target_module_dist(new_idx)
        except _NoWorkspaceRootError as e:
            return Verdict.broken(str(e)).exit

    source, source_problem = port_batch_reader.find_source(batch, None)
    if source_problem:
        return Verdict.broken(source_problem).exit
    # `frozen_commit` was already read above (`read_snapshot`) — check it
    # against the pin here, at the same point this refusal always fired,
    # rather than re-opening the same `_index.json` a second time.
    frozen_commit = port_batch_reader.check_anchor_pinned(
        frozen_commit, batch, source,
        what="frozen snapshot %r's commit" % batch.frozen_snapshot)
    commit_tag = frozen_commit[:8]

    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        return Verdict.broken(
            "NOT WRITTEN — %d error(s):\n" % len(e.findings)
            + "\n".join("  · [%s] %s" % (code, msg) for code, msg in e.findings)).exit
    led_state = ledger.replay()

    new_map: "dict[str, str]" = {}
    counts: "dict[str, int]" = {}
    mismatch: "list[tuple[str, str, str, str]]" = []
    unruled_ported: "list[str]" = []
    matched = 0
    for unit, path, module, name in _assign_entries(idx):
        key = "%s::%s" % (module, name)
        d, why = resolve_dist(path, unit, name, rules)
        newmod = _ported_destination(led_state, commit_tag, module, name)
        truth = new_module_dist.get(newmod) if newmod is not None else None
        if truth is None:
            new_val = d
        elif d == truth:
            matched += 1
            new_val = truth
        elif d == "UNRULED":
            unruled_ported.append(key)
            new_val = truth
        else:
            mismatch.append((key, d, truth, why))
            new_val = truth
        new_map[key] = new_val
        counts[new_val] = counts.get(new_val, 0) + 1

    total = len(new_map)
    ruled = total - counts.get("UNRULED", 0)
    print("batch     : %s" % batch.name)
    print("snapshot  : %s" % batch.frozen_snapshot)
    print("entries   : %d" % total)
    if total:
        print("ruled     : %d  (%.0f%%)" % (ruled, 100.0 * ruled / total))
        print("UNRULED   : %d  (%.0f%%)"
             % (counts.get("UNRULED", 0), 100.0 * counts.get("UNRULED", 0) / total))
    print("matched   : %d  (already sits where the rule agrees)" % matched)
    print("MISMATCH  (rule != where it already sits in the target tree) : %d" % len(mismatch))
    print("UNRULED   (already ported, no rule explains its dist) : %d" % len(unruled_ported))
    for key, said, truth, why in mismatch[:20]:
        print("  %s\n    rule: %s  (%s)\n    tree: %s" % (key, said, why, truth))
    if len(mismatch) > 20:
        print("  … and %d more" % (len(mismatch) - 20))
    for key in unruled_ported[:20]:
        print("  %s  — no rule explains why it sits there" % key)
    if len(unruled_ported) > 20:
        print("  … and %d more" % (len(unruled_ported) - 20))

    stale = bool(mismatch or unruled_ported)
    apply = bool(getattr(opts, "apply", False))
    if not apply:
        print("--dry-run: nothing written")
        return 1 if stale else 0

    out_path = batch.map_dir / port_batch_reader.ASSIGNED_FILENAME
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(new_map, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
        encoding="utf-8")
    print("wrote %d entr%s -> %s" % (total, "y" if total == 1 else "ies", out_path))
    if stale:
        print("RED: the assignment has rotted against where things already "
              "sit — see MISMATCH/UNRULED above")
    return 1 if stale else 0


ASSIGN = Command(add_args=_assign_args, run=_assign)


def _plan_events(*, rows: "list[tuple[str, str]]", spec: port_spec.Spec,
                  led_state: dict, commit_tag: str, new_anchor: "str | None",
                  actor: str, src: dict, dist: str, batch_name: str):
    """Every `Event` a `port-batch` run for `dist` would append, in
    upstream's own write order (rows first, `born_new` after) — ported
    (algorithm-faithful) from `ob-tools@60075cb:packages/portkit/src/
    portkit/batch/port_batch.py:88-166` (`port_batch`'s own body), minus the
    actual write: this only PLANS, `PORT_BATCH` decides whether/how to
    apply it.

    A row already `ported` at the SAME destination is idempotent — skipped
    (`port_batch.py:143`, "idempotent: same line stands"). A `born-new` node
    already carrying ANY line is skipped too — upstream's own `port_batch`
    has no such check (it re-declares every run), but a re-run of THIS
    write door is meant to be safe the same way `init`'s own "nothing to
    declare" contract already is, and a second identical `born-new`
    declaration carries no new information.

    Returns `(events, warnings)` — `warnings` holds the OVERRIDE note
    upstream prints (`port_batch.py:147-150`) when a standing `ported`
    destination disagrees with what this run would write, for the caller to
    surface however its own output route wants (stdout text here; a
    field of the envelope's `data` for a later caller)."""
    events: "list[port_ledger_reader.Event]" = []
    warnings: "list[str]" = []
    for mod, name in rows:
        node_key = "%s::%s::%s" % (commit_tag, mod, name)
        prev = led_state.get(node_key)
        # a dist relanding after an older batch split/dropped its
        # node — raise the intermediate `dropped -> in-progress` step WITH
        # its own reason instead of the whole plan refusing at the first
        # illegal `dropped -> ported` jump.
        if prev is not None and prev.status == "dropped":
            events.append(port_ledger_reader.Event(
                ts=now_ts(), node=node_key, status="in-progress", by="human",
                actor=actor,
                extra={"to": (), "reason":
                       "dist lands again after a split — node was "
                       "dropped in an older batch",
                       "blocked_by": (), "src": src}))
        rn = (spec.renames or {}).get("%s::%s" % (mod, name), name)
        if "::" in str(rn):                    # "module::name" — moves home too
            newmod, newname = str(rn).split("::", 1)
        else:
            newmod, newname = _map_module(mod, spec.module_map), rn
        new_to = "%s::%s::%s" % (new_anchor, newmod, newname)
        # never rewrite a line that already stands, and never
        # override a standing destination in silence.
        if prev is not None and prev.status == "ported":
            prev_to = tuple(prev.extra.get("to") or ())
            if prev_to == (new_to,):
                continue                        # idempotent: same line stands
            if prev_to:
                warnings.append(
                    "%s: batch writes destination %s, OVERRIDING the "
                    "standing line %s — declare the true home in `renames` "
                    "if the standing line was right"
                    % (node_key, new_to, prev_to[0]))
        reason = ("renamed/rehomed per spec"
                  if (newname != name or newmod != _map_module(mod, spec.module_map))
                  else "port %s from %s (batch %s)" % (dist, commit_tag, batch_name))
        events.append(port_ledger_reader.Event(
            ts=now_ts(), node=node_key, status="ported", by="human", actor=actor,
            extra={"to": (new_to,), "reason": reason, "blocked_by": (), "src": src}))
    for b in (spec.born_new or []):
        node_key = "%s::%s::%s" % (new_anchor, b["module"], b["name"])
        if node_key in led_state:
            continue
        events.append(port_ledger_reader.Event(
            ts=now_ts(), node=node_key, status="born-new", by="human", actor=actor,
            extra={"to": (), "reason": b.get("reason", "spec declares born-new"),
                   "blocked_by": (), "src": src}))
    return events, warnings


def _port_batch(args: argparse.Namespace) -> int:
    """`hs-code port port-batch <dist> --batch <name> [--apply]` — every
    ledger line ONE landed dist's port earns: one `ported` line per
    symbol/method the batch's frozen snapshot carries under `dist`
    (`--to` mapped through the spec's `module_map`/`renames`), one
    `born-new` line per `spec.born_new` entry.

    Dry-run by default (a plain listing, see module docstring); `--apply`
    writes for real, sequentially, stopping at the first refusal.
    """
    root, opts = args.ctx.root, args.opts
    dist, batch_name = args.dist, args.batch_name

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    try:
        batch = port_batch_reader.load_batch(port_root, batch_name)
    except (port_batch_reader.BatchNotFoundError,
            port_batch_reader.BatchConfigError) as e:
        return Verdict.unresolved(e).exit

    if not batch.frozen_snapshot:
        return Verdict.broken(
            "batch %r has no `frozen_snapshot` declared — nothing to read "
            "%r's symbols from" % (batch.name, dist)).exit

    spec_path = batch.specs_dir / ("%s.yaml" % dist)
    if not spec_path.is_file():
        return Verdict.broken("no spec at %s" % spec_path).exit
    try:
        spec = port_spec.Spec.load(spec_path)
    except port_spec.SpecError as e:
        return Verdict.broken(str(e)).exit

    if not spec.module_map and not spec.placeholder:
        # empty map is only legal for a PLACEHOLDER dist — every other dist
        # is the typo-catch it always was (`port_batch.py:97-100`).
        return Verdict.broken(
            "%s has an empty module_map — nothing to map (declare "
            "`placeholder: true` if %r truly maps nothing)"
            % (spec_path, dist)).exit

    # `find_source` runs BEFORE `frozen_commit` is resolved: `pinned_frozen_
    # snapshot` below needs `source` to check the frozen snapshot's own
    # commit against the batch's pin BEFORE any listing/dry-run output — a
    # mismatch refuses outright, writes nothing, ever.
    source, source_problem = port_batch_reader.find_source(batch, None)
    if source_problem:
        return Verdict.broken(source_problem).exit

    frozen_commit, idx = port_batch_reader.pinned_frozen_snapshot(port_root, batch, source)
    commit_tag = frozen_commit[:8]
    src = {"id": source.get("id"), "sha": frozen_commit}
    # Every OLD-tree key this dist's own snapshot still carries, at this
    # run's anchor — the ghost check's own baseline (see below); reused, not
    # copied, from `codegraph_port_ledger_verbs` (`INIT`'s own door onto
    # the same snapshot shape).
    old_keys = _snapshot_node_keys(idx, commit_tag)

    try:
        assigned = port_batch_reader.read_assigned(batch)
    except (OSError, ValueError) as e:
        return Verdict.broken(str(e)).exit
    rows = _rows_for(idx, dist, assigned)
    if not rows and not spec.born_new and not spec.placeholder:
        return Verdict.broken(
            "no symbol of %r in snapshot %r, no born_new and no placeholder "
            "flag — one of the three is a spec typo" % (dist, batch.frozen_snapshot)).exit

    new_anchor = None
    if rows or spec.born_new:
        try:
            new_anchor = port_batch_reader.new_batch_anchor(port_root, batch)
        except hs_family.RefusedError as e:
            return Verdict.broken(str(e)).exit

    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        return Verdict.broken(
            "NOT WRITTEN — %d error(s):\n" % len(e.findings)
            + "\n".join("  · [%s] %s" % (code, msg) for code, msg in e.findings)).exit

    led_state = ledger.replay()
    actor = resolve_actor()
    events, warnings = _plan_events(
        rows=rows, spec=spec, led_state=led_state, commit_tag=commit_tag,
        new_anchor=new_anchor, actor=actor, src=src, dist=dist, batch_name=batch.name)

    if not events:
        print("nothing to write — %s already reflects dist %r" % (ledger.path, dist))
        return int(Verdict.ok().exit)

    for w in warnings:
        print("⚠ %s" % w)

    apply = bool(getattr(opts, "apply", False))
    write_all(ledger, events, "port-batch", apply=False)
    if not apply:
        print("%d line(s) planned for dist %r (--apply to append):" % (len(events), dist))
        for ev in events[:20]:
            print("  %s -> %s" % (ev.node, ev.status))
        if len(events) > 20:
            print("  … %d more" % (len(events) - 20))
        return int(Verdict.ok().exit)

    write_all(ledger, events, "port-batch", apply=True)
    print("wrote %d line(s) -> %s" % (len(events), ledger.path))

    # ghost check: old-shape ledger keys neither the snapshot nor a valid
    # born-new target claims — reported, never overwritten. Ported
    # (algorithm-faithful) from `ob-tools@60075cb:packages/portkit/src/
    # portkit/batch/port_batch.py:174-179`. Printed AFTER writing, matching
    # upstream (which has no dry-run path to compute it against at all).
    state = ledger.replay()
    ghosts = sorted(k for k in state if not k.startswith("new@") and k not in old_keys)
    if ghosts:
        print("⚠ ghost keys already in the ledger (read the ghost-keys "
              "agent note before touching them): %r" % ghosts)
    return int(Verdict.ok().exit)


PORT_BATCH = Command(add_args=_port_batch_args, run=_port_batch)


# ── the `port` gate-registry page (harness/data/gate-registry.d/port.yaml) ─

def port_gate_rows(root: Path) -> "dict[str, dict]":
    """`{"map:<batch>": {"batch": <batch>, "kind": "map"}, "ledger:<batch>":
    {"batch": <batch>, "kind": "ledger"}}` for every batch this port root
    declares — TWO rows per batch — plus ONE batch-independent `control`
    row the moment the repo holds a `.port/` workspace at all (even with
    zero batches): the seeded positive-control cases, the `source` door
    `harness/data/gate-registry.d/port.yaml` names. A repo with no `.port/`
    workspace contributes no rows — never an error (`hs_family.
    gate_registry`'s own `_call_source` treats a source raising as a broken
    PAGE, which is the wrong shape for "this repo just has no batches
    yet"). Each field dict carries only the batch NAME (plus which kind
    names this row); `port_gate_run` re-resolves everything else off it at
    run time, one door, same as every other verb here."""
    try:
        port_root = port_workspace.find_port_root(Path(root))
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError):
        return {}
    rows: "dict[str, dict]" = {"control": {"kind": "control"}}
    for name in port_batch_reader.list_batches(port_root):
        rows["map:%s" % name] = {"batch": name, "kind": "map"}
        rows["ledger:%s" % name] = {"batch": name, "kind": "ledger"}
    return rows


def port_gate_run(target: Path, rows, pin=None) -> "list":
    """One `map` rule-kind check per row, `hs_family.gate_registry`'s own
    `run` door for the `port` page. Mirrors `codegraph.check.gates.
    run_code`'s own RowResult shape (pass/fail/broken) — this is a
    SEPARATE runner, not a `codemap.yaml`-declared rule, because a port
    batch's check needs TWO snapshots at once (the frozen SOURCE, for the
    dist law's own module set, and the target tree's own `new_snapshot`,
    for name/hash agreement) — `run_code`'s own descriptor path only ever
    carries one.

    `ctx.idx`/`ctx.repo` are the batch's own `new_snapshot` and the port
    root itself — the tree that snapshot was actually taken of (a snapshot
    is only ever compared with the tree it was taken of, `map.py`'s own
    module docstring); `ctx.snapshots` carries the frozen SOURCE snapshot,
    keyed by its own name, for the `applies_to` dist-law module set. A
    batch missing `frozen_snapshot`/`new_snapshot`/`dists` is `broken`,
    naming the missing key — the map rule's dist law cannot run without
    all three, and a silent skip would read as a green nobody ran.
    `pin` is accepted (the `run_code` calling convention probes for it)
    but unused — a port batch names its own two snapshots by NAME, never
    a `--snap` override of the live tree."""
    from codegraph._core.rules import RuleContext, RulesEngine, RulesError
    from codegraph.check.gates import run_result
    root = Path(target)
    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return [run_result(r, "broken", str(e)) for r in rows]

    out = []
    for r in rows:
        name = r.fields.get("batch")
        if not name and r.fields.get("kind") != "control":
            out.append(run_result(r, "broken", "row %r carries no `batch` field" % r.name))
            continue
        kind = r.fields.get("kind", "map")

        if kind == "control":
            # the seeded positive-control cases: the BIN's own detector,
            # measured against shipped fixtures, never this target's tree.
            import port_control_cases

            t0 = time.monotonic()
            try:
                results = port_control_cases.run_control_row(root)
            except Exception as e:  # noqa: BLE001 — a gate reports, never crashes the book
                out.append(run_result(r, "broken", "control cases could not run: %s" % e))
                continue
            misses = [x for x in results if not x["caught"]]
            secs = round(time.monotonic() - t0, 2)
            if misses:
                out.append(run_result(
                    r, "fail", "%d/%d case(s) NOT caught in %ss: %s"
                    % (len(misses), len(results), secs,
                       "; ".join("%s expected %s, %s" % (m["case"], m["expected"], m["detail"])
                                 for m in misses))))
            else:
                out.append(run_result(
                    r, "pass", "%d/%d seeded case(s) caught in %ss"
                    % (len(results), len(results), secs)))
            continue

        if kind == "ledger":
            # a SEPARATE, much lighter path: `LedgerCheck` resolves the
            # batch/source/ledger itself off `params["batch"]` (see its own
            # module docstring for why it reads no `ctx.idx`/`ctx.
            # snapshots`) — no frozen/new/dists precondition belongs here,
            # those are `map`'s own requirements, not a ledger scan's.
            try:
                engine = RulesEngine([{"id": r.name, "kind": "ledger", "batch": name}])
                res = engine.run(RuleContext(repo=port_root))[0]
            except RulesError as e:
                out.append(run_result(r, "broken", str(e)))
                continue
            if res.is_red:
                v = res.violations[0]
                out.append(run_result(r, "fail", "%d violation(s), first %s  %s"
                                      % (len(res.violations), v.cause, v.label)))
            else:
                out.append(run_result(r, "pass", "; ".join(res.notes[:1])))
            continue

        try:
            batch = port_batch_reader.load_batch(port_root, name)
        except (port_batch_reader.BatchNotFoundError,
                port_batch_reader.BatchConfigError) as e:
            out.append(run_result(r, "broken", str(e)))
            continue
        if not batch.frozen_snapshot:
            out.append(run_result(
                r, "broken", "batch %r has no `frozen_snapshot` declared" % name))
            continue
        if not batch.new_snapshot:
            out.append(run_result(
                r, "broken", "batch %r has no `new_snapshot` declared — "
                "nothing to check the target tree against yet" % name))
            continue
        if not batch.dists:
            out.append(run_result(
                r, "broken", "batch %r declares no `dists:` — the map "
                "rule's dist law needs a list to check names against" % name))
            continue
        source, source_problem = port_batch_reader.find_source(batch, None)
        if source_problem:
            out.append(run_result(r, "broken", str(source_problem)))
            continue
        try:
            _new_commit, new_idx = port_batch_reader.read_snapshot(port_root, batch.new_snapshot)
        except hs_family.UsageError as e:
            out.append(run_result(r, "broken", str(e)))
            continue
        try:
            frozen_commit, frozen_idx = port_batch_reader.pinned_frozen_snapshot(
                port_root, batch, source)
        except hs_family.UsageError as e:
            out.append(run_result(r, "broken", str(e)))
            continue
        except hs_family.RefusedError as e:
            out.append(run_result(r, "fail", str(e)))
            continue
        try:
            engine = RulesEngine([{
                "id": r.name, "kind": "map", "roots": ["."],
                "applies_to": batch.frozen_snapshot,
                "dists": list(batch.dists),
                "assigned": str(batch.map_dir / port_batch_reader.ASSIGNED_FILENAME),
                "frozen_commit": frozen_commit,
            }])
            ctx = RuleContext(repo=port_root, idx=new_idx,
                              snapshots={batch.frozen_snapshot: frozen_idx})
            res = engine.run(ctx)[0]
        except RulesError as e:
            out.append(run_result(r, "broken", str(e)))
            continue
        if res.is_red:
            v = res.violations[0]
            out.append(run_result(r, "fail", "%d violation(s), first %s  %s"
                                  % (len(res.violations), v.cause, v.label)))
        else:
            seen = ["%d warned" % len(res.violations)] if res.violations else []
            out.append(run_result(r, "pass", "; ".join(seen + list(res.notes[:1]))))
    return out
