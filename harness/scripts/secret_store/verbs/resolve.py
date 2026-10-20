"""resolve — the hook's door to a VALUE. Prints it, and only ever it.

This verb exists for secrets_inject (the PreToolUse hook): it answers on
stdout with the bare value so the hook can substitute in memory. It works
only when an agent binary sits above this process AND that agent's
fingerprint holds a live grant for the ref in this project; a human at a
terminal has no agent above, so the door refuses them by construction.
Every outcome — allow, deny, block, no-agent — is one access-ledger line,
and a denied ask leaves a request behind for `approve`.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from secret_store import api, approve as approve_mod, backends, fingerprint as fp
from secret_store import grants as grants_mod, home as home_mod, ledgers


def _log_access(home: Path, ref: str, agent, decision: str, dest_host) -> None:
    ledgers.append(home / "access.jsonl", {
        "ref": ref, "agent": agent, "project": str(Path.cwd()),
        "decision": decision, "source": "resolve",
        **({"dest_host": dest_host} if dest_host else {})})


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("ref", help="the secret to resolve")
    p.add_argument("--dest-host", default=None, metavar="HOST",
                   help="the remote this value is bound for (checked against allow-host)")


def run(args: argparse.Namespace) -> int:
    try:
        ref = api.check_ref(args.ref)
        home, backend, live = api.open_store(args.backend_choice)
        above = fp.agent_above()
        if above is None:
            _log_access(home, ref, None, "deny-no-agent", args.dest_host)
            print("!! resolve serves an agent under grant; no agent binary is above "
                  "this process")
            return 3
        _exe, sha = above
        grant_table = grants_mod.fold_grants(ledgers.read(home / "grants.jsonl"))
        verdict, why = grants_mod.decide(grant_table, ref=ref, agent=sha,
                                         project=str(Path.cwd()),
                                         dest_host=args.dest_host)
        if verdict != "allow":
            _log_access(home, ref, sha, verdict, args.dest_host)
            ledgers.append(home / "grants.jsonl", {
                "op": "request", "ref": ref, "agent": sha,
                "project": grants_mod.scope_of(str(Path.cwd()))})
            print("!! %s: %s — the ask is recorded for approval" % (verdict, why))
            return 3
        value = backend.get(ref)
        if value is None:
            _log_access(home, ref, sha, "deny-missing", args.dest_host)
            print("!! no secret named %s" % ref)
            return 2
        _log_access(home, ref, sha, "allow", args.dest_host)
        print(value)
        return 0
    except (api.Refusal, backends.BackendError) as e:
        print("!! %s" % e)
        return getattr(e, "code", 2)
