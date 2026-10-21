"""`ledger` — the port ledger's twelve laws (L0-L11), declared like every
other rule and run under `hs-code check` on the `port` gate page.

Ported (algorithm-faithful) from `ob-tools@60075cb:packages/portkit/src/
portkit/gates/ledger.py` (`LedgerCheck`, 84 lines) — an adapter over
`port_ledger_laws.collect`, wrapping each `Finding` as a `Violation` and
folding `LedgerScan`'s non-finding categories (uncheckable blockers,
pending decisions, drift-evidence-lost, whether the new-tree half even
ran) into `notes`, never silently dropped.

WHY THIS KIND DOES NOT READ `ctx.idx`/`ctx.snapshots`
    `map`'s own kind (this package's sibling) needs BOTH a snapshot's data
    AND the live tree at once, so it reads them off `ctx`. A ledger check
    needs only the ledger file plus two NAMED snapshots — this tree
    already owns exactly one door onto "a batch's own two snapshots, by
    name" (`port_batch_reader.load_batch`/`find_source`/
    `pinned_frozen_commit`/`new_batch_anchor`), and `port_ledger_laws.
    collect` already takes a `Batch` + its resolved source directly. Re-deriving
    that through `ctx.idx`/`ctx.snapshots` (which carry raw `_index.json`
    payloads, not a `Batch`) would mean either smuggling a `Batch` through
    `ctx.descriptor` (a field for something else entirely) or resolving the
    batch TWICE — once by the caller to build `ctx`, once again here to
    call `collect`. `ctx.repo` is the one member this kind needs: `collect`
    resolves the rest itself from `params["batch"]`, the same param shape
    `map`'s own port-gate-page wiring already gives each row (`{"batch":
    <name>}`).

L0 (a broken ledger line) never reaches `collect()` — `Ledger.load_checked`
already refuses it (`port_ledger_writer.BrokenLedgerError`) — and this kind
re-raises that, and every other "cannot even measure" failure (no
`frozen_snapshot` declared, a frozen snapshot disagreeing with its own
pin, an unknown `--source`), as `RulesError` (this engine's own "cannot
run", exit 2 — the same answer `map`'s kind gives for a missing snapshot).
Never a `Violation`: a rule whose first finding is "the ledger cannot even
be read" is not a softer verdict than every other rule, it is a different
KIND of failure.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..base import RuleContext, RuleResult, RulesError, Violation, register_kind, require_params

#: Every code `port_ledger_laws.collect` can hand back as a `Finding.law` —
#: L0 excluded (see the module docstring: it never reaches `collect`).
CAUSES = ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "L9", "L10", "L11")


class LedgerCheck:
    CAUSES = CAUSES

    def __init__(self, rule_id: str, params: dict[str, Any]) -> None:
        self.rule_id = rule_id
        self.batch_name: str = params["batch"]
        self.source_id: "str | None" = params.get("source")
        raw_decisions = params.get("decisions_root")
        self.decisions_root: "Path | None" = Path(raw_decisions) if raw_decisions else None

    def check(self, ctx: RuleContext) -> RuleResult:
        import hs_family
        import port_batch_reader
        import port_ledger_laws
        import port_ledger_reader
        from port_ledger_writer import BrokenLedgerError, Ledger

        port_root = ctx.repo
        try:
            batch = port_batch_reader.load_batch(port_root, self.batch_name)
        except (port_batch_reader.BatchNotFoundError,
                port_batch_reader.BatchConfigError) as e:
            raise RulesError(f"rule {self.rule_id!r}: {e}") from e

        source, problem = port_batch_reader.find_source(batch, self.source_id)
        if problem:
            raise RulesError(f"rule {self.rule_id!r}: {problem}")

        try:
            ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
        except BrokenLedgerError as e:
            raise RulesError(f"rule {self.rule_id!r}: {e}") from e

        decisions_root = self.decisions_root
        if decisions_root is not None and not decisions_root.is_absolute():
            decisions_root = port_root / decisions_root
        try:
            scan = port_ledger_laws.collect(
                ledger, port_root, batch, source, decisions_root=decisions_root)
        except (hs_family.UsageError, hs_family.RefusedError) as e:
            raise RulesError(f"rule {self.rule_id!r}: {e}") from e

        v = tuple(Violation(self.rule_id, f.law, f"{f.node}: {f.msg}", {"node": f.node})
                  for f in scan.findings)
        notes: "list[str]" = [
            "%d event(s), old key %s (%d row(s) under it)"
            % (scan.n_events, scan.old_key, scan.old_key_rows)]
        if scan.new_ran:
            notes.append("new key %s" % scan.new_key)
        else:
            notes.append(
                "batch %r has no `new_snapshot` declared — L7/L10/L11 did not run"
                % batch.name)
        if scan.uncheckable:
            notes.append("%d blocker(s) carry a prefix this tree cannot check "
                         "(gate:/test:/external:)" % scan.uncheckable)
        if scan.pending_decisions:
            notes.append("%d decision blocker(s) well-formed but not yet in the "
                         "register: %s" % (len(scan.pending_decisions),
                                           ", ".join(sorted(scan.pending_decisions))))
        if scan.drift_lost:
            notes.append("%d destination(s) written at a new-tree anchor with no "
                         "snapshot left to compare against — drift undecidable, "
                         "not clean" % scan.drift_lost)
        return RuleResult(self.rule_id, "error", v, tuple(notes),
                          metrics={"events": scan.n_events, "findings": len(v),
                                   "uncheckable": scan.uncheckable,
                                   "pending_decisions": len(scan.pending_decisions),
                                   "drift_lost": scan.drift_lost})


register_kind("ledger")(require_params("batch")(LedgerCheck))
