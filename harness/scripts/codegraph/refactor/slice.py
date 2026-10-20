"""hs-code refactor slice -- split one module by AST: keep/drop halves, or a
SHAPE a pack declares.

Port of ob-tools' `portkit.build.slice_module` (pinned commit `60075cb`). The
halves engine is unchanged. Upstream's second mode, `--two-backs`, imported its
five-file planner directly; here every multi-file split is a SHAPE looked up by
name in the `shapes:` table of the hs-code registry (`hs-code-registry.d/*.yaml`,
`<name>: {module: DOTTED, entry: FUNC}`). The harness ships that table empty; a
pack adds a row, and this core never imports a pack -- `declared_shapes(target)`
reads TEXT (a `module:` name), the actual `import_module` happens only once a
caller asks for that shape BY NAME (`shape_entry`). `target`'s own `codemap.yaml`
`preset:` gates which pack fragments are merged in (`pack_config.
resolve_frag_dirs`, the SAME preset-gated merge `hs_code.py`'s own domain rows
already get -- a shape row is not a special case of that mechanism).

    slice SRC --keep NAMES [--out DIR | --out-keep FILE [--out-drop FILE]]
          [--header-mode docstring|none] [--no-f821]
    slice SRC --shape NAME [--group ROLE=NAMES ...] [--target DIR] [--no-f821]

SRC is repo-relative (or absolute). Dry-run by default: the plan prints and
nothing is written; `--apply` writes the halves (beside SRC unless `--out*`
says otherwise) or the shape's files under `--target/<package>/` (a copy,
never the source tree).

A shape's entry is `entry(src_path, tree, groups) -> plan`: `groups` maps each
`--group` role to its set of names, and the entry raises `ValueError` for a
grouping it cannot honour (exit 2, its message printed). The plan carries
`package_name: str`, `files: {key: file}` with `file.name`/`file.text`, and
`render_plan() -> str`.

Exit: 0 planned (and written under --apply) · 1 a written half would not
compile honestly (an undefined name, a name in both halves) -- nothing written
· 2 the request cannot be planned (no file, does not parse, unknown shape, a
shape refusing its groups, --apply without --target).

WHAT IT CANNOT SEE
    * Dynamic imports (`importlib.import_module`, `__import__`) are not measured.
    * `ast.Name` catches bare names; `getattr(obj, "name")` strings are BLIND
      to the constants-follow-group pass -- a constant used via a string stays
      in the keep file only.
    * The F821 check sees module-level scope and top-level function locals;
      comprehension/closure scopes inside functions are not modelled.
    * `from __future__ import` must head a Python file; the machine preserves
      source order and never adds one.
    * A constant used by BOTH groups lands in keep only (by design -- no dup).
    * Multi-line decorators are recognised via `ast.parse`; a decorator that
      looks like Python but is not makes the file unparseable (exit 2).
    * A shape is trusted to split what it was given: the F821 check runs over
      its files, nothing else about them is judged here.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import hs_family

from .errors import RefactorError
from hs_family.cli import Command

#: the repo this module ships in -- the registry the shell dispatched from.
_HARNESS_REPO = Path(__file__).resolve().parents[4]


def _top_level_blocks(tree: ast.Module) -> List[Tuple[str, int, int]]:
    """[(name, first line 1-based, last line)] for every top-level block.
    Decorators BELONG to the following block (first `@` line)."""
    blocks: List[Tuple[str, int, int]] = []
    for node in tree.body:
        name: Optional[str] = None
        end = node.end_lineno or node.lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
            start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            start = node.lineno
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    name = t.id
                    break
            start = node.lineno
        else:
            continue
        if name is not None:
            blocks.append((name, start, end))
    return blocks


def _names_used(nodes: List[ast.stmt]) -> Set[str]:
    used: Set[str] = set()
    for n in nodes:
        for child in ast.walk(n):
            if isinstance(child, ast.Name):
                used.add(child.id)
    return used


def _is_import_guard(node: ast.stmt) -> bool:
    """A top-level `try:`/`if:` whose EVERY leaf statement is an import (or a
    `pass`) -- `try: import ujson as json / except ImportError: import json`,
    `if TYPE_CHECKING: from x import Y`. These bind names the body uses; a
    sliced half without them dies with an undefined name no `--keep` fixes."""
    if not isinstance(node, (ast.Try, ast.If)):
        return False
    kids = list(node.body) + list(node.orelse)
    if isinstance(node, ast.Try):
        for h in node.handlers:
            kids += h.body
        kids += node.finalbody
    if not kids:
        return False
    for k in kids:
        if isinstance(k, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(k, (ast.Try, ast.If)) and _is_import_guard(k):
            continue
        return False
    return True


def _collect_imports(tree: ast.Module) -> List[ast.stmt]:
    return [n for n in tree.body
            if isinstance(n, (ast.Import, ast.ImportFrom)) or _is_import_guard(n)]


def _is_future(node: ast.stmt) -> bool:
    return isinstance(node, ast.ImportFrom) and node.module == "__future__"


def _import_names_defined(imports: List[ast.stmt]) -> Set[str]:
    defined: Set[str] = set()
    for top in imports:
        for n in ast.walk(top):
            if isinstance(n, ast.Import):
                for alias in n.names:
                    defined.add(alias.asname if alias.asname else alias.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                for alias in n.names:
                    if alias.name == "*":
                        continue
                    defined.add(alias.asname if alias.asname else alias.name)
    return defined


def _unparse_import(node: ast.stmt) -> str:
    if isinstance(node, (ast.Try, ast.If)):
        return ast.unparse(node)
    if isinstance(node, ast.Import):
        parts = [f"{a.name} as {a.asname}" if a.asname else a.name for a in node.names]
        return "import " + ", ".join(parts)
    if isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        dots = "." * (node.level or 0)
        parts = [f"{a.name} as {a.asname}" if a.asname else a.name for a in node.names]
        return f"from {dots}{mod} import " + ", ".join(parts)
    return ""


def _is_const_node(node: ast.stmt) -> bool:
    return isinstance(node, (ast.AnnAssign, ast.Assign))


def split_groups(tree: ast.Module, keep_names: Set[str],
                 blocks: List[Tuple[str, int, int]]) -> Tuple[Set[str], Set[str]]:
    """(keep_final, drop_final) after shared-constant allocation.

    Module-level constants outside keep_names join keep when used
    (transitively) by at least one keep member."""
    block_names = {b[0] for b in blocks}
    name_to_node: Dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name_to_node[node.name] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name_to_node[node.target.id] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    name_to_node[t.id] = node
                    break

    constants = {n for n in block_names
                 if n in name_to_node and _is_const_node(name_to_node[n])}
    fixed_keep = (keep_names & block_names) - constants
    fixed_drop = (block_names - keep_names) - constants
    floating = constants

    keep_used: Set[str] = set()
    for name in fixed_keep:
        found = name_to_node.get(name)
        if found:
            keep_used |= _names_used([found])

    keep_consts: Set[str] = set()
    changed = True
    while changed:
        changed = False
        for name in floating - keep_consts:
            if name in keep_used:
                keep_consts.add(name)
                found = name_to_node.get(name)
                if found:
                    new = _names_used([found]) - keep_used
                    if new:
                        keep_used |= new
                        changed = True
    drop_consts = floating - keep_consts
    return fixed_keep | keep_consts, fixed_drop | drop_consts


def _extract_text(names: Set[str], blocks: List[Tuple[str, int, int]],
                  src_lines: List[str]) -> str:
    relevant = sorted((s, e) for (n, s, e) in blocks if n in names)
    merged: List[List[int]] = []
    for s, e in relevant:
        if merged and s <= merged[-1][1] + 2:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    parts = []
    for s, e in merged:
        chunk = "".join(src_lines[s - 1: e])
        if chunk and not chunk.endswith("\n"):
            chunk += "\n"
        parts.append(chunk)
    return "\n\n".join(parts)


def build_import_header(imports: List[ast.stmt], body_text: str) -> str:
    """Union import header: keep only the import lines whose names the body
    uses. `from __future__ import ...` is a compiler directive, not a name
    binding -- always kept, always first."""
    try:
        body_tree = ast.parse(body_text)
    except SyntaxError:
        return ""
    used: Set[str] = set()
    for n in ast.walk(body_tree):
        if isinstance(n, ast.Name):
            used.add(n.id)
    lines: List[str] = []
    seen: Set[str] = set()
    for imp in imports:
        if _is_future(imp):
            line = _unparse_import(imp)
            if line not in seen:
                lines.append(line)
                seen.add(line)
    for imp in imports:
        if _is_future(imp):
            continue
        line = _unparse_import(imp)
        if line in seen:
            continue
        if _import_names_defined([imp]) & used:
            lines.append(line)
            seen.add(line)
    return "\n".join(lines) + ("\n" if lines else "")


def _function_locals(fn: "ast.FunctionDef | ast.AsyncFunctionDef") -> Set[str]:
    local: Set[str] = set()
    for arg in (fn.args.args + fn.args.posonlyargs + fn.args.kwonlyargs):
        local.add(arg.arg)
    if fn.args.vararg:
        local.add(fn.args.vararg.arg)
    if fn.args.kwarg:
        local.add(fn.args.kwarg.arg)
    for child in ast.walk(fn):
        if child is fn:
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local.add(child.name)
            continue
        if isinstance(child, ast.Assign):
            for t in child.targets:
                if isinstance(t, ast.Name):
                    local.add(t.id)
        elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
            local.add(child.target.id)
        elif isinstance(child, ast.For) and isinstance(child.target, ast.Name):
            local.add(child.target.id)
        elif isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
            local.add(child.target.id)
    return local


def check_f821(text: str, label: str) -> List[str]:
    """Intra-file undefined-name check (F821-by-AST). Dunders are exempt."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return [f"{label}: SyntaxError during F821 check: {e}"]

    module_defined: Set[str] = set(dir(builtins))
    module_defined |= _import_names_defined(_collect_imports(tree))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_defined.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            module_defined.add(node.target.id)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    module_defined.add(t.id)

    def _dunder(name: str) -> bool:
        return name.startswith("__") and name.endswith("__")

    errors: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                    if child.id not in module_defined and not _dunder(child.id):
                        errors.append(f"{label}:{child.lineno}: F821 undefined name: {child.id!r}")
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            available = module_defined | _function_locals(node)
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                    if child.id not in available and not _dunder(child.id):
                        errors.append(f"{label}:{child.lineno}: F821 undefined name: {child.id!r}")
    return errors


def _names_in(text: str) -> Set[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    names: Set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


# ── shapes ───────────────────────────────────────────────────────────────────

def declared_shapes(target: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """The `shapes:` table, merged over the same registry files the shell
    dispatched from (the same override / fragment-dir environment), PLUS
    whatever pack `target` enables. `target` is the project whose own
    `codemap.yaml` `preset:` is read (`pack_config.read_enabled_preset`);
    defaults to `_HARNESS_REPO` for the self-hosting case, where the two are
    the same tree. Never imports the pack itself -- only its registry TEXT;
    the module a row names is imported later, only when a caller actually
    asks for that shape (`shape_entry`)."""
    import pack_config
    target_root = Path(target) if target is not None else _HARNESS_REPO
    try:
        preset = pack_config.read_enabled_preset(target_root)
    except pack_config.PackConfigError as e:
        raise RefactorError(str(e)) from e
    frag_dirs, problems = pack_config.resolve_frag_dirs(_HARNESS_REPO, preset)
    if problems:
        raise RefactorError("; ".join(problems))
    # Not an import: `shape_entry`'s later `importlib.import_module(row["module"])`
    # is the actual import, only for the one row a caller named. A shape's own
    # module lives at the pack's own directory root (never inside its
    # `hs-code-registry.d/`), the same `sys.path` a pack's gate rows already
    # need (`gates.py::_pack_sys_path`) -- every enabled pack gets one entry,
    # since a shape row's origin pack is not tracked past the merge.
    for rel in preset:
        pack_dir = str((_HARNESS_REPO / rel).resolve())
        if pack_dir not in sys.path:
            sys.path.insert(0, pack_dir)
    import hs_family.registry as hs_registry
    try:
        loaded = hs_registry.load_registry(hs_registry.family_env("hs-code"), _HARNESS_REPO,
                                           extra_blocks=("shapes",), extra_frag_dirs=frag_dirs,
                                           check_moved=False)
    except hs_registry.RegistryError as e:
        raise RefactorError(str(e)) from None
    return dict(loaded.blocks.get("shapes") or {})


def _parse_groups(values: List[str]) -> Dict[str, Set[str]]:
    """`--group ROLE=NAMES` (repeatable) -> {role: names}. Which roles exist is
    the shape's own law, not this parser's."""
    groups: Dict[str, Set[str]] = {}
    for a in values:
        m = re.match(r"^([A-Za-z_][\w-]*)=(.+)$", a)
        if not m:
            raise RefactorError("--group expects ROLE=NAME[,NAME...]; got %r" % a)
        groups.setdefault(m.group(1), set()).update(
            n.strip() for n in m.group(2).split(",") if n.strip())
    return groups


def shape_entry(name: str, target: Optional[Path] = None):
    """The callable a shape row names, or a refusal naming every declared shape."""
    shapes = declared_shapes(target)
    row = shapes.get(name)
    if row is None:
        raise RefactorError("slice: unknown shape %r -- declared: %s"
                            % (name, ", ".join(sorted(shapes)) or "(none)"))
    if not isinstance(row, dict) or not row.get("module"):
        raise RefactorError("slice: shape %r has no `module:` in the shapes table" % name)
    try:
        mod = importlib.import_module(str(row["module"]))
    except ImportError as e:
        raise RefactorError("slice: shape %r: cannot import %s (%s)"
                            % (name, row["module"], e)) from None
    entry = getattr(mod, str(row.get("entry") or "plan"), None)
    if not callable(entry):
        raise RefactorError("slice: shape %r: %s has no callable %r"
                            % (name, row["module"], row.get("entry") or "plan"))
    return entry


def _run_shape(src_path: Path, tree: ast.Module, args: argparse.Namespace,
               apply: bool, target: Optional[Path] = None) -> Dict[str, Any]:
    entry = shape_entry(args.shape, target)
    try:
        plan = entry(src_path, tree, _parse_groups(args.group))
    except ValueError as e:
        raise RefactorError("slice --shape %s: %s" % (args.shape, e)) from None
    files = list(plan.files.values())
    data: Dict[str, Any] = {"mode": "shape", "shape": args.shape, "dry_run": not apply,
                            "package": plan.package_name, "files": [f.name for f in files],
                            "plan": plan.render_plan(), "problems": [], "written": []}
    if not apply:
        return data
    if not args.target:
        raise RefactorError("slice --shape: --apply needs --target DIR (writes a copy; "
                            "never the source tree)")
    pkg = Path(args.target) / plan.package_name
    if not args.no_f821:
        for f in files:
            data["problems"] += check_f821(f.text, str(pkg / f.name))
    if data["problems"]:
        return data
    for f in files:
        dest = pkg / f.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f.text, encoding="utf-8")
        data["written"].append(str(dest))
    return data


# ── halves ───────────────────────────────────────────────────────────────────

def _run_halves(repo: Path, src_path: Path, src_text: str, tree: ast.Module,
                args: argparse.Namespace, apply: bool) -> Dict[str, Any]:
    keep_names = {n.strip() for n in args.keep.split(",") if n.strip()}
    if not keep_names:
        raise RefactorError("slice: --keep is empty")
    drop_path: Optional[Path]
    if args.out_keep:
        keep_path = repo / args.out_keep
        drop_path = repo / args.out_drop if args.out_drop else None
    elif args.out:
        keep_path = repo / args.out / f"{src_path.stem}_keep.py"
        drop_path = repo / args.out / f"{src_path.stem}_drop.py"
    else:
        keep_path = src_path.with_name(src_path.stem + "_keep.py")
        drop_path = src_path.with_name(src_path.stem + "_drop.py")

    blocks = _top_level_blocks(tree)
    keep_final, drop_final = split_groups(tree, keep_names, blocks)
    imports = _collect_imports(tree)
    header = ""
    if args.header_mode == "docstring":
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                header = f'"""{node.value.value}"""\n'
                break
    src_lines = src_text.splitlines(keepends=True)
    keep_body = _extract_text(keep_final, blocks, src_lines)
    drop_body = _extract_text(drop_final, blocks, src_lines)
    keep_text = (header + build_import_header(imports, keep_body) + "\n" + keep_body).lstrip("\n")
    drop_text = (header + build_import_header(imports, drop_body) + "\n" + drop_body).lstrip("\n")

    def rel(p: Path) -> str:
        try:
            return p.relative_to(repo).as_posix()
        except ValueError:
            return str(p)

    problems: List[str] = []
    if not args.no_f821:
        problems += check_f821(keep_text, rel(keep_path))
        if drop_path:
            problems += check_f821(drop_text, rel(drop_path))
    if drop_path:
        overlap = (_names_in(keep_text) & _names_in(drop_text)) - _import_names_defined(imports)
        problems += [f"duplicate-name: {name!r} present in BOTH halves" for name in sorted(overlap)]

    data: Dict[str, Any] = {
        "mode": "halves", "dry_run": not apply,
        "unknown": sorted(keep_names - {b[0] for b in blocks}),
        "keep": {"path": rel(keep_path), "blocks": sorted(keep_final)},
        "drop": {"path": rel(drop_path), "blocks": sorted(drop_final)} if drop_path else None,
        "problems": problems, "written": []}
    if problems or not apply:
        return data
    keep_path.parent.mkdir(parents=True, exist_ok=True)
    keep_path.write_text(keep_text, encoding="utf-8")
    data["written"].append(rel(keep_path))
    if drop_path:
        drop_path.parent.mkdir(parents=True, exist_ok=True)
        drop_path.write_text(drop_text, encoding="utf-8")
        data["written"].append(rel(drop_path))
    return data


def _print_report(data: Dict[str, Any]) -> None:
    if data["mode"] == "shape":
        print(data["plan"])
    else:
        if data["unknown"]:
            print("  warning: names absent from the file: %s" % ", ".join(data["unknown"]))
        for side in ("keep", "drop"):
            if data[side]:
                print("%s: %s (%d blocks: %s)" % (side, data[side]["path"], len(data[side]["blocks"]),
                                                  ", ".join(data[side]["blocks"])))
    for p in data["problems"]:
        print("  RED  %s" % p)
    if data["problems"]:
        print("hs-code refactor slice: REFUSED -- nothing written")
    elif data["dry_run"]:
        print("hs-code refactor slice: dry-run -- pass --apply to write"
              + (" (with --target DIR)" if data["mode"] == "shape" else ""))
    else:
        print("hs-code refactor slice: wrote %d file(s)" % len(data["written"]))


def _slice_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("src", help="the module to split (repo-relative or absolute)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--keep", default=None, help="comma-separated names to keep (halves)")
    mode.add_argument("--shape", default=None, metavar="NAME",
                      help="a split shape declared in the registry's `shapes:` table")
    ap.add_argument("--group", action="append", default=[], metavar="ROLE=NAMES",
                    help="--shape: one role's names (repeatable; roles are the shape's)")
    ap.add_argument("--target", default=None, metavar="DIR",
                    help="--shape --apply: directory the shape's package is written under")
    ap.add_argument("--out", default=None, metavar="DIR", help="halves: directory for both halves")
    ap.add_argument("--out-keep", default=None, metavar="FILE", help="halves: the keep file")
    ap.add_argument("--out-drop", default=None, metavar="FILE", help="halves: the drop file")
    ap.add_argument("--header-mode", choices=["docstring", "none"], default="none")
    ap.add_argument("--no-f821", action="store_true", help="skip the undefined-name check")


def _slice(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts

    repo = Path(root)
    apply = bool(getattr(opts, "apply", False))
    src_path = repo / args.src
    try:
        if not src_path.is_file():
            raise RefactorError("slice: no such file: %s" % src_path)
        src_text = src_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src_text, filename=str(src_path))
        except SyntaxError as e:
            raise RefactorError("slice: %s does not parse: %s" % (src_path, e)) from None
        if args.shape:
            data = _run_shape(src_path, tree, args, apply, target=repo)
        else:
            data = _run_halves(repo, src_path, src_text, tree, args, apply)
    except RefactorError as e:
        raise hs_family.UsageError(str(e)) from e

    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        _print_report(data)
    return 1 if data["problems"] else 0


SLICE = Command(add_args=_slice_args, run=_slice)
