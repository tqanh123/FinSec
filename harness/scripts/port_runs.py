"""port_runs.py — the port domain's run ledger: ONE write door plus its
compatible reader, as one FLAT module under `harness/scripts/` (sibling of
`port_ledger_reader.py`/`port_ledger_writer.py`, same reasons: `hs_code.py`
— the shell every domain shares, not only `port` — calls into this module
through its own `--step` flag, and `hs_run_port.py` reads the same file
back to compute a batch's lane status; neither caller should inherit the
`codegraph` measurement engine to do either).

LAYOUT — one runs ledger PER BATCH (never one per repo, unlike `.port/
ledger.jsonl`): `<port_root>/.port/batches/<batch>/runs.jsonl`. A batch's
own step history belongs with the rest of its own declared state
(`batch.yaml`, `specs/`, `map/`), not mixed into a different batch's.

ONE RECORD PER `hs-code ... --step BATCH/STEP[/DIST]` CALL — append-only,
never a read-modify-write (CLAUDE.md's own store discipline): `{ts, actor,
batch, step, dist, argv, exit_code}`. `dist` is `null` for a batch-wide
step. `argv` is the command WORDS AS RUN (domain, verb, every arg —
`--apply` included when the caller passed it — with only `--step BATCH/
STEP[/DIST]` itself removed): this is what `hs_run_port.py` compares
against its own freshly-RENDERED command to decide whether a step is
green, so it must be the caller's literal words, never a re-derivation.

WHY A SEPARATE FILE FROM `.port/ledger.jsonl`: the port LEDGER is a record
of PORTED CODE's state (one line per node, replayed to the node's current
status); a RUN is a record of ONE COMMAND HAVING BEEN EXECUTED, keyed by
step/dist, replayed to "is this step green" by taking the LAST record per
key — a completely different fold. Sharing one file would mean either
reader has to skip records meant for the other, which is how a broken
line in one domain's records reads as belonging to both.

ALLOWED IMPORTS: the stdlib, `hook_runtime` (`resolve_actor` — the same
door `codegraph_port_ledger_verbs.py` already uses for the sibling
ledger), and `port_ledger_writer` (`now_ts` only — a plain function, not
its `Ledger` write door, which this module does not share). Nothing under
`codegraph`: `hs_run_port.py` imports this module directly.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import port_batch_reader  # noqa: E402
from hook_runtime import resolve_actor  # noqa: E402
from port_ledger_writer import now_ts  # noqa: E402

#: (line number, raw text, error) — kept verbatim, never swallowed; a run
#: ledger a reader silently repairs is a run ledger that lies about which
#: step actually ran.
BrokenRunLine = tuple

#: Every key one run record must carry. A record missing one, or carrying
#: an extra one, is broken — this door writes a closed shape, and the
#: reader holds every writer (including a hand-edited line) to the same one.
_RECORD_KEYS: "frozenset[str]" = frozenset(
    {"ts", "actor", "batch", "step", "dist", "argv", "exit_code"})


@dataclass(frozen=True)
class StepStatus:
    """One lane step's standing as `hs-run port next` reports it: `green`
    (last run exited 0 with the command handed out), `red` (last run failed)
    or `pending` (never run, or run with another command)."""
    id: str
    dist: "str | None"
    status: str
    last_exit: "int | None"

    def as_dict(self) -> dict:
        return {"id": self.id, "dist": self.dist, "status": self.status,
                "last_exit": self.last_exit}


@dataclass(frozen=True)
class RunRecord:
    ts: str
    actor: str
    batch: str
    step: str
    dist: "str | None"
    argv: "tuple[str, ...]"
    exit_code: int

    def as_dict(self) -> dict:
        return {"ts": self.ts, "actor": self.actor, "batch": self.batch,
                "step": self.step, "dist": self.dist, "argv": list(self.argv),
                "exit_code": self.exit_code}

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=False)

    @classmethod
    def from_dict(cls, d: "dict[str, Any]") -> "RunRecord":
        extra = set(d) - _RECORD_KEYS
        missing = _RECORD_KEYS - set(d)
        if extra or missing:
            problems = []
            if missing:
                problems.append("missing key(s) %s" % sorted(missing))
            if extra:
                problems.append("unknown key(s) %s" % sorted(extra))
            raise ValueError("; ".join(problems))
        if not isinstance(d["batch"], str) or not d["batch"]:
            raise ValueError("`batch` must be a non-empty string")
        if not isinstance(d["step"], str) or not d["step"]:
            raise ValueError("`step` must be a non-empty string")
        if d["dist"] is not None and not isinstance(d["dist"], str):
            raise ValueError("`dist` must be a string or null")
        if not isinstance(d["argv"], list) or not all(isinstance(w, str) for w in d["argv"]):
            raise ValueError("`argv` must be a list of strings")
        if isinstance(d["exit_code"], bool) or not isinstance(d["exit_code"], int):
            raise ValueError("`exit_code` must be an int")
        if not isinstance(d["ts"], str) or not d["ts"]:
            raise ValueError("`ts` must be a non-empty string")
        if not isinstance(d["actor"], str) or not d["actor"]:
            raise ValueError("`actor` must be a non-empty string")
        return cls(ts=d["ts"], actor=d["actor"], batch=d["batch"], step=d["step"],
                  dist=d["dist"], argv=tuple(d["argv"]), exit_code=d["exit_code"])


def runs_path(port_root: "str | Path", batch: str) -> Path:
    """`<port_root>/.port/batches/<batch>/runs.jsonl` — never a caller-typed
    path; `batch` names a directory that must already exist as a real batch
    (the caller checks that BEFORE calling `append_run`, same discipline
    every other port write door uses)."""
    return port_batch_reader.batches_dir(port_root) / batch / "runs.jsonl"


@dataclass
class RunsReadResult:
    path: Path
    records: "list[RunRecord]" = field(default_factory=list)
    broken: "list[BrokenRunLine]" = field(default_factory=list)

    @property
    def n_broken(self) -> int:
        return len(self.broken)


def read_runs(port_root: "str | Path", batch: str) -> RunsReadResult:
    """Stream `<port_root>/.port/batches/<batch>/runs.jsonl` line by line.
    A missing file reads as an empty result (0 records, 0 broken) — no runs
    yet is a normal state, not an error. Broken-line detection mirrors
    `port_ledger_reader.read_ledger` exactly: bad JSON, a non-object value,
    an object failing `RunRecord.from_dict`, or a final line with no
    trailing newline (an interrupted write) — every one kept VERBATIM in
    `.broken`, never dropped."""
    path = runs_path(port_root, batch)
    result = RunsReadResult(path=path)
    if not path.is_file():
        return result

    with path.open("r", encoding="utf-8", newline="") as fh:
        lineno = 0
        for raw in fh:
            lineno += 1
            terminated = raw.endswith("\n")
            if not terminated:
                if raw:
                    result.broken.append((
                        lineno, raw,
                        "partial tail: file does not end with a newline "
                        "(interrupted write?)"))
                break
            content = raw[:-1].strip()
            if not content:
                continue
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                result.broken.append((lineno, content, "bad JSON: %s" % exc))
                continue
            if not isinstance(parsed, dict):
                result.broken.append((
                    lineno, content,
                    "bad record: not a JSON object (got %s)" % type(parsed).__name__))
                continue
            try:
                result.records.append(RunRecord.from_dict(parsed))
            except (KeyError, ValueError, TypeError) as exc:
                result.broken.append((lineno, content, "bad record: %s" % exc))
    return result


def broken_line_findings(broken: "list[BrokenRunLine]") -> "list[dict]":
    """`[{line, message}, ...]` — the same shape `port_ledger_reader.
    broken_line_findings` uses for the sibling ledger, minus the law code
    (a run ledger has no L0-style rule table of its own)."""
    return [{"line": lineno, "message": why} for lineno, _raw, why in broken]


def last_by_step(records: "list[RunRecord]") -> "dict[tuple[str, str | None], RunRecord]":
    """`{(step, dist): record}` — the LAST record for each (step, dist) key,
    in FILE order (append order is the only order a run ledger promises,
    same reasoning `ts` carries in the sibling port ledger: two skewed
    writing machines make `ts` untrustworthy for ordering)."""
    out: "dict[tuple[str, str | None], RunRecord]" = {}
    for rec in records:
        out[(rec.step, rec.dist)] = rec
    return out


def append_run(port_root: "str | Path", batch: str, step: str, dist: "str | None",
               argv: "list[str]", exit_code: int) -> RunRecord:
    """Append ONE record — the run ledger's only write door. Caller already
    checked `batch` names a real batch (this function does not re-check;
    `hs_code.py`'s `bind()` does that BEFORE the verb even runs, so a
    refusal never leaves a half-run command behind)."""
    path = runs_path(port_root, batch)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = RunRecord(ts=now_ts(), actor=resolve_actor(), batch=batch, step=step,
                       dist=dist, argv=tuple(argv), exit_code=int(exit_code))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record.as_json() + "\n")
    return record


def parse_step_flag(port_root: "str | Path", value: str):
    """`value` (`BATCH/STEP[/DIST]`) -> the loaded `port_batch_reader.Batch`
    for its batch name, plus `(step, dist)`. Raises `hs_family.UsageError`
    (exit 2) when the shape is wrong OR the named batch does not exist —
    this is the pre-run refusal `hs_code.py`'s `bind()` calls BEFORE the
    verb runs, so `--step` naming an unknown batch never runs the command
    at all."""
    import hs_family
    import port_config
    import port_steps
    import port_batch_reader

    try:
        batch_name, step, dist = port_steps.parse_step_value(value)
    except ValueError as exc:
        raise hs_family.UsageError(str(exc)) from exc

    try:
        batch = port_batch_reader.load_batch(port_root, batch_name)
    except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as exc:
        raise hs_family.UsageError("--step %r: %s" % (value, exc)) from exc
    try:
        extra_steps = port_config.extra_steps_for_port_root(port_root)
        lane = port_steps.lane_of(batch.steps, port_steps.load_steps(), extra_steps=extra_steps,
                                  omit_steps=tuple(port_config.omitted_steps_for_port_root(port_root)),
                                  lane=batch.lane)
    except (port_steps.UnknownStepError, port_steps.PortStepsConfigError) as exc:
        raise hs_family.UsageError("--step %r: batch %r: %s" % (value, batch_name, exc)) from exc
    if step not in lane:
        raise hs_family.UsageError("--step %r: step %r is not in batch %r's lane (%s)"
                                   % (value, step, batch_name, ", ".join(lane)))
    return batch, step, dist


#: hs-code's shell flags that ARE part of a call (`ShellFlag.identity`): accepted
#: anywhere on the command line, so where one was typed says nothing about the call.
STEP_SHELL_FLAGS = ("--apply", "--fresh", "--allow-stale")


def _canonical(argv: "list[str]") -> "tuple[list[str], list[str]]":
    words: "list[str]" = []
    flags: "set[str]" = set()
    for i, w in enumerate(argv):
        if w == "--":
            words.extend(argv[i:])
            break
        if w in STEP_SHELL_FLAGS:
            flags.add(w)
        else:
            words.append(w)
    return words, sorted(flags)


def same_call(recorded, rendered) -> bool:
    """A recorded run's argv is the rendered step command: the same words in
    order, the same `STEP_SHELL_FLAGS` wherever they were typed."""
    return _canonical(list(recorded)) == _canonical(list(rendered))


def ran_green(rec: "RunRecord | None", rendered: "list[str]") -> bool:
    """The ONE green criterion of a step: its last run exited 0 AND was the
    command rendered right now (`hs_run_port.py`'s lane walk and
    `green_step_ids` both ask this)."""
    return rec is not None and rec.exit_code == 0 and same_call(rec.argv, rendered)


def lane_step_ids(port_root: "str | Path", batch: "port_batch_reader.Batch") -> "list[str]":
    """`batch`'s lane as `hs_run_port.py` walks it (its `steps:` pick or its
    named lane, plus the project kind's steps, minus the omitted ones); an
    unknown step id reads as no lane."""
    import port_config
    import port_steps
    try:
        return port_steps.lane_of(batch.steps, port_steps.load_steps(),
                                  extra_steps=port_config.extra_steps_for_port_root(port_root),
                                  omit_steps=tuple(port_config.omitted_steps_for_port_root(port_root)),
                                  lane=batch.lane)
    except port_steps.UnknownStepError:
        return []


def open_step_ids(port_root: "str | Path", batch: "port_batch_reader.Batch") -> "list[str]":
    """The lane steps not green right now, in lane order."""
    green = set(green_step_ids(port_root, batch))
    return [s for s in lane_step_ids(port_root, batch) if s not in green]


def green_step_ids(port_root: "str | Path", batch: "port_batch_reader.Batch") -> "list[str]":
    """Every id of `batch`'s own lane (its `steps:` pick, or `port-steps.
    yaml`'s standard lane) whose LAST recorded run is green — the SAME
    criterion `hs_run_port.py`'s own batch-lane walk uses (exit 0 AND argv
    equal to the command freshly rendered right now): a per-dist step
    counts only when EVERY one of `batch.dists` is itself green. For the
    `mechanical_done` field of `.port/batches/<b>/handoff.json`
    (`port_handoff.write_step_items`'s caller in `hs_code.py`) — a
    read-only projection, never a second decision engine next to
    `hs_run_port.py`'s own (that module stays the ONE place a driver reads
    "what should I run next"; this only records "what already went green"
    for a document a human reads)."""
    import port_config
    import port_steps

    reads = read_runs(port_root, batch.name)
    last = last_by_step(reads.records)
    table = port_steps.load_steps()
    lane_ids = lane_step_ids(port_root, batch)
    out: "list[str]" = []
    for step_id in lane_ids:
        stepdef = table.steps.get(step_id)
        if stepdef is None:
            continue
        dists = list(batch.dists) if stepdef.per_dist else [None]
        if stepdef.per_dist and not dists:
            continue
        all_green = True
        for dist in dists:
            try:
                rendered = port_steps.render_argv(stepdef, batch=batch, port_root=port_root,
                                                  dist=dist, values=port_config.step_values_for_port_root(port_root, batch))
            except port_steps.StepRenderError:
                all_green = False
                break
            rec = last.get((step_id, dist))
            if not ran_green(rec, rendered):
                all_green = False
                break
        if all_green:
            out.append(step_id)
    return out


def resolve_step(target_root: "str | Path", value: str):
    """`(port_root, batch, step, dist)` for `--step BATCH/STEP[/DIST]`,
    resolving the port workspace from `target_root` (`hs-code`'s own
    `--repo`/project-root resolution) exactly like every other port reader
    walks up from the operator's own working tree — never a second, looser
    resolution rule for this one flag. Raises `hs_family.UsageError` (exit
    2) for a bad shape, no `.port/` workspace at or above `target_root`, or
    a batch name that does not exist — `hs_code.py`'s `bind()` calls this
    BEFORE the verb runs, so the refusal happens before any work does."""
    import hs_family
    import port_workspace

    try:
        port_root = port_workspace.find_port_root(Path(target_root))
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as exc:
        raise hs_family.UsageError("--step %r: %s" % (value, exc)) from exc
    batch, step, dist = parse_step_flag(port_root, value)
    return port_root, batch, step, dist


# ── batch closure: `port done` marks, append-only ────────────────────────────

DONE_STATES = ("done", "reopened")
_DONE_KEYS = frozenset({"ts", "actor", "batch", "state", "reason", "open_steps"})


class DoneMarksError(ValueError):
    """`done.jsonl` holds a line this reader cannot use — fix it by hand."""


def done_path(port_root: "str | Path", batch: str) -> Path:
    """`<port_root>/.port/batches/<batch>/done.jsonl`, next to `runs.jsonl`:
    whether a batch is closed is part of its own run history."""
    return port_batch_reader.batches_dir(port_root) / batch / "done.jsonl"


@dataclass(frozen=True)
class DoneMark:
    """One line of `done.jsonl`: a batch closed (`done`) or opened again
    (`reopened`), with the steps that were not green at the time."""
    ts: str
    actor: str
    batch: str
    state: str
    reason: "str | None"
    open_steps: "tuple[str, ...]"

    def as_dict(self) -> dict:
        return {"ts": self.ts, "actor": self.actor, "batch": self.batch, "state": self.state,
                "reason": self.reason, "open_steps": list(self.open_steps)}

    def summary(self) -> dict:
        """What `hs-run port next` shows of the mark a closed batch stands on."""
        return {k: v for k, v in self.as_dict().items() if k not in ("batch", "state")}


@dataclass(frozen=True)
class BatchStanding:
    """One batch's row in `hs-run port next`'s `batches`: its lane steps, or —
    once `port done` closed it — no walk and the mark it was closed with."""
    name: str
    steps: "tuple[StepStatus, ...]" = ()
    done: "DoneMark | None" = None

    def as_dict(self) -> dict:
        out: dict = {"name": self.name, "steps": [s.as_dict() for s in self.steps]}
        if self.done is not None:
            out["done"] = self.done.summary()
        return out


def read_done_marks(port_root: "str | Path", batch: str) -> "list[DoneMark]":
    path = done_path(port_root, batch)
    if not path.is_file():
        return []
    out: "list[DoneMark]" = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            d = json.loads(raw)
        except ValueError as e:
            raise DoneMarksError("%s:%d: not JSON (%s)" % (path, n, e)) from e
        if not isinstance(d, dict) or set(d) != _DONE_KEYS or d["state"] not in DONE_STATES \
                or not isinstance(d["open_steps"], list):
            raise DoneMarksError("%s:%d: not a done mark (keys %s, state one of %s)"
                                 % (path, n, sorted(_DONE_KEYS), "/".join(DONE_STATES)))
        out.append(DoneMark(**{**d, "open_steps": tuple(d["open_steps"])}))
    return out


def closed_mark(port_root: "str | Path", batch: str) -> "DoneMark | None":
    """The mark in force when the batch is closed (its last mark is `done`)."""
    marks = read_done_marks(port_root, batch)
    return marks[-1] if marks and marks[-1].state == "done" else None


def append_done_mark(port_root: "str | Path", batch: str, state: str, reason: "str | None",
                     open_steps: "list[str]") -> DoneMark:
    """Append ONE mark — the closure record's only write door."""
    if state not in DONE_STATES:
        raise ValueError("state must be one of %s" % "/".join(DONE_STATES))
    mark = DoneMark(ts=now_ts(), actor=resolve_actor(), batch=batch, state=state,
                    reason=reason, open_steps=tuple(open_steps))
    path = done_path(port_root, batch)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(mark.as_dict(), sort_keys=True, ensure_ascii=False) + "\n")
    return mark
