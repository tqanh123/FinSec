#!/usr/bin/env python3
"""hs_run_plan.py — the `plan` domain's verb entry points for hs-run.

The full plan
state machine — `scaffold`, `graph`, `prompt <role>`, `record <role>`, `next`.
The first cut shipped only `next`'s honest "nothing/something exists" floor
(see git history); this module replaces that floor with the real per-artifact
projection the plan lifecycle needs, while staying exactly as
honest about what it *cannot* prove (the hard cases below).

A `status` verb once sat beside `next` here — bare call, wrote nothing,
reported the same state `next` already had, differing only in exit rung.
Retired under USR-HARNESS-HS-RUN-ONE-DOOR (`hs-run <domain> next` is the one
door; a second verb earns its row only by needing an argument `next` cannot
invent, mutating on success, or answering a different question). The one
thing it carried that `next` did not — the full per-step checklist and its
one-line summary — now rides on `next` itself, via `_project()`.

Every verb WRAPS an existing script/module (survival invariant #4):
  - `scaffold`  -> scaffold.scaffold_plan()
  - `graph`     -> plan_graph.py's own read functions (parse/find_*/lint_*)
  - `prompt`/`record` -> print/ingest around a Task-tool subagent spawn the CLI
    itself can never make (the two-beat pattern) — researcher/planner/
    red-teamer roles, matching recon table A rows 3/5b+5f/7a.
  - `next` -> project purely from on-disk artifacts, never a progress
    file: re-run from scratch every call, so a deleted artifact
    correctly regresses the projected state instead of lying about it.

Nothing here re-implements a wrapped script's OWN judgment (e.g. plan_graph.py's
cycle/ordering-hazard math, scaffold.py's slug/id validation) — this module only
orders those calls, resolves *which* plan dir they apply to, and normalizes each
wrapped script's exit convention onto hs-run's own {0, 1, 2, 3, 10, 11} contract (goal
contract) rather than forwarding a raw exit code. The exit-code mapping, the
per-state disk signals, and which existing module owns each judgment this module
used to (wrongly) re-derive itself — closed defects: C0/F10 (frontmatter
`status:` was read FIRST and short-circuited every artifact check — now
corroboration only, disagreement is a first-class `state_conflict`), C1/F2 (a
stale/foreign/REJECTED approval was accepted — now delegates to
`artifact_check._check_plan_approval`), C3 (the `TBD` scan grepped the whole
file, including scaffold.py's own permanent `branch: TBD` frontmatter default,
livelocking `needs_planner` forever), C5 (five re-implemented judgments now
delegate to their real owners), F5 (three frontmatter readers disagreeing on a
leading blank line — now the one shared `frontmatter_parser`), F11 (`--persona`
was unvalidated and could write outside `plans/`), F12 (the research gate
vanished once content was filled first), F13 (`--fast` left no machine-readable
trace of what it skipped), F14 (`--plan` followed by another flag silently
swallowed that flag as the plan-dir literal).

The six-rung exit ladder (D10) is IMPORTED from `hs_run.py`,
which owns it; so is the verb declaration. The per-module copies this line
once described were retired after they drifted, and this module depends on
`hs_run.py` at import time deliberately. `EXIT_NEEDS_STEP` (11, "one more deterministic step remains,
run `next_action` and call again") is defined for contract completeness but no
plan-domain state currently returns it: every non-terminal state this module
projects genuinely needs a MODEL turn (spawn a subagent, or judge the Hard
Cases/re-review a drifted approval) — the deterministic-step-remaining cases
belong to `cook` (`needs_open`/`needs_close`/...), not to `plan`.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

# Wrap, never rewrite (survival invariant #4): every judgment
# this module used to re-derive itself now delegates to the module that
# actually owns it — imported at module level, matching hs_run_cook.py's own
# precedent for the same shared modules.
import artifact_check  # noqa: E402
import derive_plan_completion  # noqa: E402
import frontmatter_parser  # noqa: E402
import fs_guard  # noqa: E402
import plan_layout_check  # noqa: E402
import plan_status  # noqa: E402
import redteam_disposition  # noqa: E402

# The six-rung ladder (D10), IMPORTED rather than re-declared.
# Ten modules used to hand-copy these four constants and had already drifted:
# rung 11 carried two different names across the tree and two modules declared
# the same constant twice. `hs_run` owns the ladder as a closed `Exit` IntEnum;
# both historical spellings of rung 11 are bound to the same member there, so
# this import changes no name any caller in this file uses.
import attrs as _attrs  # noqa: E402  (REQUIRED — see harness/requirements.txt)
import hs_run  # noqa: E402 — the verb declaration lives here
from hs_run import (EXIT_BROKEN, EXIT_DONE, EXIT_FINDINGS, EXIT_NEEDS_MODEL,  # noqa: E402
                     EXIT_NEEDS_STEP, EXIT_REFUSED)

# A red-team-only personas are lowercase-kebab; this is what a filename built
# from `--persona` is allowed to contain (goal F11) — no `/`, no `..`, no
# whitespace, so the value can never escape the `reports/` dir it is placed in.
_PERSONA_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# ------------------------------------------------------------- disk discovery


def _plan_dirs(root: Path) -> list:
    """Every `plans/<id>-<slug>/` directory that carries a `plan.md` — the one
    disk signal that is unconditionally present the moment `scaffold.py plan`
    (this module's own `SCAFFOLD`) has run."""
    plans_dir = Path(root) / "plans"
    if not plans_dir.is_dir():
        return []
    return sorted(p for p in plans_dir.iterdir()
                  if p.is_dir() and (p / "plan.md").is_file())


def _approval_cmd() -> str:
    """`plan_approval.py` as the reader can run it: the bin's absolute script
    under the interpreter the launchers use, never `python3 harness/scripts/...`,
    which exists only in a repo that vendors the harness at ./harness."""
    import runnable_command
    return runnable_command.python_script("plan_approval.py")


def _phase_files(plan_dir: Path) -> list:
    d = Path(plan_dir) / "phases"
    if not d.is_dir():
        return []
    return sorted(d.glob("phase-*.md"))


def _contains_tbd(path: Path) -> bool:
    """True if `path`'s BODY still carries the literal `TBD` placeholder, OR
    the file could not be parsed at all (an honest "cannot confirm filled"
    default — never silently treated as filled).

    Frontmatter is EXCLUDED from this scan (closes C3): scaffold.py's own CLI
    default (`--branch TBD`, mirrored at this module's own `SCAFFOLD`) and
    both `plan-template.md`/`phase-template.md` put a legitimate, PERMANENT
    `TBD`-shaped placeholder in frontmatter (`branch:`) that no author is ever
    asked to replace. Grepping the whole file turned that into an infinite
    `needs_planner` livelock — verified live against two completed plans in
    this repo, both `status: completed` with a permanent `branch: TBD` line
    and zero `TBD` left in the body.
    Delegates the frontmatter/body split to `frontmatter_parser.parse_file` —
    the one owner — rather than re-deriving where it falls.

    A PLACEHOLDER, not the letters. `"TBD" in body` scored any sentence
    mentioning the marker as an unfilled cell, including a sentence saying the
    cells were filled ("Mọi ô TBD của phase-1 đã được điền xong" → unfilled), and
    any template quoted in a fenced block. That is not a cosmetic
    misclassification: this feeds `needs_planner`, which is the state that sends
    an agent to fill the file in — so the agent fills it, explains what it filled,
    mentions the marker while explaining, and is sent back. The livelock's cause
    is invisible because the file looks complete to the person reading it.

    So the marker counts only where a scaffold actually leaves one: alone on its
    line, or as the whole value of a list item or table cell. Prose around it is
    prose. Fenced blocks are stripped first, because a plan documenting its own
    scaffolding quotes the template verbatim."""
    parsed = frontmatter_parser.parse_file(path)
    if parsed.get("ok"):
        return _has_placeholder(parsed.get("body", ""))
    # No parseable frontmatter block (or a read/parse failure): fall back to
    # the raw text rather than silently declaring an unreadable file "filled".
    try:
        return _has_placeholder(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return True


# A scaffold leaves its marker in exactly these shapes: alone on the line, as a
# whole list item, or as a whole table cell. Everything else containing the
# letters is somebody TALKING about it.
_FENCE = re.compile(r"^\s*(```|~~~).*?^\s*\1", re.M | re.S)
# A line whose whole VALUE is the marker. The optional `label:` prefix is what a
# scaffold writes into a labelled bullet (`- owner: TBD`) — the marker is still
# the entire value, which is what makes it an empty cell rather than a mention.
#
# The label is capped at THREE words, and that cap is the whole design decision.
# `- owner: TBD` and `Ghi chú: chỗ này vẫn TBD` are the same SHAPE; only meaning
# separates them, and no pattern reads meaning. So the cap is set where a scaffold
# writes and a sentence does not: field labels are short, sentences run on. The
# residue is a deliberate choice of which way to be wrong — a three-word sentence
# ending in the marker is scored as an empty cell (an agent is sent to fill a line
# that is already prose: annoying, visible, self-correcting on the next edit)
# rather than an empty cell being scored as prose (a plan reaches approval with a
# blank field: quiet, and the gate exists to stop exactly that).
_BARE_MARKER = re.compile(
    r"(?:[-*+]\s+|\d+\.\s+|#{1,6}\s+)?"       # bullet / numbered / heading
    r"(?:\*{0,2}(?:\w[\w/-]*\s*){1,3}\*{0,2}:\s*)?"   # optional short `label:`
    r"TBD\.?")


def _has_placeholder(text: str) -> bool:
    """True when an UNFILLED cell is present, as opposed to the letters `TBD`."""
    stripped = _FENCE.sub("", text)
    for line in stripped.splitlines():
        bare = line.strip()
        if not bare:
            continue
        if bare.startswith("|") and bare.endswith("|"):
            # A table row is unfilled if any CELL is exactly the marker; a cell
            # reading "TBD until review" is a note, not an empty cell.
            if any(cell.strip() == "TBD" for cell in bare.strip("|").split("|")):
                return True
            continue
        if _BARE_MARKER.fullmatch(bare):
            return True
    return False


def _placeholder_files(plan_dir: Path) -> list:
    """plan.md, the phase files and the markdown under the plan's reference dirs
    (`plan_approval.reference_files` — the region the approval hash covers) that
    still carry a placeholder."""
    import plan_approval
    files = [f for f in [plan_dir / "plan.md"] + _phase_files(plan_dir) if f.is_file()]
    files += [f for f in plan_approval.reference_files(plan_dir) if f.suffix == ".md"]
    return [f for f in files if _contains_tbd(f)]


def _content_unfilled(plan_dir: Path) -> bool:
    """True while the design is not written: a TBD placeholder remains, or there
    is no phase file at all — zero phases is not a finished design, however
    complete plan.md reads (a planner retires the scaffold's placeholders before
    writing its own)."""
    plan_dir = Path(plan_dir)
    return not _phase_files(plan_dir) or bool(_placeholder_files(plan_dir))


def _unfilled_what(plan_dir: Path) -> str:
    """What `_content_unfilled` saw, in the words the next step needs."""
    if not _phase_files(plan_dir):
        return ("phases/ holds no phases/phase-*.md yet (the scaffold's placeholders "
                "were retired) — write the phase files")
    names = [f.relative_to(plan_dir).as_posix() for f in _placeholder_files(Path(plan_dir))]
    return "TBD placeholders remain in %s" % (", ".join(names) or "plan.md/phase content")


def _has_research(plan_dir: Path) -> bool:
    d = Path(plan_dir) / "research"
    return d.is_dir() and any(d.glob("*.md"))


def _redteam_reports(plan_dir: Path) -> list:
    """The red-team reports on disk, EMPTY FILES EXCLUDED.

    A zero-byte file is the residue of a step that started and did not finish —
    the touch-then-crash, the scaffold-then-forget. Counting it cleared the state
    and told the next reader an adversarial review had happened, which is the one
    thing this gate exists to say.

    The floor is EMPTINESS, not a size. Measured across the five red-team reports
    in this repo's history the smallest is 9,128 bytes, so any byte threshold
    would be arbitrary — and judging whether the CONTENT is a real review is not
    a CLI's job (`_redteam_findings` below says so at length, and still reports
    what it could not check). "There are no bytes in this file" needs no judgment.
    """
    return redteam_disposition.reports(plan_dir)


_REDTEAM_PERSONA = re.compile(
    r"^from-code-reviewer-to-planner-red-team-(.+)-plan-review-report\.md$")


def _redteam_findings(reports: list) -> list:
    """What the gate ACTUALLY checked, said out loud on the row.

    The gate is one glob. A file containing the single word `rác` clears it, and
    the board then prints `done: true` with nothing qualifying it — which the
    approval step and the human both read as "an adversarial review happened".

    Judging the CONTENT is not a CLI's job and a CLI that pretended otherwise
    would be worse than this. But the filename carries the persona, and `record`
    writes `unattributed` when nobody stated who wrote the report — the residue
    an inline same-session pass leaves. So the gate CAN separate an attributed
    report from an anonymous one without guessing at content, and saying which it
    saw costs nothing.

    Reasons, not a warning: a warning about every red-team report on every run is
    filtered out by the second reader. The row is where somebody looks when they
    want to know what backs the tick."""
    if not reports:
        return []
    said = ["gate counted %d report file(s) by name; their contents were not read "
            "and no check here can tell a real adversarial pass from a placeholder"
            % len(reports)]
    anonymous = sorted(
        p.name for p in reports
        if (_REDTEAM_PERSONA.match(p.name) or [None, ""])[1] == "unattributed")
    if anonymous:
        # Addressed to the reader as a DECISION, not as an observation. Blocking
        # here would be worse than useless: a blocked agent can write the missing
        # author line as easily as it can run the pass, so the gate would buy a
        # filename and call it independence. What the gate genuinely knows is
        # that it cannot tell the two apart — so it says which two, and hands the
        # judgment to whoever knows which one actually happened.
        said.append(
            "author unknown (persona `unattributed`) on: %s. Two different runs "
            "leave this same trace: an independent pass whose author was never "
            "recorded, and an inline same-session pass. Decide which one this "
            "was, then either record the author, run a real independent pass, or "
            "state that the inline pass was deliberate — the gate cannot tell "
            "them apart and will not pretend to"
            % ", ".join(anonymous))
    return said


# `skipped` on a checklist row answers one of TWO questions, and a caller that
# adds them together reads a history that never happened:
#   - flag-waived    — the user asked to bypass this gate (`--fast`)
#   - not-applicable — the gate does not apply to this plan at all (no grid)
# The envelope's `skipped[]` reports the FIRST kind only. The board keeps both,
# because a row that is not pending still owes the reader a reason.
#
# The kind is declared on the row, by the code that BUILDS it — that code already
# holds the answer (`bool(fast and not research_present)` IS the waiver) and used
# to throw it away, leaving a hand-written tuple of step names downstream to guess
# it back. The tuple's failure mode was a row missing from it: misfiled with no
# error, which is how the grid row ended up reading as a waiver nobody asked for.
WAIVED = "flag-waived"
NOT_APPLICABLE = "not-applicable"


def _resolve_mode(plan_dir: Path, flag_fast: bool):
    """Which mode this projection runs in, and whether the two sources disagree.

    The flag WINS. Typing it is a present-tense intent, and a projection that
    ignored it would be a second way to lie. What the disk buys is DURABILITY:
    before this, the mode lived in `argv` and nowhere else, so a plan that
    declared `mode: fast` and deliberately waived research reported itself as
    waiting on research the moment anyone ran a bare `next` — tomorrow morning,
    after a compaction, or from a session that never saw the original command.
    The skill body's promise that every state is re-derived from artifacts on
    disk was false for exactly this one field.

    Disagreement is LOUD. A flag that silently overrode the file left the file
    lying with nothing to notice it; the warning names both sides so the reader
    can fix whichever one is stale.

    Returns `(fast, conflict)`; `conflict` is None when the sources agree or the
    frontmatter never declared a mode — absence is not disagreement, and a plan
    written before the key existed must not start shouting."""
    fm = _frontmatter(Path(plan_dir) / "plan.md") or {}
    disk = str(fm.get("mode", "")).strip().lower() or None
    if flag_fast:
        if disk == "hard":
            return True, (
                "plan.md declares `mode: hard` but `--fast` was typed — running fast. "
                "Update the frontmatter or drop the flag; right now the file and the "
                "run disagree about which gates this plan owes.")
        return True, None
    if disk == "fast":
        return True, None
    return False, None


def _skipped_steps(plan_dir: Path, fast: bool, root=None) -> list:
    """Every gate `--fast` bypassed, READ off `_checklist` rather than re-derived.

    A skip must be visible in the JSON, never inferable only from a state name
    that looks identical to a fully-completed run — the skill body cites this
    field as the thing to read before believing a plan went through research and
    red-team.

    The hand-written version listed two of the three gates the flag removes:
    `scope_sizing` left the board with no trace, three feet from a `_checklist`
    that already carried a correct per-row boolean. Adding the third by hand was
    the patch to avoid — a ninth row would have reopened the same hole. One
    source, every channel: `next` and `record` cannot disagree because there
    is nothing left to disagree with.

    The row's own `skip_kind` filters, rather than the raw boolean, because
    `grid` is skipped on a plan that never turned the grid on — true with no flag
    typed, and a different question entirely. Asking the row rather than a list of
    step names means a row added later is classified by whoever knows the answer,
    not by whoever remembers to edit a tuple."""
    return [row["step"] for row in _checklist(plan_dir, fast, root)
            if row.get("skip_kind") == WAIVED]


def _approval_artifact(plan_dir: Path):
    art = Path(plan_dir) / "artifacts"
    for name in ("plan-approval.yaml", "plan-approval.json"):
        p = art / name
        if p.is_file():
            return p
    return None


def _read_json_or_yaml(path: Path) -> dict:
    """Best-effort structured read by extension — malformed/unreadable degrades
    to {} (an honest "cannot parse this", never a guessed verdict)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        if Path(path).suffix == ".yaml":
            import yaml_io
            rec = yaml_io.safe_load(text)
        else:
            import json
            rec = json.loads(text)
    except Exception:  # noqa: BLE001 — a malformed artifact is {} , never a crash
        return {}
    return rec if isinstance(rec, dict) else {}


def _frontmatter(plan_md: Path) -> dict:
    """The plan's YAML frontmatter as a dict. Delegates to
    `frontmatter_parser.parse_file` — the ONE owner of frontmatter reading
    (closes C5/F5/H6) — rather than a second hand-rolled regex.
    The previous `\\A---...` regex anchored to the true start of the file, so
    one leading blank line (or a BOM) made this module see NO frontmatter at
    all while `frontmatter_parser` (used by `cook` and by `artifact_check`)
    read it fine — the exact split-brain F5 reproduced. Malformed/unreadable/
    absent all degrade to {}, matching the old contract's own honest-empty
    default."""
    parsed = frontmatter_parser.parse_file(plan_md)
    fm = parsed.get("frontmatter")
    return fm if isinstance(fm, dict) else {}


def _approval_verdict(plan_dir: Path, rec: dict, root):
    """(verdict, reason) for an on-disk approval record already read into
    `rec`. `reason` is None exactly when the artifact is a currently-valid
    APPROVED verdict for THIS plan; any other string names why it is not.

    Delegates VALIDITY (plan binding + plan_hash drift, on top of the raw
    verdict) to `artifact_check._check_plan_approval` — the real owner of that
    judgment. Closes C1/F2: the previous
    `verdict == "APPROVED" or status == "approved"` let a plan.md `status:`
    line copied/left over from before override the approval ARTIFACT itself —
    accepting a stale (post-approval-drift) approval and even a REJECTED or
    another-plan's approval file. The artifact's OWN verdict/binding/drift are
    now the only authority; `status:` is never consulted here again."""
    verdict = str(rec.get("verdict") or "").strip().upper() or None
    try:
        reason = artifact_check._check_plan_approval(Path(plan_dir), rec, root)
    except Exception as e:  # noqa: BLE001 — a delegate fault degrades to an
        # honest "cannot confirm valid" rather than crashing next/status.
        reason = "cannot evaluate plan-approval validity: %s" % e
    return verdict, reason


def _is_drift_only_reason(plan_dir: Path, rec: dict) -> bool:
    """True when the ONLY reason `_check_plan_approval` rejected `rec` is
    post-approval content drift — verdict genuinely APPROVED, binding correct
    (this exact plan), only the plan_hash differs (goal R7).

    This does NOT re-derive `_check_plan_approval`'s validity judgment (that
    stays the sole authority) — it only CLASSIFIES why an
    already-known-invalid record failed, using the two cheap, already-present
    fields (`verdict`, `plan`) plus a call to `plan_approval.plan_hash` — the
    REAL owner's own hash function, not a re-implementation of the hash math —
    purely to tell "the story is stale" (approval_drifted, EXIT_NEEDS_MODEL)
    apart from "the story never matched" (state_conflict, EXIT_BROKEN). A
    binding mismatch or a non-APPROVED verdict is never drift-only, no matter
    what the hash says — those ARE a real frontmatter-vs-artifact conflict."""
    if str(rec.get("verdict") or "").strip().upper() != "APPROVED":
        return False
    if rec.get("plan") != Path(plan_dir).name:
        return False
    import plan_approval
    try:
        return plan_approval.drift_reason(plan_dir, rec) is not None
    except Exception:  # noqa: BLE001 — cannot confirm drift-only; fail closed
        # into the conservative state_conflict tier rather than risk mislabeling
        # a genuine conflict as a mere stale approval.
        return False


def _graph_state(plan_dir: Path):
    """(ok, reasons) for the plan-graph.yaml sidecar, using plan_graph.py's OWN
    parse/find_missing_post/find_cycles — never re-deriving that judgment here."""
    import plan_graph
    graph = plan_graph.parse_phase_graph(plan_dir)
    if graph.get("error"):
        return False, [graph["error"]]
    reasons = []
    missing = plan_graph.find_missing_post(graph)
    if missing:
        reasons.append("missing 'post' declaration for node(s): %s" % ", ".join(missing))
    reasons += plan_graph.find_post_problems(graph)
    cycles = plan_graph.find_cycles(graph)
    if cycles:
        reasons.append("cycle(s) detected: %s"
                        % "; ".join(" -> ".join(c) for c in cycles))
    return (not reasons), reasons


# The grid artifacts, each at a CANONICAL path under the plan's artifacts/ dir.
#
# `grid.json` is pinned HERE rather than left to the caller. `grid_engine.py build
# --out` has no default (it prints to stdout), and a working grid with no fixed home
# costs two things at once: the projection cannot tell "built, not yet expanded"
# from "never built", and the printed step cannot be a VERBATIM command, which is
# what separates exit 11 from exit 10. Pinning it buys both — three deterministic
# steps a driver can run without parsing a sentence for an embedded path.
_GRID_AXIS_SRC = "grid-axis-src.json"
_GRID_FILL_SRC = "grid-fill-src.json"
_GRID_WORKING = "grid.json"
_GRID_ARTIFACT = "coverage-grid.json"

# The scope-sizing receipt. The engine that used to write `sizing.*` into it is
# gone — the machine reports facts, never a split verdict, and the human's
# keep/split answer is recorded here by the plan-approval step alone
# (`_size_decision_receipt` below). The filename is unchanged so an OLDER
# receipt already on disk (carrying just `sizing.*`, from before this gate
# existed) still merges cleanly with a new `decided_by`/`decision` pair rather
# than colliding with it.
_SCOPE_RECEIPT = "scope-sizing.json"


# The plan-approval step's size-decision gate. Never a machine verdict a human
# answers: the machine gives no split recommendation at all — it only ever
# reports facts (`plan_facts.describe`), and the choice is the human's alone,
# made at the SAME approval step rather than a separate one (no new state, no
# new exit code — the shared `ask_user` block carries the facts instead).
_SIZE_DECISION_VALUES = ("keep", "split")


def _size_facts_for_ask_user(plan_dir: Path, root) -> "list[str]":
    """This plan's OWN size dashboard, as the dynamic lines the engine
    appends to `needs_model_attestation`'s registry-declared `ask_user.facts`
    (`hs_run._apply_ask_user_dynamic_facts`). Degrades to one honest line
    rather than raising: a facts dashboard that cannot be computed is not a
    reason to withhold the approval question itself, and `next` must still
    answer something machine-readable either way."""
    try:
        import plan_facts
        import plan_graph
    except Exception as e:  # noqa: BLE001 — a missing/broken module never blocks approval
        return ["plan size facts unavailable: %s: %s" % (type(e).__name__, e)]
    try:
        out = plan_facts.describe(plan_dir, plan_graph.parse_phase_graph,
                                  plan_graph.graph_size_facts,
                                  plan_graph.root_for_plan_dir, root=root)
    except Exception as e:  # noqa: BLE001 — same fail-open contract
        return ["plan size facts unavailable: %s: %s" % (type(e).__name__, e)]
    labels = out.get("labels")
    if not labels:
        return ["plan size facts unavailable: %s" % (out.get("error") or "no labels computed")]
    return [str(x) for x in labels]


def _size_decision_receipt(plan_dir: Path):
    """(ok, reason) for the human's keep/split decision. Two things must
    both hold: `decided_by` reads literally `"user"` — a machine-attributed
    receipt is REFUSED, never silently accepted — and `decision` is one of `keep`/`split`. Missing, unreadable
    or malformed all fold to the same honest `(False, reason)` a fresh plan
    has by default, matching `_read_json_or_yaml`'s own degrade-to-{}
    contract for every other artifact reader in this module."""
    receipt = plan_dir / "artifacts" / _SCOPE_RECEIPT
    if not receipt.is_file():
        return False, "no scope-sizing decision recorded at %s" % receipt
    rec = _read_json_or_yaml(receipt)
    decided_by = str(rec.get("decided_by") or "").strip().lower()
    if decided_by != "user":
        return False, ("%s's decided_by is %r, not \"user\" — a machine-attributed "
                       "size decision is refused" % (receipt, rec.get("decided_by")))
    decision = str(rec.get("decision") or "").strip().lower()
    if decision not in _SIZE_DECISION_VALUES:
        return False, ("%s's decision is %r, expected one of %s"
                       % (receipt, rec.get("decision"), _SIZE_DECISION_VALUES))
    return True, None


def _attestation_result(plan_dir: Path, root, *, extra_warning=None,
                        approved_pending_size: bool = False, notes=()) -> dict:
    """The `needs_model_attestation` envelope — the ONE place this state is
    built, so the "nothing else blocks yet" fallback below and the "approval
    exists but the size decision does not" re-emission (in the approval
    branch above this function, which passes `approved_pending_size=True`)
    stay a single source of prose apart from `next_action`'s ONE piece that
    genuinely differs by branch — the runnable command. `ask_user_facts`
    rides in `extra`, the domain-sets-a-key seam
    `hs_run._apply_ask_user_dynamic_facts` reads and pops — never a literal
    `ask_user` key, which `Envelope` already owns and would silently win
    over anything a verb sets on `result`.

    Both branches print a command that runs VERBATIM (`plan_approval.py
    --size-decision`, never a "write this JSON yourself" instruction) —
    hand-writing the receipt is what this closes: it overwrites `sizing`/
    `manifest`, an older sizing run's own output already on disk, instead
    of merging alongside it."""
    warnings = ["hs-run checks only that every `verdict: REVISE` red-team report's "
                "`id`-headed findings have a row in a `%s` table with no empty cell. "
                "It cannot verify that an Accept reached its phase, the validate "
                "interview, or the consistency sweep — an honest ceiling, not a "
                "silent pass" % redteam_disposition.HEADING]
    warnings.extend(notes)
    if extra_warning:
        warnings.append(extra_warning)
    if approved_pending_size:
        next_action = (
            "this plan is already APPROVED — that verdict stands and needs no "
            "re-review. Only the human's keep/split size decision is missing: this "
            "state's ask_user block hands you this plan's own size facts (no split "
            "recommendation). Record the choice with: %s --plan %s "
            "--size-decision keep|split" % (_approval_cmd(), plan_dir))
    else:
        next_action = (
            "content and plan-graph are disk-verified; a red-team report is on disk "
            "(or --fast skipped it) and no REVISE verdict is left undisposed. hs-run "
            "CANNOT verify from disk: each Accept reached its phase, the validate "
            "interview + Whole-Plan Consistency Sweep found "
            "0 contradictions, and any new term/decision is registered. Confirm those by "
            "reading the plan yourself. This state's ask_user block hands you this "
            "plan's own size facts (no split recommendation) and a keep/split choice. "
            "Record BOTH the verdict and the size choice in one call: %s --plan %s "
            "--verdict APPROVED|REJECTED --size-decision keep|split --rationale "
            "\"<why>\"" % (_approval_cmd(), plan_dir))
    return hs_run.StepResult(
        state="needs_model_attestation",
        exit_code=EXIT_NEEDS_MODEL,
        next_action=next_action,
        artifacts=[str(plan_dir)],
        warnings=warnings,
        extra={"ask_user_facts": _size_facts_for_ask_user(plan_dir, root)},
    ).as_dict()


def _grid_expanded(grid_path: Path) -> bool:
    """Has the fill loop actually run over this grid?

    Delegates to `grid_emit_guard` — the SAME predicate `plan_approval` and cook's
    Phase-DAG preflight already gate on. A second copy here would drift from both
    with nothing to notice, and each copy would read as correct on its own."""
    import grid_emit_guard
    try:
        return not grid_emit_guard._has_no_real_content(grid_path)
    except Exception:  # noqa: BLE001 — a read fault never fabricates a step
        return True


def _grid_state(plan_dir: Path):
    """The grid leg of the projection, or None when there is nothing to say.

    Returns None in the two cases that must stay silent: the plan never declared
    `grid:` (the flag is opt-in — routing a grid step on a plan that did not ask
    for one would block every plan in the repo on an artifact nobody wanted), and
    the grid is fully resolved (fall through to the rest of the chain).

    Whether `--grid` should have been declared at all is a judgment made before
    any of this; the flag on disk is the answer to it, not the question."""
    plan_dir = Path(plan_dir)
    try:
        fm = _frontmatter(plan_dir / "plan.md")
    except Exception:
        return None
    if not bool((fm or {}).get("grid")):
        return None

    art = plan_dir / "artifacts"
    axis_src = art / _GRID_AXIS_SRC
    fill_src = art / _GRID_FILL_SRC
    emitted = art / _GRID_ARTIFACT

    if not axis_src.is_file():
        return hs_run.StepResult(
            state="grid_needs_axes",
            next_action="grid mode is on and no axis receipt exists — spawn the axis selector "
                "(subagent_type \"hs:grid-axis-selector\"); it reads the plan's own prose "
                "and writes %s. Building on the canned axis defaults ships a grid whose "
                "values do not exist in this project." % axis_src,
            artifacts=[str(axis_src)],
            warnings=[],
            exit_code=EXIT_NEEDS_MODEL,
        ).as_dict()

    if not fill_src.is_file():
        return hs_run.StepResult(
            state="grid_needs_fill",
            next_action="axis receipt present, no fill source — spawn the filler "
                "(subagent_type \"hs:grid-filler\"); it writes %s. A cell it cannot "
                "ground stays an honest STUB with a reason." % fill_src,
            artifacts=[str(fill_src)],
            warnings=[],
            exit_code=EXIT_NEEDS_MODEL,
        ).as_dict()

    # The emitted artifact is checked BEFORE the working rungs. Once the coverage-grid
    # exists the intermediate grid has done its job, and asking for a rebuild because a
    # scratch file was cleaned up would regress a finished plan on a file nothing gates
    # on.
    if emitted.is_file():
        # "Is this grid actually filled" is grid_emit_guard's judgment — the SAME
        # predicate plan_approval and cook's preflight already gate on. A second copy
        # here would drift from both with nothing to notice, and each copy would look
        # right read on its own.
        if not _grid_expanded(emitted):
            return hs_run.StepResult(
                state="grid_unresolved",
                next_action="the coverage-grid exists but no cell carries real content — the "
                    "fill loop never ran (an unresolved fill source relays nothing and "
                    "every cell degrades to a STUB, which looks like an honest grid). "
                    "Re-run the fill with %s resolved, or attest each thin cell "
                    "[JUSTIFIED-THIN] IN THE ARTIFACT — a justification written only "
                    "into chat earns no credit." % fill_src,
                artifacts=[str(emitted)],
                warnings=[],
                exit_code=EXIT_NEEDS_MODEL,
            ).as_dict()
        return None

    # The three deterministic rungs. Each prints ONE verbatim-runnable command, which
    # is the promise exit 11 makes: a driver dispatching on the rung runs it without
    # parsing a human sentence for an embedded path.
    working = art / _GRID_WORKING
    engine = '"${HARNESS_BIN_ROOT:-.}"/harness/scripts/grid_engine.py'
    if not working.is_file():
        return hs_run.StepResult(
            state="grid_needs_build",
            next_action="fill source present, no skeleton grid — build it",
            next_command="python3 %s build --agent planner --plan-id %s --axes-json %s --out %s"
                % (engine, plan_dir.name, axis_src, working),
            artifacts=[str(working)],
            warnings=[],
            exit_code=EXIT_NEEDS_STEP,
        ).as_dict()

    if not _grid_expanded(working):
        replay = '"${HARNESS_BIN_ROOT:-.}"/harness/scripts/grid_fill_replay.py'
        return hs_run.StepResult(
            state="grid_needs_expand",
            next_action="skeleton grid built — validate the fill source, then relay it. An "
                "unresolved source relays no cells and every cell degrades silently, "
                "which is why the validate runs first rather than trusting exit 0",
            next_command="python3 %s --validate --grid %s --src %s && "
                "HARNESS_GRID_FILL_SRC=%s python3 %s expand --grid %s "
                "--invoker grid_fill_replay:invoke --out %s"
                % (replay, working, fill_src, fill_src, engine, working, working),
            artifacts=[str(working)],
            warnings=[],
            exit_code=EXIT_NEEDS_STEP,
        ).as_dict()

    return hs_run.StepResult(
        state="grid_needs_emit",
        next_action="grid filled, no coverage-grid artifact — emit it",
        next_command="python3 %s emit --grid %s --axes-src %s" % (engine, working, axis_src),
        artifacts=[str(emitted)],
        warnings=[],
        exit_code=EXIT_NEEDS_STEP,
    ).as_dict()


def _cook_recommendation(plan_dir: Path, fm: dict) -> str:
    """Recon table A row 11: read the approved plan's own frontmatter flags and
    compose the exact next command — the textbook DETERMINISTIC projection this
    whole mission is about. `--parallel` gets the dual recommendation
    `cook-handoff-flags.md` documents (print both, recommend one) rather than
    silently picking a side.

    The glob/glob grounding root is `plan_graph.root_for_plan_dir(plan_dir)` —
    the tree `plan_dir` itself lives in — never the ambient `hs-run` root this
    function used to be handed. Those differ whenever `--plan` targets a plan
    outside the caller's cwd (a different project, or the same plan re-checked
    from a linked worktree): grounding against the wrong tree answers the
    conflict question about files that plan never declared."""
    plan_md = Path(plan_dir) / "plan.md"
    tdd = str(fm.get("tdd", "true")).strip().lower() not in ("false", "0", "no")
    grid = bool(fm.get("grid"))
    defer_suite = fm.get("defer_suite") is True
    flags = []
    if tdd:
        flags.append("--tdd")
    if grid:
        flags.append("--grid")
    if defer_suite:
        flags.append("--defer-suite")
    base = "/hs:cook %s%s" % (plan_md, (" " + " ".join(flags)) if flags else "")

    note = ""
    try:
        import plan_graph
        graph = plan_graph.parse_phase_graph(plan_dir)
        if not graph.get("error"):
            batches = plan_graph.find_parallel_batches(graph)
            if any(len(b) > 1 for b in batches):
                par_cmd = base + " --parallel"
                conflicts = plan_graph.find_parallel_conflicts(
                    graph, root=plan_graph.root_for_plan_dir(plan_dir))
                if conflicts:
                    note = (" · parallel-capable but %d shared-file conflict(s) — "
                             "recommend sequential (%s); parallel shown for reference "
                             "in case ownership is split first (%s)"
                             % (len(conflicts), base, par_cmd))
                else:
                    note = (" · parallel-capable, disjoint ownership — recommend: %s"
                             % par_cmd)
    except Exception:  # noqa: BLE001 — the recommendation degrades to the base
        pass          # command; a graph read fault here must never crash `next`

    return "plan approved — recommended cook command: %s%s" % (base, note)


# ------------------------------------------------------------------- targeting

def _resolve_plan_arg(root: Path, plan_arg: str) -> Path:
    """Same permissive resolution plan_approval.py's own `--plan` uses: a plan
    dir, a path to a file inside it, or a bare name under plans/."""
    p = Path(plan_arg)
    if p.is_file():
        return p.parent
    if p.is_dir():
        return p
    return Path(root) / "plans" / plan_arg


def _resolve_active_plan_env(root: Path):
    """The plan HARNESS_ACTIVE_PLAN names, or None when the env is unset OR
    names something that does not resolve under <root>/plans/ (plan resolution must honour this env, matching cook's own
    `_resolve_plan_dir`). Delegates to `artifact_check.resolve_active_plan` —
    the shared owner of the env-override branch — rather than re-parsing the
    env var here; only invoked when the env is actually present so a plan
    author never has to reason about cook's separate in_progress/approved
    disk-scan fallback (that heuristic belongs to cook, not to this module)."""
    if not os.environ.get("HARNESS_ACTIVE_PLAN"):
        return None
    return artifact_check.resolve_active_plan(str(root), allow_completed=True)


def _target_plan_dir(root: Path, positionals: list, opts: dict):
    """Resolve the plan dir a verb should act on. Returns (plan_dir, None) or
    (None, reason) where reason in {"no_plan", "multiple_plans"} — the caller
    decides whether that reason is informational (`next`, exit 0) or a
    hard block (`graph`/`prompt`/`record` cannot complete their own job without
    a concrete target, exit 2)."""
    raw = opts.get("plan") or (positionals[0] if positionals else None)
    if raw:
        return _resolve_plan_arg(root, raw), None
    env_plan = _resolve_active_plan_env(root)
    if env_plan is not None:
        return env_plan, None
    dirs = _plan_dirs(root)
    if not dirs:
        return None, "no_plan"
    if len(dirs) > 1:
        return None, "multiple_plans"
    return dirs[0], None


# ---------------------------------------------------------------- flag parsing

# The `nargs="*"` slot the shared parser fills with whatever positional tokens a
# verb was given. Named once here because it is an internal plumbing dest, never
# a name an operator types.
_VARARGS_DEST = "_targets"


def _reshape(ns, options=(), flags=()):
    """The parsed Namespace as the `(positionals, opts, flagset)` triple this
    module's verbs read. The parsing is the family core's, against the declared
    set `_plan_verb` hands it.

    What the declared set buys, measured on the live CLI: `plan next --paln p`
    once took the mistyped token as a plan directory name and answered about
    `plans/--paln`, a path the operator never typed, while dropping the value
    they did type. Declared, it is `unrecognized arguments: --paln p`.

    `flags`/`options` list the recognized `--name` tokens (dash included);
    opts/flagset keys come back WITHOUT the leading dashes and with their
    hyphens intact (`no-tdd`, not `no_tdd`), because that is what the verbs
    check against."""
    positionals = list(getattr(ns, _VARARGS_DEST, None) or [])
    opts = {}
    for name in options:
        key = name.lstrip("-")
        value = getattr(ns, key.replace("-", "_"), None)
        if value is not None:
            opts[key] = value
    flagset = {name.lstrip("-") for name in flags
               if getattr(ns, name.lstrip("-").replace("-", "_"), False)}
    return positionals, opts, flagset


def _plan_verb(impl, *, options=(), flags=(), helps=None):
    """A plan verb as the family core runs it: `options`/`flags` are its declared
    set, and `impl(args, root)` reads the reshaped triple. A plan target may be
    given positionally OR by `--plan`, or not at all."""
    def run(ns):
        return _finalize_next_command(impl(_reshape(ns, options, flags), ns.ctx.root))
    return hs_run.verb(run, options=options, flags=flags, varargs=_VARARGS_DEST,
                       helps=helps)


# --------------------------------------------------------------------- states

def _project(plan_dir: Path, fast: bool = False, root=None) -> dict:
    """The projection core: derives the plan-domain state PURELY from what is
    on disk right now — every call re-reads from scratch, so a
    deleted artifact honestly regresses the state instead of a cached lie.
    Thin wrapper over `_project_inner` that additionally attaches `skipped`
    (goal F13) and `checklist`/`summary_numbers` (the retired `status` verb's
    only unique content — see the module docstring) so every returned state —
    not just the ones that happen to mention it in prose — carries the same
    machine-readable per-step board."""
    plan_dir = Path(plan_dir)
    result = _project_inner(plan_dir, fast=fast, root=root)
    result.setdefault("skipped", _skipped_steps(plan_dir, fast, root))
    result.setdefault("pending", _pending_steps(plan_dir, fast, root))
    # The full per-step board, formerly the one thing the retired `status` verb
    # carried that `next` did not (USR-HARNESS-HS-RUN-ONE-DOOR): riding it on
    # EVERY state here, not only the ones a caller happens to reach through
    # `status`'s old code path, is what `next` alone answering for the domain
    # actually requires — a field that only showed up behind a second verb is a
    # field callers of the one door never saw.
    result.setdefault("checklist", _checklist(plan_dir, fast, root))
    result.setdefault("summary_numbers", _summary_numbers(result["checklist"]))
    _attach_layout_warnings(plan_dir, result)
    _attach_route_flags(plan_dir, result)
    return result


def _attach_route_flags(plan_dir: Path, result: dict) -> None:
    """The plan's own flags, for the dispatcher's conditional step routing.

    Attached HERE, beside `skipped` and the layout warnings, for the reason that
    docstring already gives: a fact that only rides some states is a fact the reader
    cannot rely on, and the branch added next month forgets it.

    Only the verb can supply this — the dispatcher sees a state, never a plan's
    frontmatter. It exists because not every condition is a state: `--grid` costs four
    reference files that are worthless without it, and routing them on the state alone
    would load all four on every plan ever made.

    Defensive: unreadable frontmatter yields no flags rather than taking the projection
    down. The projection is what the caller came for."""
    try:
        fm = _frontmatter(Path(plan_dir) / "plan.md")
    except Exception:
        return
    # `tdd` rides here for the same reason `grid` does, and it was missing for a
    # measurable cost: `tdd-plan-mode.md` was routed at `scaffolded` and NOWHERE
    # else. That state is emitted only by the scaffold verb, so a plan dir built
    # any other way never received it — and even on the scaffolded path it landed
    # one beat before the phase files it governs are written. Conditional rather
    # than unconditional at the design step: on a `tdd: false` plan the file
    # describes a mode that is off.
    flags = [name for name, on in (("grid", bool((fm or {}).get("grid"))),
                                   ("tdd", bool((fm or {}).get("tdd")))) if on]
    if flags:
        result.setdefault("route_flags", flags)


def _attach_layout_warnings(plan_dir: Path, result: dict) -> None:
    """Plan-layout drift, ADVISORY — appended to `warnings`, never `state`.

    Attached HERE rather than in each of `_project_inner`'s branches for the
    same reason `skipped` is: a signal that only rides some states is a signal
    the reader cannot rely on, and the branch added next month will forget it.

    Advisory is a hard property, not a default. `plan_layout_check` is exit-0
    by construction; letting this move `state` would make it a gate wearing a
    warning's label, and a check that blocks work is a check someone switches
    off — this repo has already retired one over-eager guard that way.

    Why the CLI says it at all: the instruction to run the script by hand lived
    in skill prose, and prose that depends on a model remembering to run
    something produces the same silence as a clean plan when it is forgotten.
    The blocking guarantee is unchanged and stays in `plan_approval`'s hash;
    what moves is only WHO notices.

    Defensive: an unreadable plan dir yields no warning rather than taking the
    projection down. The projection is what the caller came for."""
    try:
        found = plan_layout_check.layout_warnings(plan_dir)
    except Exception:
        return
    if found:
        result["warnings"] = list(result.get("warnings") or []) + list(found)


def _project_inner(plan_dir: Path, fast: bool, root) -> dict:
    """State order: scaffold
    -> research (3, skippable, own gate — F12) -> content+plan-graph (5b/5f)
    -> red-team (7a, skippable) -> the Hard Cases (7b/9, NOT disk-provable) ->
    approval (10, JUDGMENT decision + a deterministic write this module never
    performs itself) -> the cook handoff (11, recon numbering — unrelated to
    the exit-tier 11).

    Frontmatter `status:` is CORROBORATION
    ONLY, never authority. The lifecycle claims `in_progress`/`completed` are
    read FIRST (closes C0-MOVED, see below) so they can be cross-checked
    against the artifact that would actually PROVE them — a currently-valid
    APPROVED plan-approval for either, plus `derive_plan_completion.
    completion_state` for `completed` specifically (that
    function is the real owner of "is the cook chain actually finished").
    Disagreement is a first-class `state_conflict`, exit EXIT_BROKEN, naming
    both sides — never a silent pass. Reproduced live pre-fix: delete every
    approval/verification/plan-graph/report artifact from a `status:
    completed` plan and `next` still said "completed", exit 0.

    C0-MOVED (post-postfix-redteam): the ORIGINAL fix above still returned
    `unresolved` at exit 0, silently, whenever `phases/phase-*.md` was absent
    — BEFORE frontmatter was ever read, so `status: completed` with `phases/`
    deleted regressed to a NEW state name (`unresolved` instead of
    `completed`) while the exit code stayed 0 — a `while rc != 0` driver
    behaved identically to the bug this module was supposed to have killed.
    Frontmatter is now read UNCONDITIONALLY, before the phases/-shape check:
    a missing `phases/` dir is only the honest, no-conflict `unresolved` when
    frontmatter does NOT claim real progress (`pending`/`draft`/absent); if it
    DOES claim `in_progress`/`completed`, that claim cannot be corroborated by
    a shape scaffold.py never produces, and reporting it as a quiet
    `unresolved` at exit 0 would be exactly the C0 silent pass moved one
    branch over — so it is `state_conflict`, exit EXIT_BROKEN, same as every
    other frontmatter-vs-disk disagreement this function detects.

    R14 (second postfix round): `plan_status.CANONICAL_STATUSES` is the
    SSOT for every value `status:` can legitimately hold — `pending`,
    `approved`, `in_progress`, `completed`, `cancelled` (plus `None` for an
    absent/off-vocabulary raw value, which `normalize_status` already folds
    to the same "nothing decided yet" shape `pending` gets). Before this
    round, only `in_progress`/`completed` got frontmatter-vs-disk
    corroboration; `cancelled` and `approved` fell through to the SAME
    generic "is there a valid approval artifact" fallback every
    not-yet-decided status uses — so a cancelled plan with a leftover
    APPROVED artifact was reported `state: "approved"` with a `/hs:cook`
    recommendation (hand-reproduced: `_write_approval(..., "APPROVED")` then
    `_flip_status(..., "cancelled")` → `state: "approved"`, exit 0 — the tool
    actively invited work on a plan someone deliberately cancelled), and a
    cancelled plan with NO approval marched through the same
    needs_research/needs_planner/needs_redteam/needs_model_attestation chain
    a live, uncancelled plan gets. Fixed two ways below: (1) `cancelled` is
    now checked FIRST, right after frontmatter, unconditionally terminal
    regardless of what else is on disk — a deliberate human cancellation is
    not something the artifact trail can override, so no corroboration
    logic applies to it, only reporting. (2) `approved` — a status
    `plan_approval.py`'s own `flip_status` writes ONLY immediately after a
    real artifact write, so under normal operation it is always backed —
    now gets the SAME kind of corroboration `in_progress`/`completed`
    already had: `status: approved` with NO approval artifact on disk at
    all is `state_conflict`, not a silent slide into the research/planner
    chain. `approved` status backed by an artifact that IS present but
    invalid for another reason (drifted, wrong plan, or a later REJECTED
    verdict overwriting an earlier APPROVED one — see
    `test_c1_rejected_verdict_wins_even_when_frontmatter_status_still_says_approved`)
    is deliberately left to the EXISTING generic fallback below, unchanged —
    that fallback already reports `approval_invalid`/`rejected` correctly
    for those cases, and a REJECTED verdict discovered under `status:
    approved` is not the fundamental disk-vs-artifact conflict `cancelled`
    or "no artifact at all" are; it is the ordinary "reviewed and rejected,
    revise and resubmit" outcome C1 already established as truthful. The
    full state-by-state table for every `CANONICAL_STATUSES` value is in
    `plan_status.CANONICAL_STATUSES` itself."""
    plan_dir = Path(plan_dir)
    plan_md = plan_dir / "plan.md"
    if not plan_md.is_file():
        # This branch is ONLY ever reached with an explicit (or env-resolved)
        # target — `NEXT`'s own `no_plan`/`multiple_plans` handling
        # already intercepts the "nothing/ambiguous, no --plan given" cases
        # before `_project` is ever called, and `_plan_dirs()`'s auto-select
        # only ever returns dirs that already carry plan.md. So a target that
        # lands here always means the caller named something that is not a
        # real, scaffolded plan at all (conformance property 7) — a genuinely
        # blocking situation, not an informational "nothing to do" one.
        return hs_run.StepResult(
            # `not_a_plan_dir`, not `unresolved`. A previous pass tried to settle
            # the two-rung split by copying this rung onto the sibling and had to
            # revert it — correctly, because the cases really do differ: no
            # plan.md at all is a stop, a half-scaffolded dir is an ordinary
            # shape at rung 0. What was wrong was giving both the same WORD, so
            # the difference lived only in the rung a caller had to notice.
            state="not_a_plan_dir",
            exit_code=EXIT_BROKEN,
            next_action="%s has no plan.md — not a scaffolded plan dir" % plan_dir,
            artifacts=[str(plan_dir)],
            warnings=["hs-run cannot resolve a plan without plan.md"],
        ).as_dict()

    # Frontmatter is read HERE — before the phases/-shape check below — so a
    # `status: in_progress`/`completed` claim can never bypass corroboration
    # by exiting through an earlier, unconditional-exit-0 branch (C0-MOVED).
    fm = _frontmatter(plan_md)
    status = plan_status.normalize_status(fm.get("status"))

    if status == "cancelled":
        # R14: checked FIRST, unconditionally terminal — no corroboration
        # logic applies. A leftover APPROVED artifact from before the
        # cancellation must NOT make this report `state: "approved"` (the
        # hand-reproduced bug: it recommended a `/hs:cook` command on a
        # deliberately cancelled plan); the absence of research/graph/
        # red-team must NOT make it march into needs_research either — both
        # of those readings ask "what should happen next", and the honest
        # answer to that question for a cancelled plan is "nothing, by
        # design, until a human un-cancels it."
        return hs_run.StepResult(
            state="cancelled",
            exit_code=EXIT_DONE,
            next_action="plan was cancelled (status: cancelled) — nothing for the "
                             "plan domain to do; if this was unintended, correct "
                             "plan.md's status by hand",
            artifacts=[str(plan_dir)],
            warnings=[],
        ).as_dict()

    if not _phase_files(plan_dir) and not (
            status not in ("in_progress", "completed") and (plan_dir / "phases").is_dir()):
        # A `phases/` dir with no phase file in it, on a plan that claims no
        # progress, is the scaffold's own shape mid-authoring: the planner
        # retired the placeholder(s) before writing its own. That falls through
        # to the ordinary chain, where `_content_unfilled` holds it at the
        # planner step. Every other zero-phase shape lands here.
        #
        # No phases/phase-*.md at all: this does not match scaffold.py's own
        # output shape (plan.md + phases/ always land together — scaffold_plan
        # writes both in the same call). A frontmatter claim of real progress
        # cannot be corroborated by this shape (C0-MOVED) — only an
        # honestly-unadvanced plan (no such claim) gets the no-conflict
        # `unresolved` floor case the first cut already had.
        if status in ("in_progress", "completed"):
            return hs_run.StepResult(
                state="state_conflict",
                exit_code=EXIT_BROKEN,
                next_action="frontmatter status=%r claims this plan progressed past scaffolding, "
                    "but %s has no phases/phase-*.md at all — that does not match "
                    "scaffold.py's own output shape (plan.md + phases/ always land "
                    "together) and cannot be corroborated; hs-run will not trust either "
                    "side silently. Inspect both, then either restore phases/ or correct "
                    "plan.md's status." % (status, plan_dir),
                artifacts=[str(plan_dir)],
                warnings=[
                    "frontmatter side: status=%r" % status,
                    "artifact side: no phases/phase-*.md found under %s" % plan_dir,
                ],
            ).as_dict()
        return hs_run.StepResult(
            state="unresolved",
            # 0, matching the registry: `unresolved` is "half-scaffolded plan
            # to finish, or stray dir to remove" — an ordinary shape, kept
            # deliberately distinct from `state_conflict`, which is the state
            # that DOES stop the line. Patched to BROKEN on the sibling-emitter
            # argument, then reverted: the sibling covers a different case (no
            # plan.md at all), and copying its rung across erased the very
            # distinction both states exist to draw.
            exit_code=EXIT_DONE,
            next_action=("%s has plan.md but no phases/phase-*.md — this does not "
                         "match scaffold.py's own output shape; hs-run cannot safely "
                         "project a next step. Inspect manually, or re-scaffold." % plan_dir),
            artifacts=[str(plan_dir)],
            warnings=["hs-run plan next: plan dir does not match a known scaffold shape"],
        ).as_dict()

    approval = _approval_artifact(plan_dir)
    # {} rather than None for "no artifact": `_read_json_or_yaml` already degrades an
    # unreadable or malformed file to {}, so a second empty-shape here would have to be
    # distinguished from the first at every use — and the three readers below are all
    # inside `if approval is not None:` anyway. One empty shape, not two.
    approval_rec: dict = _read_json_or_yaml(approval) if approval is not None else {}
    approval_verdict = approval_reason = None
    if approval is not None:
        approval_verdict, approval_reason = _approval_verdict(plan_dir, approval_rec, root)

    if status in ("in_progress", "completed"):
        if approval is None:
            return hs_run.StepResult(
                state="state_conflict",
                exit_code=EXIT_BROKEN,
                next_action="frontmatter status=%r claims this plan was approved and handed "
                    "to cook, but no plan-approval artifact exists on disk at all — "
                    "hs-run will not trust either side silently; inspect both, then "
                    "either re-run plan_approval.py or correct plan.md's status"
                    % status,
                artifacts=[str(plan_dir)],
                warnings=[
                    "frontmatter side: status=%r" % status,
                    "artifact side: no plan-approval artifact present",
                ],
            ).as_dict()
        if approval_reason is not None:
            if _is_drift_only_reason(plan_dir, approval_rec):
                # R7: this is NOT a frontmatter-vs-artifact LIE — the artifact
                # really was a currently-valid APPROVED verdict for THIS exact
                # plan; only the plan BODY changed afterward (an ordinary
                # mid-cook edit, e.g. one comment line in plan-graph.yaml or a
                # phase file). Reporting that as `state_conflict` is a false
                # alarm that also states something factually wrong ("no
                # currently-valid plan-approval artifact backs that on disk")
                # while a valid APPROVED artifact for this exact plan is sitting
                # right there — exactly the kind of alarm that trains people to
                # ignore alarms. This is a re-review need (a model judgment),
                # not a broken CLI: EXIT_NEEDS_MODEL, not EXIT_BROKEN.
                return hs_run.StepResult(
                    state="approval_drifted",
                    exit_code=EXIT_NEEDS_MODEL,
                    next_action="frontmatter status=%r and the plan WAS validly approved for "
                        "this exact plan, but the body changed afterward (%s) — this is "
                        "a stale approval, not a conflicting story; re-review the drifted "
                        "content, then re-run: %s "
                        "--plan %s --verdict APPROVED|REJECTED --rationale \"<why>\""
                        % (status, approval_reason, _approval_cmd(), plan_dir),
                    artifacts=[str(plan_dir), str(approval)],
                    warnings=[approval_reason],
                ).as_dict()
            return hs_run.StepResult(
                state="state_conflict",
                exit_code=EXIT_BROKEN,
                next_action="frontmatter status=%r claims this plan was approved and handed "
                    "to cook, but the on-disk plan-approval artifact does not currently "
                    "back that (%s) — hs-run will not trust either side silently; "
                    "inspect both, then either re-run plan_approval.py or correct "
                    "plan.md's status" % (status, approval_reason),
                artifacts=[str(plan_dir), str(approval)],
                warnings=[
                    "frontmatter side: status=%r" % status,
                    "artifact side: %s" % approval_reason,
                ],
            ).as_dict()
        if status == "completed":
            comp = derive_plan_completion.completion_state(plan_dir, root=root)
            if not comp["complete"]:
                return hs_run.StepResult(
                    state="state_conflict",
                    exit_code=EXIT_BROKEN,
                    next_action="frontmatter status=\"completed\" but the cook chain is not "
                        "artifact-complete (%s) — hs-run will not trust either side "
                        "silently; inspect both" % comp["reason"],
                    artifacts=[str(plan_dir)],
                    warnings=["frontmatter side: status=\"completed\"",
                                 "artifact side: %s" % comp["reason"]],
                ).as_dict()
            return hs_run.StepResult(
                state="completed",
                exit_code=EXIT_DONE,
                next_action="plan is completed — nothing left for the plan domain",
                artifacts=[str(plan_dir)],
                warnings=[],
            ).as_dict()
        return hs_run.StepResult(
            state="handed_off_to_cook",
            exit_code=EXIT_DONE,
            next_action="plan is in_progress — hand off to hs-run cook next",
            artifacts=[str(plan_dir)],
            warnings=[],
        ).as_dict()

    if status == "approved" and approval is None:
        # R14 (approved half): `plan_approval.py`'s own `flip_status` writes
        # `status: approved` ONLY immediately after a real artifact write —
        # under normal operation this is always backed. If the artifact is
        # later deleted, frontmatter still claims an approval EVENT
        # happened; the generic fallback below would otherwise silently
        # slide this into the needs_research/needs_planner/... chain, the
        # exact class of silent pass C0 already closed for in_progress/
        # completed. `approved` gets the same corroboration here — but ONLY
        # for the "no artifact at all" case; an artifact that IS present but
        # invalid for another reason (drift/binding/a later REJECTED
        # overwrite) still falls through to the fallback below unchanged,
        # which already reports it truthfully (approval_invalid/rejected).
        return hs_run.StepResult(
            state="state_conflict",
            exit_code=EXIT_BROKEN,
            next_action="frontmatter status=\"approved\" claims this plan was approved, but no "
                "plan-approval artifact exists on disk at all — hs-run will not trust "
                "either side silently; inspect both, then either re-run plan_approval.py "
                "or correct plan.md's status",
            artifacts=[str(plan_dir)],
            warnings=[
                "frontmatter side: status=\"approved\"",
                "artifact side: no plan-approval artifact present",
            ],
        ).as_dict()

    if approval is not None:
        if approval_reason is None:
            # A currently-valid APPROVED verdict is necessary but no
            # longer SUFFICIENT — the human's keep/split size decision must
            # also be on disk, attributed to a person (`decided_by: user`),
            # before this plan reports done. Re-emits the SAME
            # `needs_model_attestation` state rather than inventing a new
            # one: the question the human still owes an answer to is the
            # same `ask_user` block they already saw, not a different step.
            size_ok, size_reason = _size_decision_receipt(plan_dir)
            if not size_ok:
                return _attestation_result(plan_dir, root, extra_warning=size_reason,
                                           approved_pending_size=True)
            return hs_run.StepResult(
                state="approved",
                exit_code=EXIT_DONE,
                next_action=_cook_recommendation(plan_dir, fm),
                artifacts=[str(approval)],
                warnings=[],
            ).as_dict()
        if approval_verdict == "REJECTED":
            return hs_run.StepResult(
                state="rejected",
                exit_code=EXIT_DONE,
                next_action="plan was REJECTED (%s) — revise plan.md/phases per the "
                                 "rationale, then re-run plan_approval.py"
                                 % (approval_rec.get("rationale") or "no rationale recorded"),
                artifacts=[str(approval)],
                warnings=[],
            ).as_dict()
        return hs_run.StepResult(
            state="approval_invalid",
            exit_code=EXIT_NEEDS_MODEL,
            next_action="the on-disk plan-approval artifact is not currently valid for this "
                "plan (%s) — re-review, then re-run: %s --plan %s --verdict "
                "APPROVED|REJECTED --rationale \"<why>\""
                % (approval_reason, _approval_cmd(), plan_dir),
            artifacts=[str(approval)],
            warnings=[approval_reason],
        ).as_dict()

    # Research is its OWN gate (closes F12), checked independently of content
    # so it cannot silently vanish just because a planner filled content
    # before research happened — `_checklist`'s own "research" step already
    # checks this unconditionally; `next` must agree with it, not disagree
    # depending on content state.
    if not fast and not _has_research(plan_dir):
        if _content_unfilled(plan_dir):
            msg = ("%s, and no research report found — "
                   "spawn the researcher: hs-run plan prompt researcher --plan %s (or "
                   "re-run with --fast to skip research)" % (_unfilled_what(plan_dir), plan_dir))
        else:
            msg = ("planner content is already filled but no research report exists "
                   "— research normally precedes planner content; spawn the "
                   "researcher retroactively: hs-run plan prompt researcher --plan %s "
                   "(or re-run with --fast to acknowledge skipping it)" % plan_dir)
        return hs_run.StepResult(
            state="needs_research",
            next_action=msg,
            artifacts=[str(plan_dir)],
            warnings=[],
            exit_code=EXIT_NEEDS_MODEL,
            extra={"summary_numbers": "0 research report(s)"},
        ).as_dict()

    if _content_unfilled(plan_dir):
        # Mode-aware wording, one lane each: the hard plan DELEGATES the design
        # step (that lane stays heavy by decision, so the envelope says so and
        # hands the prompt command); a fast plan is the light lane where MAIN writes
        # the content — telling that driver to "spawn the planner" points at
        # tooling for a delegation the engine neither needs nor enforces.
        if fast:
            msg = ("%s — write the "
                   "planner step at main (a fast plan stays inline; the delegation "
                   "is the hard lane)" % _unfilled_what(plan_dir))
        else:
            msg = ("%s — spawn the "
                   "planner: hs-run plan prompt planner --plan %s"
                   % (_unfilled_what(plan_dir), plan_dir))
        return hs_run.StepResult(
            state="needs_planner",
            next_action=msg,
            artifacts=[str(plan_dir)],
            warnings=[],
            exit_code=EXIT_NEEDS_MODEL,
        ).as_dict()

    graph_ok, graph_reasons = _graph_state(plan_dir)
    if not graph_ok:
        if fast:
            msg = ("plan-graph.yaml missing/invalid (%s) — author the planner step "
                   "at main (a fast plan stays inline; the delegation is the hard "
                   "lane)" % "; ".join(graph_reasons))
        else:
            msg = ("plan-graph.yaml missing/invalid (%s) — spawn the planner to "
                   "author it: hs-run plan prompt planner --plan %s"
                   % ("; ".join(graph_reasons), plan_dir))
        return hs_run.StepResult(
            state="needs_planner",
            next_action=msg,
            artifacts=[str(plan_dir / "plan-graph.yaml")],
            warnings=graph_reasons,
            exit_code=EXIT_NEEDS_MODEL,
        ).as_dict()

    # Grid resolves BEFORE red-team, the order the reference prose has always
    # stated. After it, a red-teamer can spend a whole turn on coverage that is
    # about to change.
    grid = _grid_state(plan_dir)
    if grid is not None:
        return grid

    redteam = _redteam_reports(plan_dir)
    if not fast and not redteam:
        return hs_run.StepResult(
            state="needs_redteam",
            next_action="content+graph ready, no red-team report found — spawn the "
                             "red-teamer: hs-run plan prompt red-teamer --plan %s (or re-run "
                             "with --fast to skip)" % plan_dir,
            artifacts=[str(plan_dir)],
            warnings=[],
            exit_code=EXIT_NEEDS_MODEL,
            extra={"summary_numbers": "0 red-team report(s)"},
        ).as_dict()

    disposition = redteam_disposition.check(plan_dir, redteam)
    if disposition.blocking:
        return hs_run.StepResult(
            state="needs_redteam_disposition",
            next_action=(
                "%s. Dispose of every finding in plan.md under `%s` — one row per "
                "finding, no empty cell (Accept: the phase that now carries it / "
                "Reject: the evidence) — propagate each Accept into its phase file, "
                "then re-run: hs-run plan next --plan %s"
                % ("; ".join(disposition.blocking), redteam_disposition.HEADING,
                   plan_dir)),
            artifacts=[str(plan_dir / "plan.md")] + [str(p) for p in redteam],
            warnings=disposition.notes,
            exit_code=EXIT_NEEDS_MODEL,
        ).as_dict()

    # Everything hs-run can PROVE from disk is ready. What remains (the
    # Hard Cases) is NOT disk-provable: the red-team disposition table has no
    # open cells, the validate interview + Whole-Plan Consistency Sweep found 0
    # contradictions, any new term/decision is registered. Reporting "ready for
    # approval" here would be exactly the silent pass this contract forbids — so
    # this state names the ceiling honestly instead, and hands the (JUDGMENT
    # decision + DETERMINISTIC write) of approval to the model directly, since
    # that write is not one of this module's own verbs.
    #
    # exit_code=EXIT_NEEDS_MODEL, not EXIT_DONE: this state is NOT terminal —
    # the plan cannot proceed until a model turn (read + judge, then run
    # plan_approval.py) happens. See test_needs_model_attestation_exit_code_is_10
    # and test_naive_loop_until_exit_zero_driver_does_not_mistake_attestation_for_done.
    #
    # Delegates to `_attestation_result` (defined above, beside the size-
    # decision receipt reader) rather than building the envelope here a
    # second time — the "approval exists but the size decision does not"
    # branch further up needs the exact same envelope, and two hand-written
    # copies is how they drift.
    return _attestation_result(plan_dir, root, notes=disposition.notes)


@_attrs.define(frozen=True, kw_only=True)
class ChecklistRow:
    """One row of the board `next` prints (as `checklist`) and `pending` derives from.

    These were eight separate dict literals with INCONSISTENT keys: five carried
    `skipped`, three did not, one carried `reasons`. Every consumer therefore had
    to reach for `.get()` and hope — and a consumer that wrote `row["skipped"]`
    would raise KeyError on three rows out of eight, in production, on a board
    that renders perfectly.

    Declared once, every row carries every key. `skipped` defaults False (a step
    that never opted out was never skipped) and `reasons` to an empty tuple, so
    the defaults state what the missing keys used to mean implicitly, where a
    reader can see it.

    `skipped` is DERIVED from `skip_kind` rather than passed alongside it. Two
    independently-set fields answering one question is the shape that let the
    board carry `state: "approved"` above a checklist row saying otherwise; the
    only way a row can now claim a skip is by saying which kind, and the only way
    to claim a kind is to be skipped."""

    step = _attrs.field()
    done: bool = _attrs.field(converter=bool)
    evidence = _attrs.field(default=None)
    skip_kind = _attrs.field(default=None)
    reasons = _attrs.field(default=(), converter=tuple)

    @skip_kind.validator
    def _kind_is_one_of_the_two(self, _attribute, value):
        """A free-text kind would reintroduce the ambiguity in a new spelling."""
        if value is not None and value not in (WAIVED, NOT_APPLICABLE):
            raise ValueError(
                "skip_kind=%r on step %r: a skip is either %r (somebody asked to "
                "bypass a gate that applied) or %r (there was no gate). A third "
                "spelling puts the reader back to guessing."
                % (value, self.step, WAIVED, NOT_APPLICABLE))

    @property
    def skipped(self) -> bool:
        """Kept because every existing consumer indexes it, and because "did this
        run" is a real question separate from "why not"."""
        return self.skip_kind is not None

    def as_dict(self) -> dict:
        """The wire form. Lists, not tuples, because that is what `json.dumps`
        emits and what every existing consumer already indexes."""
        blob = _attrs.asdict(self)
        blob["reasons"] = list(blob["reasons"])
        blob["skipped"] = self.skipped
        return blob


@_attrs.define(frozen=True, kw_only=True)
class IngestReceipt:
    """What `record` filed, and what it did NOT check.

    `verified` and `note` are FIELDS rather than a sentence in a docstring
    because they are the load-bearing half. This verb copies a file: it never
    opens it, so it cannot tell an independent `@red-teamer` report from an
    inline persona pass — and the gate downstream counts files rather than
    reading them. Left unsaid, the pair reads as a verification, and a green
    gate looks like a checked one.

    `verified` defaults False and there is no code path that sets it True. That
    is the honest state today; a future check that really does inspect content
    is what earns the flip, and declaring the field is what makes its absence
    visible instead of assumed."""

    role = _attrs.field()
    path = _attrs.field()
    verified: bool = _attrs.field(default=False, converter=bool)
    note = _attrs.field(
        default=("filed as-is; the CLI does not read the file and cannot tell an "
                 "independent agent's report from an inline pass"))

    def as_dict(self) -> dict:
        return _attrs.asdict(self)


def _checklist(plan_dir: Path, fast: bool, root=None) -> list:
    """The full rundown `next` carries as `checklist` — every step recon table A
    names, each with the exact disk evidence backing its done/not-done call.
    Unlike `next_action`, which picks just one thing to do, this is the whole
    board at a glance — the one piece of content the now-retired `status` verb
    used to be the only way to see.

    The "approval" step delegates to the SAME `_approval_verdict` helper
    `_project` uses (closes F3/C5): the old code re-derived "is this approval
    good" with its own naive `verdict == "APPROVED"` check, independent of
    `_project`'s (buggier) one — the two could, and did, disagree in the same
    JSON blob (`state: "approved"` at the top, `{"step": "approval", "done":
    false}` in the checklist). One shared truth function closes that gap by
    construction, not by coincidence."""
    plan_dir = Path(plan_dir)
    scaffolded = bool(_phase_files(plan_dir))
    content_ok = scaffolded and not _content_unfilled(plan_dir)
    if scaffolded:
        graph_ok, graph_reasons = _graph_state(plan_dir)
    else:
        graph_ok, graph_reasons = False, ["plan not scaffolded"]
    research_present = _has_research(plan_dir)
    redteam = _redteam_reports(plan_dir)
    redteam_open = redteam_disposition.check(plan_dir, redteam).blocking
    approval = _approval_artifact(plan_dir)
    approved = False
    if approval is not None:
        rec = _read_json_or_yaml(approval)
        _, reason = _approval_verdict(plan_dir, rec, root)
        approved = reason is None
    rows = [
        ChecklistRow(step="scaffold", done=scaffolded,
                     evidence=str(plan_dir / "phases")),
        ChecklistRow(step="research", done=research_present or fast,
                     skip_kind=WAIVED if fast and not research_present else None,
                     evidence=str(plan_dir / "research")),
        ChecklistRow(step="planner_content", done=content_ok,
                     evidence=str(plan_dir / "plan.md")),
        ChecklistRow(step="plan_graph", done=graph_ok,
                     evidence=str(plan_dir / "plan-graph.yaml"), reasons=graph_reasons),
        # The grid leg, keyed off the SAME `_grid_state` the projection returns —
        # never a second reading of the same artifacts. `_checklist` shipped without
        # it for one commit and the board said "all done" while `next` was asking for
        # an axis receipt; the module's own history (see this docstring) is that two
        # independent derivations of one fact disagree in the same JSON blob.
        # NOT_APPLICABLE, never WAIVED: no flag turns this gate off. A plan that
        # never declared `grid:` was never subject to it, and filing that as a
        # bypass invents a decision nobody made.
        ChecklistRow(step="grid", done=_grid_state(plan_dir) is None,
                     skip_kind=(None
                                if (_frontmatter(plan_dir / "plan.md") or {}).get("grid")
                                else NOT_APPLICABLE),
                     evidence=str(plan_dir / "artifacts" / _GRID_ARTIFACT)),
        ChecklistRow(step="red_team",
                     done=(bool(redteam) and not redteam_open) or (fast and not redteam),
                     skip_kind=WAIVED if fast and not redteam else None,
                     reasons=_redteam_findings(redteam) + redteam_open,
                     evidence=[str(p) for p in redteam]),
        ChecklistRow(step="approval", done=approved,
                     evidence=str(approval) if approval else None),
    ]
    return [r.as_dict() for r in rows]


def _summary_numbers(checklist: list) -> str:
    """The one line a reader takes away, and the hardest claim to disbelieve.

    A skipped row is `done` — correctly, there is nothing left to do on it — so
    counting `done` put waivers in the NUMERATOR. MEASURED on a directory holding
    no plan at all: `4/8 step(s) done`, three of the four being gates `--fast`
    removed and one a grid that never applied. Half the pipeline reported complete
    on an empty folder.

    Both other fixes for this are worse. Counting skipped rows as not-done would
    leave a plan that legitimately waived research permanently short of its own
    total, so the board never reads finished. Dropping them from the denominator
    shrinks the job silently, and a reader comparing two runs sees a smaller
    pipeline rather than a bypassed gate.

    So: the fraction counts only rows that ran, and the skips are stated beside
    it in their own words. Nothing is hidden and nothing is credited."""
    ran = [row for row in checklist if not row.get("skip_kind")]
    parts = ["%d/%d step(s) done" % (sum(1 for r in ran if r.get("done")), len(ran))]
    for kind, label in ((WAIVED, "waived"), (NOT_APPLICABLE, "n/a")):
        count = sum(1 for row in checklist if row.get("skip_kind") == kind)
        if count:
            parts.append("%d %s" % (count, label))
    return ", ".join(parts)


def _pending_steps(plan_dir: Path, fast: bool, root=None) -> list:
    """Every step still outstanding, in order — the `pending[]` the envelope carries.

    Derived from `_checklist`, not from a second walk of the same predicates. A flat
    `state` can only ever name ONE thing; two legs can be unresolved at once, and a
    caller that can only see the first has to run the loop to discover the second.
    `pending` shows the road ahead without turning it into a stored cursor: it is
    re-derived from disk on every call, so deleting an artifact puts its step back
    exactly the way it puts `state` back.

    A `skipped` step is NOT pending — `--fast` skipping research is a decision that
    was made, not work that is waiting."""
    return [row["step"] for row in _checklist(plan_dir, fast, root)
            if not row.get("done") and not row.get("skipped")]


# ---------------------------------------------------------------------- verbs

def _finalize_next_command(result: dict) -> dict:
    """Every dispatcher-visible verb result must carry `next_command`
    EXPLICITLY (D14): `next_command` is a string runnable
    VERBATIM, or `null` — a field a driver can `subprocess.run()` without
    parsing a human sentence for an embedded command. `next_action` stays
    the human-facing sentence, unchanged; the two fields now serve two
    different readers instead of one field trying to serve both (which is
    exactly the hole D14 records: a relaxed "next_action contains a
    runnable command" check let `"preflight clear — open the plan: hs-run
    cook open <dir>"` pass validation while a `while exit==11:
    run(next_action)` driver still could not run it literally).

    The dispatcher enforces the invariant (exit 11 => next_command
    non-null; exit 10 => next_command null) — this module's OWN obligation
    is narrower: never leave the key ABSENT. An absent field and an
    explicit `null` are different claims (goal L7 follow-up): absent could
    mean "this verb forgot to think about it," `null` means "this verb
    considered it and there is no verbatim-runnable step."

    `setdefault`, not assignment: the plan domain USED to have zero exit-11
    states, and this function unconditionally wrote `None`. The grid rungs
    (`grid_needs_build`/`grid_needs_expand`/`grid_needs_emit`) are the first
    real ones — each prints a single runnable command against pinned paths —
    so a state that supplies its own `next_command` must keep it. Overwriting
    here would strand exactly the drivers exit 11 exists for, and the
    `StepResult` invariant (exit 11 demands a non-empty `next_command`) would
    turn every one of those steps into EXIT_BROKEN."""
    result.setdefault("next_command", None)
    return result


_DEFAULT_PHASE = "main"
_PHASES_HELP = ("comma-separated phase names, one phases/phase-N-<name>.md each "
                "(e.g. --phases api,ui,docs); omitted, one phase named %s" % _DEFAULT_PHASE)


def _cmd_scaffold_impl(args, root) -> dict:
    """Wraps scaffold.scaffold_plan() — every validation (slug/id shape,
    clobber-without-force) stays scaffold.py's own; this verb only resolves
    args and normalizes its exit convention.

    Exit-code mapping: scaffold.py's own CLI (`main()`) maps
    ValueError/FileExistsError/FileNotFoundError to exit 1 (a refusal) — this
    verb does NOT forward that raw 1; every wrapped-script refusal normalizes
    to hs-run's own EXIT_BROKEN (2), never passed through uninterpreted
    (goal E24).

    `next_action` is derived from `_project()` — the SAME core `next` uses —
    rather than a hardcoded string (closes the seam defect a skill author
    hit live: `scaffold`'s own next_action said "spawn the planner next" while
    `hs-run plan next`, called immediately afterward on the identical disk
    state, said `needs_research` / "spawn the researcher". Two different
    answers to "what next" from the same tool, one command apart, is exactly
    the kind of drift the projection contract exists to prevent — the fix is to never let
    scaffold's own text diverge from a real projection in the first place, not
    to hand-tune the hardcoded string to match today's step order. Deriving
    from the shared core closed that seam only in the DEFAULT mode: on
    `--mode fast` the scaffold's own envelope still projected in hard mode
    ("spawn the researcher") while `next` read the mode the scaffold had just
    written and said "spawn the planner" with research/red-team waived — the
    same seam reopened along the mode axis. The projection therefore runs in
    the mode `_resolve_mode` reads back off the frontmatter this verb just
    wrote (the same resolver, asked the same question `next` will ask one
    beat later), so the agreement holds in every mode, not just the default.
    `state` stays "scaffolded" (scaffold's OWN job — creating the plan tree —
    genuinely finished, exit EXIT_DONE) even though the projected next_action
    itself may require a model turn; that distinction is `next`'s to report,
    not scaffold's to re-derive under a different name."""
    root = Path(root)
    positionals, opts, flags = args
    missing = [k for k in ("slug", "title") if k not in opts]
    if missing:
        return hs_run.StepResult(
            state="error",
            next_action="scaffold requires --%s" % " and --".join(missing),
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    import scaffold as scaffold_mod
    ident = opts.get("id") or scaffold_mod._now_id()
    phases_raw = opts.get("phases", "")
    phases = [s.strip() for s in phases_raw.split(",") if s.strip()]
    defaulted = not phases
    phases = phases or [_DEFAULT_PHASE]
    mode = opts.get("mode", "hard")
    tdd = "no-tdd" not in flags
    branch = opts.get("branch", "TBD")
    force = "force" in flags
    defer_suite = "defer-suite" in flags
    try:
        plan_dir = scaffold_mod.scaffold_plan(
            root=root, plan_id=ident, slug=opts["slug"], title=opts["title"], mode=mode,
            tdd=tdd, phases=phases, branch=branch, created=scaffold_mod.created_for(ident),
            force=force, defer_suite=defer_suite,
            decisions=scaffold_mod.parse_decisions(opts.get("decisions", "")))
    except (ValueError, FileExistsError, FileNotFoundError) as e:
        return hs_run.StepResult(
            state="error",
            next_action="scaffold failed: %s" % e,
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    fast, _ = _resolve_mode(plan_dir, False)
    projection = _project(plan_dir, fast=fast, root=root)
    warnings = list(projection.get("warnings", []))
    if defaulted:
        # A default that quietly applies is a decision nobody made: say it, once.
        warnings.append("no --phases given: wrote ONE phase, phases/phase-1-%s.md — for N "
                        "phases re-run with --id %s --slug %s --force --phases a,b,c, then "
                        "delete phases/phase-1-%s.md (--force rewrites, it never removes)"
                        % (_DEFAULT_PHASE, ident, opts["slug"], _DEFAULT_PHASE))
    return hs_run.StepResult(
        state="scaffolded",
        exit_code=EXIT_DONE,
        next_action=projection.get("next_action", ""),
        artifacts=[str(plan_dir)],
        warnings=warnings,
        extra={"summary_numbers": "1 plan scaffolded", "skipped": list(projection.get("skipped", []))},
    ).as_dict()


def _cmd_graph_impl(args, root) -> dict:
    """Wraps plan_graph.py's own read functions verbatim — no cycle/hazard/
    conflict math lives here, only the call order + hs-run's exit-code
    normalization.

    Exit-code mapping: plan_graph.py's OWN `_main()` already speaks hs-run's
    0/2 convention for this exact script (2 = hard stop on a missing `post` or,
    with `--require`, a missing sidecar; 0 = advisory otherwise) — goal E24
    flags this script as one of the ALREADY-ALIGNED ones. That does not make
    the mapping a free pass-through: it is still made explicit here (never a
    bare `return graph_main(argv)`) and tested like every other mapping in
    this module, per the instruction to never forward a wrapped script's exit
    code uninterpreted."""
    root = Path(root)
    positionals, opts, flags = args
    plan_dir, reason = _target_plan_dir(root, positionals, opts)
    if plan_dir is None:
        return hs_run.StepResult(
            state="error",
            next_action="cannot resolve a plan (%s) — pass --plan <dir>" % reason,
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    plan_dir = Path(plan_dir)
    if not (plan_dir / "plan.md").is_file():
        # `_target_plan_dir`/`_resolve_plan_arg` is deliberately permissive (it
        # must accept a plan dir that legitimately does not exist yet — the
        # scaffold-in-progress case), so a typo'd `--plan totally-bogus` on an
        # otherwise-empty repo used to sail through here and report the
        # "absent plan-graph" state at exit 0 — a mistyped, nonexistent target
        # is a blocking situation, not an advisory "nothing here yet" one
        # (this verb cannot do its own job — read a graph — without a real
        # plan to read it FROM).
        return hs_run.StepResult(
            state="error",
            next_action="%s has no plan.md — not a scaffolded plan; cannot read a "
                             "plan-graph that has no plan to belong to" % plan_dir,
            artifacts=[str(plan_dir)],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    import plan_graph
    graph = plan_graph.parse_phase_graph(plan_dir)
    require = "require" in flags
    sidecar = Path(plan_dir) / "plan-graph.yaml"
    if graph.get("error"):
        exit_code = EXIT_BROKEN if require else EXIT_DONE
        # "absent" vs "malformed" is read straight off the filesystem (does the
        # sidecar file exist at all?), never by pattern-matching plan_graph's
        # error STRING — a wording change there must not silently break this.
        state = "absent" if not sidecar.is_file() else "malformed"
        return hs_run.StepResult(
            state=state,
            next_action=graph["error"],
            artifacts=[str(sidecar)],
            warnings=[graph["error"]],
            exit_code=exit_code,
        ).as_dict()
    warnings = list(plan_graph.lint_no_status(graph))
    cycles = plan_graph.find_cycles(graph)
    batches = plan_graph.find_parallel_batches(graph)
    missing = plan_graph.find_missing_post(graph)
    bad_post = plan_graph.find_post_problems(graph)
    blocked = plan_graph.phase_holds(graph)["blocked"]
    # The same list plan_graph.py prints, grounded against the tree `plan_dir`
    # LIVES IN — never the ambient `root` this verb was invoked under.
    found = plan_graph.advisory_findings(graph, plan_dir)
    if found["scan_warning"]:
        warnings.append("import-edge-scan: %s" % found["scan_warning"])
    warnings.extend(found["lines"])
    phase_file_problems = found["phase_file_problems"]
    bad_files = [p["node"] for p in phase_file_problems] if require else []
    # A finding, not BROKEN: the graph parsed and the linter found a real,
    # fixable authoring defect (a node missing its `post:` obligation) — the
    # same shape as a secret scan finding a real secret. `absent`/`malformed`
    # above (the graph could not even be READ) are the genuinely broken cases.
    invalid = bool(missing or bad_files or bad_post)
    exit_code = EXIT_FINDINGS if invalid else EXIT_DONE
    state = "invalid" if invalid else "ok"
    if not invalid:
        next_action = "plan-graph.yaml valid — %d parallel batch(es)" % len(batches)
    else:
        next_action = "; ".join(
            ([("plan-graph.yaml missing 'post' for node(s): %s" % ", ".join(missing))]
             if missing else [])
            + ([("plan-graph.yaml `file:` points nowhere usable for node(s): %s"
                 % ", ".join(bad_files))] if bad_files else [])
            + ["plan-graph.yaml %s" % p for p in bad_post])
    return hs_run.StepResult(
        state=state,
        next_action=next_action,
        artifacts=[str(sidecar)],
        warnings=warnings,
        exit_code=exit_code,
        extra={"cycles": cycles, "parallel_batches": batches, "missing_post": missing,
               "bad_post": bad_post,
               "blocked_phases": blocked},
    ).as_dict()


# The two-beat model roles this floor supports (recon table A rows 3, 5b+5f,
# 7a) — matching subagent_type strings are the real ones the harness already
# ships (`harness/plugins/hs/agents/{researcher,planner,red-teamer}.md`).
_ROLES = {
    "researcher": {
        "subagent_type": "hs:researcher",
        "template": ("Research the open questions for the plan at %s. Write findings to "
                     "%s/research/<topic>.md (one file per aspect), >=3 independent "
                     "sources per important claim, end with a ranked conclusion. Return "
                     "the report path(s)."),
    },
    "planner": {
        "subagent_type": "hs:planner",
        # The phase-file TEMPLATE is named in the prompt, not left to the agent.
        # `--deep` and `--parallel` are SKILL flags that change what a phase file
        # must CONTAIN (a file inventory, a test-scenario matrix, a dependency
        # map; a dependency matrix and a file-ownership table), and measured, the
        # flag does change the output: 5/5 phase files grew the sections with it,
        # 0/5 without. On `--hard` the writing is DELEGATED, and a subagent sees
        # neither the flag nor the skill body — it saw "fill in the TBDs", so the
        # requirement died at the handoff.
        #
        # The prompt names the file rather than inlining the sections, so the
        # requirements keep one home. It cannot say "honour the mode frontmatter
        # declares" — measured on a scaffolded plan, the frontmatter carries
        # `mode` and `tdd` and nothing for deep/parallel, so those two reach disk
        # nowhere, and they are skill flags the CLI rejects at exit 2 rather than
        # argv it could read.
        #
        # So the requirement is addressed to the agent instead. The previous
        # wording — "the spawner must say so here" — put an instruction for the
        # calling model inside the text delivered to the subagent: the one party
        # that cannot act on it, and it read that line precisely on the runs
        # where the spawner had already forgotten. The agent CAN act on this
        # one; the plan files are in front of it, and it decides from what the
        # mode sections themselves say a deep or parallel plan looks like.
        "template": ("Fill plan.md + phases/phase-*.md at %s (replace every TBD with real "
                     "content) and author its plan-graph.yaml sidecar (edges + per-phase "
                     "files_to_create/files_to_modify + a 'post' artifact obligation per "
                     "node). Use any research reports under %s/research/. Phase files "
                     "follow the template in the plan skill's "
                     "references/phase-decomposition.md — read it first, including its "
                     "--deep and --parallel mode sections: apply either one this plan's "
                     "own scope calls for, and say in your summary which you applied and "
                     "why. Return a 1-line summary of what changed."),
    },
    "red-teamer": {
        "subagent_type": "hs:red-teamer",
        "template": ("Adversarially review plan.md + phase files at %s. Pick 2-4 personas "
                     "from the risk surface. Every finding needs file:line or a repro "
                     "command; cap 15 findings. Write ONE report for the round, naming "
                     "its personas inside, to %s/reports/"
                     "from-code-reviewer-to-planner-red-team-<round>-plan-review-report.md "
                     "— that file is the requested deliverable the plan gate reads, so a "
                     "default against writing report files does not apply to it. Its first "
                     "line is `verdict: PASS` or `verdict: REVISE`; its findings table's "
                     "first column is headed `id`, one row per finding. "
                     "Return the report path + a 1-line verdict."),
    },
}


def _next_redteam_round(plan_dir) -> str:
    """`r<N>`, N one past the red-team reports already in the plan's reports/ — a
    round's personas share one report, so the round, not a persona, names it."""
    reports = Path(plan_dir) / "reports"
    n = sum(1 for p in reports.glob("*.md") if _REDTEAM_PERSONA.match(p.name)) \
        if reports.is_dir() else 0
    return "r%d" % (n + 1)


def _cmd_prompt_impl(args, root) -> dict:
    """Beat one of the two-beat pattern: print the exact text to
    hand a subagent and mark the step waiting (EXIT_NEEDS_MODEL). This verb
    NEVER spawns anything itself — the outer model does that, then calls
    `record <role> --out <file>` to ingest the result."""
    root = Path(root)
    positionals, opts, _flags = args
    if not positionals:
        return hs_run.StepResult(
            state="error",
            next_action="usage: hs-run plan prompt <role> [--plan <dir>]",
            artifacts=[],
            warnings=["missing <role> positional"],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    role, rest = positionals[0], positionals[1:]
    if role not in _ROLES:
        return hs_run.StepResult(
            state="error",
            next_action="unknown role %r (known: %s)" % (role, ", ".join(sorted(_ROLES))),
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    plan_dir, reason = _target_plan_dir(root, rest, opts)
    if plan_dir is None:
        return hs_run.StepResult(
            state="error",
            next_action="cannot resolve a plan to prompt for (%s) — pass --plan <dir>" % reason,
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    spec = _ROLES[role]
    prompt_text = spec["template"] % (str(plan_dir), str(plan_dir))
    if role == "red-teamer":
        prompt_text = prompt_text.replace("<round>", _next_redteam_round(plan_dir))
    return hs_run.StepResult(
        state="waiting_on_model",
        exit_code=EXIT_NEEDS_MODEL,
        next_action="spawn Task(subagent_type=%r, ...) with the prompt below, then run: "
                         "hs-run plan record %s --out <file-the-subagent-wrote> --plan %s"
                         % (spec["subagent_type"], role, plan_dir),
        artifacts=[str(plan_dir)],
        warnings=[],
        extra={"role": role, "subagent_type": spec["subagent_type"], "prompt": prompt_text},
    ).as_dict()


def _cmd_record_impl(args, root) -> dict:
    """Beat two of the two-beat pattern: ingest a completed subagent's raw
    output. researcher/red-teamer roles place `--out`'s content at the
    canonical artifact path (a DETERMINISTIC placement gate around the
    MODEL-REQUIRED content, matching recon table A row 6d's own
    classification); planner edits plan.md/phases/plan-graph.yaml directly in
    place, so `--out` is optional there — only existence-checked, never
    relocated. Then re-projects via `_project` so the model does not need a
    second `next` call to see what comes after ingesting this one.

    The canonical destination is validated (goal F11) before any write:
    `--persona` is restricted to `[a-z0-9-]` (closes the path-traversal-shaped
    hole a persona like `a/../../../src/OWNED` opened) AND the resolved
    destination is routed through `fs_guard.assert_under(..., "plans")` — the
    same containment helper every other harness script's write goes through —
    so a write can never land outside the plan's own dir even if some future
    edit here forgets the persona check."""
    root = Path(root)
    positionals, opts, flags = args
    if not positionals:
        return hs_run.StepResult(
            state="error",
            next_action="usage: hs-run plan record <role> --out <file> [--plan <dir>]",
            artifacts=[],
            warnings=["missing <role> positional"],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    role, rest = positionals[0], positionals[1:]
    if role not in _ROLES:
        return hs_run.StepResult(
            state="error",
            next_action="unknown role %r (known: %s)" % (role, ", ".join(sorted(_ROLES))),
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    plan_dir, reason = _target_plan_dir(root, rest, opts)
    if plan_dir is None:
        return hs_run.StepResult(
            state="error",
            next_action="cannot resolve a plan to record for (%s) — pass --plan <dir>" % reason,
            artifacts=[],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()
    plan_dir = Path(plan_dir)

    # goal L4: `_resolve_plan_arg` (via `_target_plan_dir`) is DELIBERATELY
    # permissive — it accepts a bare name under plans/ that does not exist yet,
    # since `--plan` also has to work for a plan that legitimately IS being
    # scaffolded right now elsewhere. `fs_guard.assert_under` only checks that
    # the eventual write stays UNDER plans/, not that plan_dir names a real,
    # already-scaffolded plan — so a typo'd `--plan totally-bogus` sailed
    # straight through both checks, and `mkdir(parents=True, exist_ok=True)`
    # below silently manufactured a brand-new plan tree from nothing but a
    # typo. Refuse before any write, naming exactly what's missing.
    if not (plan_dir / "plan.md").is_file():
        return hs_run.StepResult(
            state="error",
            next_action="%s has no plan.md — refusing to record against a plan that "
                             "does not exist; scaffold it first: hs-run plan scaffold "
                             "--slug <slug> --title <title>" % plan_dir,
            artifacts=[str(plan_dir)],
            warnings=[],
            exit_code=EXIT_BROKEN,
        ).as_dict()

    out = opts.get("out")
    ingested = None
    if role in ("researcher", "red-teamer"):
        if not out:
            return hs_run.StepResult(
                state="error",
                next_action="role %r requires --out <file> (the subagent's raw output "
                                 "to place at the canonical artifact path)" % role,
                artifacts=[str(plan_dir)],
                warnings=[],
                exit_code=EXIT_BROKEN,
            ).as_dict()
        src = Path(out)
        if not src.is_file():
            return hs_run.StepResult(
                state="error",
                next_action="--out %s is not a file" % out,
                artifacts=[str(plan_dir)],
                warnings=[],
                exit_code=EXIT_BROKEN,
            ).as_dict()
        if role == "researcher":
            dest_dir = plan_dir / "research"
            dest = dest_dir / src.name
        else:
            # `unattributed`, not `combined`. The old default was a parameter
            # fallback wearing the name of a claim: a report filed with no
            # `--persona` said several personas went into it, which nothing here
            # knows. A label must never be promoted past what the caller supplied.
            persona = opts.get("persona", "unattributed")
            if not _PERSONA_RE.match(persona):
                return hs_run.StepResult(
                    state="error",
                    next_action="--persona %r is invalid — personas may contain only lowercase "
                        "letters, digits, and single hyphens between them (no path "
                        "separators, no leading/trailing hyphen)" % persona,
                    artifacts=[str(plan_dir)],
                    warnings=[],
                    exit_code=EXIT_BROKEN,
                ).as_dict()
            dest_dir = plan_dir / "reports"
            dest = dest_dir / ("from-code-reviewer-to-planner-red-team-%s-plan-review-report.md"
                                % persona)
            # Already filed under a red-team name in this plan's reports/: ingest it
            # where it is. A copy under the default persona put the same findings on
            # disk twice, and every gate reading reports/ counted them twice.
            if (_REDTEAM_PERSONA.match(src.name)
                    and src.resolve().parent == dest_dir.resolve()):
                dest = src
        try:
            fs_guard.assert_under(dest, "plans", root=root)
        except fs_guard.FenceError as e:
            return hs_run.StepResult(
                state="error",
                next_action=str(e),
                artifacts=[str(plan_dir)],
                warnings=[],
                exit_code=EXIT_BROKEN,
            ).as_dict()
        dest_dir.mkdir(parents=True, exist_ok=True)
        if dest.is_file() and src.resolve() != dest.resolve():
            # Refuse rather than overwrite. Measured: two ordinary red-team
            # passes with no `--persona` landed on the same path, the second
            # destroyed the first, and the gate downstream counted ONE report
            # labelled as a combined pass — evidence deleted at the step whose
            # entire job is producing evidence, with nothing recording that the
            # first report ever existed. The caller names the persona and both
            # survive. A deliberate refusal, not BROKEN: the write is entirely
            # possible, `record` declines it on purpose to protect the report
            # already on disk.
            return hs_run.StepResult(
                state="error",
                next_action=(
                    "%s already exists — recording over it would delete a report "
                    "nobody has read. Name this pass with `--persona <name>` so "
                    "both are kept, or remove the existing file if it is stale."
                    % dest),
                artifacts=[str(dest)],
                warnings=[],
                exit_code=EXIT_REFUSED,
            ).as_dict()
        if src.resolve() != dest.resolve():
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        ingested = str(dest)
    elif out:
        if not Path(out).is_file():
            return hs_run.StepResult(
                state="error",
                next_action="--out %s is not a file" % out,
                artifacts=[str(plan_dir)],
                warnings=[],
                exit_code=EXIT_BROKEN,
            ).as_dict()
        ingested = out

    # The flag WINS and the disk is DURABLE — the same two-source rule `next`
    # resolves by, so this verb's trailing projection cannot answer a different
    # question than a `next` run one beat later. Measured pre-fix: filing a
    # planner report on a `mode: fast` plan without retyping `--fast`
    # re-projected in hard mode — "spawn the researcher retroactively" — while
    # `next` on the identical tree said `needs_planner` with research waived.
    fast, conflict = _resolve_mode(plan_dir, "fast" in flags)
    result = dict(_project(plan_dir, fast=fast, root=root))
    if conflict:
        result.setdefault("warnings", []).append(conflict)
    result["ingested"] = IngestReceipt(role=role, path=ingested).as_dict()
    return result


# The `--fact-subjects` spelling itself (`module::name`, `pkg.mod::Class.meth`),
# found in plan prose. Anchored so a URL's `host/a::b` or a mid-word `x::` never
# starts a match.
_SUBJECT_RE = re.compile(
    r"(?<![\w/:.])([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*::[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)")


def _plan_named_subjects(plan_dir) -> list:
    """Symbols plan.md + the phase files name in `--fact-subjects` form, in
    first-seen order — what a caller of the planner step could pass."""
    out: list = []
    plan_dir = Path(plan_dir)
    for f in [plan_dir / "plan.md"] + _phase_files(plan_dir):
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _SUBJECT_RE.findall(text):
            if m not in out:
                out.append(m)
    return out


def _attach_fact_branch(result: dict, plan_dir) -> None:
    """`fact_branch` = the plan's declared `branch:`, ABSENT when it names none —
    read by `hs_run._apply_facts`, which warns when the checkout is elsewhere."""
    from hs_run_facts import engine as facts_engine
    branch = facts_engine.declared_branch(_frontmatter(Path(plan_dir) / "plan.md"))
    if branch:
        result.setdefault("fact_branch", branch)


def _redteam_fact_subjects(plan_dir) -> list:
    """The files the plan declares it will modify or delete — the ones whose
    importers a changed signature can break. A created file has none yet."""
    import plan_graph
    graph = plan_graph.parse_phase_graph(plan_dir)
    out: set = set()
    for node in (graph.get("subtasks") or {}) if "error" not in graph else ():
        out |= plan_graph._modified_or_deleted(graph, node)
    return sorted(out)


def _attach_fact_subjects(result: dict, opts: dict, plan_dir=None) -> None:
    """`--fact-subjects <qualname[,qualname...]>` -> `result["fact_subjects"]`
    via `hs_run.request_fact_subjects`, at ANY state: `needs_planner` declares
    `code.callers`/`code.mentions` (`subjects: explicit`) itself, and at every
    other state `hs_run._apply_facts` reads the same facts on request without
    moving the step. An empty/all-comma value names nothing, the same as no
    flag. Without the flag, `needs_planner` reports what the plan itself names
    (`fact_subjects_expected`), so the engine warns only when there WAS
    something to pass."""
    named = hs_run.request_fact_subjects(result, opts.get("fact-subjects"))
    if not named and plan_dir is not None and result.get("state") == "needs_redteam":
        result["fact_subjects"] = _redteam_fact_subjects(plan_dir)
    if plan_dir is not None and (named or result.get("state") == "needs_planner"):
        _attach_fact_branch(result, plan_dir)
    if not named and result.get("state") == "needs_planner" and plan_dir is not None:
        result["fact_subjects_expected"] = _plan_named_subjects(plan_dir)


def _cmd_next_impl(args, root) -> dict:
    """Project the plan domain's next actionable step from disk artifacts
    alone."""
    root = Path(root)
    positionals, opts, flags = args
    plan_dir, reason = _target_plan_dir(root, positionals, opts)
    if reason == "no_plan":
        return hs_run.StepResult(
            state="no_plan",
            exit_code=EXIT_DONE,
            # `--mode` is named even though it defaults to `hard`, because the
            # default is only right for one of the two lanes. A run invoked as
            # `/hs:plan --fast` that copies this line verbatim writes
            # `mode: hard`, and every later `--fast` then trips the frontmatter
            # disagreement warning — a warning about a mismatch this very hint
            # created. Naming the flag here is what lets the two agree from the
            # start; the warning stays for the case where they genuinely differ.
            next_action="no plan exists yet — scaffold one: hs-run plan scaffold "
                             "--slug <slug> --title <title> --phases <a,b,c> "
                             "[--mode fast|hard] (default hard; pass fast if the "
                             "skill was invoked with --fast, so the frontmatter "
                             "and the run agree)",
            artifacts=[],
            warnings=_absent_standards(root),
            # `checklist: []` alongside `summary_numbers` — no plan dir resolved
            # at all, so there is no board to walk; both fields still ride,
            # matching what the retired `status` verb reported here, so a caller
            # that only ever spoke to `status` sees no field vanish under `next`.
            extra={"summary_numbers": "0 plans found", "checklist": []},
        ).as_dict()
    if reason == "multiple_plans":
        dirs = _plan_dirs(root)
        return hs_run.StepResult(
            state="multiple_plans",
            # 0, on the registry's signed reading: "Ambiguity, not failure —
            # name the target with `--plan <dir>` rather than guessing", filed
            # alongside `waiting_on_model` as an ORDINARY STEP, not an error.
            # Patched to BROKEN here on the general argument that a verb asking
            # the caller to disambiguate cannot be finished, then reverted: the
            # measurement (this exits 0) was right, the framing was not.
            exit_code=EXIT_DONE,
            next_action=("more than one plan dir found — pick one via --plan <dir>: %s"
                         % ", ".join(str(d) for d in dirs)),
            artifacts=[str(d) for d in dirs], warnings=[],
            # No single target resolved, so the board has nothing to walk either
            # — same reasoning as the `no_plan` branch just above.
            extra={"summary_numbers": "%d plan(s) found" % len(dirs), "checklist": []},
        ).as_dict()
    fast, conflict = _resolve_mode(plan_dir, "fast" in flags)
    result = _project(plan_dir, fast=fast, root=root)
    if conflict:
        result.setdefault("warnings", []).append(conflict)
    handoff = _handoff_report(plan_dir, root)
    result["handoff_anchors"] = handoff["counts"]
    if handoff["warnings"]:
        result.setdefault("warnings", []).extend(handoff["warnings"])
    if redteam_disposition.disposition_rows(Path(plan_dir) / "plan.md") is not None:
        try:
            cites = redteam_disposition.check_cites(plan_dir, root)
        except Exception as e:  # noqa: BLE001 — an advisory never breaks `next`
            cites = {"counts": {"error": "%s: %s" % (type(e).__name__, e)}, "warnings": [
                "disposition cites could not be checked: %s: %s" % (type(e).__name__, e)]}
        result["disposition_cites"] = cites["counts"]
        result.setdefault("warnings", []).extend(cites["warnings"])
    _attach_fact_subjects(result, opts, plan_dir)
    return result


def _handoff_report(plan_dir, root) -> dict:
    """Hand-off anchors this plan writes into others — counted always, warned on
    when one no longer holds or cannot be checked, and targets it drifted. A
    target's state is its own `_project` answer — never a second reading."""
    import plan_handoff

    def state_of(d):
        return _project(d, fast=_resolve_mode(d, False)[0], root=root).get("state")
    try:
        return plan_handoff.report(plan_dir, root, state_of)
    except Exception as e:  # noqa: BLE001 — an advisory never breaks `next`
        return {"counts": {"error": "%s: %s" % (type(e).__name__, e)},
                "warnings": ["hand-off anchors could not be checked: %s: %s" % (type(e).__name__, e)]}



def _absent_standards(root) -> list:
    """Warn once, at `no_plan`, for a routed standards document that is not there.

    The envelope already NAMES these at this state, and naming a path is where the
    promise is made: a consumer reading `references` has no way to tell an absent file
    from one it simply has not opened yet. `refs_for` joins strings and returns them —
    nothing between the registry row and the agent checks that any of it resolves.

    Scoped to `no_plan` on purpose. The paths are repo-relative to the CONSUMING
    project, so the answer is a property of that project, not of the run — it cannot
    change mid-plan, and repeating it at every state would train the reader to skip
    warnings. `no_plan` is also the one state whose whole job is "nothing exists yet",
    which is the moment a missing base is still cheap to fix.

    A warning rather than an error: the skill body says STOP, but a project may
    legitimately be planning its own standards, and a CLI that refused would make the
    first plan in a fresh repo impossible. What was missing was not enforcement, it was
    the run saying out loud that it is planning on an empty base."""
    root = Path(root)
    routing = _routing_row("no_plan")
    absent = [d for d in (routing.get("docs") or []) if not (root / d).is_file()]
    if not absent:
        return []
    one = len(absent) == 1
    return ["planning on an empty base: %s named at this step %s not present under %s "
            "— author %s (see harness/scripts/scaffold_standards.py) or the plan will "
            "invent standards the rest of the repo does not share"
            % (", ".join(absent), "is" if one else "are", root, "it" if one else "them")]


def _routing_row(state: str) -> dict:
    """The `no_plan` routing row, or an empty dict when routing is unreadable.

    Read through the registry rather than a second hardcoded list of the same three
    paths: a copy here would keep warning about a document the table stopped routing,
    which is the drift the routing table exists to remove."""
    try:
        import hs_run
        blk = (hs_run.load_step_routing() or {}).get("plan") or {}
        return (blk.get("states") or {}).get(state) or {}
    except Exception:
        return {}


# `status` (a bare, no-argument, nothing-written, same-state-as-`next` verb —
# USR-HARNESS-HS-RUN-ONE-DOOR ground: it earned no row) is retired. Its only
# content `next` did not already carry — the full per-step checklist and its
# one-line summary — now rides `next` itself, via `_project()` above.


SCAFFOLD = _plan_verb(_cmd_scaffold_impl,
                      options=("--slug", "--title", "--mode", "--phases", "--id", "--branch",
                               "--decisions"),
                      flags=("--force", "--no-tdd", "--defer-suite"),
                      helps={"--phases": _PHASES_HELP})
GRAPH = _plan_verb(_cmd_graph_impl, options=("--plan",), flags=("--require",))
PROMPT = _plan_verb(_cmd_prompt_impl, options=("--plan",))
RECORD = _plan_verb(_cmd_record_impl, options=("--plan", "--out", "--persona"), flags=("--fast",))
NEXT = _plan_verb(_cmd_next_impl, options=("--plan", "--fact-subjects"), flags=("--fast",),
                  helps={"--fact-subjects": hs_run.FACT_SUBJECTS_HELP})


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
        % (Path(__file__).name, 'plan'))
    raise SystemExit(EXIT_BROKEN)
