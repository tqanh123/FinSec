#!/usr/bin/env python3
"""write_verification.py — deterministic, preferred write path for a plan's
verification artifact.

The PostToolUse hook (phase_progress_writer.py) only snapshots a verification
written through the Write tool. A verification written from Bash never trips the
hook, so it gets no per-phase snapshot and no lifecycle flip — the plan sticks at
"not done" and the ship gate blocks. This script closes that hole: it writes the
canonical verification, drives the SAME shared snapshot + lifecycle as the hook
(verification_snapshot.py — no logic duplicated), then self-verifies, in one run.

Two input modes (mutually exclusive):
  flag-builder:  --phase p1 --stage ship --verdict PASS --check unit:PASS ...
                 (actor + ts auto-filled)
  --from FILE|-  read a composed YAML/JSON record (- = stdin); missing
                 actor/ts/plan are filled.

The phase must be a real node in the plan's plan-graph.yaml: a casing typo
(A1 vs a1) would snapshot to verification-A1.json, never match node a1, and the
plan would never reach N/N — exactly the class of bug this path exists to kill.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_THIS = os.path.dirname(os.path.abspath(__file__))
if _THIS not in sys.path:
    sys.path.insert(0, _THIS)
_HOOKS = str(Path(__file__).resolve().parent.parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.append(_HOOKS)
import verification_snapshot as vsnap  # noqa: E402
import plan_graph  # noqa: E402
import test_policy  # noqa: E402
from hook_runtime import resolve_actor  # noqa: E402

_VERDICTS = {"PASS", "PASS_WITH_RISK", "BLOCKED"}
_USAGE = (
    "usage: write_verification.py <plan_dir> "
    "(--phase ID --verdict V [--stage S] [--check name:status ...] | --from FILE|-)")


def _die(msg, code=2):
    sys.stderr.write("error: %s\n%s\n" % (msg, _USAGE))
    raise SystemExit(code)


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _parse_checks(items):
    checks = []
    for it in items or []:
        name, sep, status = it.partition(":")
        if not name or not sep or not status:
            _die("bad --check %r — expected name:status" % it)
        checks.append({"name": name, "status": status})
    return checks


def _json_or_die(raw):
    """json.loads with a friendly _die on malformed input — the YAML fallback path
    must not surface a raw JSONDecodeError traceback to the caller."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        _die("--from source is not valid JSON/YAML: %s" % e)


def _read_from(src):
    raw = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
    # JSON is a strict subset of YAML 1.1 for our records; one parser handles both.
    try:
        import yaml_io
        rec = yaml_io.safe_load(raw)
    except ImportError:
        rec = _json_or_die(raw)
    except (yaml_io.YAMLError, ValueError):
        # PyYAML's timestamp/int constructors raise a bare ValueError (not a
        # YAMLError subclass) on an out-of-range value like `ts: 2024-13-45`;
        # treat both as a parse failure, matching artifact_check._parse_artifact_text.
        rec = _json_or_die(raw)
    if not isinstance(rec, dict):
        _die("--from source is not a mapping/object")
    return rec


def _build_record(args, plan_name):
    if args.from_src is not None:
        if args.stage or args.verdict or args.check:
            _die("--from carries the record; --stage/--verdict/--check would rewrite it — "
                 "put them in the file")
        rec = _read_from(args.from_src)
        if args.phase:
            # A cross-check, never an override: the file says which phase it verified.
            named = rec.get("phase") if isinstance(rec, dict) else None
            if named and named != args.phase:
                _die("--phase %s but %s records phase %s — fix one of them"
                     % (args.phase, args.from_src, named))
            if isinstance(rec, dict) and not named:
                rec["phase"] = args.phase
    else:
        absent = [f for f, v in (("--phase", args.phase), ("--verdict", args.verdict)) if not v]
        if absent:
            _die("missing %s (or use --from)" % " and ".join(absent))
        rec = {
            "stage": args.stage or "cook",
            "plan": plan_name,
            "checks": _parse_checks(args.check),
            "verdict": args.verdict,
            "phase": args.phase,
        }
    rec.setdefault("plan", plan_name)
    # Guard the CALLS, not just the result: setdefault always evaluates its arg, so
    # the bare form shells out to git (resolve_actor) even when actor is present.
    if "actor" not in rec:
        rec["actor"] = resolve_actor()
    if "ts" not in rec:
        rec["ts"] = _now_iso()
    if rec.get("verdict") not in _VERDICTS:
        _die("verdict must be one of %s" % sorted(_VERDICTS))
    if not rec.get("phase"):
        _die("record has no phase")
    return rec


def _check_phase_is_node(plan_dir, phase):
    """Reject a phase that is not a plan-graph node. Degrade (warn + allow) only
    when the graph itself can't be parsed — never silently accept a bad phase
    against a graph that parsed fine."""
    graph = plan_graph.parse_phase_graph(plan_dir)
    if "error" in graph:
        sys.stderr.write("[advisory] plan-graph unparsable (%s) — skipping phase "
                         "node check\n" % graph["error"])
        return
    nodes = plan_graph._all_nodes(graph)
    if phase not in nodes:
        _die("phase %r is not a plan-graph node; valid nodes: %s%s"
             % (phase, sorted(nodes), plan_graph.phase_spelling_hint(graph, phase)))


_LEGAL_STATUSES = ("PASS", "FAIL", "SKIP", "BLOCKED")


def _invalid_statuses(rec) -> list:
    """Check statuses outside the schema enum, as human-readable strings.

    The enum used to be {PASS, FAIL, SKIP}, and nothing enforced it at write time
    — so six spellings appeared in live artifacts (BLOCKED, UNMEASURED, UNOWNED,
    UNPROVABLE, DEFERRED_TO_MAIN_TREE, BLOCKED_NO_LLM_NETWORK). They were not
    carelessness: FAIL means "measured, and it is wrong", and people needed a way
    to say "could not measure at all". Those are different facts and a reader has
    to tell them apart, so BLOCKED is now first-class and names the hole.

    BLOCKED is a vocabulary change, NOT a weakening: it is absent from the gate's
    PASS/SKIP allowlist, so it stops a hard stage exactly like FAIL. What it buys
    is a report that can count holes separately from defects.

    Everything else is rejected here, at write time, naming the legal set — an
    off-enum status used to travel silently until a gate blocked on it weeks later
    with no hint of what the writer had actually meant.
    """
    bad = []
    for c in rec.get("checks") or []:
        if not isinstance(c, dict):
            bad.append("a check entry is not a mapping: %r" % (c,))
            continue
        st = c.get("status")
        if st not in _LEGAL_STATUSES:
            bad.append(
                "check %r has status %r — legal values are %s. Use BLOCKED when "
                "the check could not be measured at all (missing tool, no network, "
                "not yet owned); FAIL means it ran and came back wrong."
                % (c.get("name", "?"), st, "/".join(_LEGAL_STATUSES)))
    return bad


_EXAMPLE_TOOLS = (("lint", "ruff"), ("typecheck", "mypy"), ("unit", "full-suite"))


def _examples(valid):
    """Example check names drawn from the types THIS policy accepts — an advisory that
    rejects a name must never offer that same name as the right form."""
    names = ["%s/%s" % (t, tool) for t, tool in _EXAMPLE_TOOLS if t in valid]
    names = names or ["%s/<name>" % t for t in sorted(valid)[:3]]
    return (", ví dụ " + ", ".join(names)) if names else ""


def _validate_check_names(rec, plan_dir):
    """Apply the off|soft|hard ramp to check names not in test-policy test_types.
    Resolution can't be self-contradictory: the mode lives INSIDE the policy, so
    a load failure can't be classified as hard — it degrades to soft (warn, still
    write), never off (off would swallow the very signal test-policy exists for).
    Only enforce hard when the policy actually parsed and said hard."""
    try:
        policy = test_policy.load_test_policy(
            root=plan_graph.root_for_plan_dir(plan_dir))
    except test_policy.TestPolicyError as e:
        sys.stderr.write(
            "[advisory] write_verification: test-policy đọc-lỗi (%s) — đang chạy "
            "soft mặc định, KHÔNG kiểm được tên check. Sửa config để bật lại "
            "kiểm tên.\n" % e)
        return  # degrade to soft: do not block on a broken config

    mode = policy.get("check_name_validation", "soft")
    unknown = test_policy.unknown_check_names(rec, policy)
    if not unknown:
        return
    valid = set((policy.get("test_types") or {}).keys())
    listing = "{%s}" % ", ".join(sorted(valid))
    msg = ("write_verification: check name(s) %s KHÔNG nằm trong test-policy "
           "test_types %s. Tên sai = DoD gate sẽ KHÔNG thấy kết quả này → ship "
           "rớt dù test xanh. Giữ tên thật của công cụ dưới một loại chuẩn: "
           "<type>/<name>%s. Verify lại "
           "với tên đúng sẽ thay bản ghi này (bản cũ chuyển sang phase-superseded)."
           % (sorted(unknown), listing, _examples(valid)))
    if mode == "hard":
        _die(msg, code=1)
    sys.stderr.write("[advisory] %s\n" % msg)  # soft: warn loudly, still write


def _canonical_target(plan_dir):
    """The canonical file the snapshot loader will read (.yaml preferred, .json
    legacy). When neither exists, default to .json (the snapshot output form)."""
    art = plan_dir / "artifacts"
    yaml_p, json_p = art / "verification.yaml", art / "verification.json"
    if yaml_p.exists():
        return yaml_p, json_p
    return json_p, yaml_p


def _atomic_write(target, rec):
    # Route through the shared gate-artifact writer: it stamps run_seq (D1) from the
    # orchestrator-exported env and does the same-dir .tmp + same-volume assert +
    # os.replace this used to do inline. Thin wrapper so _self_verify + callers are
    # untouched; a dev without an orchestrator gets run_seq:null (back-compat).
    import artifact_io
    artifact_io.stamp_and_write(target, rec)


def _self_verify(plan_dir, rec):
    """After snapshot, confirm the per-phase snapshot holds THIS write.
    (a) it does -> silent; (b) absent + THIS write is PASS -> nonzero exit, a
    script-path regression (a PASS write must always leave a snapshot behind);
    (c) absent + THIS write is non-PASS -> silent (a phase that never earned a
    PASS has no snapshot by design); (d) present but another record -> the
    snapshot kept the record that first counted the phase, so say where THIS
    write lives — any difference, a same-verdict re-verify included."""
    phase = vsnap._safe_phase(rec.get("phase"))
    if phase is None:
        return
    snap = plan_dir / "artifacts" / ("verification-%s.json" % phase)
    if not snap.exists():
        if rec.get("verdict") in vsnap._PASS_VERDICTS:
            _die("self-verify: PASS+phase written but snapshot %s is missing — "
                 "the script write path failed to snapshot" % snap.name, code=1)
        return
    try:
        got = json.loads(snap.read_text(encoding="utf-8"))
    except Exception:
        return
    written, _problem = vsnap.artifact_check._load_artifact(plan_dir, "verification")
    if got == written:
        return
    sys.stderr.write(
        "[advisory] write_verification: %s keeps an earlier record of phase "
        "%s (verdict %s, ts %s) — THIS write did NOT take there (first-wins). This "
        "write (verdict %s) is the phase's latest record: phase-latest-%s.json.\n"
        % (snap.name, phase, got.get("verdict"), got.get("ts"), rec.get("verdict"), phase))


def _fill_blast_radius(rec: dict, root) -> None:
    """`rec["blastRadius"]` from `hs_run_facts.engine.resolve()`
    (`code.importers` + `code.tests_reached`, DEFAULT subjects -- the same
    diff-vs-merge-base + dirty-file scope cook/review already read through
    `_default_subjects`) — the ONE writer this field has (schema's own
    description: "never hand-authored"). Never leaves the key half-filled:
    every branch below sets it to something, even a hard failure of this
    function's own machinery, because `artifact_check.py`'s gate treats a
    MISSING key (never an `unavailable`/`stale` one) as the defect on a
    code-change verification."""
    try:
        import hs_run
        import hs_run_facts.engine as facts_engine
    except Exception as e:  # noqa: BLE001 — an import hiccup degrades, never blocks the write
        rec["blastRadius"] = {"unavailable": True,
                              "reason": "facts engine unavailable: %s: %s" % (type(e).__name__, e)}
        return
    try:
        policy = hs_run.fact_providers_policy()
        outcome = facts_engine.resolve(
            root=root, declared=["code.importers", "code.tests_reached"], subjects=None,
            providers=policy["providers"], sources=policy["sources"],
            domain="write_verification", state="blast_radius")
    except Exception as e:  # noqa: BLE001 — never block a verification write on this
        rec["blastRadius"] = {"unavailable": True,
                              "reason": "blast-radius computation crashed: %s: %s"
                                        % (type(e).__name__, e)}
        return

    facts = outcome.get("facts") or {}
    importers = facts.get("code.importers") or {}
    tests = facts.get("code.tests_reached") or {}
    # Both facts share ONE snapshot selection on this call -- an outcome
    # that rewrote the top-level `state` (facts_stale / broken) means
    # NEITHER fact has a real value; read that off the outcome itself
    # rather than each fact's own marker shape twice.
    if outcome.get("state") == "facts_stale":
        rec["blastRadius"] = {
            "stale": True,
            "reason": importers.get("reason") or tests.get("reason")
                     or "code-graph snapshot is stale",
        }
        return
    if importers.get("unavailable") or tests.get("unavailable"):
        rec["blastRadius"] = {
            "unavailable": True,
            "reason": importers.get("reason") or tests.get("reason")
                     or "code facts unavailable",
        }
        return
    rec["blastRadius"] = {
        "reverseDeps": importers.get("value") or {},
        "testsTouched": tests.get("value") or [],
        "basis": "code.importers + code.tests_reached (hs_run_facts.engine), default subjects",
        "snapshot": importers.get("snapshot") or tests.get("snapshot"),
    }


def _maybe_fill_blast_radius(rec: dict, plan_dir: Path) -> None:
    """Only for a CODE change — `change_class_derivation.derive_from_repo`'s
    OWN `src_changed` signal, the exact predicate `artifact_check.py`'s
    gate re-derives too (never trust a self-declared `change_class`: see
    that module's own docstring). A docs/config-only write never gets the
    key at all — `blastRadius` on a change that touched no source would be
    an empty answer to a question nobody asked, not an honest zero."""
    try:
        import change_class_derivation as ccd
        root = plan_graph.root_for_plan_dir(plan_dir)
        derivation = ccd.derive_from_repo(root)
    except Exception:  # noqa: BLE001 — a derivation hiccup must not block the write
        return
    if "src_changed" not in derivation.signals:
        return
    _fill_blast_radius(rec, root)


def _trace(rec):
    try:
        import trace_log
        trace_log.append_event("write_verification", "verification_write",
                               target=rec.get("plan", ""),
                               status=rec.get("verdict", ""))
    except Exception:
        pass  # telemetry: best-effort, never blocks the write


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True, usage=_USAGE)
    ap.add_argument("plan_dir")
    ap.add_argument("--from", dest="from_src")
    ap.add_argument("--phase")
    ap.add_argument("--stage")
    ap.add_argument("--verdict")
    ap.add_argument("--check", action="append", default=[])
    args = ap.parse_args(argv)

    plan_dir = Path(args.plan_dir).resolve()
    rec = _build_record(args, plan_dir.name)
    _check_phase_is_node(plan_dir, rec["phase"])
    bad_status = _invalid_statuses(rec)
    if bad_status:
        # Hard, unlike the check-NAME ramp above: a name typo degrades a gate
        # reading, an off-enum status is a record no reader can interpret at all.
        _die("write_verification: %s" % "\n  ".join(bad_status), code=1)
    _validate_check_names(rec, plan_dir)  # off|soft|hard ramp — hard exits before any write
    _maybe_fill_blast_radius(rec, plan_dir)

    target, sibling = _canonical_target(plan_dir)
    _atomic_write(target, rec)
    # Exactly one canonical on disk: drop a stale sibling in the other format so
    # the loader can't read an older verdict than the one just written (C1).
    if sibling.exists():
        try:
            sibling.unlink()
        except OSError:
            pass  # best-effort: stale sibling is non-critical

    # Written before snapshot()/drive_lifecycle() so derive_plan_completion sees
    # THIS write's true verdict for this phase before any auto-close decision.
    vsnap.record_latest(plan_dir)
    vsnap.snapshot(plan_dir)
    # Only drive the lifecycle of the plan this record is bound to — a record
    # whose plan field points elsewhere must not close THIS plan.
    if rec.get("plan") == plan_dir.name and vsnap.auto_finalize_enabled():
        vsnap.drive_lifecycle(plan_dir)

    _self_verify(plan_dir, rec)
    _trace(rec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
