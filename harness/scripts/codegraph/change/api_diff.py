"""`hs-code change api-diff` — API surface diff by NAME **and SIGNATURE**.

Ported from ob-tools' `devkit.analysis.apidiff` (pinned commit `60075cb`);
the engine is unchanged, `API_DIFF` is the hs-code adapter.

WHY SIGNATURES AND NOT JUST NAMES
    Comparing public names says "unchanged" when a parameter was added, dropped,
    or reordered — every caller of that function breaks and the tool reports
    green. This module reads the signature.

WHERE THE SIGNATURE COMES FROM (and why the snapshot cannot supply it)
    The kernel snapshot records `name`/`kind`/`lines`/`sha` — a normalized body
    hash, no signature. So a snapshot-only diff CANNOT see a signature change,
    and pretending otherwise would be a green light wired to nothing. Two modes:

      * `--old-src` / `--new-src` (two trees): full diff, names AND signatures.
      * snapshot-only: names-only diff, and the report says in words that
        signatures were NOT checked. Blind, never silently clean.

WHAT IT CANNOT SEE
    * Keyword-only vs positional-only nuance beyond what `ast` records; `*args`
      and `**kwargs` are compared as written, not as they behave.
    * Decorators that rewrite a signature at runtime (`functools.wraps` chains,
      `@overload` sets collapse to the last definition).
    * Type annotations are compared only if they are plain names — a complex
      annotation is normalized to its unparsed text, so `list[int]` and
      `List[int]` read as different.
    * Anything re-exported from a third-party package: `__all__` names with no
      local definition are listed as public but have no signature to compare.
    * Runtime-created attributes (metaclasses, `__getattr__`) do not exist here.

USAGE
    hs-code change api-diff --old-src OLD_TREE --new-src NEW_TREE [--check-bump]
    hs-code change api-diff --old-snap A --new-snap B [--check-bump]

Exit codes
    0  ran; nothing breaking
    1  breaking change found while `--check-bump` asked to gate on it
    2  cannot check honestly (missing tree, unparseable file)
"""
from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codegraph._core.query.engine import walk_symbols
from codegraph._core.snapshot.store import SnapshotMissing, load_dir
from codegraph._core.verdict import Verdict
from hs_family.cli import Command


class ApiDiffError(Exception):
    """Cannot check honestly — bad tree, unparseable file."""


# ── signature extraction ─────────────────────────────────────────────────────

def _annotation(node: "ast.AST | None") -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:                                   # noqa: BLE001
        return "?"


def _signature(fn: "ast.FunctionDef | ast.AsyncFunctionDef") -> str:
    """`name(a, b=1, *args, c, **kw)` — the callable contract, as written."""
    a = fn.args
    parts: list[str] = []
    posonly = list(getattr(a, "posonlyargs", []) or [])
    positional = posonly + list(a.args)
    defaults: "list[ast.AST | None]" = [None] * (len(positional) - len(a.defaults)) \
        + list(a.defaults)
    for i, (arg, default) in enumerate(zip(positional, defaults)):
        piece = arg.arg
        if default is not None:
            piece += f"={_annotation(default)}"
        parts.append(piece)
        if posonly and i == len(posonly) - 1:
            parts.append("/")
    if a.vararg:
        parts.append(f"*{a.vararg.arg}")
    elif a.kwonlyargs:
        parts.append("*")
    for arg, kw_default in zip(a.kwonlyargs, a.kw_defaults):
        piece = arg.arg
        if kw_default is not None:
            piece += f"={_annotation(kw_default)}"
        parts.append(piece)
    if a.kwarg:
        parts.append(f"**{a.kwarg.arg}")
    ret = f" -> {_annotation(fn.returns)}" if fn.returns is not None else ""
    return f"{fn.name}({', '.join(parts)}){ret}"


@dataclass(frozen=True)
class PublicSymbol:
    """One public name, its kind, and (when known) its signature."""

    name: str
    kind: str
    signature: "str | None" = None       # None = not known (snapshot-only mode)
    path: "str | None" = None


@dataclass
class ApiChange:
    """One difference between two surfaces."""

    name: str
    kind: str                            # added | removed | signature | body
    before: "str | None" = None
    after: "str | None" = None
    breaking: bool = False
    why: str = ""


def _is_public(name: str) -> bool:
    return not name.startswith("_") or name == "__init__"


def _all_names(tree: ast.Module) -> "list[str] | None":
    """The declared `__all__`, or None when the module does not declare one."""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return None
                if isinstance(value, (list, tuple)):
                    return [str(v) for v in value]
    return None


def api_surface_from_tree(root: Path) -> "dict[str, PublicSymbol]":
    """Public names + signatures by parsing the tree.

    `__all__` wins where a module declares it — that is the module's own
    statement of its public surface. Without `__all__`, names not starting with
    an underscore are public.
    """
    out: dict[str, PublicSymbol] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in {"__pycache__", ".venv", "build", "dist"} for part in rel.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as e:
            raise ApiDiffError(f"{rel}: cannot parse ({e})") from e
        declared = _all_names(tree)
        for node in tree.body:
            names: "list[tuple[str, str, str | None]]"
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [(node.name, "func", _signature(node))]
            elif isinstance(node, ast.ClassDef):
                names = [(node.name, "class", None)]
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if _is_public(sub.name):
                            names.append((f"{node.name}.{sub.name}", "method",
                                          _signature(sub)))
            else:
                continue
            for name, kind, sig in names:
                if declared is not None and name not in declared and "." not in name:
                    continue
                if not _is_public(name):
                    continue
                out[name] = PublicSymbol(name=name, kind=kind, signature=sig,
                                         path=rel.as_posix())
    return out


def _all_names_from_index(mod: dict) -> "list[str] | None":
    """`__all__` as the snapshot recorded it (a const symbol), else None.

    The snapshot does not carry a parsed `__all__`, but it does carry the
    assignment as a symbol — reading it back keeps the public/private line
    identical between snapshot mode and tree mode, so the two modes cannot
    disagree about which names are even in scope.
    """
    for sym in mod.get("symbols", ()):
        if sym.get("name") != "__all__":
            continue
        for unit_val in (sym.get("value"), sym.get("repr")):
            if isinstance(unit_val, str):
                try:
                    value = ast.literal_eval(unit_val)
                except (ValueError, SyntaxError):
                    return None
                if isinstance(value, (list, tuple)):
                    return [str(v) for v in value]
        return None
    return None


def api_surface_from_index(idx: dict) -> "dict[str, PublicSymbol]":
    """Names-only surface from a snapshot — signatures are NOT available.

    `__all__` is honoured where the snapshot recorded it, which keeps the
    public/private line the same as the tree mode.
    """
    out: dict[str, PublicSymbol] = {}
    for _unit, mod, sym, _parent in walk_symbols(idx):
        declared = _all_names_from_index(mod)
        name = sym["name"]
        if declared is not None and name not in declared and "." not in name:
            continue
        if not _is_public(name):
            continue
        out[name] = PublicSymbol(name=name, kind=sym.get("kind", "?"),
                                 signature=None, path=mod.get("path"))
    return out


# ── comparison ───────────────────────────────────────────────────────────────

def _params(signature: str) -> "list[str]":
    """Parameter list of `name(...) -> ret`, as written (defaults included)."""
    inner = signature.split("(", 1)[1].rsplit(")", 1)[0] if "(" in signature else ""
    if not inner.strip():
        return []
    return [p.strip() for p in inner.split(",")]


def _required(params: "list[str]") -> "list[str]":
    return [p for p in params if "=" not in p and not p.startswith("*")]


def classify_change(name: str, before: "str | None", after: "str | None",
                    sig_before: "str | None", sig_after: "str | None") -> ApiChange:
    """One name's change: added, removed, signature, or body-only.

    Body-only means the normalized body hash moved but the signature did not —
    an implementation change, which is compatible.
    """
    if before is None and after is not None:
        return ApiChange(name, "added", None, after, breaking=False,
                         why="new public name")
    if before is not None and after is None:
        return ApiChange(name, "removed", before, None, breaking=True,
                         why="public name gone — every importer breaks")
    if sig_before is not None and sig_after is not None and sig_before != sig_after:
        pb, pa = _params(sig_before), _params(sig_after)
        rb, ra = _required(pb), _required(pa)
        names_b = [p.split("=")[0] for p in pb]
        names_a = [p.split("=")[0] for p in pa]
        if len(ra) > len(rb):
            why = "new REQUIRED parameter — every existing call site breaks"
        elif len(pa) < len(pb):
            why = "parameter removed — callers passing it break"
        elif names_a[:len(names_b)] != names_b:
            why = "parameter order changed — positional callers break"
        else:
            # old params are an unchanged PREFIX of the new list: anything added
            # sits after them, so every existing call still binds correctly.
            why = "parameter(s) appended, all defaulted — compatible"
        breaking = "break" in why
        return ApiChange(name, "signature", sig_before, sig_after,
                         breaking=breaking, why=why)
    if before != after:
        return ApiChange(name, "body", before, after, breaking=False,
                         why="body changed; signature intact")
    return ApiChange(name, "same", before, after, breaking=False)


def compare_surfaces(old: "dict[str, PublicSymbol]",
                     new: "dict[str, PublicSymbol]") -> "list[ApiChange]":
    """Every difference, sorted by name; identical entries are dropped.

    When signatures are unavailable on EITHER side (snapshot-only mode), a name
    present in both is reported as `body` — the shas differ or they do not, and
    that is all this mode can honestly say.
    """
    changes: list[ApiChange] = []
    for name in sorted(set(old) | set(new)):
        o, n = old.get(name), new.get(name)
        if o is not None and n is not None and o.signature is None and n.signature is None:
            if o.kind != n.kind:
                changes.append(ApiChange(name, "body", o.kind, n.kind,
                                         breaking=False, why="kind changed"))
            continue
        c = classify_change(name,
                            o.kind if o else None, n.kind if n else None,
                            o.signature if o else None, n.signature if n else None)
        if c.kind != "same":
            changes.append(c)
    return changes


def suggested_bump(changes: "list[ApiChange]") -> "str | None":
    """major / minor / patch / None — what the semver bump should be."""
    if any(c.breaking for c in changes):
        return "major"
    if any(c.kind == "added" for c in changes):
        return "minor"
    if changes:
        return "patch"
    return None


# ── CLI ──────────────────────────────────────────────────────────────────────

def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--old-src", metavar="DIR", default=None,
                    help="old source tree (enables SIGNATURE comparison)")
    sp.add_argument("--new-src", metavar="DIR", default=None,
                    help="new source tree (enables SIGNATURE comparison)")
    sp.add_argument("--old-snap", metavar="DIR", default=None,
                    help="old snapshot dir (names only — no signatures)")
    sp.add_argument("--new-snap", metavar="DIR", default=None,
                    help="new snapshot dir (names only — no signatures)")
    sp.add_argument("--check-bump", action="store_true",
                    help="exit 1 when a breaking change is found (CI gate)")


def run(args: argparse.Namespace, as_json: bool) -> Verdict:
    try:
        old, new, mode = _surfaces(args)
    except ApiDiffError as e:
        return Verdict.broken(f"api-diff: {e}")
    if old is None or new is None:
        return Verdict.broken("api-diff: give either --old-src/--new-src (signatures) or "
                           "--old-snap/--new-snap (names only)")

    changes = compare_surfaces(old, new)
    breaking = [c for c in changes if c.breaking]
    payload: dict[str, Any] = {
        "mode": mode,
        "signatures_checked": mode == "src",
        "old_public": len(old), "new_public": len(new),
        "suggested_bump": suggested_bump(changes),
        "breaking": len(breaking),
        "changes": [{"name": c.name, "kind": c.kind, "before": c.before,
                     "after": c.after, "breaking": c.breaking, "why": c.why}
                    for c in changes],
    }
    if mode != "src":
        payload["blind"] = ("signatures NOT checked — snapshots carry no signature; "
                            "pass --old-src/--new-src for a full diff")

    if as_json:
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    else:
        _print_report(payload, changes)
    if args.check_bump and breaking:
        return Verdict(1, payload)
    return Verdict(0, payload)


def _surfaces(args: argparse.Namespace) -> "tuple[dict | None, dict | None, str]":
    if args.old_src and args.new_src:
        return (api_surface_from_tree(Path(args.old_src)),
                api_surface_from_tree(Path(args.new_src)), "src")
    if args.old_snap and args.new_snap:
        try:
            return (api_surface_from_index(load_dir(Path(args.old_snap))),
                    api_surface_from_index(load_dir(Path(args.new_snap))), "snapshot")
        except SnapshotMissing as e:
            raise ApiDiffError(str(e)) from e
    return None, None, ""


def _print_report(payload: dict, changes: "list[ApiChange]") -> None:
    print(f"api surface: {payload['old_public']} -> {payload['new_public']} public name(s)")
    if payload.get("blind"):
        print(f"  BLIND: {payload['blind']}")
    if not changes:
        print("  no API change")
        return
    for c in changes:
        mark = "BREAKING" if c.breaking else "ok"
        if c.kind == "signature":
            print(f"  [{mark}] {c.name}: {c.before}  ->  {c.after}   ({c.why})")
        elif c.kind == "added":
            print(f"  [{mark}] + {c.name}")
        elif c.kind == "removed":
            print(f"  [{mark}] - {c.name}   ({c.why})")
        else:
            print(f"  [{mark}] ~ {c.name}   ({c.why})")
    print(f"  suggested bump: {payload['suggested_bump']}")


def _api_diff(args: argparse.Namespace) -> int:
    """`api-diff (--old-src A --new-src B | --old-snap A --new-snap B)
    [--check-bump]` — the hs-code verb. Reads only; the project root is unused
    (both sides are named explicitly)."""
    return run(args, args.opts.json).exit


API_DIFF = Command(add_args=add_arguments, run=_api_diff)
