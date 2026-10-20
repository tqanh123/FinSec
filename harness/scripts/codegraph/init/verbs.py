"""hs-code's `init` domain — what a repo needs before the code map can serve it.

    init status            one row per initializer + the adoption state
    init run [--only a,b]  dry by default; --apply runs every row with `run:`
    init decline           remember "not now" for this session

The rows are data: each domain declares `initializers:` in its own
`hs-code-registry.d/<domain>.yaml` fragment, merged by the shared registry
loader. Adding a domain's setup step is one block in its fragment, never a
branch here. Adoption (offer / declined / unattended / unsupported) is
`codemap_adoption`'s answer, the same one hs-run reads.

Exit: status 0 every runnable row ready · 1 some are not · 3 the repo has no
language the code map reads. run 0 · 2 broken · 3 unsupported. decline 0.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import codemap_adoption  # noqa: E402
import pack_config  # noqa: E402
import hs_family.registry as hs_registry  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from hs_family.cli import Command  # noqa: E402

_BLOCK = "initializers"
_HARNESS_REPO = _SCRIPTS.parents[1]   # the install whose harness/data holds the registry


def _rows(root: Path) -> list:
    """Every declared initializer, in `order`: the core fragments plus those of
    every pack `root`'s own codemap.yaml enables (the dirs the shell dispatched from)."""
    preset = pack_config.read_enabled_preset(root)
    frag_dirs, problems = pack_config.resolve_frag_dirs(_HARNESS_REPO, preset)
    if problems:
        raise pack_config.PackConfigError("; ".join(problems))
    for rel in preset:   # a pack row's module lives at its pack dir root
        pack_dir = str((_HARNESS_REPO / rel).resolve())
        if pack_dir not in sys.path:
            sys.path.insert(0, pack_dir)
    loaded = hs_registry.load_registry(hs_registry.family_env("hs-code"), _HARNESS_REPO,
                                       extra_blocks=(_BLOCK,), extra_frag_dirs=frag_dirs,
                                       check_moved=False)
    rows = []
    for name, spec in (loaded.blocks.get(_BLOCK) or {}).items():
        rows.append(dict(spec or {}, name=str(name)))
    return sorted(rows, key=lambda r: (int(r.get("order", 100)), r["name"]))


def _call(ref: dict, *args):
    return getattr(importlib.import_module(ref["module"]), ref["entry"])(*args)


def _status_row(row: dict, root: Path) -> dict:
    st = _call(row["status"], root)
    out = {"name": row["name"], "summary": row.get("summary", ""),
           "ready": bool(st.get("ready")), "detail": st.get("detail", "")}
    if row.get("route"):
        out["route"] = row["route"]
        out["action"] = "" if out["ready"] else "owned elsewhere: %s" % row["route"]
    else:
        out["action"] = "" if out["ready"] else "hs-code init run --apply --only %s" % row["name"]
    return out


def _emit(data: dict, opts) -> None:
    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return
    a = data.get("adoption") or {}
    if a:
        print("adoption: %s — %s" % (a.get("state"), a.get("reason")))
    for r in data.get("initializers") or []:
        mark = "ready" if r.get("ready") else "todo"
        print("  %-8s %-5s %s" % (r["name"], mark, r.get("action") or r.get("detail", "")))


# ── status ─────────────────────────────────────────────────────────────────

def _status_args(ap: argparse.ArgumentParser) -> None:
    del ap


def _status(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    adoption = codemap_adoption.status(root)
    try:
        rows = [_status_row(r, root) for r in _rows(root)]
    except (pack_config.PackConfigError, hs_registry.RegistryError) as e:
        return Verdict.broken("init status: %s" % e).exit
    data = {"adoption": adoption.as_dict(), "initializers": rows}
    _emit(data, opts)
    if adoption.state == "unsupported":
        return Verdict.refused("init: %s" % adoption.reason, **data).exit
    runnable = [r for r in rows if not r.get("route")]
    return 0 if all(r["ready"] for r in runnable) else 1


STATUS = Command(add_args=_status_args, run=_status)


# ── run ────────────────────────────────────────────────────────────────────

def _run_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--only", default=None, metavar="NAME[,NAME]",
                    help="run only these initializers")


def _run(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    try:
        rows = _rows(root)
    except (pack_config.PackConfigError, hs_registry.RegistryError) as e:
        return Verdict.broken("init run: %s" % e).exit
    if args.only:
        wanted = [w.strip() for w in args.only.split(",") if w.strip()]
        known = [r["name"] for r in rows]
        unknown = [w for w in wanted if w not in known]
        if unknown:
            return Verdict.broken("init run: unknown initializer(s) %s — declared: %s"
                                  % (", ".join(unknown), ", ".join(known))).exit
        rows = [r for r in rows if r["name"] in wanted]
    adoption = codemap_adoption.status(root)
    if adoption.state == "unsupported":
        return Verdict.refused("init run: %s" % adoption.reason,
                               adoption=adoption.as_dict()).exit
    apply = bool(getattr(opts, "apply", False))
    out, worst = [], 0
    for row in rows:
        entry = _status_row(row, root)
        if entry["ready"] or row.get("route") or not row.get("run"):
            entry["ran"] = False
        else:
            result = _call(row["run"], args.ctx, opts)
            entry.update(result, ran=apply)
            worst = max(worst, int(result.get("exit_code", 0)))
        out.append(entry)
    if apply and worst == 0 and any(r.get("ran") for r in out):
        codemap_adoption.record(root, "accepted")
    data = {"dry_run": not apply, "adoption": codemap_adoption.status(root).as_dict(),
            "initializers": out}
    _emit(data, opts)
    return 2 if worst >= 2 else worst


RUN = Command(add_args=_run_args, run=_run)


# ── decline ────────────────────────────────────────────────────────────────

def _decline_args(ap: argparse.ArgumentParser) -> None:
    del ap


def _decline(args: argparse.Namespace) -> int:
    row = codemap_adoption.record(args.ctx.root, "declined")
    # What was decided; when and by whom stay in the ledger row itself.
    _emit({"recorded": {k: row[k] for k in ("decision", "session", "root")},
           "ledger": str(codemap_adoption.consent_path(args.ctx.root))}, args.opts)
    return 0


DECLINE = Command(add_args=_decline_args, run=_decline)
