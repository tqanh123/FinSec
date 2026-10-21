#!/usr/bin/env python3
"""manual_test.py — admissibility of a manual-test result.

The anti-fabrication floor: a telemetry-anchored
output proves "a real command ran and this is its real output" — it defeats pure
hallucination — but it does NOT prove the command exercised the right thing (an
agent with a shell can run a real command against the WRONG endpoint and cite a
real trace). So anchored is a FLOOR, never a correctness proof:

  - evidence_tier `claimed` (agent-written) is below the floor — never hard-admissible.
  - `anchored` is honored only when the cited anchor id is actually present in
    the manual-test anchor telemetry the hook wrote; a fabricated id is REJECTED.
  - a hard gate additionally needs a human charter CO-SIGN — a rostered reviewer
    distinct from the author. Anchored-without-co-sign stays SOFT.

This is presence + tamper-evidence, NOT authentication. The co-sign is the human
judgement the machine cannot supply; the anchor is the floor that makes pure
fabrication cost a real command run.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

ANCHOR_SINK = "manual-test-anchor.jsonl"


def anchor_id_for(command: str) -> str:
    """Deterministic anchor id for a Bash command (sha256, 16 hex). No clock /
    randomness, so the citing artifact and the hook record agree."""
    return hashlib.sha256((command or "").encode("utf-8")).hexdigest()[:16]


def build_anchor(command, output=None, *, session=None, source="hook") -> dict:
    """The anchor record the PostToolUse hook writes (actor + ts are added by
    telemetry_paths on append). output_hash links the trace to the real output
    without storing it.

    `source` records which creation path wrote the anchor — "hook" (the
    PostToolUse telemetry hook) or "cli" (the explicit `manual_test.py --anchor`
    path). It defaults to "hook", so a record written before this field existed
    reads as hook-sourced."""
    rec = {
        "anchor_id": anchor_id_for(command),
        "cmd_hash": anchor_id_for(command),
        "source": source,
    }
    if output is not None:
        rec["output_hash"] = hashlib.sha256(
            str(output).encode("utf-8")).hexdigest()[:16]
    if session:
        rec["session"] = session
    return rec


def anchor_exists(anchor_id, root) -> bool:
    """True when `anchor_id` is present in the anchor sink under `root`
    (root = the state dir holding telemetry/). Fail-soft on a missing/corrupt
    sink — a missing record reads as 'not anchored', never a crash.

    Unions the legacy sink with every per-session shard. The hook writes through
    the sharding writer, so a session-bearing run lands in
    `manual-test-anchor-<disc>.jsonl` while this read used to open only the
    unsharded path — and every real manual test carries a session, so every real
    anchor graded as a fabricated citation."""
    import telemetry_paths
    for p in telemetry_paths.sink_read_files(Path(root) / "telemetry",
                                             ANCHOR_SINK):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for ln in text.splitlines():
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("anchor_id") == anchor_id:
                return True
    return False


def admissibility(check, root):
    """(tier, reason) for a manual-test check. tier ∈ anchored | claimed |
    rejected. `root` is the state dir holding telemetry/."""
    tier = (check or {}).get("evidence_tier")
    if tier != "anchored":
        return "claimed", ("evidence_tier is %r (agent-written) — below the "
                           "anchored floor" % tier)
    aid = check.get("anchor_id") or check.get("trace_id")
    if not aid:
        return "rejected", "evidence_tier is anchored but no anchor id is cited"
    if anchor_exists(aid, root):
        return "anchored", "anchor %s witnessed by the telemetry hook" % aid
    return "rejected", ("cited anchor %s is not in the manual-test anchor "
                        "telemetry — fabricated citation" % aid)


def hard_admissible(check, root, team_path=None, team=None):
    """(ok, reason): a manual-test check is HARD-admissible only when its output
    is genuinely anchored AND the charter carries a co-sign. Personal-first: the
    co-sign is attribution (who vouched the command tested the right thing), not a
    roster check. Claimed/rejected, or anchored-without-co-sign, stays soft. The
    team/team_path params are accepted for caller compatibility, now unused."""
    tier, reason = admissibility(check, root)
    if tier != "anchored":
        return False, "manual evidence is %s — %s" % (tier, reason)
    cosign = (check or {}).get("charter_cosign")
    if not cosign:
        return False, ("anchored output but no charter co-sign — a hard manual "
                       "gate needs a co-sign vouching the charter (the anchor "
                       "proves a real command ran, not that it tested the right "
                       "thing)")
    actor = (check or {}).get("actor")
    if not actor or cosign == actor:
        # A hard gate needs a co-sign DISTINCT from the author. An absent/empty
        # actor cannot establish distinctness, so it is NOT hard either — else an
        # agent could simply omit `actor` to dodge the self-co-sign check.
        return False, ("charter co-sign must be present AND distinct from the "
                       "author (actor) — the schema promises a rostered reviewer "
                       "distinct from the author; a self or unattributed co-sign "
                       "vouches for one's own work and stays soft")
    return True, "anchored output + distinct charter co-sign present"


def verify_portable(check, observed_output=None):
    """Transport-INTEGRITY only — NOT anti-fabrication, NOT a gate input, and it
    grants NO evidence tier. It lets a reviewer on another machine confirm a
    cited manual-check's hashes are internally consistent, and (when the reviewer
    RE-RUNS THE REAL COMMAND THEMSELVES and passes its output) that the output
    hash matches what was cited.

    It never executes anything itself. That is deliberate: a self-referential
    command (`printf PASS`, `echo OK`) recomputes to a matching hash yet proves
    nothing, so a match here is evidence the record survived transit intact —
    NEVER that the command ran, and never that it tested the right thing. The
    reviewer must run the real command in the real environment and judge whether
    it exercised the right thing; this helper only guards against a record being
    altered between machines. It is intentionally NOT called by `admissibility`
    / `hard_admissible` / the DoD gate — portability must not move the floor.

    Returns (ok, reason)."""
    check = check or {}
    cmd = check.get("cmd") or check.get("command")
    cmd_hash = check.get("cmd_hash")
    if cmd is None or cmd_hash is None:
        return False, "cited check lacks cmd/cmd_hash — nothing to integrity-check"
    if anchor_id_for(cmd) != cmd_hash:
        return False, ("cmd_hash does not match the cited command — the record "
                       "was altered in transit")
    if observed_output is not None:
        expected = check.get("output_hash")
        got = hashlib.sha256(str(observed_output).encode("utf-8")).hexdigest()[:16]
        if expected != got:
            return False, ("re-run output hash does not match the cited "
                           "output_hash — output differs from what was cited")
        return True, ("cmd_hash + your re-run output_hash both match (transit "
                      "intact — you must still confirm the command tested the "
                      "right thing)")
    return True, ("cmd_hash matches the cited command (transit intact; re-run "
                  "the real command to confirm the output and that it tested "
                  "the right thing)")


# ---------------------------------------------------------------------------
# Explicit CLI anchor — a SECOND creation path (subagent-safe, self-exec only)
# ---------------------------------------------------------------------------
#
# `--anchor --cmd "<command>"` stamps ONE deliberate probe without depending on
# the session env. It runs in a subagent (it is just a Bash call). It is closed
# against the self-authentication hole a naive design would open: the CLI SELF-
# EXECUTES the command and hashes the output IT captured. There is deliberately
# NO way for the caller to supply the output (no --output-file / --output-stdin),
# so the agent cannot write an anchor for a command that never ran. This puts the
# CLI at exactly the hook's trust level: "the harness ran a real command, this is
# its real output". It does NOT prove the command tested the right thing — the
# command the agent chose can still be trivial (`echo PASS`); the ceiling stays
# the human charter co-sign (hard_admissible), never the anchor itself.

_ANCHOR_TIMEOUT_S = 120
# A live suite runs for minutes, so the bound is the caller's (`--timeout`). The
# cap sits above a two-hour run's own wall, so that wall ends the run rather than
# the anchor, and still keeps a typo from parking a subagent for a day.
ANCHOR_TIMEOUT_MAX_S = 14400


def run_and_anchor(command, session=None, timeout=_ANCHOR_TIMEOUT_S):
    """Self-execute `command`, hash the REAL captured output, and append a
    cli-sourced anchor. Returns (anchor_id, exit_code), or (None, None) when the
    command could not be spawned. A timeout raises `subprocess.TimeoutExpired`
    after the process group is killed — a run cut short is not anchored, and the
    caller says it timed out rather than that it never ran. A command that runs
    and exits non-zero DID run: it is anchored, with its exit code recorded.

    `errors="replace"` keeps a probe that emits non-UTF-8 bytes (a binary curl
    body, an openssl dump) fail-SOFT: the command ran, so its output is captured
    and hashed with the undecodable bytes replaced, rather than a strict decode
    raising UnicodeDecodeError and turning a real run into a crash."""
    try:
        import process_group
        proc = process_group.run(
            ["bash", "-c", command],
            capture_output=True, text=True, timeout=timeout, errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise
    except (OSError, subprocess.SubprocessError):
        return None, None
    output = proc.stdout if proc.stdout else proc.stderr
    rec = build_anchor(command, output, session=session, source="cli")
    rec["exit_code"] = proc.returncode
    import telemetry_paths
    telemetry_paths.append_event(ANCHOR_SINK, rec)
    return rec["anchor_id"], proc.returncode


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="manual_test.py",
        description="Manual-test evidence helper. --anchor self-executes a "
        "command and records a cli-sourced anchor (the caller cannot supply the "
        "output; the harness captures it).",
    )
    p.add_argument("--anchor", action="store_true",
                   help="self-execute --cmd and record a cli-sourced anchor")
    p.add_argument("--cmd", help="the command to self-execute and anchor")
    p.add_argument("--session", default=None, help="optional session id to stamp")
    p.add_argument("--timeout", type=int, default=_ANCHOR_TIMEOUT_S, metavar="S",
                   help="seconds before the command is killed and nothing is anchored "
                   "(default %d, max %d)" % (_ANCHOR_TIMEOUT_S, ANCHOR_TIMEOUT_MAX_S))
    return p


def main(argv=None) -> int:
    args = _build_argparser().parse_args(argv)
    if args.anchor:
        if not args.cmd:
            print("--anchor requires --cmd", file=sys.stderr)
            return 2
        if not 0 < args.timeout <= ANCHOR_TIMEOUT_MAX_S:
            print("--timeout must be 1..%d seconds (got %d)"
                  % (ANCHOR_TIMEOUT_MAX_S, args.timeout), file=sys.stderr)
            return 2
        try:
            anchor_id, exit_code = run_and_anchor(args.cmd, session=args.session,
                                                  timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print("command timed out after %d s and was killed — no anchor written; "
                  "raise --timeout (max %d)" % (args.timeout, ANCHOR_TIMEOUT_MAX_S),
                  file=sys.stderr)
            return 1
        if anchor_id is None:
            print("command could not be executed — no anchor written", file=sys.stderr)
            return 1
        print("anchor_id=%s exit_code=%s" % (anchor_id, exit_code))
        return 0
    _build_argparser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
