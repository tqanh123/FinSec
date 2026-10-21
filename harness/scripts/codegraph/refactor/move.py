"""hs-code refactor move -- one door, three sizes.

`hs-code refactor move (--flat | --module | --symbol) ...` merges the three
ob-tools move verbs (`move-flat`, `move-module`, `move-symbol`) behind one
size-selecting flag, matching the family's "one door, a flag for shape"
convention every other multi-shape hs-code verb already uses. Each size is
its own engine module (`moveflat.py`, `movemodule.py`, `movesymbol.py`) --
this door only parses argv, dispatches, and renders the uniform report.

Dry-run is the default; `--apply` (hs-code's own shell flag, `opts.apply`)
writes for real -- the same convention `hs-code graph init`/`incremental`
already read `opts.apply` through (see `codegraph.graph.rules.
INIT`'s own docstring).

    --flat --move SRC=DST [--move SRC=DST ...] [--converge [--max-rounds N]]
        move a file between two directories that both sit directly on
        sys.path; rewrites every git-tracked text file's literal mentions of
        an old path (see moveflat.py). `--converge` plans every module
        `codemap.yaml` `owners.channels`/`owners.layout` assigns, running
        `check owners` to a fixed point first (see owners_converge.py);
        combines with an explicit `--move` (a disagreement between the two
        is refused, never silently picked).

    --module (--package DOTTED | --root DIR) --move OLD=NEW [--move OLD=NEW ...]
        move a module inside one dotted package (resolved through the
        target repo's own `codemap.yaml` import_roots, never a hard-coded
        `packages/` convention); rewrites imports and dotted-name references
        (see movemodule.py).

    --symbol --name SYMBOL --from SRC --to DST [--allow-dangling]
        cut one top-level definition out of SRC and paste it into DST,
        leaving a re-export behind (see movesymbol.py); `--apply` records
        that bridge as debt for `refactor shim-debt` (see shim_debt.py).

Exit: 0 every move planned clean (applied when --apply, printed otherwise) ·
1 one or more moves refused (target exists, source missing, symbol would
dangle, ...) -- nothing is written even under --apply · 2 the request itself
cannot be planned (bad flags, no codemap.yaml, ambiguous package, ...).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import attrs
import hs_family

from . import moveflat, movemodule, movesymbol, oldrefs, owners_converge, shim_debt
from ._layout import optional_descriptor
from .errors import RefactorError
from hs_family.cli import Command


def _import_roots_if_declared(repo: Path) -> tuple:
    """`descriptor.import_roots` when the target repo declares a
    `codemap.yaml`, `()` when it declares none at all -- `--symbol` works on
    any two Python files and does not REQUIRE a layout schema, only benefits
    from one when present (see `movesymbol._module_of`). A codemap.yaml that
    exists but is malformed still raises (broken, never silently ignored)."""
    descriptor = optional_descriptor(repo)
    return tuple(descriptor.import_roots) if descriptor is not None else ()


def _parse_pairs(specs: List[str], sep: str, what: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for spec in specs:
        if sep not in spec:
            raise RefactorError("--move wants %s, got %r" % (what, spec))
        left, right = spec.split(sep, 1)
        if left in out:
            raise RefactorError("listed twice: %s" % left)
        out[left] = right
    return out


def _converged_moves(repo: Path, max_rounds: int) -> "tuple[dict, int]":
    """Run `codegraph.check.owners.assign` to a fixed point against the
    CURRENT tree and turn the converged table into a `{src: dst}` move map --
    the live counterpart of upstream's static `--map FILE` (not ported: see
    owners_converge.py's own docstring)."""
    from codegraph.check.owners import OwnerSpec
    from codegraph.check.tree import measure
    tree = measure(repo)
    owners_doc = tree.descriptor.owners
    if not owners_doc:
        raise RefactorError(
            "--converge needs codemap.yaml `owners.channels` declared -- nothing to plan from")
    spec = OwnerSpec.from_doc(owners_doc)
    layout = dict(owners_doc.get("layout") or {})
    conv = owners_converge.assign_owners_converged(
        tree.idx, spec, tree.repo, layout=layout or None,
        entrypoints=tree.descriptor.entrypoints, max_rounds=max_rounds)
    if not conv.converged:
        raise RefactorError(
            "owners did not converge within %d round(s): oscillating %s"
            % (max_rounds, ", ".join(conv.oscillating)))
    return owners_converge.moves_from_map(conv.report, layout), conv.rounds


def _run_flat(repo: Path, args: argparse.Namespace, apply: bool) -> Dict[str, Any]:
    moves = _parse_pairs(args.move, "=", "SRC=DST")
    rounds = None
    if args.converge:
        try:
            mapped, rounds = _converged_moves(repo, args.max_rounds)
        except owners_converge.OwnersConvergeError as e:
            raise RefactorError(str(e)) from e
        for s, d in mapped.items():
            if s in moves and moves[s] != d:
                raise RefactorError(
                    "--move and --converge disagree about %s: %s vs %s" % (s, moves[s], d))
            moves[s] = d
    if not moves:
        raise RefactorError("--flat needs at least one --move SRC=DST or --converge")
    plan = moveflat.plan_flat_move(repo, moves)
    data: Dict[str, Any] = {
        "size": "flat", "dry_run": not apply,
        "moves": [attrs.asdict(m) for m in plan.moves],
        "edited": sorted(plan.texts), "changes": len(plan.changes),
    }
    if rounds is not None:
        data["rounds"] = rounds
    data["refused"] = [m for m in data["moves"] if m["status"] == "refused"]
    data["old_mentions"] = _old_mentions(repo, plan.moves, plan.texts)
    if not data["refused"] and apply:
        data["written"] = moveflat.apply_flat_plan(plan)
    return data


def _run_module(repo: Path, args: argparse.Namespace, apply: bool) -> Dict[str, Any]:
    if bool(args.package) == bool(args.root):
        raise RefactorError("--module needs --package DOTTED or --root DIR (one of them)")
    mapping = _parse_pairs(args.move, "=", "OLD=NEW")
    if not mapping:
        raise RefactorError("--module needs at least one --move OLD=NEW")
    plan = movemodule.plan_module_move(repo, args.package or "", mapping, with_tests=args.tests,
                                       root=args.root)
    data: Dict[str, Any] = {
        "size": "module", "dry_run": not apply, "package": args.package, "root": args.root,
        "moves": [attrs.asdict(m) for m in plan.moves],
        "edited": sorted(plan.texts), "changes": len(plan.changes),
        "new_inits": list(plan.new_inits),
        "manual": [attrs.asdict(n) for n in plan.manual],
        "blind": [attrs.asdict(n) for n in plan.blind],
    }
    data["refused"] = [m for m in data["moves"] if m["status"] == "refused"]
    data["old_mentions"] = _old_mentions(repo, plan.moves, plan.texts)
    if not data["refused"] and apply:
        data["written"] = movemodule.apply_module_plan(plan)
    return data


def _run_symbol(repo: Path, args: argparse.Namespace, apply: bool) -> Dict[str, Any]:
    if not (args.name and args.src and args.dst):
        raise RefactorError("--symbol needs --name SYMBOL --from FILE --to FILE")
    result = movesymbol.plan_move(repo, args.name, args.src, args.dst,
                                  allow_dangling=args.allow_dangling,
                                  import_roots=_import_roots_if_declared(repo))
    moves = []
    for p in result.plans:
        moves.append({
            "symbol": p.symbol, "src": p.src_path.as_posix(), "dst": p.dst_path.as_posix(),
            "status": "refused" if p.refused else "pending", "reason": p.refused or "",
            "dangling": [{"name": d.name, "line": d.line} for d in p.dangling],
            "notes": list(p.notes),
        })
    # no path moves: the symbol's module keeps its name, and its re-export keeps old importers working
    data: Dict[str, Any] = {"size": "symbol", "dry_run": not apply, "symbol": args.name, "moves": moves,
                            "old_mentions": []}
    data["refused"] = [m for m in moves if m["status"] == "refused"]
    if not data["refused"] and apply:
        data["written"] = result.apply()
        roots = _import_roots_if_declared(repo)
        for p in result.plans:
            src, dst = p.src_path.as_posix(), p.dst_path.as_posix()
            data["debt"] = str(shim_debt.record_bridge(
                repo, p.symbol, src, dst, movesymbol._module_of(src, roots),
                movesymbol._module_of(dst, roots)))
    return data


def _old_mentions(repo: Path, moves, texts: Dict[str, str]) -> List[Dict[str, Any]]:
    """What still names each moved path once the plan is written -- read from the planned
    texts, so a dry-run and `--apply` print the same list. `history`/`relative_roots` come
    from THIS repo's own codemap.yaml (`oldrefs.declared`, `((), ())` when it declares
    neither) -- never a hard default baked in for one particular repo's own layout."""
    history, relative_roots = oldrefs.declared(repo)
    rows: List[Dict[str, Any]] = []
    for m in moves:
        if m.status != "refused":
            rows += oldrefs.as_rows(m.src, oldrefs.old_mentions(
                repo, m.src, m.dst, texts=texts, history=history, relative_roots=relative_roots))
    return rows


def _print_report(data: Dict[str, Any]) -> None:
    print("hs-code refactor move --%s" % data["size"])
    for m in data["moves"]:
        tag = {"pending": "move   ", "done": "done   ", "refused": "REFUSED"}.get(m["status"], m["status"])
        line = "  %s  %s -> %s" % (tag, m["src"], m["dst"])
        if m.get("reason"):
            line += "   (%s)" % m["reason"]
        print(line)
    oldrefs.print_report(data.get("old_mentions") or [])
    if data["refused"]:
        print("hs-code refactor move: REFUSED -- %d move(s) cannot run; nothing written"
              % len(data["refused"]))
        return
    if data["dry_run"]:
        print("hs-code refactor move: dry-run -- pass --apply to write")
    else:
        print("hs-code refactor move: applied -- %d file(s) written" % len(data.get("written") or []))
    if data.get("debt"):
        print("  DEBT recorded: %s -- audit with: hs-code refactor shim-debt --strict" % data["debt"])


def _move_args(ap: argparse.ArgumentParser) -> None:
    size = ap.add_mutually_exclusive_group(required=True)
    size.add_argument("--flat", action="store_true",
                      help="move a file between two flat (sys.path) directories")
    size.add_argument("--module", action="store_true",
                      help="move a module inside one dotted package")
    size.add_argument("--symbol", action="store_true",
                      help="cut one top-level definition into another module")
    ap.add_argument("--move", action="append", default=[], metavar="OLD=NEW",
                    help="--flat: SRC=DST repo-relative file move; --module: dotted move "
                         "relative to --package (repeatable either way)")
    ap.add_argument("--package", default=None, metavar="DOTTED",
                    help="--module: the dotted package the modules live in")
    ap.add_argument("--root", default=None, metavar="DIR",
                    help="--module: a declared import root instead; its top-level modules "
                         "move by bare name (`flat=pkg.flat` takes a script into a package)")
    ap.add_argument("--name", default=None, metavar="SYMBOL",
                    help="--symbol: the top-level name to move")
    ap.add_argument("--from", dest="src", default=None, metavar="FILE", help="--symbol: source module")
    ap.add_argument("--to", dest="dst", default=None, metavar="FILE", help="--symbol: destination module")
    ap.add_argument("--allow-dangling", action="store_true",
                    help="--symbol: accept a name that would dangle after the move")
    ap.add_argument("--tests", action="store_true",
                    help="--module: move test_<leaf>.py alongside a module gaining a "
                         "subpackage (schema-driven tests dir; see movemodule._tests_dir)")
    ap.add_argument("--converge", action="store_true",
                    help="--flat: plan every codemap.yaml owners-assigned module's move "
                         "by running `check owners` to a fixed point (combines with --move)")
    ap.add_argument("--max-rounds", type=int, default=owners_converge.DEFAULT_MAX_ROUNDS,
                    metavar="N", help="--flat --converge: round cap before refusing as "
                         "oscillating (default: %d)" % owners_converge.DEFAULT_MAX_ROUNDS)


def _move(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts

    apply = bool(getattr(opts, "apply", False))
    repo = Path(root)
    try:
        if args.flat:
            data = _run_flat(repo, args, apply)
        elif args.module:
            data = _run_module(repo, args, apply)
        else:
            data = _run_symbol(repo, args, apply)
    except RefactorError as e:
        raise hs_family.UsageError(str(e)) from e

    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        _print_report(data)
    return 1 if data["refused"] else 0


MOVE = Command(add_args=_move_args, run=_move)
