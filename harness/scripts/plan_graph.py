#!/usr/bin/env python3
"""plan_graph.py — read a plan's machine-readable phase-DAG sidecar and derive
advisory findings (cycles, ordering hazards, parallel batches + file conflicts).

Single responsibility: parse the sidecar + delegate cycle detection + derive read-only
findings. It NEVER edits the plan — option D / red line #4: detection only, no AI auto-fix
after approval. The sidecar `plans/<slug>/plan-graph.yaml` carries ONLY edges + per-phase
file ownership, never status (status lives mutable in plan.md's `## Phases`).

Edge direction is pinned: ``{from: A, to: B}`` means "A runs BEFORE B" (A is B's
prerequisite). build_adj maps that semantics explicitly rather than reusing graph_core's
children_of/parents_of, which read the opposite way.

Parallelism is DERIVED, never authored: a parallel batch is a topological level (an
antichain — phases with no edge between them). But an edge alone lies about parallelism in
a file-based harness: the real blocker is usually a shared file, not a logical dependency.
So a safe parallel batch is an antichain AND a pairwise-disjoint file set; find_parallel_
conflicts turns the prose "shared-file" warning into a machine finding.

Each node MUST declare a `post:` artifact obligation. A missing/malformed
`post` is a structural contract violation, not a heuristic advisory: find_missing_post +
the CLI hard-fail (exit 2, both modes) refuse it at validate + cook-preflight time. The
node_artifacts default is only a runtime backstop for an already-approved graph.
"""
import re
import sys
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve()
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))
import codegraph_change_reader  # noqa: E402 — the one door for the
                                 # snapshot's own "pkg.mod::name" import-target
                                 # convention; see file_import_edge below
import graph_core  # noqa: E402
import harness_paths  # noqa: E402 — root_for_plan_dir's ambient-project fallback
import scope_match  # noqa: E402 — the one path-glob matcher; a files_to_*
                     # entry is a glob (scope_match semantics), not a literal
                     # string

_SIDECAR = "plan-graph.yaml"


def root_for_plan_dir(plan_dir) -> Path:
    """The tree `plan_dir` itself lives in — the one its relative
    declarations (`files_to_*`, and now the glob/glob conflict grounding's
    `git ls-files` universe) are written against.

    Moved here from `graph_test_scope.py` (which now calls this instead of
    keeping its own copy) because `find_parallel_conflicts`'s CLI entry point
    (`_main`, below) needs the exact same derivation and this module cannot
    import that one (`graph_test_scope` already imports `plan_graph`).

    Declared paths in a sidecar are relative to the project that OWNS the
    plan, so a disk cross-check has to ask that project. Asking the ambient
    root instead (env/CWD-derived) answers correctly about a different tree:
    measured under a plan outside the ambient project, a declared test
    sitting next to the plan read `declared_absent`, and a same-named file in
    the ambient tree read `resolved`. Both are wrong about the plan in hand.

    Derived structurally: `<root>/plans/<plan>/` is the layout every plan dir
    has, so the grandparent of a dir under `plans/` is the root. A plan dir
    not laid out that way falls back to the ambient project root, which is
    the behavior every caller had before and stays correct under self-host.
    """
    try:
        resolved = Path(plan_dir).resolve(strict=False)
    except OSError:
        return harness_paths.project_root().resolve()
    for parent in resolved.parents:
        if parent.name == "plans":
            return parent.parent
    return harness_paths.project_root().resolve()


def parse_phase_graph(plan_dir) -> dict:
    """Load the sidecar. Returns the parsed mapping, or ``{"error": <msg>}`` on a
    malformed file — never raises (a planner-facing finding, not a crash)."""
    import yaml_io
    path = Path(plan_dir) / _SIDECAR
    if not path.is_file():
        return {"error": "no %s in %s" % (_SIDECAR, plan_dir)}
    try:
        data = yaml_io.safe_load(path.read_text(encoding="utf-8"))
    except yaml_io.YAMLError as exc:
        return {"error": "malformed %s: %s" % (_SIDECAR, exc)}
    if not isinstance(data, dict):
        return {"error": "%s is not a mapping" % _SIDECAR}
    # Normalize EMPTY, reject WRONG-TYPED — two different inputs that used to collapse
    # into the same answer.
    #
    # Absent, or present with an empty value (`edges:` on its own), parses to None. That
    # is a legitimately empty sidecar — a single-phase plan has no edges — so it is
    # coerced and callers always see list/dict. setdefault would not do it: the key is
    # present, its value is just None.
    #
    # A wrong-typed value (`subtasks:` authored as a LIST, a scalar, …) is corruption and
    # must surface as a finding. It used to be coerced too, and the cost was not
    # cosmetic: with the graph silently discarded, `--require` printed
    # "parallel batches: []" and exited 0, so the cook preflight accepted a plan whose
    # phase graph it had thrown away — while the SAME corruption authored in mapping form
    # exited 2 via the missing-post check. The gate's verdict depended on which wrong
    # shape the author happened to write.
    #
    # Returned as {"error": ...} rather than raised, matching this function's contract
    # (a planner-facing finding, never a crash). Advisory mode still exits 0 on it;
    # --require is the door that says no. Do NOT "simplify" this back to a truthiness or
    # isinstance-else-default check — that erases the distinction again. See
    # test_plan_graph.py, the empty-vs-wrong-typed block.
    for key, want, want_label in (("edges", list, "a list"),
                                  ("subtasks", dict, "a mapping")):
        val = data.get(key)
        if val is None:
            data[key] = want()
        elif not isinstance(val, want):
            return {"error": "%s: `%s:` must be %s, got %s"
                             % (_SIDECAR, key, want_label, type(val).__name__)}
    # Node ids are text: a `--phase` token and a snapshot filename are, and an
    # unquoted `1:` key loads as an int neither could ever name.
    data["subtasks"] = {str(k): v for k, v in data["subtasks"].items()}
    for e in data["edges"]:
        if isinstance(e, dict):
            for end in ("from", "to"):
                if e.get(end) is not None:
                    e[end] = str(e[end])
    for key in (_CROSS, _WAITS):
        if key in data:
            return {"error": "%s: `%s:` is reserved — a cross-plan edge is written in "
                             "`edges:` as {from: <plan>/<phase>, to: <phase>}" % (_SIDECAR, key)}
    return _split_cross_plan(data)


# A phase of a SIBLING plan named in `edges:` — `{from: <plan dir name>/<phase>, to:
# <phase>}`: that phase must pass before this plan's `to` may start. Only the plan
# that waits writes it, so each such edge lives in one file. Read apart from the
# plan's own edges at parse time: every in-plan algorithm (cycles, batches, clusters,
# conflicts) sees this plan's phases only, and the hold is applied by the caller that
# can read the other plan's progress (`with_waits`).
_CROSS = "_cross_plan"
# {phase: [<plan>/<phase>, ...]} — the cross-plan prerequisites not yet passed, set
# by `with_waits`. `phase_holds` reads it, so every door that asks what is runnable
# (the batch, `cook next`, ready-alongside) holds a waiting phase the same way.
_WAITS = "_waits"


# `waived: {decision, reason}` on a cross-plan edge: a ruling that the edge is met
# before the other plan's phase passed. The phase is handed out; the other phase is
# still owed by its own plan.
_WAIVER_KEYS = ("decision", "reason")


def _waiver_problem(src: str, dst: str, waived) -> "str | None":
    if (isinstance(waived, dict) and set(waived) == set(_WAIVER_KEYS)
            and all(isinstance(waived[k], str) and waived[k].strip() for k in _WAIVER_KEYS)):
        return None
    return ("%s: edge %s -> %s: `waived:` is {decision: <the ruling's id>, reason: <why "
            "the edge is met>}, both non-empty, nothing else; got %r" % (_SIDECAR, src, dst, waived))


def _split_cross_plan(data: dict) -> dict:
    local: list = []
    cross: list = []
    for e in data["edges"]:
        src = e.get("from") if isinstance(e, dict) else None
        dst = e.get("to") if isinstance(e, dict) else None
        if isinstance(e, dict) and "waived" in e and not (src is not None and "/" in src):
            return {"error": "%s: edge %s -> %s: `waived:` is written on a cross-plan edge "
                             "only ({from: <plan>/<phase>, ...}); an edge inside this plan "
                             "is met when its phase passes" % (_SIDECAR, src, dst)}
        if dst is not None and "/" in dst:
            return {"error": "%s: edge to %r names another plan's phase — a cross-plan "
                             "edge is written in the plan that waits, as {from: <this "
                             "plan>/<phase>, to: <its phase>}" % (_SIDECAR, dst)}
        if src is not None and "/" in src:
            if dst is None:
                return {"error": "%s: cross-plan edge from %r has no `to:`" % (_SIDECAR, src)}
            plan, _, phase = src.rpartition("/")
            edge = {"plan": plan, "phase": phase, "to": dst}
            if any(c["plan"] == plan and c["phase"] == phase and c["to"] == dst for c in cross):
                return {"error": "%s: cross-plan edge %s -> %s is written twice -- once, with "
                                 "its `waived:` if it has one" % (_SIDECAR, src, dst)}
            if "waived" in e:
                problem = _waiver_problem(src, dst, e["waived"])
                if problem:
                    return {"error": problem}
                edge["waived"] = {k: e["waived"][k].strip() for k in _WAIVER_KEYS}
            cross.append(edge)
            continue
        local.append(e)
    data["edges"] = local
    data[_CROSS] = cross
    return data


def cross_plan_edges(graph: dict) -> list:
    """[{plan, phase, to}] — this plan's phases that wait on a sibling plan's phase."""
    return list(graph.get(_CROSS) or [])


def _sibling(plan_dir, plan: str) -> "Path | None":
    """The sibling plan dir `plan` names, or None when it is not a plain relative
    name under this plan's own plans dir."""
    p = Path(plan)
    if not plan or p.is_absolute() or ".." in p.parts or "\\" in plan:
        return None
    return Path(plan_dir).parent / p


def find_cross_plan_problems(graph: dict, plan_dir) -> list:
    """One sentence per cross-plan edge that cannot hold: a plan that is not there, a
    phase it does not have, a `to` this plan does not have, the plan itself, and any
    cycle the edges close through other plans."""
    out = []
    here = Path(plan_dir).resolve()
    own = _all_nodes(graph)
    for e in cross_plan_edges(graph):
        name = "%s/%s" % (e["plan"], e["phase"])
        d = _sibling(plan_dir, e["plan"])
        if d is None:
            out.append("cross-plan edge %s -> %s: %r is not a plan dir name beside this "
                       "plan" % (name, e["to"], e["plan"]))
            continue
        if e["to"] not in own:
            out.append("cross-plan edge %s -> %s: %s is not a phase of this plan"
                       % (name, e["to"], e["to"]))
        if d.resolve() == here:
            out.append("cross-plan edge %s -> %s names this plan itself — write it as "
                       "{from: %s, to: %s}" % (name, e["to"], e["phase"], e["to"]))
            continue
        if not (d / "plan.md").is_file():
            out.append("cross-plan edge %s -> %s names no plan under %s"
                       % (name, e["to"], Path(plan_dir).parent))
            continue
        other = parse_phase_graph(d)
        if other.get("error"):
            out.append("cross-plan edge %s -> %s: %s's graph cannot be read (%s)"
                       % (name, e["to"], e["plan"], other["error"]))
        elif e["phase"] not in _all_nodes(other):
            out.append("cross-plan edge %s -> %s: %s is not a phase of %s (known: %s)"
                       % (name, e["to"], e["phase"], e["plan"], ", ".join(
                           sorted(_all_nodes(other), key=phase_sort_key)) or "none"))
    for cycle in _cross_plan_cycles(graph, plan_dir):
        out.append("cross-plan cycle: %s — no phase on it can ever start" % " -> ".join(cycle))
    return out


def _cross_plan_cycles(graph: dict, plan_dir) -> list:
    """Cycles through two plans or more in the union of this plan and every plan its
    cross-plan edges reach, nodes spelled `<plan>/<phase>`."""
    base = Path(plan_dir).parent
    start = Path(plan_dir).name
    adj: dict = {}
    seen, todo = set(), [(start, graph)]
    while todo:
        name, g = todo.pop()
        if name in seen or g.get("error"):
            continue
        seen.add(name)
        for n, dsts in build_adj(g).items():
            adj.setdefault("%s/%s" % (name, n), []).extend("%s/%s" % (name, d) for d in dsts)
        for e in cross_plan_edges(g):
            d = _sibling(base / name, e["plan"])
            if e.get("waived") or d is None or not (d / "plan.md").is_file():
                continue   # a waived edge holds nothing, so it closes no cycle
            other = d.relative_to(base).as_posix()
            adj.setdefault("%s/%s" % (other, e["phase"]), []).append("%s/%s" % (name, e["to"]))
            adj.setdefault("%s/%s" % (name, e["to"]), [])
            if other not in seen:
                todo.append((other, parse_phase_graph(d)))
    # A cycle inside one plan is `find_cycles`' finding there; only one that runs
    # through two plans or more is this check's.
    return [c for c in graph_core.find_dep_cycles(adj)
            if len({n.rpartition("/")[0] for n in c}) > 1]


def unmet_cross_plan(graph: dict, plan_dir, passed_of) -> dict:
    """{phase: [<plan>/<phase>, ...]} for each cross-plan prerequisite not passed.
    `passed_of(plan_dir)` is the other plan's passed-phase set; a plan that is not
    there has passed nothing, so its edge holds rather than lapses."""
    out: dict = {}
    for e in cross_plan_edges(graph):
        if e.get("waived"):
            continue
        d = _sibling(plan_dir, e["plan"])
        passed = passed_of(d) if d is not None and (d / "plan.md").is_file() else set()
        if e["phase"] not in passed:
            out.setdefault(e["to"], []).append("%s/%s" % (e["plan"], e["phase"]))
    return out


def waived_open(graph: dict, plan_dir, passed_of) -> list:
    """One sentence per waived cross-plan edge whose other phase has not passed: the
    waiting phase runs on a ruling, and the phase it needed is still owed."""
    out = []
    for e in cross_plan_edges(graph):
        w = e.get("waived")
        if not w:
            continue
        d = _sibling(plan_dir, e["plan"])
        if d is None or not (d / "plan.md").is_file():
            continue   # no plan owes it: `find_cross_plan_problems` names the edge
        if e["phase"] not in passed_of(d):
            out.append("cross-plan edge %s/%s -> %s is waived by %s (%s); %s/%s has not passed "
                       "and is still owed by its own plan"
                       % (e["plan"], e["phase"], e["to"], w["decision"], w["reason"], e["plan"], e["phase"]))
    return out


def with_waits(graph: dict, waits: dict) -> dict:
    """A copy of `graph` holding each phase of `waits` until its prerequisites pass."""
    out = dict(graph)
    out[_WAITS] = {k: list(v) for k, v in (waits or {}).items()}
    return out


def waits_on(graph: dict, phase: str) -> list:
    """The cross-plan prerequisites holding `phase`: its own, and those of every
    phase upstream of it in this plan; [] when none holds it."""
    waits = graph.get(_WAITS) or {}
    adj = build_adj(graph)
    held = [w for p in sorted(waits, key=phase_sort_key)
            if p == phase or phase in graph_core._closure(adj, p) for w in waits[p]]
    return sorted(set(held))


# ---------------------------------------------------------------- adjacency ---

def _all_nodes(graph: dict) -> set:
    nodes = set(graph.get("subtasks", {}) or {})
    for e in graph.get("edges", []) or []:
        nodes.add(e.get("from"))
        nodes.add(e.get("to"))
    nodes.discard(None)
    return nodes


# ASCII: under Unicode IGNORECASE `ſ` and the Kelvin sign spell `phase` and `k`.
_ORDINAL_ID = re.compile(r"(?:(?:p|phase)[-_]?)?([0-9]{1,9})", re.IGNORECASE | re.ASCII)
# A node naming its phase after the number, spelled out: `phase-<n>-<slug>`, the slug
# opening on a letter. `p<n>-<slug>` reads as a priority label (`p1-critical-bugs`) and
# `<n>-<slug>` as prose (`2-phase-commit`), so neither carries a phase number.
_SLUGGED_NODE = re.compile(r"[Pp][Hh][Aa][Ss][Ee][-_]?([0-9]{1,9})[-_][^\W\d_][\w-]*")


def _ordinal(phase_id: str, pattern: "re.Pattern[str]" = _ORDINAL_ID) -> "int | None":
    """The phase number `phase_id` carries in `pattern`'s shape — by default the bare
    ordinal (`P<n>`, `phase-0<n>`, `<n>`) — else None: `api-v1`/`oauth2` end in a digit
    without being the first phase."""
    m = pattern.fullmatch(phase_id)
    return int(m.group(1)) if m else None


_DIGITS = re.compile(r"([0-9]+)")


def phase_sort_key(name) -> tuple:
    """Order phase ids (and phase file names) by the numbers in them: 2 before 10,
    `phase-9-x` before `phase-10-y`. re.split alternates text and digit runs, so each
    position compares like with like; the text itself breaks a tie between 2 and 02."""
    text = str(name)
    parts = _DIGITS.split(text)
    return tuple(int(p) if i % 2 else p for i, p in enumerate(parts)), text


def resolve_phase_id(graph: dict, token: str) -> "tuple[str | None, list]":
    """The node a `--phase` token names: `(token, [])` when it IS a node, else
    `(node, [node])` when exactly one node is another spelling of it, else
    `(None, candidates)`. Spellings are tried in tiers, the first non-empty one
    deciding: its casing (`p<n>` -> `P<n>`), then the same number in a bare ordinal
    id (`<n>`, zero-padded, `phase-<n>` -> `P<n>`), then in a `phase-<n>-<slug>` id.
    Two candidates in a tier name nothing: a guess between them is the wrong-node
    write this id exists to prevent."""
    nodes = _all_nodes(graph)
    if token in nodes:
        return token, []
    folded, number = token.casefold(), _ordinal(token)
    tiers = [[n for n in nodes if n.casefold() == folded]]
    if number is not None:
        tiers += [[n for n in nodes if _ordinal(n) == number],
                  [n for n in nodes if _ordinal(n, _SLUGGED_NODE) == number]]
    for tier in tiers:
        if tier:
            cands = sorted(tier, key=phase_sort_key)
            return (cands[0] if len(cands) == 1 else None), cands
    return None, []


def phase_spelling_hint(graph: dict, token: str) -> str:
    """The clause a refusal of `token` appends: the node it most likely meant, or
    an exact id to copy. Empty when the graph has no node at all."""
    _node, cands = resolve_phase_id(graph, token)
    if len(cands) == 1:
        return " — did you mean --phase %s?" % cands[0]
    if cands:
        return " — %r could name %s; pass one exactly, e.g. --phase %s" % (
            token, " or ".join(cands), cands[0])
    nodes = sorted(_all_nodes(graph), key=phase_sort_key)
    return " — pass one exactly, e.g. --phase %s" % nodes[0] if nodes else ""


def build_adj(graph: dict) -> dict:
    """edges → adjacency with the plan's own direction: {from: [to, ...]}.
    Every node is a key (targets get an empty list) so cycle detection sees them."""
    adj = {n: [] for n in _all_nodes(graph)}  # every node is a key (incl. pure targets)
    for e in graph.get("edges", []) or []:
        src, dst = e.get("from"), e.get("to")
        if src is not None and dst is not None:
            adj[src].append(dst)  # src is already a key via _all_nodes
    return adj


def find_cycles(graph: dict) -> list:
    return graph_core.find_dep_cycles(build_adj(graph))


# ------------------------------------------------------------ file ownership ---

def _subtask(graph: dict, phase: str) -> dict:
    """The subtask mapping for a phase, coerced to a dict — a non-dict / scalar value
    from a hand-edited sidecar yields {} instead of raising AttributeError downstream
    (keeps the 'never raises' contract)."""
    st = (graph.get("subtasks", {}) or {}).get(phase)
    return st if isinstance(st, dict) else {}


def _files(graph: dict, phase: str, *keys) -> set:
    """The file paths a phase declares under `keys`, coerced the same way
    `_subtask` coerces a non-dict subtask — a hand-edited sidecar is the input
    here, and this module's contract is never to raise.

    The value must be a SEQUENCE of strings. Written as a bare scalar
    (`files_to_modify: harness/tests/test_x.py`, no list dash) `set()` iterated
    it character by character, and two phases sharing that path reported
    conflicts on the letters 'x' and 'y' — the operator is sent to serialize
    phases over a collision that does not exist, which is worse than silence.
    A non-string scalar raised TypeError straight through the contract."""
    st = _subtask(graph, phase)
    out = set()
    for k in keys:
        val = st.get(k)
        if isinstance(val, (list, tuple, set)):
            out |= {v for v in val if isinstance(v, str)}
    return out


def _owned(graph: dict, phase: str) -> set:
    return _files(graph, phase, *_OWNERSHIP_KEYS)


# The keys anything actually READS out of the sidecar. Kept next to the readers so
# adding a key without adding it here is a visible edit, not a silent one — the
# unread-key lint below is only as honest as this list.
_OWNERSHIP_KEYS = ("files_to_create", "files_to_modify", "files_to_delete")
_SUBTASK_KEYS = frozenset(_OWNERSHIP_KEYS) | {
    "file",           # the node's phase document (phase_file / find_phase_file_problems)
    "post",           # artifact obligation (node_artifacts)
    "status",         # not read here, but it has its own dedicated warning below
    "paused",         # phase_holds -> paused out of the batch, denominator, sweep;
                      # everything downstream of it blocked (out of the batch only)
    "paused_reason",  # printed by the lint; a pause without one is a finding
    "reopen_when",    # printed alongside the reason, purely for the reader
    "resources",      # shared non-file resources (a DB schema, an outside ledger) —
                      # find_parallel_conflicts treats two unordered users like one file
}


def is_paused(graph: dict, phase: str) -> bool:
    """True when a phase is deliberately on hold. The flag alone; what a pause
    does to the counters is `phase_holds`'s answer.

    Unknown phase -> False. Answering "paused" about a phase there is no record of
    would silently drop a real one out of every count.
    """
    return bool(_subtask(graph, phase).get("paused"))


def paused_phases(graph: dict) -> set:
    return {p for p in _all_nodes(graph) if is_paused(graph, p)}


def phase_holds(graph: dict) -> dict:
    """``{"paused": {phase}, "blocked": {phase: note}}`` — the ONE answer the
    parallel batch, the completion denominator and the stage gate's snapshot sweep
    all read, so the three cannot drift into three different answers the way they
    did when nothing read the flag.

    An edge means "needs". A paused phase will not produce what its dependents
    need, so every phase reachable downstream of one is BLOCKED: it cannot be
    handed out as runnable work. The two holds differ on purpose:

      paused  — a deliberate deferral. Out of the batch, the denominator and the
                sweep (the plan is not held open for work its author parked).
      blocked — nobody deferred it; it simply cannot start. Out of the batch
                ONLY: it is still real unfinished work, so the plan cannot read as
                done and the gate still owes its snapshot while one remains.

    A phase that does not need the paused one should not carry the edge; there is
    no opt-out key. The note names every paused ancestor with its reason, so the
    reader knows which pause to lift.
    """
    blocked = _paused_ancestors(graph)
    paused = paused_phases(graph)
    pieces: dict = {}
    for d, ancestors in blocked.items():
        pieces[d] = ["paused %s: %s" % (p, _pause_reason(graph, p))
                     for p in sorted(ancestors, key=phase_sort_key)]
    # A phase waiting on a sibling plan cannot start either, nor can anything after
    # it here: blocked, never paused — it is real work the plan still owes.
    for d in sorted(_all_nodes(graph) - paused, key=phase_sort_key):
        pending = waits_on(graph, d)
        if pending:
            pieces.setdefault(d, []).extend("%s to pass" % w for w in pending)
    notes = {d: "%s waits on %s" % (d, "; ".join(p)) for d, p in pieces.items()}
    return {"paused": paused, "blocked": notes}


def _paused_ancestors(graph: dict) -> dict:
    """{phase: [paused phases upstream of it]} for every phase a pause blocks."""
    paused = paused_phases(graph)
    adj = build_adj(graph)
    blocked: dict = {}
    for p in sorted(paused, key=phase_sort_key):
        for d in graph_core._closure(adj, p):
            if d not in paused:
                blocked.setdefault(d, []).append(p)
    return blocked


def held_by(graph: dict, phase: str) -> list:
    """The pauses holding `phase`: itself when paused, else every paused phase
    upstream of it; [] when nothing holds it."""
    if is_paused(graph, phase):
        return [phase]
    return sorted(_paused_ancestors(graph).get(phase, []), key=phase_sort_key)


def pause_fields(graph: dict, phase: str) -> dict:
    """`paused_reason` and `reopen_when` as the plan-graph declares them ('' when absent)."""
    st = _subtask(graph, phase)
    return {"paused_reason": str(st.get("paused_reason") or "").strip(),
            "reopen_when": str(st.get("reopen_when") or "").strip()}


def _pause_reason(graph: dict, phase: str) -> str:
    return str(_subtask(graph, phase).get("paused_reason") or "").strip() or "no reason given"


def pause_note(graph: dict, phase: str) -> str:
    """One human sentence for a paused phase, for whoever reads a count that got
    smaller. Empty when the phase is not paused."""
    st = _subtask(graph, phase)
    if not st.get("paused"):
        return ""
    note = "%s is paused (%s)" % (phase, _pause_reason(graph, phase))
    reopen = str(st.get("reopen_when") or "").strip()
    if reopen:
        note += "; reopens when: %s" % reopen
    return note


# ------------------------------------------------------------ phase document ---

def _phase_file_problem(value, plan_dir) -> "tuple[Path | None, str | None]":
    """(resolved path, None) for a usable `file:` value, else (None, why)."""
    if not isinstance(value, str) or not value.strip():
        return None, "`file:` must be a non-empty string, got %s" % type(value).__name__
    if Path(value).is_absolute():
        return None, "`file: %s` must be relative to the plan dir" % value
    base = Path(plan_dir).resolve()
    target = (base / value).resolve()
    if not target.is_relative_to(base):
        return None, "`file: %s` resolves outside the plan dir (%s)" % (value, base)
    if not target.is_file():
        return None, "`file: %s` does not exist under %s" % (value, base)
    return target, None


def phase_file(graph: dict, node: str, plan_dir) -> "Path | None":
    """The phase document `node` declares via `file:`, resolved under `plan_dir`.
    None when the node declares none, or declares one `find_phase_file_problems`
    refuses — a caller is never handed a path the validator would not accept."""
    st = _subtask(graph, node)
    if "file" not in st:
        return None
    return _phase_file_problem(st["file"], plan_dir)[0]


_RELATED_HEADING = re.compile(r"^##\s+related code files\b", re.IGNORECASE)
_BACKTICK = re.compile(r"`([^`]+)`")
_BRACES = re.compile(r"\{([^{}]*)\}")
_PATHLIKE = re.compile(r"^[\w.\-/]+$")
_ELLIPSIS = ".../"
_EXT_ONLY = re.compile(r"^\.[A-Za-z0-9]{1,4}$")
# A dotted token is a file only with a file extension: `seams.BoundaryDetector`,
# `RunBudget.load` and `vsf.di.x.ocr_engine` are code, not paths.
_FILE_EXTS = frozenset("""
py pyi md mdx rst txt yaml yml toml json jsonl lock cfg ini conf env sh bash zsh ps1 bat
cmd js mjs cjs ts tsx jsx vue svelte html htm css scss sql go rs java kt kts rb php c h cc
cpp hpp cs swift xml csv tsv typed j2 jinja tf hcl proto graphql gql mk gradle properties
ipynb pdf png svg jpg jpeg gif""".split())
# A table row whose action cell says the file is only read or run owns nothing.
_READ_ACTIONS = frozenset(("read", "đọc", "run", "chạy"))


def _related_section(doc: Path, plan_dir=None) -> "list | None":
    """The body of `doc`'s Related Code Files section; a pointer line in it reads as
    the reference file it points to (`plan_refs`)."""
    try:
        lines = doc.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    start = next((i for i, l in enumerate(lines) if _RELATED_HEADING.match(l.strip())), None)
    if start is None:
        return None
    out = []
    for line in lines[start + 1:]:
        if line.lstrip().startswith("#"):
            break
        out.append(line)
    import plan_refs
    return plan_refs.expand(out, doc, Path(plan_dir) if plan_dir is not None else doc.parent)


def _expand_braces(span: str) -> list:
    m = _BRACES.search(span)
    if not m:
        return [span]
    head, tail = span[:m.start()], span[m.end():]
    return [x for alt in m.group(1).split(",") for x in _expand_braces(head + alt.strip() + tail)]


def _is_file_token(cand: str) -> bool:
    if not _PATHLIKE.match(cand) or cand.endswith("/") or _EXT_ONLY.match(cand):
        return False
    last = cand.rsplit("/", 1)[-1]
    if last.startswith(".") and last.count(".") == 1:
        return True
    if "." not in last:
        return "/" in cand
    return last.rsplit(".", 1)[1].lower() in _FILE_EXTS


def _paths_in(text: str) -> list:
    return [c for span in _BACKTICK.findall(text)
            for c in _expand_braces(span.strip()) if _is_file_token(c)]


def _prose_paths(lines: list) -> list:
    """Concrete paths named in backticks: braces expanded, globs/dirs/code skipped.

    A section holding a table is read from its rows only — prose around it is
    commentary ("`x.toml` stays unchanged"). In a row, only the first cell naming a
    path is the file column; later cells (a guarding test, a note) are not owned.
    A row whose first cell is a read/run action owns nothing."""
    rows = [l.strip() for l in lines if l.strip().startswith("|")]
    found: list = []
    if rows:
        for k, row in enumerate(rows):
            cells = [c.strip() for c in row.strip("|").split("|")]
            if all(set(c) <= set(":- ") for c in cells):
                continue
            nxt = rows[k + 1] if k + 1 < len(rows) else ""
            if nxt and all(set(c.strip()) <= set(":- ") for c in nxt.strip("|").split("|")):
                continue  # header row
            if cells and cells[0].strip("*`_ ").lower() in _READ_ACTIONS:
                continue
            for cell in cells:
                paths = _paths_in(cell)
                if paths:
                    found.extend(paths)
                    break
    else:
        for line in lines:
            found.extend(_paths_in(line))
    return list(dict.fromkeys(found))


def _names(prose: str, declared: str) -> bool:
    """Prose shortens paths (`.../x.py`, `tests/x.py`, a bare `x.py`), so a prose
    path matches a declared one it is a tail of. A glob or directory entry covers a
    `.../` or bare name placed under its literal prefix; any other path must match
    the glob as written."""
    tail = prose[len(_ELLIPSIS):] if prose.startswith(_ELLIPSIS) else prose
    if declared == tail or declared.endswith("/" + tail):
        return True
    is_dir = declared.endswith("/")
    if not (is_dir or scope_match.has_glob_chars(declared)):
        return False
    glob = declared + "**" if is_dir else declared
    first = min((glob.index(c) for c in "*?[{" if c in glob), default=len(glob))
    prefix = glob[:first].rpartition("/")[0]
    shortened = prose.startswith(_ELLIPSIS) or "/" not in prose
    probe = (prefix + "/" + tail if shortened and prefix and not tail.startswith(prefix + "/")
             else tail)
    return scope_match.scope_matches([glob], [probe])


def find_phase_file_list_mismatches(graph: dict, plan_dir) -> list:
    """Per node with a `file:`, the paths its `Related Code Files` section names that
    `files_to_*` does not declare, and the reverse. Advisory: the prose is free text,
    so only concrete backticked paths are compared — globs and directories are not."""
    out = []
    for node in sorted(_all_nodes(graph), key=phase_sort_key):
        doc = phase_file(graph, node, plan_dir)
        if doc is None:
            continue
        lines = _related_section(doc, plan_dir)
        if lines is None:
            continue
        prose = _prose_paths(lines)
        declared = sorted(declared_files(graph, node))
        for p in prose:
            if not any(_names(p, d) for d in declared):
                out.append({"node": node, "file": p, "msg": (
                    "%s's phase file %s names %s under Related Code Files; plan-graph.yaml "
                    "does not declare it in files_to_*" % (node, doc.name, p))})
        for d in declared:
            if scope_match.has_glob_chars(d) or d.endswith("/"):
                continue
            if not any(_names(p, d) for p in prose):
                out.append({"node": node, "file": d, "msg": (
                    "%s declares %s in plan-graph.yaml; its phase file %s never names it "
                    "under Related Code Files" % (node, d, doc.name))})
    return out


def find_phase_file_problems(graph: dict, plan_dir) -> list:
    """Nodes whose declared `file:` is not a string, is absolute, escapes the plan
    dir, or names nothing on disk. An undeclared `file:` is not a problem — the key
    is optional; a declared one that points nowhere is a false statement in the plan."""
    out = []
    for node in sorted(_all_nodes(graph), key=phase_sort_key):
        st = _subtask(graph, node)
        if "file" not in st:
            continue
        _path, why = _phase_file_problem(st["file"], plan_dir)
        if why:
            out.append({"node": node, "file": st["file"], "msg": "node %s: %s" % (node, why)})
    return out


# `merger_owned` has a real reader outside this module — hs_run_git.py's declared-
# scope check treats a staged path in this list as exempt from the outside-declared
# block. Listed here so lint_no_status does not tell an author to delete it.
_TOPLEVEL_KEYS = frozenset({"edges", "subtasks", "status", "merger_owned", _CROSS, _WAITS})
_EDGE_KEYS = frozenset({"from", "to", "status", "waived"})


# ------------------------------------------------------- artifact obligation ---
# A node declares its end-of-phase artifact obligation as `post: [name, ...]`.
# This is the SAME obligation the derive counter (derive_plan_completion) already
# assumed implicitly (node -> verification-<node>.json); naming it here makes it a
# single declarative source the counter reads, instead of a hard-coded prefix. No
# `pre` this round (deferred — schema leaves room, nothing reads it yet).

def _str_list(v):
    """v as a list of non-empty str, or [] when v is anything else (scalar, dict,
    a list containing a non-str). Lets a malformed `post` degrade to the default
    rather than raise — same 'never crash on a hand-edited sidecar' contract."""
    if isinstance(v, list) and v and all(isinstance(x, str) and x for x in v):
        return list(v)
    return []


def node_artifacts(graph: dict, node: str) -> dict:
    """The node's declared artifact obligation: ``{"post": [str, ...]}``.

    RUNTIME reader (derive_plan_completion / artifact_check call it on an already
    *approved* graph). It keeps a defensive default = ``["verification-<node>.json"]``
    when `post` is unstated/malformed, so a node that slipped through resolves instead
    of crashing cook. This default is NOT an authoring allowance: `find_missing_post`
    + the `_main` gate refuse a missing/malformed `post` at validate + cook-preflight
    time, so a graph that reaches the runtime always has explicit
    post. `pre` is deferred: this reader never returns a "pre" key even if authored."""
    st = _subtask(graph, node)
    post = _str_list(st.get("post")) or ["verification-%s.json" % node]
    return {"post": post}


def find_missing_post(graph: dict) -> list:
    """Nodes (sorted) that do NOT declare an explicit, valid ``post`` — the MANDATORY
    end-of-phase artifact obligation. "Missing" covers no subtask entry, no
    `post` key, and a malformed `post` (not a non-empty list-of-str). The `_main` gate
    turns a non-empty result into a hard exit-2 refusal; the node_artifacts default
    still backstops the runtime so this is an authoring gate, not a runtime crash."""
    return sorted((n for n in _all_nodes(graph)
                   if not _str_list(_subtask(graph, n).get("post"))), key=phase_sort_key)


_SNAPSHOT_POST = re.compile(r"^verification-(.+)\.json$")


def find_post_problems(graph: dict) -> list:
    """`post:` entries that can never be satisfied, one line each. Two shapes:
    an `artifacts/` prefix (entries already resolve under <plan>/artifacts/),
    and a `verification-<X>.json` naming anything but this node — `cook verify
    --phase <node>` writes `verification-<node>.json`, so the entry waits on
    a file no verify of this node writes."""
    out = []
    for node in sorted(_all_nodes(graph), key=phase_sort_key):
        for name in _str_list(_subtask(graph, node).get("post")) or []:
            if name.startswith("artifacts/") or name.startswith("artifacts\\"):
                out.append("%s: post %r — drop the 'artifacts/' prefix, entries already "
                           "resolve under <plan>/artifacts/" % (node, name))
                continue
            m = _SNAPSHOT_POST.match(name)
            if m and m.group(1) != node:
                out.append("%s: post %r — verify of this node writes "
                           "verification-%s.json, never that one" % (node, name, node))
    return out


def all_declared_files(graph: dict) -> set:
    """Union of every phase's `files_to_create`/`files_to_modify`/`files_to_delete`
    — the "tệp tự khai" (self-declared files) figure a plan-size dashboard
    reports. Reuses `_owned`, so a bare-scalar or non-string entry degrades the
    same way `_files` already does (never raises)."""
    out: set = set()
    for n in _all_nodes(graph):
        out |= _owned(graph, n)
    return out


def expand_declared(root, files):
    """A declaration entry that names a DIRECTORY (trailing `/`) is not one
    file: expand it to what git tracks under it AT THE TIME ASKED, so both
    the posture question and the nudge's cover question count reality
    rather than the directory string. Non-directory entries pass
    through; a root git cannot read degrades to the unexpanded entry — the
    same fail-open every other graph read here carries."""
    import subprocess
    out = set()
    for f in files:
        entry = str(f)
        if not entry.endswith("/"):
            out.add(entry)
            continue
        try:
            r = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", entry],
                               capture_output=True, text=True)
        except OSError:
            r = None
        if r is not None and r.returncode == 0 and r.stdout.strip("\0"):
            out.update(ln for ln in r.stdout.split("\0") if ln)
        else:
            out.add(entry)
    return out


def declared_files(graph: dict, node: str) -> set:
    """ONE node's `files_to_create`/`files_to_modify`/`files_to_delete` union —
    the per-phase figure the workflow-weight execution posture is asked over.
    Same degradation contract as `all_declared_files` (never raises)."""
    return set(_owned(graph, node))


def _created(graph: dict, phase: str) -> set:
    return _files(graph, phase, "files_to_create")


def _modified_or_deleted(graph: dict, phase: str) -> set:
    return _files(graph, phase, "files_to_modify", "files_to_delete")


def find_ordering_hazards(graph: dict) -> list:
    """For each edge A→B, flag a file the prerequisite A modifies/deletes that the
    dependent B newly creates — A would touch a file that does not exist yet."""
    out = []
    for e in graph.get("edges", []) or []:
        a, b = e.get("from"), e.get("to")
        if a is None or b is None:
            continue
        shared = _modified_or_deleted(graph, a) & _created(graph, b)
        for f in sorted(shared):
            out.append({"prereq": a, "dependent": b, "file": f,
                        "msg": "%s modifies/deletes %s that %s only creates later"
                               % (a, f, b)})
    return out


# --------------------------------------------------------------- parallelism ---

def _kahn_levels(adj: dict) -> list:
    """Topological levels (Kahn) of an adjacency map; each level in `phase_sort_key`
    order. A cycle
    stops the derivation (named on stderr) — `find_cycles` owns the finding."""
    indeg = defaultdict(int)
    for src, dsts in adj.items():
        indeg.setdefault(src, indeg.get(src, 0))
        for d in dsts:
            indeg[d] += 1
    remaining = dict(indeg)
    levels = []
    while remaining:
        level = sorted((n for n, d in remaining.items() if d == 0), key=phase_sort_key)
        if not level:  # a cycle remains — stop deriving batches
            print("plan-graph: cycle detected — %d nodes unreachable: %s" %
                  (len(remaining), ", ".join(sorted(remaining, key=phase_sort_key))),
                  file=sys.stderr)
            break
        levels.append(level)
        for n in level:
            del remaining[n]
            for d in adj.get(n, []):
                if d in remaining:
                    remaining[d] -= 1
    return levels


def find_parallel_batches(graph: dict) -> list:
    """Topological levels (Kahn). Each level is an antichain — a batch that may run
    in parallel. Returns levels as name lists in `phase_sort_key` order.

    Paused phases AND every phase blocked behind one (`phase_holds`) are left out.
    No edge is rewired: the held set is closed under "downstream of", so every
    phase that remains has all of its prerequisites remaining too. Bridging around
    the paused node instead (the earlier rule) offered its dependents before the
    thing they need existed.
    """
    holds = phase_holds(graph)
    held = holds["paused"] | set(holds["blocked"])
    adj = {n: [d for d in dsts if d not in held]
           for n, dsts in build_adj(graph).items() if n not in held}
    return _kahn_levels(adj)


# ------------------------------------------------------------ plan-size facts ---
# The machine reports FACTS about a plan's size (declared files, directories,
# phases, independent clusters, longest chain), never a split recommendation — the
# old cell-count heuristic recommended `multi` on 89/89 calibration plans in a
# repo-wide calibration pass, and the connectivity rule it would have needed
# (edge-collector) only reaches 4/23 recall [0.070-0.371] against a labeled set of
# real plans. The chosen connectivity rule these functions implement:
# "independent cluster" = a connected component of the UNDIRECTED phase graph,
# after dropping every "collector" phase (a closing/wiring node that glues real
# independent work into one artificial component).

def _in_degree(adj: dict) -> dict:
    indeg: dict = defaultdict(int)
    for src in adj:
        indeg.setdefault(src, 0)
    for dsts in adj.values():
        for d in dsts:
            indeg[d] += 1
    return indeg


def collector_phases(graph: dict) -> set:
    """Phases with NO successor (a sink — no edge runs FROM it, so nothing in the
    plan depends on it finishing) AND >=2 predecessors — "pha gom" (a final
    wiring/docs phase that depends on everything else) as defined by the
    `edge-collector` connectivity rule: left in, it would glue the whole plan's
    connected-component count down to 1 regardless of how many real independent
    workstreams it actually has."""
    adj = build_adj(graph)
    indeg = _in_degree(adj)
    return {n for n in adj if not adj.get(n) and indeg.get(n, 0) >= 2}


def independent_clusters(graph: dict) -> list:
    """Connected components of the phase graph treated as UNDIRECTED, after
    dropping every `collector_phases()` node (and every edge touching one) from
    the vertex set entirely — the `edge-collector` connectivity rule. A collector
    does not count as its own singleton cluster either: it is glue, not an
    independent chunk of work. Measured on a real six-phase plan whose final
    phase depends on all five others: keeping that collector in would read as 2
    clusters — the real 5-phase chain plus a stray collector singleton — where
    the human's own "proceed, one plan" decision on that real plan endorses 1.

    Returns each cluster as a sorted list of phase names, the clusters
    themselves sorted by their first member — deterministic, not discovery
    order.
    """
    collectors = collector_phases(graph)
    adj = build_adj(graph)
    nodes = sorted(_all_nodes(graph) - collectors, key=phase_sort_key)
    undirected: dict = defaultdict(set)
    for src, dsts in adj.items():
        if src in collectors:
            continue
        for d in dsts:
            if d in collectors:
                continue
            undirected[src].add(d)
            undirected[d].add(src)
    node_set = set(nodes)
    seen: set = set()
    clusters = []
    for n in nodes:
        if n in seen:
            continue
        comp = ({n} | graph_core._closure(undirected, n)) & node_set
        seen |= comp
        clusters.append(sorted(comp, key=phase_sort_key))
    return sorted(clusters, key=lambda c: phase_sort_key(c[0]))


def longest_chain(graph: dict) -> int:
    """The longest path through the DAG, counted in PHASES (nodes), not edges —
    the "chuỗi dài nhất" figure. A single phase with no edges at all is a
    chain of length 1. A SIZE fact about the whole plan, so it walks every phase
    — paused and blocked ones included — over the full graph's topological
    order (cycle-safe: a node that never resolves out of a cycle simply never
    gets a DP entry, so this degrades gracefully on a corrupt sidecar rather
    than looping or raising — the finding for an actual cycle is
    `find_cycles`'s job, not this function's)."""
    adj = build_adj(graph)
    parents: dict = defaultdict(list)
    for src, dsts in adj.items():
        for d in dsts:
            parents[d].append(src)
    order = [n for level in _kahn_levels(adj) for n in level]
    memo: dict = {}
    for n in order:
        preds = parents.get(n, [])
        memo[n] = 1 + max((memo.get(p, 0) for p in preds), default=0)
    return max(memo.values(), default=0)


def graph_size_facts(graph: dict) -> dict:
    """The plan-size dashboard's raw numbers (declared files, directories,
    phases, independent clusters, longest chain, collector phases), computed
    purely from an already-parsed graph dict — no file I/O, no plan_dir.

    This is the injection seam `plan_facts.py` calls through instead of
    importing `plan_graph` itself: `plan_facts` already depends on this
    module (it needs a plan's graph parsed, and needs these same numbers),
    so a reverse `plan_graph -> plan_facts` import — added so `plan_graph`'s
    own CLI could print the facts dashboard — would close a two-file cycle.
    Keeping every primitive `plan_facts` needs callable from THIS module,
    and having `plan_graph`'s CLI pass this function (and `parse_phase_graph`,
    `root_for_plan_dir`) into `plan_facts.describe` as arguments, keeps the
    dependency one-directional: `plan_graph -> plan_facts`, never the reverse."""
    files = all_declared_files(graph)
    dirs = {Path(f).parent.as_posix() for f in files}
    return {
        "declared_files": len(files),
        "directories": len(dirs),
        "phases": len(_all_nodes(graph)),
        "independent_clusters": len(independent_clusters(graph)),
        "longest_chain": longest_chain(graph),
        "collector_phases": sorted(collector_phases(graph), key=phase_sort_key),
    }


def unordered_phase_pairs(graph: dict) -> list:
    """Every pair of distinct, non-paused phases with NO path between them in
    EITHER direction of the DAG — the real "could run at the same time" set the
    parallel-safety checks below need.

    `find_parallel_batches`'s topological LEVELS undercount this: two phases
    sharing no edge at all can still land in different levels purely because one
    of them has OTHER predecessors the other lacks (a level groups by DEPTH, not
    by "no relation exists between these two"). Measured (plan.md VL-6): a
    shared file planted between a batch-3 phase and a batch-4 phase with no edge
    between them went unreported, because the old `find_parallel_conflicts` only
    ever compared phases inside one batch.

    Paused phases are dropped (a paused phase cannot run, so it cannot conflict
    with anything). Reachability is read over the FULL graph, paused nodes
    included: p1 -> p2(paused) -> p3 still orders p1 before p3. A phase blocked
    behind a pause stays a candidate — it is real work that runs once the pause
    lifts, and its file overlaps are true about the plan now. Returns sorted
    `(a, b)` tuples with `a < b`, in a deterministic order.
    """
    paused = paused_phases(graph)
    adj = build_adj(graph)
    nodes = sorted(_all_nodes(graph) - paused, key=phase_sort_key)
    reach = {n: graph_core._closure(adj, n) for n in nodes}
    out = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            a, b = nodes[i], nodes[j]
            if b in reach[a] or a in reach[b]:
                continue
            out.append((a, b))
    return out


def cross_level_independent_pairs(graph: dict) -> list:
    """Unordered pairs `parallel batches` cannot show: no path between them, but in
    different topological levels. Held (paused/blocked) phases are left out, as there."""
    level = {n: i for i, batch in enumerate(find_parallel_batches(graph)) for n in batch}
    return [(a, b) for a, b in unordered_phase_pairs(graph)
            if a in level and b in level and level[a] != level[b]]


def _merger_owned(graph: dict) -> set:
    """The sidecar's `merger_owned:` list, coerced to a set of literal file
    paths — files a human merger reconciles across lanes at merge time
    (DEC ids, manifest regen, hand-kept per-domain tables, ...), so two
    unordered phases both declaring one is NOT a parallel-safety conflict.
    Deliberately file-granular, never glob-matched: the format documents that a
    same-FILE collision here might be two edits to the same line (the merger's
    job) or two different lines (no real collision) — `plan_graph` cannot tell
    which from a `files_to_*` declaration alone, so it never tries; it only
    ever exempts an EXACT literal path this list names."""
    val = graph.get("merger_owned")
    if isinstance(val, list):
        return {v for v in val if isinstance(v, str)}
    return set()


def _git_tracked_paths(root, include_untracked: bool = False) -> frozenset:
    """git-known paths under `root`, `-z`-delimited so a non-ASCII path never
    comes back C-quoted (the same defect `hs_run_git.py`'s own staged-path
    reader had — see its `-z` fix). Empty, never raises, when `root` is not
    inside a git repo at all: a throwaway test fixture, or a plan authored
    outside version control — a missing universe degrades to "no glob/glob
    grounding available", never a crash.

    `include_untracked=True` widens the pool with `--others --exclude-standard`
    — a test-first file is written and read BEFORE `git add`, so the plain
    `--cached` listing alone drops it. Still bounded the same way `--cached`
    is: `--exclude-standard` honors `.gitignore`, and a NESTED git checkout
    (a linked worktree such as `.claude/worktrees/<agent>`, or a submodule)
    is never walked into — git reports it as one opaque `<dir>/`-suffixed
    entry instead of descending, so filtering that trailing slash out is
    the whole exclusion; no separate worktree-detection code needed."""
    import subprocess
    import secret_scan_before_ship as secret_scan
    args = ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(root), "ls-files", "-z",
            "--cached"]
    if include_untracked:
        args += ["--others", "--exclude-standard"]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30,
                            env=secret_scan.git_safe_env())
    except OSError:
        return frozenset()
    if r.returncode != 0:
        return frozenset()
    return frozenset(p for p in r.stdout.split("\0") if p and not p.endswith("/"))


def _is_git_repo(root) -> bool:
    """True iff `root` sits inside a git working tree at all — the probe a
    caller needs to tell "git has an opinion here and it is genuinely empty"
    apart from "git has no opinion here, fall back to a filesystem walk"."""
    import subprocess
    import secret_scan_before_ship as secret_scan
    try:
        r = subprocess.run(
            ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(root), "rev-parse",
             "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=30, env=secret_scan.git_safe_env())
    except OSError:
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def _declared_universe(graph: dict, root) -> frozenset:
    """The candidate concrete-path pool a glob<->glob conflict grounds
    against: every git-tracked path under `root` (when given), plus every
    LITERAL (no glob metachar) `files_to_create` entry anywhere in the
    graph — a file a phase is about to create is a real, specific path even
    though git has never heard of it yet. A `files_to_create` entry that is
    itself a glob contributes nothing concrete here; it names no single
    path."""
    literal_creates = set()
    for phase in _all_nodes(graph):
        for f in _files(graph, phase, "files_to_create"):
            if not scope_match.has_glob_chars(scope_match.normalize_dir_prefix(f)):
                literal_creates.add(f)
    tracked = _git_tracked_paths(root) if root is not None else frozenset()
    return tracked | literal_creates


def _shared_files(graph: dict, a: str, b: str, universe_fn) -> set:
    """The declared files phases `a` and `b` both touch, by SCOPE (glob-aware),
    not literal-string equality — the one comparison both `find_parallel_
    conflicts` and `find_merger_owned_overlaps` need, kept in one place so they
    can never quietly diverge on how a glob pair is grounded.

    A `files_to_*` entry is a path glob (`scope_match` semantics), not a
    literal string — `pkg/` declares everything under it, and `pkg/*.py`
    covers `pkg/a.py` but not `pkg/sub/a.py`. Two entries that are each
    literal are compared directly; anything involving a glob on either side
    goes through `scope_match.declared_conflict`, grounded against
    `universe_fn()` only when BOTH sides are globs (a literal/glob pair needs
    no universe — the literal path is the only candidate there is)."""
    shared = set()
    for ea in _owned(graph, a):
        for eb in _owned(graph, b):
            na = scope_match.normalize_dir_prefix(ea)
            nb = scope_match.normalize_dir_prefix(eb)
            both_globs = (scope_match.has_glob_chars(na)
                          and scope_match.has_glob_chars(nb)
                          and na != nb)
            shared |= scope_match.declared_conflict(
                ea, eb, universe=universe_fn() if both_globs else ())
    return shared


# The decision register is appended through `decision_register.py`, which reserves
# each id across every worktree of the clone — two parallel phases add distinct
# records, and the merge keeps both. It needs a merger, not an edge.
_DECISION_REGISTER = frozenset({"docs/decisions.yaml", "docs/decisions.md"})
_DECISION_REGISTER_FIX = ("the decision register: ids are reserved across worktrees, so "
                          "list it under `merger_owned:` rather than serializing")


def find_parallel_conflicts(graph: dict, root=None) -> list:
    """Over every UNORDERED phase pair (`unordered_phase_pairs` — no path
    between them in either direction, not just "same topological batch"),
    flag a file two phases both own — parallel edits of a shared path
    clobber. The machine form of the prose 'shared-file' warning.

    A file the sidecar's own `merger_owned:` list names is EXEMPT here
    — a human merger reconciles it at merge time — but never silently: the
    same pair+file still surfaces through `find_merger_owned_overlaps`, a
    separate channel this function's caller can print alongside. `root` is
    optional: without it, a glob/glob pair still gets the literal
    `files_to_create` entries as its universe, just not `git ls-files` (see
    `_declared_universe`)."""
    out = []
    universe_cache: dict = {}

    def _universe():
        if "u" not in universe_cache:
            universe_cache["u"] = _declared_universe(graph, root)
        return universe_cache["u"]

    merger_owned = _merger_owned(graph)
    for a, b in unordered_phase_pairs(graph):
        shared = _shared_files(graph, a, b, _universe)
        for f in sorted(shared - merger_owned):
            fix = (_DECISION_REGISTER_FIX if f in _DECISION_REGISTER
                   else "serialize or split ownership")
            out.append({"phases": [a, b], "file": f,
                        "msg": "%s ∥ %s both touch %s — %s" % (a, b, f, fix)})
        for r in sorted(_resources(graph, a) & _resources(graph, b)):
            out.append({"phases": [a, b], "resource": r,
                        "msg": "%s ∥ %s both use resource %s — add an edge or split "
                               "the work" % (a, b, r)})
    return out


def _resources(graph: dict, phase: str) -> set:
    """A phase's declared `resources:` — what two phases can collide on without
    sharing a file: a DB schema both migrate, a ledger outside the repo."""
    st = (graph.get("subtasks") or {}).get(phase) or {}
    val = st.get("resources") if isinstance(st, dict) else None
    return {str(v) for v in val} if isinstance(val, list) else set()


def find_merger_owned_overlaps(graph: dict, root=None) -> list:
    """The same pairwise scan as `find_parallel_conflicts`, restricted to files
    the sidecar's `merger_owned:` list names — printed SEPARATELY, never
    folded into the conflict count. The graph declares files, not lines: a
    merger-owned collision at file granularity might be two phases editing the
    SAME line (the merger's own job to reconcile) or two different lines in the
    same file (no real collision at all), and `plan_graph` cannot tell which
    from a `files_to_*` declaration alone — so it is exempt from BLOCKING but
    never exempt from being SEEN. Empty list (no scan performed) when the
    sidecar carries no `merger_owned:` at all."""
    merger_owned = _merger_owned(graph)
    if not merger_owned:
        return []
    out = []
    universe_cache: dict = {}

    def _universe():
        if "u" not in universe_cache:
            universe_cache["u"] = _declared_universe(graph, root)
        return universe_cache["u"]

    for a, b in unordered_phase_pairs(graph):
        shared = _shared_files(graph, a, b, _universe)
        for f in sorted(shared & merger_owned):
            out.append({"phases": [a, b], "file": f,
                        "msg": "%s ∥ %s both touch merger-owned %s — reconciled "
                               "at merge, not a parallel-safety conflict"
                               % (a, b, f)})
    return out


def resolve_snapshot_units(root) -> "tuple[dict | None, str | None]":
    """Auto-select `root`'s own hs-code snapshot the same way every other
    fact provider already does (`docs_symbols.head_snapshot`,
    `hs_run_facts.engine.store_base` + `select_snapshot`) — a caller of
    `find_import_edge_conflicts` no longer has to be TOLD which snapshot
    directory to hand in via `--units-json`; the code picks it, with the
    schema_version check `codegraph_snapshot_reader.read_index` already
    does and the freshness check `select_snapshot` already does (a
    snapshot of anything but the CURRENT clean HEAD tree is never used).

    Returns `(units, warning)`. `units` is `None` — never a crash, never a
    false claim — when the project has not adopted hs-code (`store_base`
    returns `None`), its data home cannot be resolved, or no snapshot under
    the store is `status == "fresh"`; `warning` then names why (or `None`
    for the ordinary "not adopted" case, which is silent by design, same
    as `find_import_edge_conflicts(graph, units=None)` today).
    """
    from hs_run_facts import engine
    root = Path(root)
    try:
        base = engine.store_base(root)
    except engine.DataHomeUnresolved as exc:
        return None, "snapshot store's data home unresolved: %s" % exc
    if base is None:
        return None, None
    picked = engine.select_snapshot(root, base)
    if picked.status != "fresh":
        return None, picked.warning or ("no fresh snapshot of HEAD under %s" % base)
    import codegraph_snapshot_reader
    index = codegraph_snapshot_reader.read_index(picked.dir)
    return _snapshot_units(index), picked.warning


def _snapshot_units(idx: dict) -> dict:
    """path -> the hs-code graph snapshot's own record for it, folding every
    category `idx["units"]` groups records under into one flat map — every
    caller here only ever asks "is there a record for this path"."""
    out = {}
    for records in (idx.get("units") or {}).values():
        for rec in records or ():
            if isinstance(rec, dict) and rec.get("path"):
                out[rec["path"]] = rec
    return out


def file_import_edge(file_a: str, file_b: str, units: dict) -> bool:
    """True iff the snapshot records a real import edge between `file_a` and
    `file_b` in EITHER direction — direction does not matter for a
    parallel-safety check: either phase can now observe (or be observed by)
    the other's file mid-run. `units` is a path -> snapshot-record map (see
    `_snapshot_units`); a path with no record (outside every declared root,
    or the snapshot predates it) never joins an edge."""
    rec_a, rec_b = units.get(file_a), units.get(file_b)
    if rec_a is None or rec_b is None:
        return False
    mod_a, mod_b = rec_a.get("module"), rec_b.get("module")
    _base = codegraph_change_reader._base_target
    imports_a = ({_base(t) for t in (rec_a.get("imports_module_level") or ())}
                 | {_base(t) for t in (rec_a.get("imports_function_local") or ())})
    imports_b = ({_base(t) for t in (rec_b.get("imports_module_level") or ())}
                 | {_base(t) for t in (rec_b.get("imports_function_local") or ())})
    return bool((mod_b and mod_b in imports_a) or (mod_a and mod_a in imports_b))


def find_import_edge_conflicts(graph: dict, units) -> list:
    """Two phases can own no file in common and STILL be
    unsafe to run in parallel, when one's file imports the other's — a write to
    `a.py` can change what `b.py` sees mid-run even though the plan never
    declared them as touching the same path. Flags the first (file_a, file_b)
    pair joined by a real import edge (`file_import_edge`, read from the
    project's hs-code graph snapshot) for every unordered phase pair that
    `find_parallel_conflicts` did NOT already flag (a shared file already
    reports there; this channel only ever ADDS phases with no file in common,
    so the two never double-count one hazard).

    `units` is the caller's own already-resolved snapshot map (path -> record,
    see `_snapshot_units`) — resolving WHICH snapshot to read is a separate,
    I/O-owning step (`cook_parallel_plan.py` and this module's own `_main`
    each do it their own way) so this stays a pure, fixture-driven function.
    `units=None` (no snapshot available) degrades to `[]` — no import
    information is not a crash, and not a false claim either.
    """
    if not units:
        return []
    out = []
    for a, b in unordered_phase_pairs(graph):
        files_a, files_b = _owned(graph, a), _owned(graph, b)
        if files_a & files_b:
            continue  # already reported by find_parallel_conflicts
        hit = None
        for fa in sorted(files_a):
            for fb in sorted(files_b):
                if file_import_edge(fa, fb, units):
                    hit = (fa, fb)
                    break
            if hit:
                break
        if hit:
            fa, fb = hit
            out.append({"phases": [a, b], "file": "%s ~ %s" % (fa, fb),
                        "msg": "%s ∥ %s unsafe in parallel — %s and %s share no "
                               "declared file but are joined by a real import "
                               "edge" % (a, b, fa, fb)})
    return out


def _unread(present, known, where: str) -> list:
    """Warn about keys nobody reads. A sidecar key that no reader consumes is
    invisible in both directions: it changes nothing, and nothing says so — the
    author has every reason to believe it took effect. Measured instance: a plan
    carried `paused: true` + `paused_reason` + `reopen_when`, the phase stayed in
    the parallel batch and in the completion denominator, and not one line was
    printed. This warns on the CLASS, so it also catches the next invented key."""
    extra = sorted(k for k in present if k not in known)
    if not extra:
        return []
    return ["%s: unread key(s) %s in %s — nothing consumes them, so they change "
            "nothing. Remove them, or open an issue for the behaviour you wanted"
            % (where, ", ".join(extra), _SIDECAR)]


def lint_no_status(graph: dict) -> list:
    """A status key must not live in the sidecar (status is mutable, owned by plan.md).
    Warn — do not raise — so a stray status is surfaced, not silently hashed.

    Also warns about keys no reader consumes (see _unread)."""
    warnings = []
    if "status" in graph:
        warnings.append("top-level 'status' key in %s — status belongs in plan.md" % _SIDECAR)
    warnings += _unread(graph, _TOPLEVEL_KEYS, "top-level")
    for phase, st in (graph.get("subtasks", {}) or {}).items():
        if isinstance(st, dict) and "status" in st:
            warnings.append("subtask %s carries 'status' in %s — status belongs in "
                            "plan.md's `## Phases`" % (phase, _SIDECAR))
        # `post` is the declarative artifact obligation; a malformed value silently
        # degrades to the default in node_artifacts, so surface it here instead.
        if isinstance(st, dict) and "post" in st and not _str_list(st.get("post")):
            warnings.append("subtask %s has a malformed 'post' in %s — expected a "
                            "list of artifact names; using default "
                            "[verification-%s.json]" % (phase, _SIDECAR, phase))
        if isinstance(st, dict):
            # A pause removes a phase from three counters at once. Doing that with no
            # sentence attached is the same defect shape as a silent N/A: the number
            # got smaller and nothing on disk says why.
            if st.get("paused") and not str(st.get("paused_reason") or "").strip():
                warnings.append("subtask %s is paused with no 'paused_reason' in %s — "
                                "a pause drops the phase from the batch, the completion "
                                "denominator and the gate; say why" % (phase, _SIDECAR))
            elif st.get("paused"):
                warnings.append("subtask %s: %s" % (phase, pause_note(graph, phase)))
            # The half-edit that outlives a resume: someone cleared `paused` and left
            # the prose behind, so the sidecar READS paused to a human and does not to
            # the machine.
            if not st.get("paused") and (st.get("paused_reason") or st.get("reopen_when")):
                warnings.append("subtask %s carries pause prose but no `paused: true` in "
                                "%s — it reads as paused and is not; clear the prose or "
                                "set the flag" % (phase, _SIDECAR))
            warnings += _unread(st, _SUBTASK_KEYS, "subtask %s" % phase)
    for e in graph.get("edges", []) or []:
        if isinstance(e, dict) and "status" in e:
            warnings.append("edge %s->%s carries 'status' in %s — status belongs in "
                            "plan.md" % (e.get("from"), e.get("to"), _SIDECAR))
        if isinstance(e, dict):
            warnings += _unread(e, _EDGE_KEYS,
                                "edge %s->%s" % (e.get("from"), e.get("to")))
    return warnings


def advisory_findings(graph: dict, plan_dir, units=None) -> dict:
    """Every advisory finding over a parsed graph, as `kind: msg` lines — the one
    list `plan_graph.py` and `hs-run plan graph` both print, so neither door can
    drop a finder the other runs. `units`: the import-edge scan's input; None
    reads the plan tree's code-map snapshot (`scan_warning` says when it cannot).
    `phase_file_problems` is returned too: under `--require` they are findings."""
    root = root_for_plan_dir(plan_dir)
    scan_warning = None
    if units is None:
        units, scan_warning = resolve_snapshot_units(root)
    lines = ["cycle: %s" % " -> ".join(c) for c in find_cycles(graph)]
    lines += ["ordering-hazard: %s" % h["msg"] for h in find_ordering_hazards(graph)]
    lines += ["blocked: %s" % note for _p, note in sorted(phase_holds(graph)["blocked"].items(), key=lambda kv: phase_sort_key(kv[0]))]
    lines += ["parallel-conflict: %s" % c["msg"] for c in find_parallel_conflicts(graph, root=root)]
    lines += ["merger-owned: %s" % c["msg"] for c in find_merger_owned_overlaps(graph, root=root)]
    lines += ["import-edge-conflict: %s" % c["msg"] for c in find_import_edge_conflicts(graph, units)]
    problems = find_phase_file_problems(graph, plan_dir)
    lines += ["phase-file: %s" % prob["msg"] for prob in problems]
    lines += ["phase-files: %s" % m["msg"] for m in find_phase_file_list_mismatches(graph, plan_dir)]
    return {"lines": lines, "phase_file_problems": problems, "scan_warning": scan_warning}


def _main(argv=None) -> int:
    import argparse
    import json
    p = argparse.ArgumentParser(description="read a plan's phase-DAG sidecar (advisory)")
    # Positional OR `--plan`: plan_approval.py and hs-run spell the target
    # `--plan DIR`, so a command line copied from either must not die here.
    p.add_argument("plan_dir", nargs="?")
    p.add_argument("--plan", dest="plan_flag", metavar="DIR",
                   help="the plan dir (same as the positional)")
    p.add_argument("--require", action="store_true",
                   help="exit 2 if the sidecar is missing (cook preflight gate); "
                        "default stays advisory (exit 0) for validate-time use")
    p.add_argument("--units-json",
                   help="EXPLICIT override for the import-edge scan's snapshot "
                        "units (raw _index.json, or an already-flattened "
                        "path->record map) — for a fixture, or a snapshot other "
                        "than the project's own auto-selected HEAD one. Omitted "
                        "(the normal case): the scan auto-selects the project's "
                        "own store snapshot of HEAD (resolve_snapshot_units, "
                        "the same store_base+select_snapshot selection every "
                        "other hs-run fact provider uses) — no snapshot adopted "
                        "or none fresh at HEAD degrades to no import-edge line, "
                        "same as before this flag existed.")
    p.add_argument("--facts", action="store_true",
                   help="print plan_facts.describe(plan_dir)'s JSON (declared "
                        "files/directories/phases, independent clusters, longest "
                        "chain, a rank against old plans, descriptive labels) and "
                        "exit — a standalone reporting mode, never mixed with the "
                        "other advisory lines below, so a caller reading this JSON "
                        "as machine input never has to skip prose around it.")
    p.add_argument("--root", help="project root for --facts's historical-plan "
                   "ranking population (default: derived from plan_dir)")
    args = p.parse_args(argv)
    if args.plan_dir and args.plan_flag and args.plan_dir != args.plan_flag:
        p.error("the positional plan dir and --plan name different dirs (%s vs %s)"
                % (args.plan_dir, args.plan_flag))
    args.plan_dir = args.plan_dir or args.plan_flag
    if not args.plan_dir:
        p.error("a plan dir is required (positional or --plan DIR)")
    if args.facts:
        import plan_facts
        out = plan_facts.describe(args.plan_dir, parse_phase_graph, graph_size_facts,
                                  root_for_plan_dir, root=args.root)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 2 if out.get("error") else 0
    graph = parse_phase_graph(args.plan_dir)
    if graph.get("error"):
        print("error: %s" % graph["error"])
        # The sidecar is a mandatory plan artifact. --require makes its absence a
        # hard cook-preflight block (exit 2); advisory mode never blocks the
        # planner mid-authoring (exit 0).
        return 2 if args.require else 0
    for w in lint_no_status(graph):
        print("status-leak: %s" % w)
    # Zero phases AND a key nothing reads: the graph was written in a shape the reader
    # does not know (`nodes:`), not left empty. An empty sidecar with no stray key stays
    # valid; a stray key beside a real graph stays a warning.
    unread = sorted(k for k in graph if k not in _TOPLEVEL_KEYS)
    if args.require and unread and not _all_nodes(graph):
        print("error: %s declares no phase; its graph sits under %s, which nothing "
              "reads — write it as `edges:` + `subtasks:`" % (_SIDECAR, ", ".join(unread)))
        return 2
    batches = find_parallel_batches(graph)
    print("parallel batches: %s" % batches)
    pairs = cross_level_independent_pairs(graph)
    if pairs:
        print("also independent across levels: %s"
              % ", ".join("%s ∥ %s" % pair for pair in pairs))
    units = None
    if args.units_json:
        raw = json.loads(Path(args.units_json).read_text(encoding="utf-8"))
        units = _snapshot_units(raw) if isinstance(raw, dict) and "units" in raw else raw
    found = advisory_findings(graph, args.plan_dir, units=units)
    if found["scan_warning"]:
        print("import-edge-scan: %s" % found["scan_warning"], file=sys.stderr)
    for line in found["lines"]:
        print(line)
    # A declared phase file that points nowhere: printed always, blocking under
    # --require only — mid-authoring, the graph may name a phase not yet written.
    phase_file_problems = found["phase_file_problems"]
    # `post` is MANDATORY per node. Unlike cycles/conflicts (heuristic
    # advisories), a missing `post` is a structural contract violation: HARD-fail (exit 2)
    # in BOTH advisory and --require modes so validate-time + cook-preflight both refuse it.
    missing = find_missing_post(graph)
    for n in missing:
        print("missing-post: node %s must declare 'post:' (required artifact "
              "obligation, e.g. post: [verification-%s.json])" % (n, n))
    bad_post = find_post_problems(graph)
    for line in bad_post:
        print("bad-post: %s" % line)
    # A cross-plan edge that cannot hold is the same kind of contract break as a
    # missing `post`: the phase it gates would wait forever, or never wait at all.
    cross = find_cross_plan_problems(graph, args.plan_dir)
    for line in cross:
        print("cross-plan: %s" % line)
    return 2 if missing or bad_post or cross or (args.require and phase_file_problems) else 0


if __name__ == "__main__":
    raise SystemExit(_main())
