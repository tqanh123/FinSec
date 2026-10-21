"""One reader for "what does this verb result say", across both spellings.

Several gates ask questions about verb results by walking the AST: which rung a
branch names, which states the CLI can emit, whether an 11 carries a runnable
command. Each grew its own `isinstance(node, ast.Dict)` loop, which was correct
while every verb returned a dict literal.

Verb results are migrating onto `StepResult(...)` — the rule this repo already
argued and won for the ledger line, the checklist row and the run context ("a
record that crosses a boundary is a DECLARED object, not a dict literal"). The
first two verbs converted went invisible to a scanner written for the old shape,
and the conformance gate reported their states as dead vocabulary while they
were still printed on every run. A detector that keeps asking the pre-migration
question answers confidently about a tree that no longer exists.

So the shape question is answered HERE, once, and the gates ask this module.
When a third spelling appears, one file learns it and every gate sees it — which
is the same argument the value objects themselves are making, applied to the
tests that police them.

Everything returned is AST NODES, not values: the callers need `.lineno` for
their messages and need to distinguish `None` written explicitly from a key that
is absent, which a normalised dict of values would erase.
"""

import ast

# Constructors whose keyword arguments carry the same meaning the dict keys did.
_RESULT_CLASSES = ("StepResult", "Envelope")


def _kwargs_of(call: ast.Call) -> dict:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def result_literals(tree: ast.AST):
    """Yield `(node, fields)` for every verb-result literal in `tree`.

    `node` is what carries `.lineno` for an error message; `fields` maps a field
    name to its AST value node. Three spellings are recognised:

      * `return {...}`            — the original, still the majority
      * `name = {...}`            — built, conditionally amended, returned later.
        This one hid a real branch from an earlier sweep: `not_approved` was
        assembled this way, so a scan that walked only `ast.Return` reported the
        file clean while the branch it could not see was the one that mattered.
      * `StepResult(...)` / `Envelope(...)` — the value-object form.

    A dict without `state` is not a verb result — `hs_run_review_pr.py` builds
    per-CI-check records shaped `{"name": ..., "state": ...}`, and callers that
    care filter further on `next_action`, which every verb result carries and no
    inner record does."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Return, ast.Assign)) and isinstance(node.value, ast.Dict):
            fields = {k.value: v for k, v in zip(node.value.keys, node.value.values)
                      if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "state" in fields:
                yield node, fields
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", None))
            if name in _RESULT_CLASSES:
                fields = _kwargs_of(node)
                if "state" in fields:
                    yield node, fields
                continue
            # A result dict passed as an ARGUMENT: `finalize({...})`. The whole
            # ship domain is spelled this way, so every gate reading through
            # here saw zero results in it and passed by finding nothing — which
            # looks exactly like passing by being clean. Any callee is accepted
            # rather than a name list: what makes this a verb result is the
            # `state` key, and a list of wrapper names would need editing every
            # time somebody adds one, silently exempting the module until then.
            for arg in list(node.args) + [kw.value for kw in node.keywords]:
                if not isinstance(arg, ast.Dict):
                    continue
                fields = {k.value: v for k, v in zip(arg.keys, arg.values)
                          if isinstance(k, ast.Constant) and isinstance(k.value, str)}
                if "state" in fields:
                    yield node, fields


def states_named(fields: dict, scope=None):
    """Every literal state a result can carry, descending into conditionals.

    `"opened" if res.changed else "already_open"` is one return and two states;
    a scan that reads only `ast.Constant` sees neither.

    `scope` — the enclosing function — lets a state that was assigned to a local
    first be resolved. `hs_run_plan.py`'s `graph` verb writes
    `state = "absent" if not sidecar.is_file() else "malformed"` and passes the
    NAME, and without this all four of that verb's states are invisible: a
    routing audit then reported the domain fully covered while four live states
    had no row. Only same-name literal (or conditional-of-literals) assignments
    in that function are followed — anything computed stays unresolvable, which
    is honest, and callers that care assert on the count."""
    out = set()

    def walk(node, depth=0):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.add(node.value)
        elif isinstance(node, ast.IfExp):
            walk(node.body, depth)
            walk(node.orelse, depth)
        elif isinstance(node, ast.Name) and scope is not None and depth == 0:
            # depth-guarded: follow a name ONCE, so a pair of locals assigned
            # from each other cannot walk forever.
            for assign in ast.walk(scope):
                if not isinstance(assign, ast.Assign):
                    continue
                for tgt in assign.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == node.id:
                        walk(assign.value, depth + 1)

    walk(fields.get("state"))
    return out


def engine_overlay_states(domain, engine_src=None, scope=None):
    """States the ENGINE writes over a domain's answer, for the gates that scan a
    domain module and would otherwise call them phantom rows.

    `hs_run` rewrites a repeated answer into `stuck` before the envelope is built.
    That state is real, routed, and reaches the reader — but it is emitted by the
    engine, so every reader that source-scans `hs_run_<domain>.py` reports it as
    routed-but-never-returned. The choice is between teaching those readers about
    the one overlay seam and moving the overlay into six domain modules, which is
    the copy the seam exists to prevent.

    Derived, never listed. The names come from source-scanning the overlay
    function, and the scope comes from the declared policy — so deleting the
    overlay empties this set and a `stuck` row goes red again, and dropping a
    domain from the policy makes that domain's row go red while the others stay.
    A hard-coded `{"stuck"}` would survive both and turn a live gate into a
    decoration."""
    from pathlib import Path

    src = Path(engine_src) if engine_src else Path(__file__).resolve().parent / "hs_run.py"
    if not src.is_file():
        return set()
    names = set()
    for fn in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
        if not isinstance(fn, ast.FunctionDef) or fn.name != "_apply_escalation":
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Constant) and t.slice.value == "state"
                    for t in node.targets):
                names |= states_named({"state": node.value}, scope=fn)
    if not names:
        return set()
    if scope is None:
        import hs_run
        scope = hs_run.escalation_policy()["domains"]
    return names if domain in set(scope) else set()


def facts_overlay_states(domain, engine_src=None, scope=None):
    """States `hs_run_facts.engine` writes over a domain's answer — the
    `facts:` seam's own analog of `engine_overlay_states` above (the same
    problem: the rewrite to `facts_stale` happens one file further out, in
    `hs_run_facts/engine.py`'s `_stale_outcome`, never in `hs_run_
    <domain>.py` itself, so a reader that only scans the domain module
    reports a `facts_stale` row as phantom vocabulary).

    Scanned through `result_literals`/`states_named` — the SAME shared
    reader every domain module is scanned with — rather than a second
    hand-rolled AST walk: `_stale_outcome` returns `{"state":
    "facts_stale", ...}` as a plain dict literal (`return {...}`), a
    DIFFERENT AST shape from `_apply_escalation`'s subscript-assignment
    (`result["state"] = "stuck"`), and `result_literals` already recognizes
    both spellings.

    `scope` (default `None` → every domain that declares `facts:` on ANY
    state in the REAL registry) is DATA-derived, not a second hand-kept
    domain list beside `advice_escalation.domains` — a domain that starts
    declaring `code.tests_reached` earns the overlay automatically; a
    policy list kept by hand would drift the day a new domain adds a
    `facts:` row and its author forgets to also edit this one."""
    from pathlib import Path

    src = Path(engine_src) if engine_src else (
        Path(__file__).resolve().parent / "hs_run_facts" / "engine.py")
    if not src.is_file():
        return set()
    tree = ast.parse(src.read_text(encoding="utf-8"))
    # The engine names its own state-rewriting functions (`OVERLAY_OUTCOMES`), so a
    # new overlay is one tuple entry there, never a second name kept here.
    overlays = {"_stale_outcome"}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "OVERLAY_OUTCOMES" for t in node.targets)
                and isinstance(node.value, (ast.Tuple, ast.List))):
            overlays = {e.value for e in node.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    names = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name not in overlays:
            continue
        for _node, fields in result_literals(fn):
            names |= states_named(fields, scope=fn)
    if not names:
        return set()
    if scope is None:
        import hs_run
        routing = hs_run.load_step_routing()
        scope = {dom for dom, blk in routing.items()
                 for row in (blk.get("states") or {}).values()
                 if row.get("facts")}
    return names if domain in set(scope) else set()


def domain_modules(domain, registry=None):
    """Every verb module `domain` declares, in first-DECLARATION order.

    Order is the registry's own dict order (Python dicts keep insertion
    order; `load_registry()`'s comes from `yaml.safe_load` walking the
    merged fragment's keys top to bottom) — text on disk, never a `set`,
    so it cannot reshuffle with `PYTHONHASHSEED`. Deduped keeping the
    FIRST occurrence, so a domain whose verbs all share one module still
    gets it back once.

    `registry` lets a caller that already loaded `hs_run.load_registry()
    .get(domain)` skip re-reading it; default `None` reads it here."""
    import hs_run

    verbs = registry if registry is not None else (hs_run.load_registry().get(domain) or {})
    modules = []
    for spec in (verbs or {}).values():
        mod = (spec or {}).get("module")
        if mod and mod not in modules:
            modules.append(mod)
    return modules


def domain_emitted_states_ordered(domain, *, overlays=True, scripts_dir=None) -> list:
    """Every state a domain can emit, in first-appearance order, from ONE scan
    (set-shaped readers derive `set(...)` of this list; one AST walk answers
    both "which states" and "in what order", rather than two independent scans
    that could each learn a spelling the other missed and quietly disagree
    about which states exist while still agreeing on the count).

    This is the list `render_state_map.py` renders rows from — the map's own
    claim is that a row's position is a path a run walks, not an
    alphabetized dictionary, so the tie-break below has to be a rule, not
    an accident of `ast.walk`'s traversal order or a `set`'s iteration
    order (measured on an earlier private copy of this walk: four
    different row orders on four `PYTHONHASHSEED` values, i.e. a
    "generated" file whose bytes depended on the interpreter's hash salt).

    THE TIE-BREAK RULE (deterministic, and this is the one place it is
    decided):
      1. module, in `domain_modules`'s declaration order (registry dict
         order — see there for why that is stable and a `set` was not).
      2. within a module, a literal `result_literals`/`states_named` hit
         sorts by `(lineno, col_offset)` — source position, so the row
         order reads as the order a run walks the module. A tie (one
         result naming two states, e.g. `"a" if x else "b"` — same node,
         same position) is broken by NAME.
      3. a bare `State.NAME` enum reference has no result-literal position
         to sort by; every one sorts AFTER every literal hit in its own
         module, alphabetically among themselves.
      4. overlay states (`stuck` from the engine, `facts_stale` from
         `hs_run_facts.engine`) have no source position at all — engine
         overlay first, then facts overlay, each alphabetical among its
         own names, both AFTER every module's hits.
    A name already placed keeps its FIRST position; a later hit for the
    same name (a literal also named by an overlay, or the same enum member
    referenced twice) is dropped rather than duplicated or re-ordered.

    `scripts_dir` (default: this file's own directory, i.e. `harness/
    scripts/`) is the SAME override door `_states_a_domain_can_emit`'s own
    positive/negative controls depend on — paired with a monkeypatched
    `hs_run.load_registry`, it lets a caller prove this reader against a
    PLANTED module rather than only against whatever the shipped tree
    happens to contain today. It is also what makes this reader
    ROOT-scoped for a caller like the renderer: passed `root / "harness" /
    "scripts"`, both the domain's module(s) AND the two overlay sources
    (`hs_run.py`, `hs_run_facts/engine.py`) resolve under that root rather
    than under wherever this file itself happens to be installed — under a
    global install those are two different trees, and scanning this file's
    own directory would silently render a map for modules that are not the
    ones a run of the CALLER's project actually calls.

    `overlays` (default `True`) folds in `engine_overlay_states(domain)`
    (the `stuck` rewrite `hs_run.py`'s `_apply_escalation` makes AFTER a
    module returns) and `facts_overlay_states(domain)` (the `facts_stale`
    rewrite `hs_run_facts.engine._stale_outcome` makes one file further
    out) — both real for `fix` today, at least one real for most `covers_
    skill` domains. `overlays=False` opts a caller OUT of BOTH at once:
    there is no call site in this tree today that needs one overlay but
    not the other, and a caller that ever does should say so explicitly at
    its own call site (`| engine_overlay_states(domain)` etc.) rather than
    this function growing a second boolean nobody has needed yet."""
    import re
    from pathlib import Path

    import hs_run

    base = Path(scripts_dir) if scripts_dir else Path(__file__).resolve().parent
    modules = domain_modules(domain, hs_run.load_registry().get(domain))

    hits = []
    for module_index, module in enumerate(modules):
        src = base / ("%s.py" % module)
        if not src.is_file():
            continue
        text = src.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node, fields in result_literals(fn):
                for name in sorted(states_named(fields, scope=fn)):
                    hits.append((module_index, 0, node.lineno, node.col_offset, name))
        # The engine rewrites a repeated answer AFTER a module returns, and
        # `State.` is a bare enum reference rather than a `state=`/`"state"`
        # literal — neither shape is a call `result_literals` recognises, so
        # a module that speaks ONLY this spelling would otherwise scan empty.
        # No result-literal position exists for these, so they sort after
        # every literal hit in this module (rank 1 vs 0), tied by name.
        enum_names = sorted({s.lower() for s in re.findall(r"\bState\.([A-Z_]+)", text)})
        for name in enum_names:
            hits.append((module_index, 1, 0, 0, name))

    ordered: list = []
    for key in sorted(hits):
        name = key[-1]
        if name not in ordered:
            ordered.append(name)

    if overlays:
        engine_src = base / "hs_run.py"
        facts_src = base / "hs_run_facts" / "engine.py"
        for name in sorted(engine_overlay_states(domain, engine_src=engine_src)):
            if name not in ordered:
                ordered.append(name)
        for name in sorted(facts_overlay_states(domain, engine_src=facts_src)):
            if name not in ordered:
                ordered.append(name)
    return ordered


