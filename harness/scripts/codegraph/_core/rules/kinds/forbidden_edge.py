"""`forbidden_edge` — no import edge from one set of files into another.

`from:` and `to:` are path globs (the descriptor's own `path_matches`:
fnmatch, plus `prefix/**` for a subtree). Every import edge in the snapshot
whose importer matches `from` and whose target module's file matches `to` is
a violation — module-level AND function-local: a lazy edge imports fine at
load time and dies on exactly the branch that never runs, so it counts. So
does a type-only one (under `if TYPE_CHECKING:`): it never loads, but a law
over layers is a dependency direction, and naming a forbidden type in an
annotation is coupling. `include_type_only: false` leaves them out, per rule.

PENDING (`pending: FILE`, YAML). A forbidden edge whose fix is DECIDED but not yet done
waits in a list, each line judged the moment it is declared:

    pending:
    - {from: <importer path>, to: <imported path>, decision: DEC-...,
       becomes: {from: <path>, to: <path>}}

`becomes` is the edge the fix turns it into — answerable NOW, where a deadline only moves.
A valid line turns its edge into a NOTE: parked work, not a defect, and not hidden either.
A line lies four ways, each a violation: `pending-malformed` (not exactly those keys),
`pending-decision-unreadable` (no DEC `decision_register.is_readable_id` reads — the
retired `D-NN` included), `pending-becomes-forbidden` (the result is still an edge this
rule forbids: a fix that fixes nothing), `pending-edge-gone` (the rule finds no such
forbidden edge: the fix happened, and the line reads as work that is not there). No file
is said in a note, never read as a clean list.

WHAT IT CANNOT SEE
    * Edges the snapshot does not resolve: `importlib.import_module("x")`,
      `__import__`, sys.path tricks. A green is "no edge AMONG THE VISIBLE".
    * A target outside the scanned roots has no file, so it matches no `to`.
    A `from` or `to` that names no scanned file is refused, not read as green.
    * Whether a `becomes` will really be the edge the fix produces: the line is checked
      for being self-consistent, not prophetic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...descriptor.model import path_matches
from ..base import (RuleContext, RuleResult, RulesError, Violation,
                    register_kind, require_params)

_EDGE_KEYS = (("imports_module_level", "module"), ("imports_function_local", "lazy"),
              ("imports_type_only", "type_only"))


def _globs(rule_id: str, params: dict[str, Any], key: str) -> tuple[str, ...]:
    raw = params[key]
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(g, str) and g for g in raw):
        raise RulesError(f"rule {rule_id!r}: `{key}` must be a list of path globs")
    return tuple(raw)


_PENDING_KEYS = {"from", "to", "decision", "becomes"}


def load_pending(p: Path) -> tuple[list[dict[str, Any]], list[str]] | None:
    """(valid-shaped lines, malformed-line messages); None when there is no file."""
    if not p.is_file():
        return None
    import yaml_io
    doc = yaml_io.yaml_load(p.read_text(encoding="utf-8")) or {}
    rows = doc.get("pending") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return [], [f"{p.name}: `pending:` must be a list"]
    out: list[dict[str, Any]] = []
    bad: list[str] = []
    for i, row in enumerate(rows, 1):
        b = row.get("becomes") if isinstance(row, dict) else None
        if not isinstance(row, dict) or set(row) != _PENDING_KEYS or not isinstance(b, dict) \
                or set(b) != {"from", "to"} \
                or not all(isinstance(x, str) and x for x in (row["from"], row["to"], row["decision"],
                                                              b["from"], b["to"])):
            bad.append(f"{p.name} line {i}: needs exactly from/to/decision/becomes{{from, to}}, got "
                       f"{sorted(row) if isinstance(row, dict) else type(row).__name__}")
            continue
        out.append({"i": i, "from": row["from"], "to": row["to"], "decision": row["decision"],
                    "becomes": (b["from"], b["to"])})
    return out, bad


class ForbiddenEdgeCheck:
    CAUSES = ("forbidden-edge", "pending-malformed", "pending-decision-unreadable",
              "pending-becomes-forbidden", "pending-edge-gone")

    def __init__(self, rule_id: str, params: dict[str, Any]) -> None:
        self.rule_id = rule_id
        self.src = _globs(rule_id, params, "from")
        self.dst = _globs(rule_id, params, "to")
        type_only = params.get("include_type_only", True)
        if not isinstance(type_only, bool):
            raise RulesError(f"rule {rule_id!r}: `include_type_only` must be true or false")
        self.keys = tuple(k for k in _EDGE_KEYS if type_only or k[1] != "type_only")
        self.pending = Path(params["pending"]) if params.get("pending") else None

    def check(self, ctx: RuleContext) -> RuleResult:
        if ctx.idx is None:
            raise RulesError(f"rule {self.rule_id!r}: needs a snapshot index")
        mods = [m for ms in ctx.idx["units"].values() for m in ms]
        path_of = {m["module"]: str(m.get("path") or "") for m in mods}
        for key, globs in (("from", self.src), ("to", self.dst)):
            if not any(path_matches(p, g) for p in path_of.values() if p for g in globs):
                raise RulesError(f"rule {self.rule_id!r}: `{key}: {list(globs)}` names no file in "
                                 f"the snapshot — a law over nothing is green while it checks nothing")
        v: list[Violation] = []
        seen = {kind: 0 for _, kind in self.keys}
        for m in mods:
            src_path = path_of[m["module"]]
            if not any(path_matches(src_path, g) for g in self.src):
                continue
            for key, kind in self.keys:
                for tgt in m.get(key, []):
                    seen[kind] += 1
                    dst_mod = tgt.split("::", 1)[0]
                    dst_path = path_of.get(dst_mod, "")
                    if dst_path and any(path_matches(dst_path, g) for g in self.dst):
                        v.append(Violation(
                            self.rule_id, "forbidden-edge", f"{m['module']}  ->  {tgt}  ({kind})",
                            {"kind": kind, "src": src_path, "dst": dst_path}))
        notes = [f"edges out of `from`: {seen}"]
        if self.pending is not None:
            v, notes = self._judge_pending(ctx.repo, self.pending, v, notes)
        return RuleResult(self.rule_id, "error", tuple(v), tuple(notes), metrics={"edges_from": seen})

    def _forbids(self, src: str, dst: str) -> bool:
        return any(path_matches(src, g) for g in self.src) and any(path_matches(dst, g) for g in self.dst)

    def _judge_pending(self, repo: Path, pending: Path, v: list[Violation],
                       notes: list[str]) -> tuple[list[Violation], list[str]]:
        import decision_register
        p = pending if pending.is_absolute() else repo / pending
        loaded = load_pending(p)
        if loaded is None:
            return v, [*notes, f"no waiting list at {pending} — nothing declared, "
                               f"which is not the same as declared-and-clean"]
        lines, bad = loaded
        out = [Violation(self.rule_id, "pending-malformed", m, {"file": str(pending)}) for m in bad]
        found = {(x.detail["src"], x.detail["dst"]) for x in v}
        parked: set[tuple[str, str]] = set()
        for e in lines:
            where = f"{pending.name} line {e['i']}: {e['from']} -> {e['to']}"
            ok = True
            if not decision_register.is_readable_id(e["decision"]):
                ok = False
                out.append(Violation(self.rule_id, "pending-decision-unreadable",
                                     f"{where} — `{e['decision']}` is no DEC this harness reads",
                                     {"decision": e["decision"]}))
            if self._forbids(*e["becomes"]):
                ok = False
                out.append(Violation(self.rule_id, "pending-becomes-forbidden",
                                     f"{where} — becomes {e['becomes'][0]} -> {e['becomes'][1]}, still "
                                     f"forbidden: a fix that fixes nothing", {"becomes": list(e["becomes"])}))
            if (e["from"], e["to"]) not in found:
                ok = False
                out.append(Violation(self.rule_id, "pending-edge-gone",
                                     f"{where} — no such forbidden edge in the graph: the fix happened; "
                                     f"delete the line in the SAME commit", {"from": e["from"], "to": e["to"]}))
            if ok:
                parked.add((e["from"], e["to"]))
        out += [x for x in v if (x.detail["src"], x.detail["dst"]) not in parked]
        notes = [*notes, f"{len(parked)} forbidden edge(s) DECIDED and waiting (work, not violations): "
                         + (", ".join("%s -> %s" % pr for pr in sorted(parked)) or "—")]
        return out, notes


register_kind("forbidden_edge")(require_params("from", "to")(ForbiddenEdgeCheck))
