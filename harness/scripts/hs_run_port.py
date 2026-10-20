#!/usr/bin/env python3
"""hs_run_port.py — the `port` domain's verb entry point for hs-run.

`next` reads `<target>/.port/` and reports exactly what a driver should do
next: nothing planned yet, config broken, or — once at least one batch is
declared — that batch's own STEP LANE, read from `.port/batches/<name>/
runs.jsonl` against the standard lane `harness/data/port-steps.yaml`
declares. It writes nothing itself; every write (a batch's own runs.jsonl,
the port ledger, the ported code) happens through `hs-code port` — this
verb only ever reads what is already on disk and hands back the exact
`hs-code` command to run next (rung 11) or the reason nothing can run yet
(rung 2 or 10).

Root discipline — the OPPOSITE of every sibling domain, on purpose: every
other `hs_run_*.py` module reads `<root>`, hs-run's OWN resolved project
root, and is written to NEVER fall back to the process CWD (`root: /tmp` is
the standing trap a bare-path invocation produces there). This module reads
neither. `<target>` here is the directory `--repo` names, or — walking UP
from the process CWD — the nearest ancestor holding a `.port/` directory
(`port_workspace.find_port_root`). `root` (the
function parameter) is accepted, matching every other verb's call
signature, and deliberately UNUSED: measured, honouring it here reproduced
the exact `root: /tmp` failure shape the other domains avoid — pinning
`HARNESS_DATA_ROOT` at an unrelated tree while standing inside a real,
valid `.port/` fixture answered `no_workspace` against the wrong directory.
A reader tempted to "fix" this back to reading `root` should read
`port_workspace.py`'s module docstring first — this domain answers "what
repo is the operator standing in", the same question `git status` answers,
never "what project does this session's env point at".

The config checker itself (`port.yaml`'s allowed keys, `rules:` shape, the
unkillable-`port.L0` refusal) lives in `port_config.py` — a sibling flat
module, imported here rather than duplicated, so the hs-code side (which may
import `codegraph`) validates the exact same file through the exact same
door as this hs-run verb does.

LEDGER — the config check above is unchanged; this verb ALSO reads
`<target>/.port/ledger.jsonl` when one exists, through the streaming,
compatible reader `port_ledger_reader.read_ledger` (never a whole-
file read — see that module's own docstring for why). A clean ledger's
`{lines, broken, nodes}` stats ride along in the `no_batch`/batch-lane
envelope under `ledger`; a ledger holding at least one broken line reports
`ledger_broken` instead, carrying the same stats plus `l0_findings`, ahead
of every batch check below — a broken *port* ledger blocks reading batch
state at all, the same way a broken *run* ledger (below) blocks reading
ONE batch's own lane. No ledger file at all leaves `ledger` out of the
envelope entirely — its absence is not a fact this verb has anything to
add to either state's own sentence.

BATCHES — once the config and port ledger both read clean, this verb walks
every batch NAME under `<target>/.port/batches/` (sorted; a dir carrying
its own `batch.yaml`, never a bare dir), computing EACH batch's lane
status from `.port/batches/<name>/runs.jsonl` (`port_runs.read_runs`)
against the lane its own `batch.yaml` `steps:` picks, or — absent that —
`port-steps.yaml`'s standard `lane`, both read through `port_steps.py`, the
one loader `port_batch_reader.py` ALSO validates a batch's `steps:` pick
against (never a second table). A step (per dist, for a `per_dist: true`
step) is GREEN iff its LAST recorded run has `exit_code == 0` AND `argv`
is the command this verb freshly renders for it right now (`port_runs.
ran_green`: same words in order, same `--apply`/`--fresh`/`--allow-stale`
wherever they were typed) — a
successful DRY RUN (no `--apply` in the recorded argv) is not green,
because the rendered command always carries `--apply` for a writing step;
neither is a stale record left over from a `batch.yaml`/`port-steps.yaml`
edit since it ran.

The FIRST batch (sorted) carrying a non-green step decides the state:
`batch_planned` (that batch's own `runs.jsonl` carries NO records at all —
nothing has been attempted yet), `steps_remaining` (the batch has SOME runs
already, but the deciding step/dist was never run, or was run with a
different command than the one rendered now) — both rung 11, `next_command`
the literal `hs-code --step <batch>/<step>[/<dist>] <argv...>` a driver runs
verbatim — or `step_red` (the deciding step/dist's LAST record failed): rung
10 with a null `next_command`, because a step is deterministic and the same
command re-run unchanged fails the same way; `next_action` names what to fix
and the command to re-run after. Every batch fully green reports `closed`, rung 0. A `runs.jsonl`
holding a line this reader cannot use (`port_runs.read_runs`'s own broken-
line detection — bad JSON, a non-object value, a record failing its own
shape check, or a partial tail) reports `runs_broken`, rung 2, naming the
broken line — append-only, the same "fix it by hand, never rewrite it"
policy the port ledger's own `ledger_broken` already carries. A batch whose
OWN `batch.yaml` fails to load (an unknown top-level key, an unknown
`steps:` id, a bad `pinned_sha`, ...) reports `config_invalid`, rung 2,
naming the batch and every problem `port_batch_reader` found. A batch
`hs-code port done` closed (`port_runs.closed_mark`) is not walked: it is
listed with its mark (`done`: ts, actor, reason, the steps not green when it
closed) and no steps, and counts as finished for `closed`.
"""
from __future__ import annotations

from pathlib import Path

import hs_run_next_command
import port_batch_reader
import port_config
import port_handoff
import port_open
import port_runs
import port_steps
import port_workspace
import port_ledger_reader

import hs_run  # noqa: E402
from hs_run import EXIT_BROKEN, EXIT_DONE, EXIT_NEEDS_MODEL, EXIT_STEP_REMAINING  # noqa: E402


def _next(ns) -> dict:
    """Report the port's state — `.port/port.yaml` absent or invalid, nothing
    planned yet, or each declared batch's step lane (module docstring). Writes
    nothing; every write goes through the `hs-code` command it hands back.

    `ns.ctx.root` is deliberately IGNORED — the target this verb reads is resolved from
    `--repo`/the process CWD via `port_workspace.find_port_root`, never
    from hs-run's own project root. See the module docstring for why.
    """
    return hs_run_next_command.finalize(_cmd_next_impl(ns.repo))


NEXT = hs_run.verb(_next, options=("repo",))


def _cmd_next_impl(repo_raw) -> dict:
    repo_arg = Path(repo_raw) if repo_raw else None

    try:
        port_root = port_workspace.find_port_root(Path.cwd(), repo_arg)
    except port_workspace.BadRepoPathError as exc:
        # A bad --repo VALUE, not an unported repo — same state review-pr's
        # `_cmd_next_impl` uses for a malformed --poll-n/--pr: `error` +
        # EXIT_BROKEN, the CLI's own precedent for "the flag you passed is
        # wrong", distinct from `no_workspace` ("the flag was fine, nothing
        # is there yet").
        return hs_run.StepResult(
            state="error", exit_code=EXIT_BROKEN,
            next_action=str(exc),
        ).as_dict()
    except port_workspace.PortRootNotFoundError as exc:
        return hs_run.StepResult(
            state="no_workspace", exit_code=EXIT_NEEDS_MODEL,
            next_action="%s" % exc,
        ).as_dict()

    config_path = port_workspace.port_yaml_path(port_root)
    if not config_path.is_file():
        # `.port/` exists (the finder requires it) but `port.yaml` inside it
        # does not — still nothing planned for this repo yet.
        return hs_run.StepResult(
            state="no_workspace", exit_code=EXIT_NEEDS_MODEL,
            next_action="no `.port/port.yaml` under %s — run /hs:port to plan "
                        "a port" % port_root,
            extra={"port_root": str(port_root)},
        ).as_dict()

    cfg, problems = port_config.load_and_validate(config_path)
    if problems:
        extra = port_config.ConfigProblems(problems=problems).as_dict()
        extra["port_root"] = str(port_root)
        return hs_run.StepResult(
            state="config_invalid", exit_code=EXIT_BROKEN,
            next_action="%s — fix `.port/port.yaml` before `hs-run port next` "
                        "can proceed" % problems[0],
            extra=extra,
        ).as_dict()

    extra = {"port_root": str(port_root)}
    ledger_file = port_ledger_reader.ledger_path(port_root)
    if ledger_file.is_file():
        read = port_ledger_reader.read_ledger(ledger_file)
        extra["ledger"] = read.as_stats()
        if read.broken:
            extra["l0_findings"] = port_ledger_reader.broken_line_findings(read.broken)
            return hs_run.StepResult(
                state="ledger_broken", exit_code=EXIT_BROKEN,
                next_action="%d broken line(s) in %s — inspect and fix by "
                            "hand (append-only: never edit a line in place, "
                            "supersede it with a new one); see "
                            "`l0_findings`" % (len(read.broken), ledger_file),
                extra=extra,
            ).as_dict()

    batch_names = port_batch_reader.list_batches(port_root)
    if not batch_names:
        return hs_run.StepResult(
            state="no_batch", exit_code=EXIT_NEEDS_MODEL,
            next_action="`.port/port.yaml` is valid and no batch is planned yet "
                        "— run /hs:port to plan the first batch under "
                        ".port/batches/",
            extra=extra,
        ).as_dict()

    return _batches_state(port_root, batch_names, extra)


def _step_value(batch_name: str, step_id: str, dist) -> str:
    return "%s/%s%s" % (batch_name, step_id, ("/%s" % dist if dist else ""))


def _item_fact(item: "port_open.Item") -> str:
    """One line per pending item for the registry's `ask_user.facts` list
    (`hs_run._apply_ask_user_dynamic_facts` appends these onto the static
    block) — id, kind, subject, candidates, and the model's own proposal
    when one exists. No recommendation of its own: hs-run states the facts,
    the human picks."""
    line = "%s (%s) %s -- candidates: %s" % (
        item.id, item.kind, item.subject, ", ".join(item.candidates))
    if item.proposal:
        line += "; proposed by model: choice=%r scope=%s why=%r" % (
            item.proposal.get("choice"), item.proposal.get("scope"), item.proposal.get("why"))
    else:
        line += "; no proposal yet"
    return line


def _open_items_result(batch_name: str, pending: "list", extra: dict) -> dict:
    """`open_items`, rung 10 (`EXIT_NEEDS_MODEL`) — a real fork
    (`port_open`'s own open-items store) sits in `batch_name`'s own `open.jsonl`
    and nothing mechanical can settle it further. Checked ahead of every
    step decision (`batch_planned`/`step_red`/`steps_remaining`) — a step
    that failed BECAUSE it is waiting on this fork stays red forever on a
    re-run; only a model proposal + a human `approve`/`reject` moves it."""
    unproposed = [it for it in pending if it.proposal is None]
    if unproposed:
        next_action = (
            "propose a choice for %d open item(s) in batch %r — `hs-code port "
            "propose <id> --batch %s --choice <c> --why <reason> --apply` — then "
            "ask the human to approve or reject it"
            % (len(unproposed), batch_name, batch_name))
    else:
        next_action = (
            "ask the human to approve or reject %d open item(s) in batch %r — "
            "`hs-code port approve <id> --batch %s [--choice <c> | --reject --why "
            "<reason>] --apply`" % (len(pending), batch_name, batch_name))
    return hs_run.StepResult(
        state="open_items", exit_code=EXIT_NEEDS_MODEL, next_action=next_action,
        extra={
            **extra,
            "batch": batch_name,
            "open_items": [it.as_dict() for it in pending],
            "ask_user_facts": [_item_fact(it) for it in pending],
        },
    ).as_dict()


def _handoff_result(port_root: Path, batch: "port_batch_reader.Batch", batch_name: str,
                    extra: dict) -> "dict | None":
    """`None` when `batch_name` has no `handoff.json`, or one whose `items`
    list is empty — nothing for a person or `/hs:port` pass 2 to route yet,
    so the caller falls through to the ordinary step-lane read below.
    Raises `port_handoff.HandoffError` (mapped to `config_invalid` by the
    caller) for a `handoff.json` that exists but does not parse.

    Otherwise: `handoff_unplanned` (rung 10) when `batch` carries no
    `plan:`, or its `handoff_sha256:` disagrees with the file's CURRENT
    bytes (a step re-ran and changed the items since the plan was
    recorded) — `/hs:port` pass 2 is the only thing that clears it.
    `handoff_in_cook` (rung 10) once the recorded hash matches: the plan
    already exists, and each phase's own `done_when` command is the
    acceptance criterion `hs:cook` checks, so this engine hands off rather
    than re-deriving anything about the plan's own content."""
    doc = port_handoff.read_handoff(port_root, batch_name)
    if doc is None or not doc.items:
        return None
    current_hash = port_handoff.sha256_bytes(port_root, batch_name)
    if not batch.plan or batch.handoff_sha256 != current_hash:
        return hs_run.StepResult(
            state="handoff_unplanned", exit_code=EXIT_NEEDS_MODEL,
            next_action="/hs:port pass 2: turn handoff.json into plan phases, "
                        "then record `plan:` and `handoff_sha256:` in batch.yaml",
            extra={**extra, "batch": batch_name, "handoff_items": len(doc.items)},
        ).as_dict()
    return hs_run.StepResult(
        state="handoff_in_cook", exit_code=EXIT_NEEDS_MODEL,
        next_action="/hs:cook %s" % batch.plan,
        extra={**extra, "batch": batch_name, "plan": batch.plan,
              "handoff_items": len(doc.items)},
    ).as_dict()


def _batches_state(port_root: Path, batch_names, extra: dict) -> dict:
    """The lane state across every declared batch — see the module
    docstring's BATCHES section for the full contract. `extra` already
    carries `port_root` (and `ledger`, when a port ledger exists); this
    function only ever ADDS `batches` (and, on a hard error, the fields
    that error names) to it, never removes what the caller built."""
    step_table = port_steps.load_steps()
    # ONE function decides the project kind's extra steps/`{app}` value — the
    # same `port_config.extra_steps_for_port_root`/`step_values_for_port_root`
    # `port_runs.py`'s own `--step` handling calls, so the lane this verb
    # reports and the lane `hs-code --step` checks membership against can
    # never drift apart.
    extra_steps = port_config.extra_steps_for_port_root(port_root)
    omitted = port_config.omitted_steps_for_port_root(port_root)
    batches_extra: "list[dict]" = []

    for name in batch_names:
        try:
            batch = port_batch_reader.load_batch(port_root, name)
        except (port_batch_reader.BatchNotFoundError,
                port_batch_reader.BatchConfigError) as exc:
            bad_extra = dict(extra)
            bad_extra.update({"batch": name, "problems": [str(exc)]})
            return hs_run.StepResult(
                state="config_invalid", exit_code=EXIT_BROKEN,
                next_action="batch %r: %s" % (name, exc),
                extra=bad_extra,
            ).as_dict()

        try:
            closed = port_runs.closed_mark(port_root, name)
        except port_runs.DoneMarksError as exc:
            bad_extra = dict(extra)
            bad_extra.update({"batch": name, "problems": [str(exc)]})
            return hs_run.StepResult(
                state="config_invalid", exit_code=EXIT_BROKEN,
                next_action="batch %r: %s" % (name, exc),
                extra=bad_extra,
            ).as_dict()
        if closed is not None:
            # `hs-code port done` closed it: a claim, recorded with the steps
            # that were not green then — never walked again, so a lane that
            # grew a step afterwards does not reopen a finished batch.
            batches_extra.append(port_runs.BatchStanding(name=name, done=closed).as_dict())
            continue

        reads = port_runs.read_runs(port_root, name)
        if reads.broken:
            bad_extra = dict(extra)
            bad_extra.update({
                "batch": name,
                "run_findings": port_runs.broken_line_findings(reads.broken),
            })
            return hs_run.StepResult(
                state="runs_broken", exit_code=EXIT_BROKEN,
                next_action="%d broken line(s) in %s — inspect and fix by "
                            "hand (append-only: never edit a line in place, "
                            "supersede it with a new one); see "
                            "`run_findings`" % (len(reads.broken), reads.path),
                extra=bad_extra,
            ).as_dict()

        try:
            pending = port_open.pending_items(port_root, name)
        except port_open.OpenLedgerError as exc:
            bad_extra = dict(extra)
            bad_extra.update({"batch": name, "problems": [str(exc)]})
            return hs_run.StepResult(
                state="config_invalid", exit_code=EXIT_BROKEN,
                next_action="batch %r: %s" % (name, exc),
                extra=bad_extra,
            ).as_dict()
        if pending:
            # Checked BEFORE the step lane below is even read: a step whose
            # LAST run failed while an item sat pending would report
            # `step_red` forever — re-running the exact same command can
            # never turn it green, only a human's `approve`/`reject` can.
            return _open_items_result(name, pending, extra)

        try:
            handoff_result = _handoff_result(port_root, batch, name, extra)
        except port_handoff.HandoffError as exc:
            bad_extra = dict(extra)
            bad_extra.update({"batch": name, "problems": [str(exc)]})
            return hs_run.StepResult(
                state="config_invalid", exit_code=EXIT_BROKEN,
                next_action="batch %r: %s" % (name, exc),
                extra=bad_extra,
            ).as_dict()
        if handoff_result is not None:
            # Checked BEFORE the step lane below, same reasoning as
            # `open_items` above: a step whose last run failed while a
            # handoff item sits unplanned would otherwise report `step_red`
            # forever — re-running the exact same command never clears a
            # finding only `/hs:port` pass 2 or `/hs:cook` settles.
            return handoff_result

        last = port_runs.last_by_step(reads.records)
        total_runs = len(reads.records)
        try:
            lane_ids = port_steps.lane_of(batch.steps, step_table, extra_steps=extra_steps,
                                          omit_steps=tuple(omitted), lane=batch.lane)
        except port_steps.UnknownStepError as exc:
            bad_extra = dict(extra)
            bad_extra.update({"batch": name, "problems": [str(exc)]})
            return hs_run.StepResult(
                state="config_invalid", exit_code=EXIT_BROKEN,
                next_action="batch %r: %s" % (name, exc),
                extra=bad_extra,
            ).as_dict()

        steps_status: "list[port_runs.StepStatus]" = []
        decision = None  # (state, next_action, next_command, rung)

        for step_id in lane_ids:
            # Stop at the first non-green step, per batch — a step further
            # down the lane has not been REACHED yet (its own dist list, or
            # its own render inputs, may not even be declared until an
            # earlier step's output exists), so checking it before the
            # deciding step is fixed would report a config problem about
            # work nobody is being asked to do yet.
            if decision is not None:
                break

            stepdef = step_table.steps.get(step_id)
            if stepdef is None:
                bad_extra = dict(extra)
                bad_extra["batch"] = name
                return hs_run.StepResult(
                    state="config_invalid", exit_code=EXIT_BROKEN,
                    next_action="batch %r names step id %r not declared "
                                "under `steps:` in port-steps.yaml (known: "
                                "%s)" % (name, step_id,
                                        ", ".join(sorted(step_table.known_ids))),
                    extra=bad_extra,
                ).as_dict()

            dists = list(batch.dists) if stepdef.per_dist else [None]
            if stepdef.per_dist and not dists:
                bad_extra = dict(extra)
                bad_extra["batch"] = name
                return hs_run.StepResult(
                    state="config_invalid", exit_code=EXIT_BROKEN,
                    next_action="batch %r's step %r needs `dists:` declared "
                                "— it is a per-dist step and the batch "
                                "declares none" % (name, step_id),
                    extra=bad_extra,
                ).as_dict()

            for dist in dists:
                if decision is not None:
                    break
                try:
                    rendered = port_steps.render_argv(
                        stepdef, batch=batch, port_root=port_root, dist=dist, values=port_config.step_values_for_port_root(port_root, batch))
                except port_steps.StepRenderError as exc:
                    bad_extra = dict(extra)
                    bad_extra["batch"] = name
                    return hs_run.StepResult(
                        state="config_invalid", exit_code=EXIT_BROKEN,
                        next_action="batch %r: %s" % (name, exc),
                        extra=bad_extra,
                    ).as_dict()

                rec = last.get((step_id, dist))
                green = port_runs.ran_green(rec, rendered)
                if green:
                    status = "green"
                elif rec is not None and rec.exit_code != 0:
                    status = "red"
                else:
                    status = "pending"
                steps_status.append(port_runs.StepStatus(
                    id=step_id, dist=dist, status=status,
                    last_exit=rec.exit_code if rec is not None else None,
                ))

                if decision is None and not green:
                    step_value = _step_value(name, step_id, dist)
                    next_command = "hs-code --step %s %s" % (
                        step_value, " ".join(rendered))
                    dist_note = " [%s]" % dist if dist else ""
                    # Plain literal assigns, one per branch, on PURPOSE — never
                    # collapsed into a tuple literal: `_emitted_states()` (the
                    # conformance suite's AST sweep) traces a bare `state =
                    # "word"` assign back to a `state=state` kwarg, the same
                    # shape `plan graph`'s `absent`/`malformed`/`invalid` ladder
                    # already relies on, but it does NOT descend into a tuple's
                    # own elements — `decision = ("step_red", ...)` would leave
                    # these three words invisible to that scan, so they end up
                    # in KNOWN_STATES only by accident (or, worse, drift out
                    # unnoticed once the drift IS accidental).
                    rung = EXIT_STEP_REMAINING
                    if rec is not None and rec.exit_code != 0:
                        # A step is deterministic: re-run unchanged, it fails the
                        # same way. Rung 10, no `next_command` — a `while exit ==
                        # 11` driver stops here instead of looping on the failure.
                        state = "step_red"
                        rung = EXIT_NEEDS_MODEL
                        next_action = (
                            "batch %r step %r%s last failed (exit %d) — fix what it "
                            "reported (or commit what it refused on), then re-run: `%s`"
                            % (name, step_id, dist_note, rec.exit_code, next_command))
                    elif total_runs == 0:
                        state = "batch_planned"
                        next_action = (
                            "batch %r is planned, nothing run yet — run: `%s`"
                            % (name, next_command))
                    else:
                        state = "steps_remaining"
                        next_action = (
                            "batch %r step %r%s not yet run with the "
                            "current command — run: `%s`"
                            % (name, step_id, dist_note, next_command))
                    decision = (state, next_action,
                                next_command if rung == EXIT_STEP_REMAINING else None, rung)

        batches_extra.append(port_runs.BatchStanding(name=name, steps=tuple(steps_status)).as_dict())

        if decision is not None:
            state, next_action, handed, rung = decision
            done_extra = dict(extra)
            done_extra["batches"] = batches_extra
            return hs_run.StepResult(
                state=state, exit_code=rung,
                next_action=next_action, next_command=handed,
                warnings=list(omitted.values()), extra=done_extra,
            ).as_dict()

    closed_extra = dict(extra)
    closed_extra["batches"] = batches_extra
    return hs_run.StepResult(
        state="closed", exit_code=EXIT_DONE,
        next_action="every batch is green — the port workspace is closed",
        warnings=list(omitted.values()), extra=closed_extra,
    ).as_dict()


if __name__ == "__main__":  # pragma: no cover - refusal path, asserted by subprocess
    # Not a command. On this ladder `0` is a CLAIM — "nothing left to do" — so a
    # module that runs here, does nothing and exits 0 tells an automated caller
    # the work is finished. Refuse loudly on the broken rung instead.
    import sys as _sys
    _sys.stderr.write(
        "%s is a hs-run domain module, not a command.\n"
        "Verbs are dispatched through the CLI, which resolves the project root, "
        "loads the registry and owns the exit ladder — none of which happens when "
        "this file is run on its own.\n"
        "Run it through the dispatcher instead:  hs-run %s <verb>\n"
        % (Path(__file__).name, 'port'))
    raise SystemExit(EXIT_BROKEN)
