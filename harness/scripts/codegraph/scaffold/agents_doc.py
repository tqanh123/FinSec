"""hs-code scaffold agents-doc -- generate AGENTS.md from a snapshot, with a
hand region (marker-spliced), plus a `--check` freshness gate (`CLAUDE.md` is
a symlink to it).

Adapted from ob-tools' `devkit.docs.agentsdoc` (pinned commit `60075cb89c`):
upstream targets one "dist" -- an OpsBrain `packages/<name>` layout concept,
read off a module's carried `payload.dist` -- absent from this kernel's own
schema. This kernel's grouping primitive is the UNIT (a root-prefixed
snapshot key): `--unit` replaces `--dist`, and the modules of a
unit are simply `idx["units"][unit]` -- already partitioned by the snapshot
itself, no per-module filter needed the way `dist_modules` walked every
symbol filtering by payload.

TWO REGIONS, MARKED
    Everything between `<!-- BEGIN GENERATED -->` and `<!-- END GENERATED -->`
    is written by this tool; everything outside is kept byte-for-byte. First
    run on a marker-less file inserts the generated block at the top and
    keeps everything else -- nothing is ever discarded.

WHAT IT CANNOT SEE
    * The generated region states what the SNAPSHOT measured; a stale
      snapshot produces a confident, wrong document.
    * Dynamically reached symbols (getattr, string dispatch) are absent from
      the snapshot -- the table is a LOWER bound of the API.
    * `loc` is parser-measured lines, not logic lines.
    * The symlink is created only when absent; a REAL `CLAUDE.md` is refused,
      never clobbered (two real files are two places to disagree).

Exit: 0 generated (and written/symlinked, if a path/--symlink is given) ·
1 --check finds the file stale or missing · 2 no such unit in the snapshot,
or an existing CLAUDE.md is a real file / a symlink to the wrong target.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import hs_family

from codegraph._core.snapshot.store import SnapshotMissing, load_dir
from codegraph._core.units import resolve_unit, select_units

from .errors import ScaffoldError
from hs_family.cli import Command

BEGIN = "<!-- BEGIN GENERATED -->"
END = "<!-- END GENERATED -->"

#: Refused rather than clobbered: a real CLAUDE.md is a second source of truth.
SYMLINK_TARGET = "AGENTS.md"


def _unit_modules(idx: dict, unit: str) -> "list[dict]":
    """Every module record of `unit`, through the same door `--unit` and the
    query engine already resolve through (`select_units`) — a unit key the
    snapshot never declared is refused by name, not read back as empty."""
    return sorted(select_units(idx, unit), key=lambda m: m["module"])


def _symbol_names(m: dict) -> "list[str]":
    return [s["name"] for s in m.get("symbols", [])]


def render(idx: dict, unit: str, *, lang: str = "en") -> str:
    """The generated block body (markers included). Deterministic: same
    snapshot + same unit -> same bytes, so `--check` compares something
    that never shuffles between runs."""
    mods = _unit_modules(idx, unit)
    loc = sum(int(m.get("loc") or 0) for m in mods)
    lines = [BEGIN, "# `%s` -- AGENTS.md" % unit, ""]
    if lang == "vi":
        lines += ["*Sinh từ ảnh chụp `%s` (commit `%s`). Vùng giữa hai marker do tool "
                  "ghi; ngoài marker là tay người." % (idx.get("snapshot", "?"), idx.get("commit", "?")), ""]
    else:
        lines += ["*Generated from snapshot `%s` (commit `%s`). Between the markers is "
                  "tool-written; outside is hand-kept." % (idx.get("snapshot", "?"), idx.get("commit", "?")), ""]
    lines.append("## Module (%d)" % len(mods) if lang == "vi" else "## Modules (%d)" % len(mods))
    lines.append("")
    if not mods:
        lines.append("(không module nào mang unit này trong ảnh chụp)" if lang == "vi"
                     else "(no module carries this unit in the snapshot)")
        lines.append("")
    else:
        lines.append("| module | path | loc | symbols |")
        lines.append("|---|---|---|---:|")
        for m in mods:
            names = _symbol_names(m)
            shown = ", ".join("`%s`" % n for n in names[:6])
            if len(names) > 6:
                shown += " ... (+%d)" % (len(names) - 6)
            lines.append("| `%s` | `%s` | %d | %s |" % (
                m["module"], m["path"], int(m.get("loc") or 0), shown or "-"))
        lines.append("")
        lines.append(("Tổng: %d module, %d dòng đo được." % (len(mods), loc)) if lang == "vi"
                     else ("Total: %d module(s), %d measured line(s)." % (len(mods), loc)))
        lines.append("")
    if lang == "vi":
        lines += ["## CÁI NÓ KHÔNG THẤY", "",
                  "* Symbol truy cập động (`getattr`, dispatch theo chuỗi) không có",
                  "  trong ảnh chụp -- bảng trên là CẬN DƯỚI của diện API.",
                  "* `loc` là dòng ĐO ĐƯỢC của parser, không phải dòng logic.", ""]
    else:
        lines += ["## WHAT IT CANNOT SEE", "",
                  "* Dynamically reached symbols (`getattr`, string dispatch) are not",
                  "  in the snapshot -- the table above is a LOWER bound of the API.",
                  "* `loc` is parser-measured lines, not logic lines.", ""]
    lines.append(END)
    return "\n".join(lines)


def splice(existing: "str | None", block: str) -> str:
    """Put `block` into `existing`, preserving every hand-kept byte.

    Markers present -> replace only between them. No markers -> insert at the
    top and keep everything, so the first run never destroys a file."""
    if existing is None or not existing.strip():
        return block + "\n"
    if BEGIN in existing and END in existing:
        head, rest = existing.split(BEGIN, 1)
        _old, tail = rest.split(END, 1)
        return head + block + tail
    return block + "\n\n" + existing.lstrip("\n")


def ensure_symlink(agents: Path) -> str:
    """Create CLAUDE.md -> AGENTS.md beside `agents`. Never clobbers a real file."""
    link = agents.with_name("CLAUDE.md")
    if link.is_symlink():
        if link.readlink().as_posix() != SYMLINK_TARGET:
            raise ScaffoldError(
                "%s is a symlink to %s -- expected %s; fix by hand" % (link, link.readlink(), SYMLINK_TARGET))
        return "ok"
    if link.exists():
        raise ScaffoldError(
            "%s is a REAL file -- refusing to replace it. Two real files are two "
            "places to disagree: delete it and rerun to get the symlink" % link)
    link.symlink_to(SYMLINK_TARGET)
    return "created"


def _agents_doc_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("snapshot", help="snapshot directory (the measurement)")
    ap.add_argument("--unit", required=True, help="unit key (bare or root-prefixed) to document")
    ap.add_argument("--path", default=None,
                    help="AGENTS.md to read/write; omitted -- plan only, print the rendered "
                         "block, write nothing (same doctrine as scaffold new-module)")
    ap.add_argument("--lang", choices=("en", "vi"), default="en", help="prose language of the GENERATED region")
    ap.add_argument("--check", action="store_true", help="exit 1 when the file differs from the generated form")
    ap.add_argument("--symlink", action="store_true", help="also ensure CLAUDE.md -> AGENTS.md")


def _agents_doc(args: argparse.Namespace) -> int:
    opts = args.opts
    as_json = bool(getattr(opts, "json", False))

    try:
        idx = load_dir(Path(args.snapshot))
        unit = resolve_unit(idx, args.unit)
    except SnapshotMissing as e:
        raise hs_family.UsageError(str(e)) from e
    except ValueError as e:
        raise hs_family.UsageError(str(e)) from e

    block = render(idx, unit, lang=args.lang)

    if args.path is None:
        if args.check:
            raise hs_family.UsageError("scaffold agents-doc --check: needs --path (which file to compare)")
        data = {"path": None, "unit": unit, "outcome": "planned", "symlink": None}
        if as_json:
            print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        else:
            print("agents-doc: planned for %s (no --path: nothing written)" % unit)
            print(block)
        return 0

    target = Path(args.path)
    existing = target.read_text(encoding="utf-8") if target.is_file() else None
    wanted = splice(existing, block)

    if args.check:
        if existing is None:
            data = {"stale": True, "missing": True, "path": str(target)}
            _emit(data, as_json, "agents-doc: %s does not exist -- the tree has no AGENTS.md for %s"
                  % (target, unit), stream=sys.stderr)
            return 1
        if existing != wanted:
            data = {"stale": True, "missing": False, "path": str(target)}
            _emit(data, as_json, "agents-doc: %s is STALE -- it does not match what the snapshot would "
                  "generate" % target, stream=sys.stderr)
            return 1
        data = {"stale": False, "path": str(target)}
        _emit(data, as_json, "agents-doc: %s matches the snapshot" % target)
        return 0

    outcome = "current"
    if existing is None or existing != wanted:
        outcome = "updated" if existing is not None else "created"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(wanted, encoding="utf-8")

    link = None
    if args.symlink:
        try:
            link = ensure_symlink(target)
        except ScaffoldError as e:
            raise hs_family.UsageError(str(e)) from e

    data = {"path": str(target), "unit": unit, "outcome": outcome, "symlink": link}
    if not as_json:
        print("agents-doc: %s %s" % (outcome, target))
        if link:
            print("  CLAUDE.md: %s" % link)
    else:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    return 0


AGENTS_DOC = Command(add_args=_agents_doc_args, run=_agents_doc)


def _emit(data: dict, as_json: bool, message: str, *, stream=None) -> None:
    if as_json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print(message, file=stream or sys.stdout)
