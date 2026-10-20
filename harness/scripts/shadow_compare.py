#!/usr/bin/env python3
"""shadow_compare — a second, cheaper judge looks at a decision the harness already made.

Off by default (`harness/data/shadow-compare.yaml`); a project turns it on with its own
`<data home>/shadow-compare.yaml` (`enabled: true`) or `HARNESS_SHADOW_COMPARE=1`. Whatever
the judge says, the decision already made stands: `record` answers `decision` = the legacy
answer, always.

One round:

  prepare   validate a request {profile, consumer, state, legacy, candidates}, redact the
            facts (`secret_patterns.redact`), mask the candidates as C1..Cn in an order the
            legacy answer cannot leak through, and park the code->name mapping under the
            data home; the returned packet is what the judge sees and holds no name. More
            candidates than `max_candidates` skips the round rather than cutting the list.
  record    map the judge's code back, append one ledger line (`actor` + `ts`), and on a
            disagreement name the second look the profile asks for (independent-revalidator
            or red-teamer)
  resolve   append the outcome of that second look; the divergence stops being open
  rate      per profile, divergences k of n with a Wilson interval (`wilson.interval`)
  benchmark replay the ledger against a labels file: how often each side was right

Who the judge is (a cheap subagent, a partner lane) is the caller's choice; this module
never calls a model. `hs_run_advisories` is how an open divergence reaches `hs-run`'s
envelope; `mask` is shared with `independent-revalidator`, which borrows the masking.

CLI: `shadow_compare.py prepare|record|resolve|rate|benchmark|mask [--root DIR]`; JSON on
stdout; exit 0 done, 2 broken input, 3 refused (off, unknown id or code).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import yaml_io  # noqa: E402

CONFIG_PATH = _HERE.parent / "data" / "shadow-compare.yaml"


class ShadowError(Exception):
    exit_code = 2


class Refusal(ShadowError):
    exit_code = 3


class Disabled(Refusal):
    pass


# ------------------------------------------------------------------ config

def _load_yaml(p: Path) -> dict:
    try:
        return yaml_io.yaml_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def config() -> dict:
    cfg = _load_yaml(CONFIG_PATH)
    if not isinstance(cfg.get("profiles"), dict):
        raise ShadowError("shadow-compare config %s has no profiles" % CONFIG_PATH)
    return cfg


def _data_home(root: Path) -> Path:
    import harness_paths
    return Path(harness_paths.data_root_for(root))


def enabled(root: Path) -> bool:
    if os.environ.get("HARNESS_SHADOW_COMPARE", "").lower() in ("1", "true", "yes", "on"):
        return True
    local = _load_yaml(_data_home(root) / "shadow-compare.yaml")
    if "enabled" in local:
        return bool(local["enabled"])
    return bool(config().get("enabled", False))


def _ledger(root: Path) -> Path:
    return _data_home(root) / "shadow" / "ledger.jsonl"


def _pending_dir(root: Path) -> Path:
    return _data_home(root) / "shadow" / "pending"


# ------------------------------------------------------------------ masking

def mask(candidates: list, seed: str, max_n: Optional[int] = None) -> dict:
    """Codes C1..Cn for the candidates, ordered by a hash of (seed, name): the same seed
    always gives the same codes, and no position encodes which name was the legacy
    answer. Over `max_n` the round is skipped, never cut."""
    names = sorted(set(str(c) for c in candidates))
    if max_n is not None and len(names) > max_n:
        return {"status": "skipped", "reason": "%d candidates > %d" % (len(names), max_n)}
    ordered = sorted(names, key=lambda n: hashlib.sha256(("%s\0%s" % (seed, n)).encode()).hexdigest())
    return {"status": "ok", "codes": {"C%d" % (i + 1): n for i, n in enumerate(ordered)}}


# ------------------------------------------------------------------ round

def _redact(value):
    import secret_patterns
    if isinstance(value, str):
        return secret_patterns.redact(value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def prepare(root: Path, request: dict) -> dict:
    if not enabled(root):
        raise Disabled("shadow comparison is off (turn it on in <data home>/shadow-compare.yaml "
                       "or HARNESS_SHADOW_COMPARE=1)")
    cfg = config()
    prof = cfg["profiles"].get(request.get("profile"))
    if prof is None:
        raise Refusal("unknown profile %r (known: %s)" % (request.get("profile"), ", ".join(cfg["profiles"])))
    cands = request.get("candidates")
    if not isinstance(cands, list) or len(cands) < 2:
        raise Refusal("a comparison needs at least two candidates")
    if request.get("legacy") not in cands:
        raise Refusal("the legacy answer %r is not one of the candidates" % request.get("legacy"))
    body = json.dumps(request, sort_keys=True, ensure_ascii=False)
    rid = hashlib.sha256(("%s\0%d" % (body, time.time_ns())).encode()).hexdigest()[:16]
    m = mask(cands, seed=rid, max_n=int(cfg.get("max_candidates", 5)))
    if m["status"] != "ok":
        return {"id": rid, "status": "skipped", "reason": m["reason"]}
    packet = {"id": rid, "profile": request["profile"], "question": prof.get("question", ""),
              "facts": _redact(request.get("state") or {}), "options": sorted(m["codes"], key=lambda c: int(c[1:]))}
    pend = _pending_dir(root)
    pend.mkdir(parents=True, exist_ok=True)
    (pend / ("%s.json" % rid)).write_text(json.dumps({
        "id": rid, "profile": request["profile"], "consumer": request.get("consumer"),
        "legacy": request["legacy"], "mapping": m["codes"]}, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"id": rid, "status": "ok", "packet": packet}


def _stamp(rec: dict) -> dict:
    sys.path.insert(0, str(_HERE.parent / "hooks"))
    import hook_runtime
    rec.setdefault("actor", hook_runtime.resolve_actor())
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
    return rec


def _append(root: Path, rec: dict) -> dict:
    p = _ledger(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_stamp(rec), ensure_ascii=False) + "\n")
    return rec


def record(root: Path, rid: str, verdict: str, judge: str) -> dict:
    if not enabled(root):
        raise Disabled("shadow comparison is off")
    p = _pending_dir(root) / ("%s.json" % rid)
    if not p.is_file():
        raise Refusal("no pending comparison %s (already recorded, or never prepared)" % rid)
    pend = json.loads(p.read_text(encoding="utf-8"))
    if verdict not in pend["mapping"]:
        raise Refusal("verdict %r is not one of %s" % (verdict, ", ".join(sorted(pend["mapping"]))))
    answer = pend["mapping"][verdict]
    agree = answer == pend["legacy"]
    prof = config()["profiles"][pend["profile"]]
    rec = _append(root, {"kind": "comparison", "id": rid, "profile": pend["profile"],
                         "consumer": pend.get("consumer"), "legacy": pend["legacy"],
                         "judge_answer": answer, "judge": judge, "agree": agree})
    p.unlink()
    return {"id": rid, "agree": agree, "decision": pend["legacy"], "judge_answer": answer,
            "suggest": [] if agree else [prof.get("suggest", "independent-revalidator")],
            "ts": rec["ts"]}


def resolve(root: Path, rid: str, outcome: str) -> dict:
    return _append(root, {"kind": "resolution", "id": rid, "outcome": outcome})


def _read(root: Path) -> list:
    p = _ledger(root)
    if not p.is_file():
        return []
    out = []
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        rec = json.loads(line)
        if not isinstance(rec, dict):
            raise ShadowError("%s:%d is not a record" % (p, n))
        out.append(rec)
    return out


def open_divergences(root: Path, domain: Optional[str] = None) -> list:
    recs = _read(root)
    closed = {r["id"] for r in recs if r.get("kind") == "resolution"}
    profiles = config()["profiles"]
    out = []
    for r in recs:
        if r.get("kind") != "comparison" or r.get("agree") or r["id"] in closed:
            continue
        if domain is not None and domain not in (profiles.get(r["profile"]) or {}).get("domains", []):
            continue
        out.append(r)
    return out


def hs_run_advisories(root: Path, domain: str) -> list:
    """One warning line per open divergence of this domain's profiles; never raises."""
    try:
        if not enabled(root):
            return []
        profiles = config()["profiles"]
        return ["shadow comparison %s (%s): the judge answered %r where the decision was %r — "
                "the decision stands; spawn %s for a second look, then "
                "`shadow_compare.py resolve --id %s --outcome ...`"
                % (r["id"], r["profile"], r["judge_answer"], r["legacy"],
                   profiles[r["profile"]].get("suggest", "independent-revalidator"), r["id"])
                for r in open_divergences(root, domain)]
    except Exception:  # noqa: BLE001 — a report line must never break hs-run's envelope
        return []


def rate(root: Path) -> dict:
    import wilson
    out: dict = {}
    for r in _read(root):
        if r.get("kind") != "comparison":
            continue
        s = out.setdefault(r["profile"], {"k": 0, "n": 0})
        s["n"] += 1
        s["k"] += 0 if r.get("agree") else 1
    for s in out.values():
        s["wilson"] = list(wilson.interval(s["k"], s["n"]))
    return out


def benchmark(root: Path, labels: Path) -> dict:
    import wilson
    truth = {}
    for line in Path(labels).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            truth[rec["id"]] = rec["truth"]
    out: dict = {}
    for r in _read(root):
        if r.get("kind") != "comparison" or r["id"] not in truth:
            continue
        s = out.setdefault(r["profile"], {"n": 0, "legacy_correct": 0, "judge_correct": 0})
        s["n"] += 1
        s["legacy_correct"] += r["legacy"] == truth[r["id"]]
        s["judge_correct"] += r["judge_answer"] == truth[r["id"]]
    for s in out.values():
        s["legacy_wilson"] = list(wilson.interval(s["legacy_correct"], s["n"]))
        s["judge_wilson"] = list(wilson.interval(s["judge_correct"], s["n"]))
    return out


# ------------------------------------------------------------------ CLI

def _root(ns) -> Path:
    if ns.root:
        return Path(ns.root)
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="shadow_compare.py")
    sub = ap.add_subparsers(dest="verb", required=True)
    for name in ("prepare", "rate"):
        sub.add_parser(name).add_argument("--root")
    r = sub.add_parser("record")
    r.add_argument("--id", required=True)
    r.add_argument("--verdict", required=True)
    r.add_argument("--judge", required=True)
    r.add_argument("--root")
    s = sub.add_parser("resolve")
    s.add_argument("--id", required=True)
    s.add_argument("--outcome", required=True)
    s.add_argument("--root")
    b = sub.add_parser("benchmark")
    b.add_argument("--labels", required=True)
    b.add_argument("--root")
    m = sub.add_parser("mask")
    m.add_argument("--max", type=int, default=None)
    m.add_argument("--seed", default="")
    m.add_argument("--root")
    ns = ap.parse_args(argv)
    root = _root(ns)
    try:
        if ns.verb == "prepare":
            out = prepare(root, json.loads(sys.stdin.read()))
        elif ns.verb == "record":
            out = record(root, ns.id, ns.verdict, ns.judge)
        elif ns.verb == "resolve":
            out = resolve(root, ns.id, ns.outcome)
        elif ns.verb == "rate":
            out = rate(root)
        elif ns.verb == "benchmark":
            out = benchmark(root, Path(ns.labels))
        else:
            out = mask(json.loads(sys.stdin.read()), seed=ns.seed, max_n=ns.max)
    except ShadowError as e:
        print("shadow_compare: %s" % e, file=sys.stderr)
        return e.exit_code
    except (ValueError, KeyError) as e:
        print("shadow_compare: bad input: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
