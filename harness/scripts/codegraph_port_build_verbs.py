"""hs-code's `port` domain — `build`: spec YAML -> a dist tree under the
target repo, as one `codegraph_port_*_verbs.py` module (a tree-write verb,
same reachability reason every sibling in this glob names).

Ported (algorithm-faithful) from `ob-tools@60075cb:packages/portkit/src/
portkit/build/builder.py` — `dist_nodes_of`/`guard_drift`/`_module_shas`/
`exclude_ported_spans`/`_write_guarded`/`merge_slice_into`/`_polished_files`/
`build_dist` — trimmed to exactly the sources a dist spec's `module_map`/
`unit_sources`/`file_sources`/`slice_sources`/`hand_polish`/`renames`
closure reaches (no-orphan, function level). The slice engine
(`slice_file`/`prune_all_list`/`stdlib_imports_needed`) lives in
`port_slicer.py`; the hand-polish engine (`apply_polish`) in `port_polish.py`
— both flat `port_*.py` siblings, reached from this module's slice pass and
its post-write polish step, the same closed-set-extension precedent
`port_spec.py`'s own docstring names.

NO HARD-CODED NAMESPACE/LAYOUT: upstream's own `dist_root = repo /
"packages" / spec.dist` (`builder.py:328`) and `[tool.ob]` (`:631`) are the
two literals a namespace other than `ob`, or a dist tree with no `src`
segment, went red on. Both come from the target's own `codemap.yaml`
descriptor instead (`_layout.target_descriptor`), through the ONE dist layout door
(`codegraph._core.layout.dist_root`/`dist_src_root`): `namespace_root`,
`layout.packages_dir` and `layout.src_subdir`. No `codemap.yaml` -> exit 2,
onboard before porting; no `packages_dir` or no `namespace_root` -> exit 2
naming the key (the descriptor itself refuses `packages_dir` without
`namespace_root`); `src_subdir` absent -> no src segment (a real flat-dist
layout), never an invented `"src"` default.

SOURCE dotted identity goes through the ONE path->dotted door,
`codegraph._core.layout.dotted_of` — reused via `codegraph_port_tree_verbs.
src_module` (`add-module`'s own function), never a second derivation.
TARGET dotted->path is a plain dot-to-slash join under the computed `ns`
root — the inverse direction `dotted_of` does not provide, and does not
need to: it is deterministic string surgery given a known base directory,
not a root-matching search.

"ALREADY LANDED" (`exclude_ported_spans`): reads the ledger through the ONE
ledger door, `port_ledger_writer.Ledger.load_checked` — a broken ledger line
refuses the WHOLE build (exit 2, naming the line number(s)), never
upstream's own swallow (`except ValueError: continue`, `builder.py:
117-122`.) `exclude_foreign` (a symbol the SNAPSHOT assigns to a dist that
has not landed yet, reinserted inside the calling function via a deferred
door) IS ported now: the deferred home is DERIVED from the target's own
`codemap.yaml` descriptor (`namespace_root` + the dist spec's own
`package`), never upstream's hard-coded `_pkg_prefix(dist) + ".deferred"`
(the `ob`-only, five-dist-convention translation) — a namespace other than
`ob` reinserts under its OWN deferred home, not `ob`'s.

GIT: read-only (`git ls-files`, via `codegraph._core.git.git_text`) for the
tracked-vs-untracked distinction `_write_guarded` needs — never commit/push
(`harness/tests/port/test_port_verb_modules_never_write_git.py` holds every
module in this glob to that).

WHAT IT CANNOT SEE (inherited from the frozen machine, unchanged)
    * Import rewriting is LINE regex: dynamic-string imports are never
      rewritten; `import X.Y` long form is untouched by the leftover-import
      sweep (`rewrite_from_package`).
    * Measured deps see importable NAMES: a future entry-point/dynamic
      import counts as not-needed, like every AST census in this tree.
    * `--into`/`--source` (upstream: a trial destination directory, a frozen
      checkout to read source bytes from instead of the live tree) are not
      ported — nothing accepted below needs either escape hatch, and
      `dist_root` is a DETERMINISTIC function of `(port_root, descriptor,
      dist)` with no caller override, so the exit-3
      "resolves outside the target repo" refusal `add-module` carries has no
      way to fire here either (there is no caller-suppliable destination to
      check against the target/source repo boundary) and is not ported.
    * Does not run tests, does not write the ledger, does not run gates —
      that is the batch pipeline's business, not this verb's.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import py_compile
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import port_batch_reader  # noqa: E402
import port_config  # noqa: E402
import port_polish  # noqa: E402
import port_slicer  # noqa: E402
import port_spec  # noqa: E402
import port_workspace  # noqa: E402
import codegraph_snapshot_reader  # noqa: E402
from codegraph._core.git import git_text  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from codegraph._core import layout as _layout  # noqa: E402
from codegraph.refactor import import_rewrite  # noqa: E402
from codegraph_port_tree_verbs import src_module  # noqa: E402
from hs_family.cli import Command  # noqa: E402
from port_ledger_writer import BrokenLedgerError, Ledger  # noqa: E402

# ── pure engine pieces (ported, algorithm-faithful) ─────────────────────────

def dist_nodes_of(idx: dict, dist: str,
                  assigned: "Optional[Dict[str, str]]" = None) -> "dict[str, dict]":
    """{`module::name`: node} for every symbol/method of `dist` in the snapshot —
    the dist read through `port_batch_reader.node_dists` (the assign sidecar
    first; methods inherit the class's dist). Keyed by the QUALIFIED name: a
    short name (`logger`, `__all__`, `main`) repeats across modules, and a key
    that drops the module lets the last one silently replace the rest."""
    out: "dict[str, dict]" = {}
    for mod, sym, d in port_batch_reader.node_dists(idx, assigned):
        if d == dist:
            out["%s::%s" % (mod, sym["name"])] = {"module": mod, "kind": sym.get("kind"),
                                                  "lines": sym.get("lines")}
    return out


def guard_drift(path: Path, mod: str, shas: "dict[str, str]",
                fname: str, problems: "List[str]") -> None:
    """Refuse a build whose source tree drifted from the frozen snapshot:
    cut/copy LINE spans come from the snapshot while BYTES are read from
    the live tree — a drifted tree gets the wrong bytes copied SILENTLY.
    `file_sha` is the snapshot's own sha256[:16] of the whole file, so this
    catches any drift. A module absent from the snapshot is not this
    guard's business (a brand-new file the snapshot never saw)."""
    want = shas.get(mod)
    if not want:
        return
    got = hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()[:16]
    if got != want:
        problems.append(
            "%s: source drifted from the frozen milestone (sha %s != snapshot %s) "
            "— re-checkout the source at the pinned commit before building"
            % (fname, got, want))


def _module_shas(idx: dict) -> "dict[str, str]":
    out: "dict[str, str]" = {}
    for _unit, mods in idx["units"].items():
        for m in mods:
            if m.get("file_sha"):
                out[m["module"]] = m["file_sha"]
    return out


def exclude_ported_spans(text: str, mod: str, idx: dict, ledger: Ledger,
                         *, tgt_mod: str, dist: "Optional[str]" = None,
                         exclude_foreign: bool = False,
                         deferred_home: "Optional[str]" = None,
                         assigned: "Optional[Dict[str, str]]" = None) -> str:
    """Cut top-level symbols whose LAST ledger line ports them OUTSIDE
    `tgt_mod` (this file source's own destination module) — the assignment
    cannot know this at spec-write time; the LEDGER is the record of what
    already landed under another roof. Comments/decorators glued above ride
    out with the symbol.

    `exclude_foreign` (needs `dist` + `deferred_home`) additionally cuts a
    symbol the SNAPSHOT itself assigns to another dist that has not landed
    yet — copying it here would define it in two houses later. It is
    reinserted `from <deferred_home> import <name>` INSIDE the first
    function whose body calls it, never at module level: the deferred home
    raises `ImportError` until its own dist lands, and a module-level import
    would fire that at import time and kill the whole module.

    `ledger` is an already-loaded `Ledger` (via `Ledger.load_checked` —
    the caller's job, so ONE broken-line refusal covers every file this
    build pass reads the ledger for, not a re-read per file)."""
    last = ledger.replay()                       # {node: Event}, last-wins
    cut: "List[Tuple[int, int, str, str]]" = []
    for _unit, mods in idx["units"].items():
        for m in mods:
            if m["module"] != mod:
                continue
            for s in m["symbols"]:
                ev = last.get("*::%s::%s" % (mod, s["name"])) or next(
                    (v for k, v in last.items()
                     if k.endswith("::%s::%s" % (mod, s["name"]))), None)
                if ev is None or ev.status != "ported":
                    continue
                to = ev.extra.get("to")
                if not to:
                    continue
                to_key = str(to[-1])
                parts = to_key.split("::")
                to_mod = parts[1] if len(parts) > 1 else ""
                to_name = parts[-1] if len(parts) > 1 else s["name"]
                if to_mod == tgt_mod:
                    continue
                item = "%s as %s" % (to_name, s["name"]) if to_name != s["name"] else s["name"]
                cut.append((s["lines"][0], s["lines"][1], to_mod, item))
            if exclude_foreign:
                assert deferred_home is not None, "exclude_foreign needs deferred_home"
                for s in m["symbols"]:
                    s_dist = (assigned or {}).get("%s::%s" % (mod, s["name"])) or s.get("dist")
                    if (s_dist and s_dist != dist
                            and not any(c[3].split(" as ")[-1] == s["name"] for c in cut)):
                        cut.append((s["lines"][0], s["lines"][1], deferred_home, s["name"]))
    if not cut:
        return text
    lines = text.splitlines(keepends=True)
    spans: "List[List[int]]" = []
    for a, b, _dst, _nm in sorted(x for x in cut if x[0]):
        s0 = a - 1
        while s0 - 1 >= 0 and lines[s0 - 1].strip().startswith(("@", "#")):
            s0 -= 1
        if spans and s0 <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([s0, b])
    for s0, e in reversed(spans):
        del lines[s0:e]
    by_home: "Dict[str, List[str]]" = {}
    foreign: "Dict[str, List[str]]" = {}
    for _a, _b, dst, nm in cut:
        target_map = foreign if dst == deferred_home else by_home
        target_map.setdefault(dst, []).append(nm)
    last_import = 0
    for i, ln in enumerate(lines):
        if ln.startswith(("import ", "from ")):
            last_import = i
    j = last_import
    if lines[j].rstrip().endswith(("(", ",")):
        while j + 1 < len(lines) and not lines[j].strip().startswith(")"):
            j += 1
    reimports = ["from %s import %s\n" % (home, ", ".join(names))
                for home, names in sorted(by_home.items())]
    lines[j + 1:j + 1] = reimports
    text = "".join(lines)
    if foreign:
        text = _reinsert_foreign_in_function(text, foreign)
    return text


def _reinsert_foreign_in_function(text: str, foreign: "Dict[str, List[str]]") -> str:
    """Place `from <deferred_home> import <name>` at the TOP of the first
    function that uses each name — the deferred door raises `ImportError`
    until its dist lands, and the source guarded these calls with
    try/except; a module-level import would defeat that guard."""
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    inserts: "Dict[int, List[str]]" = {}
    for home, names in sorted(foreign.items()):
        for name in names:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                        isinstance(n, ast.Name) and n.id == name
                        for n in ast.walk(node)):
                    first_body = node.body[0].lineno - 1
                    pad = lines[first_body][:len(lines[first_body]) - len(lines[first_body].lstrip())]
                    inserts.setdefault(first_body, []).append(
                        "%sfrom %s import %s\n" % (pad, home, name))
                    break
    for ln_idx in sorted(inserts, reverse=True):
        lines[ln_idx:ln_idx] = inserts[ln_idx]
    return "".join(lines)


def merge_slice_into(existing: str, head: str, parts: "List[str]") -> str:
    """A slice whose destination already carries a unit/file body MERGES —
    old body first, slice after: the slice head's `from __future__` must go
    (it may only head a FILE, and the existing body already carries its
    own)."""
    head = re.sub(r"from __future__ import [^\n]*\n", "", head, count=1)
    return existing.rstrip("\n") + "\n\n\n" + head + "\n\n".join(parts)


def _toml_str(value: Any) -> str:
    """A JSON literal is a valid TOML basic string/array; `ensure_ascii=False`
    keeps non-ASCII text as written — a TOML comment is never unescaped, so a
    `\\uXXXX` there is what the reader sees."""
    return json.dumps(value, ensure_ascii=False)


def _polished_files(spec: "port_spec.Spec") -> "Set[str]":
    """Dist-root-relative paths a DECLARED `hand_polish` step owns — the
    rebuild guard (`_write_guarded`'s own `polished` argument) skips them:
    `hand_polish` rewrites bytes AFTER the write, so a raw pre-polish copy
    legitimately differs from the previously-committed, post-polish target;
    the polish step re-derives the same bytes right after."""
    out: "Set[str]" = set()
    for st in spec.hand_polish or []:
        if not isinstance(st, dict):
            continue
        for tp in ("replace", "import"):
            body = st.get(tp)
            if isinstance(body, dict) and body.get("file"):
                out.add(str(body["file"]))
    return out


def _write_guarded(tgt: Path, text: str, problems: "List[str]",
                   src_label: str, dist_root: Path,
                   tracked: "Optional[set]" = None,
                   polished: "Optional[Set[str]]" = None) -> None:
    """Refuse to SILENTLY overwrite a COMMITTED target that DIFFERS —
    a hand edit wiped by the next rebuild without a sound is a real,
    repeated upstream incident. Same bytes is a no-op. An UNTRACKED draft of
    the machine's own previous build (a spec evolving between two
    uncommitted builds, `tracked` a real, computed set that does not name
    `tgt`) is fair game. `tracked is None` — the caller could NOT compute a
    tracked set (no git repo yet, `git ls-files` failed) — is the opposite:
    a diff is refused CONSERVATIVELY, the same as upstream's own default
    (`tracked` starts life as a real `set()`, populated whenever a git repo
    is available at all; `None` only ever reaches this door from a caller
    that skipped computing it on purpose, and "unknown" is not a license to
    overwrite). `polished` (the caller's own `_polished_files(spec)`,
    dist-root-relative paths) is the ONE exemption: a diff on a file a
    DECLARED `hand_polish` step owns is expected (the raw pre-polish copy
    vs. the committed post-polish bytes) and is written through silently —
    the polish step re-derives the same final bytes right after; anything
    NOT in `polished` stays a hand-edit refusal naming the file."""
    if (tgt.is_file() and tgt.read_text(encoding="utf-8") != text
            and (tracked is None or str(tgt) in tracked)):
        rel = str(tgt.relative_to(dist_root))
        if polished and rel in polished:
            tgt.write_text(text, encoding="utf-8")
            return
        problems.append(
            "%s: %s already exists and DIFFERS — declare the change in the "
            "spec (hand_polish/handbuilt) instead of a silent overwrite"
            % (src_label, tgt.name))
        return
    tgt.write_text(text, encoding="utf-8")


# ── build_dist ────────────────────────────────────────────────────────────

def build_dist(dist: str, spec: "port_spec.Spec", *, port_root: Path,
               source_repo: Path, source: dict, descriptor: Any,
               idx: dict, ledger: Ledger, apply: bool,
               assigned: "Optional[Dict[str, str]]" = None,
               ) -> "Tuple[int, List[str], List[str], List[Dict[str, Any]]]":
    """Build one dist from its already-loaded spec. Returns
    `(exit_code, plan_lines, problems, handoff)` — `exit_code` follows the
    family's table (0 clean, 1 findings, 2 broken); the refusal arm (3) has
    no way to fire given this function's config-deterministic destination
    (see module docstring). `handoff` is the STRUCTURED subset of
    `problems` a person still has to route through a plan phase —
    `{kind, path, why}` per item, `path` repo-root-relative — never
    derived from `problems`' own prose after the fact: each entry is
    appended at the exact call site that already knows its kind and path.
    Always present (possibly empty), even on a clean build — the CALLER
    (`hs-code`'s own `--step` writer) treats an EMPTY list as "this run
    cleared whatever the step's last run left behind", so a producer that
    only appended on failure would leave a stale item behind after a fix."""
    plan: "List[str]" = []
    problems: "List[str]" = []
    handoff: "List[Dict[str, Any]]" = []

    ns = descriptor.namespace_root
    dist_root = port_root / _layout.dist_root(descriptor, dist)
    dst_root = port_root / _layout.dist_src_root(descriptor, dist)

    if not spec.package:
        return 2, plan, ["dist %r spec lacks 'package': the directory under %s/"
                        % (dist, dst_root.relative_to(port_root).as_posix())], handoff

    #: `exclude_foreign`'s reinsert home — DERIVED from the target's own
    #: namespace + this dist's own package (never a hard-coded `ob`, never
    #: upstream's `_pkg_prefix(dist)` translation).
    deferred_home = "%s.%s.deferred" % (ns, spec.package)
    #: dist-root-relative paths a DECLARED `hand_polish` step owns — the
    #: rebuild guard's one exemption (see `_write_guarded`).
    polished_files = _polished_files(spec)

    dist_nodes = dist_nodes_of(idx, dist, assigned)
    placeholder = not dist_nodes
    if placeholder and (spec.unit_sources or spec.file_sources or spec.slice_sources):
        return 2, plan, ["assignment has no symbol for %s" % dist], handoff
    if not placeholder and not (spec.unit_sources or spec.file_sources or spec.slice_sources):
        return 2, plan, ["assignment gives %s %d symbol(s) but its spec declares no source "
                         "(`sources:` unit/file/slice): nothing would land"
                         % (dist, len(dist_nodes))], handoff

    module_shas = _module_shas(idx)
    src_r_package = source.get("package")
    src_subdir_source = source.get("src_subdir")
    #: repo-root-relative base a unit's dotted name expands under — `rel`
    #: passed to `src_module` below stays relative to `source_repo` (never
    #: `src_root`), matching add-module's own convention: `dotted_of`
    #: itself strips `src_subdir` as the declared import root, so stripping
    #: it a second time here would leave `rel` with nothing left to match.
    src_root = source_repo / src_subdir_source if src_subdir_source else source_repo

    pkg_dir = dst_root / spec.package

    tracked: "Optional[set]" = None
    if dist_root.is_dir():
        try:
            ls = git_text(port_root, "ls-files", "-z", "--",
                          str(dist_root.relative_to(port_root)), check=False)
            tracked = {str((port_root / ln).resolve()) for ln in ls.split("\0") if ln}
        except Exception:  # noqa: BLE001 — no git repo yet: fair game, see _write_guarded
            tracked = None

    if apply:
        pkg_dir.mkdir(parents=True, exist_ok=True)

    file_written: "set[str]" = set()

    def _dst_path(target_mod: str) -> Path:
        return dst_root / Path(*target_mod.removeprefix(ns + ".").split(".")).with_suffix(".py")

    def _destination(src_mod: str, to: "Optional[str]", what: str) -> "Optional[str]":
        """The declared home of `src_mod`: its own `to`, else its `module_map`
        entry, else itself when it already lives under the target namespace.
        Anything else has no declared home, and writing it under the dist at
        its source name would invent one — so it is a named problem instead."""
        if to:
            return to
        mapped = import_rewrite.mapped_module(src_mod, spec)
        if mapped is not None:
            return mapped
        if src_mod == ns or src_mod.startswith(ns + "."):
            return src_mod
        problems.append("%s: module %s has no declared destination (no module_map "
                        "entry covers it and it names no `to`)" % (what, src_mod))
        return None

    # 1) whole units
    for unit in spec.unit_sources:
        upath = src_root / Path(*unit.split("."))
        if not upath.is_dir():
            return 2, plan, ["unit does not exist: %s" % upath], handoff
        for f in sorted(upath.rglob("*.py")):
            rel = f.relative_to(source_repo)
            src_mod = src_module(str(rel), source)
            if src_mod is None:
                problems.append("%s: source's own layout names it no dotted module "
                               "(check src_subdir)" % rel)
                continue
            guard_drift(f, src_mod, module_shas, str(rel), problems)
            target_mod = _destination(src_mod, None, str(rel))
            if target_mod is None:
                continue
            tgt = _dst_path(target_mod)
            if apply:
                tgt.parent.mkdir(parents=True, exist_ok=True)
                text = import_rewrite.rewrite_imports(
                    f.read_text(encoding="utf-8"), src_mod, spec, str(rel), problems)
                _write_guarded(tgt, text, problems, str(rel), dist_root, tracked,
                              polished_files)
                file_written.add(str(tgt))
            plan.append("unit %s: %s -> %s" % (unit, rel, tgt.relative_to(dst_root)))

    # 2) whole files
    for fs in spec.file_sources:
        fpath = fs["file"]
        f = source_repo / fpath
        if not f.is_file():
            return 2, plan, ["file does not exist: %s" % f], handoff
        rel = Path(fpath)
        src_mod = fs.get("from_module") or src_module(fpath, source)
        if src_mod is None:
            problems.append("%s: source's own layout names it no dotted module "
                           "(check src_subdir)" % fpath)
            continue
        guard_drift(f, src_mod, module_shas, fpath, problems)
        to_mod = _destination(src_mod, fs.get("to"), fpath)
        if to_mod is None:
            continue
        tgt = _dst_path(to_mod)
        if apply:
            tgt.parent.mkdir(parents=True, exist_ok=True)
            body = f.read_text(encoding="utf-8")
            if fs.get("exclude_ported") or fs.get("exclude_foreign"):
                try:
                    body = exclude_ported_spans(
                        body, src_mod, idx, ledger, tgt_mod=to_mod, dist=dist,
                        exclude_foreign=bool(fs.get("exclude_foreign")),
                        deferred_home=deferred_home, assigned=assigned)
                except BrokenLedgerError as e:
                    return 2, plan, [msg for _code, msg in e.findings], handoff
            body = import_rewrite.rewrite_imports(body, src_mod, spec, fpath, problems)
            _write_guarded(tgt, body, problems, fpath, dist_root, tracked, polished_files)
            file_written.add(str(tgt))
        plan.append("file %s -> %s" % (fpath, to_mod))

    # 3) slices by symbol — file head = declared/kept docstring + measured
    #    stdlib imports; slices sharing a destination MERGE into one file
    #    (the FIRST slice's head, unioned with every later head's own import
    #    lines, then every body in declaration order).
    slice_parts: "Dict[str, List[str]]" = {}
    slice_heads: "Dict[str, str]" = {}
    for raw_slice in spec.slice_sources:
        m = raw_slice["module"]
        entries = {k.split("::")[-1]: v for k, v in dist_nodes.items() if v["module"] == m}
        only = set(raw_slice.get("symbols", []))
        top: "Dict[str, Tuple[int, int]]" = {}
        for name, v in entries.items():
            if only and name not in only:
                continue
            if v.get("kind") in ("class", "func", "const"):
                top[name] = tuple(v["lines"])
        if only and only - set(top):
            problems.append("slice %s -> %s: declared symbols absent from the "
                           "snapshot: %s" % (m, raw_slice.get("to"), sorted(only - set(top))))
        if not top:
            problems.append("slice %s: no symbol of %s in the snapshot" % (m, dist))
            continue
        f = src_root / Path(*m.split(".")).with_suffix(".py")
        if not f.is_file():
            return 2, plan, ["slice source does not exist: %s (check the source's "
                             "src_subdir/package in .port/port.yaml)" % f], handoff
        guard_drift(f, m, module_shas, f.name, problems)
        tgt_mod = _destination(m, raw_slice.get("to"), "slice %s" % m)
        if tgt_mod is None:
            continue
        tgt = _dst_path(tgt_mod)
        if apply:
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst_root / "_slice_body.py"
            try:
                found = port_slicer.slice_file(f, set(top), top, tmp, problems)
                missing = set(top) - found
                if missing:
                    problems.append("slice %s: missing %s" % (m, sorted(missing)))
                body = tmp.read_text(encoding="utf-8")
            finally:
                tmp.unlink(missing_ok=True)
            body = import_rewrite.rewrite_imports(body, m, spec, f.name, problems)
            body = port_slicer.prune_all_list(body, tgt_mod, problems)
            head = ""
            if raw_slice.get("docstring"):
                head = '"""%s"""\n' % raw_slice["docstring"]
            elif raw_slice.get("keep_source_docstring"):
                src_text = f.read_text(encoding="utf-8")
                dm = re.match(r'\s*""".*?"""', src_text, re.S)
                head = (dm.group(0) + "\n") if dm else ""
            head += "".join(port_slicer.stdlib_imports_needed(body))
            head += "".join(ln + "\n" for ln in raw_slice.get("imports", []))
            head += "\n"
            slice_parts.setdefault(str(tgt), []).append(body)
            if str(tgt) in slice_heads:
                # two slices, one destination — the SECOND head's own import
                # lines must SURVIVE (union, deduped).
                for head_ln in head.splitlines(keepends=True):
                    if re.match(r"(from |import )", head_ln) and head_ln not in slice_heads[str(tgt)]:
                        slice_heads[str(tgt)] += head_ln
            else:
                slice_heads[str(tgt)] = head
        plan.append("slice %s (%d symbol) -> %s" % (m, len(top), tgt_mod))

    for tgt_str, parts in slice_parts.items():
        p = Path(tgt_str)
        head = slice_heads[tgt_str]
        if tgt_str in file_written:
            # destination already carries a unit/file body — MERGE, never
            # overwrite (duplicate imports across the halves are legal and
            # harmless at module level).
            p.write_text(merge_slice_into(p.read_text(encoding="utf-8"), head, parts),
                        encoding="utf-8")
        else:
            p.write_text(head + "\n\n".join(parts), encoding="utf-8")
        file_written.add(tgt_str)

    # 4) placeholder door — no source unit, synthesized directly
    if placeholder and apply:
        door = pkg_dir / "__init__.py"
        door.parent.mkdir(parents=True, exist_ok=True)
        _write_guarded(door, (
            '"""%s\n\n'
            "Door of dist `%s` (tier %s). Generated by `hs-code port build`.\n"
            "Placeholder dist: the milestone snapshot carries 0 symbols "
            "for this dist by design.\n\"\"\"\n" % (spec.question, dist, spec.tier)),
            problems, "%s door" % dist, dist_root, tracked, polished_files)
        plan.append("placeholder door -> %s" % door.relative_to(dst_root))

    # 5) handbuilt: existence check only
    for rel in spec.handbuilt:
        path_only = rel.split("#", 1)[0].strip()
        if not (pkg_dir / path_only).is_file() and not (dst_root / path_only).is_file():
            problems.append("handbuilt missing: %s" % path_only)

    if not apply:
        return (1 if problems else 0), plan, problems, handoff

    # Second sweep: unresolved source-package from-imports
    src_roots = frozenset({src_r_package}) if src_r_package else frozenset()
    unresolved_from_pkg: "List[str]" = []
    for f in sorted(dst_root.rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        res = import_rewrite.rewrite_from_package(
            text, str(f.stem), spec.module_map or {}, src_roots, f.name)
        if res.text != text:
            f.write_text(res.text, encoding="utf-8")
        unresolved_from_pkg.extend(res.unresolved)
        for u in res.unresolved:
            handoff.append({"kind": "rewrite", "path": f.relative_to(port_root).as_posix(),
                            "why": u})
    if unresolved_from_pkg:
        plan.append("UNRESOLVED SOURCE-PACKAGE FROM-IMPORTS (not red — the porter decides):")
        plan.extend("  %s" % u for u in unresolved_from_pkg)

    # hand_polish: applied BEFORE the py_compile self-check, so the
    # self-check sees the POLISHED output — the same order upstream ran it.
    if spec.hand_polish:
        polish_findings: "List[Dict[str, Any]]" = []
        problems.extend(port_polish.apply_polish(dist_root, spec.hand_polish,
                                                 findings=polish_findings))
        for pf in polish_findings:
            rel_file = pf.get("file")
            polish_path = ((dist_root / rel_file).relative_to(port_root).as_posix()
                          if rel_file else dist_root.relative_to(port_root).as_posix())
            handoff.append({"kind": "rej-hunk", "path": polish_path, "why": pf["why"]})

    # self-check: py_compile + no leftover source/relative import
    for f in sorted(dst_root.rglob("*.py")):
        rel_f = f.relative_to(port_root).as_posix()
        try:
            py_compile.compile(str(f), doraise=True)
        except py_compile.PyCompileError as e:
            why = "generated file does not compile: %s: %s" % (f.name, e)
            problems.append(why)
            handoff.append({"kind": "compile", "path": rel_f, "why": why})
        for ln, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if src_r_package and s.startswith(("from %s" % src_r_package, "import %s" % src_r_package)):
                why = "%s:%d: source-package import left: %s" % (f.name, ln, s)
                problems.append(why)
                handoff.append({"kind": "rewrite", "path": rel_f, "why": why})
            elif s.startswith(("from .", "import .")):
                why = "%s:%d: relative import left: %s" % (f.name, ln, s)
                problems.append(why)
                handoff.append({"kind": "rewrite", "path": rel_f, "why": why})

    # 6) pyproject + measured deps
    ws_map = import_rewrite.workspace_dist_map(port_root / descriptor.packages_dir, ns)
    third, wsd = import_rewrite.measure_deps(dst_root, ns, ws_map=ws_map)

    def _base(r: str) -> str:
        for c in "<>=!~;":
            r = r.split(c)[0]
        return r.strip()

    extra_bases = {_base(d) for d in spec.deps_extra}
    deps = ([d for d in sorted(third) if d != dist and _base(d) not in extra_bases]
           + [w for w in sorted(wsd) if w != dist]
           + list(spec.deps_extra))
    pyproject = (
        "# %s — tier `%s`. Generated by hs-code port build.\n"
        "# Domain question: %s\n"
        "# Dependencies MEASURED from the built code: third-party %s · workspace %s\n"
        "[project]\n"
        "name = \"%s\"\n"
        "version = \"%s\"\n"
        "description = %s\n"
        "requires-python = \">=3.12\"\n"
        "dependencies = %s\n\n"
        "[build-system]\n"
        "requires = [\"hatchling\"]\n"
        "build-backend = \"hatchling.build\"\n\n"
        "[tool.hatch.build.targets.wheel]\n"
        "packages = [\"%s\"]\n\n"
        "[tool.%s]\n"
        "tier = \"%s\"\n"
        % (dist, spec.tier, _toml_str(spec.question), _toml_str(sorted(third)),
          _toml_str(sorted(w for w in wsd if w != dist)),
          dist, spec.version, _toml_str(spec.description or spec.question),
          _toml_str(deps), dst_root.relative_to(dist_root).as_posix(), ns, spec.tier))
    #: the same rebuild guard every source file goes through: a pin written into
    #: the committed pyproject by hand is otherwise wiped without a sound.
    _write_guarded(dist_root / "pyproject.toml", pyproject, problems,
                   "pyproject.toml (measured deps; a pin belongs in the spec's deps_extra)",
                   dist_root, tracked, polished_files)
    plan.append("OK: built %s at %s" % (dist, dst_root.parent))

    return (1 if problems else 0), plan, problems, handoff


# ── CLI ──────────────────────────────────────────────────────────────────

def _build_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("dist")
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--source", default=None, dest="source_id")


def _build(args: argparse.Namespace) -> int:
    """`hs-code port build DIST --batch NAME [--source ID] [--apply]` —
    build one dist from `.port/batches/<NAME>/specs/<DIST>.yaml` into the
    target repo. Dry-run by default (a plan, nothing written); `--apply`
    writes. The envelope's `data` is one document (`schema: port.build/1`).

    Exit: 2 for anything malformed (unknown batch/flag, invalid `.port/
    port.yaml`, no `codemap.yaml` or one without `namespace_root`/`layout.
    packages_dir`, a missing spec/unit/file, a broken ledger line, an
    assignment with declared sources but no snapshot symbol); 1 when the
    build ran but left findings (an unresolved leftover import, an
    already-existing destination that differs and is not covered by a
    declared `hand_polish` step, a missing handbuilt file); 0 otherwise.
    """
    root, opts = args.ctx.root, args.opts
    dist, batch_name, source_id = args.dist, args.batch_name, args.source_id

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    config_path = port_workspace.port_yaml_path(port_root)
    if not config_path.is_file():
        return Verdict.broken(
            "no `.port/port.yaml` under %s — run /hs:port to plan a port" % port_root).exit
    cfg, problems = port_config.load_and_validate(config_path)
    if problems:
        return Verdict.broken(
            "`.port/port.yaml` is invalid:\n" + "\n".join("  · %s" % p for p in problems)).exit

    try:
        batch = port_batch_reader.load_batch(port_root, batch_name)
    except (port_batch_reader.BatchNotFoundError,
            port_batch_reader.BatchConfigError) as e:
        return Verdict.unresolved(e).exit

    source, source_problem = port_config.find_source(cfg, source_id)
    if source_problem:
        return Verdict.unresolved(source_problem).exit
    repo_rel = source.get("repo")
    if not repo_rel:
        return Verdict.broken("source %r declares no `repo`" % source.get("id")).exit
    source_repo = (port_root / repo_rel).resolve()

    descriptor, layout_problem = _layout.target_descriptor(port_root)
    if layout_problem:
        return Verdict.broken(layout_problem).exit

    spec_path = batch.specs_dir / ("%s.yaml" % dist)
    if not spec_path.is_file():
        return Verdict.broken("missing spec %s" % spec_path).exit
    try:
        spec = port_spec.Spec.load(spec_path)
    except port_spec.SpecError as e:
        return Verdict.broken(str(e)).exit

    if not batch.frozen_snapshot:
        return Verdict.broken(
            "batch %r has no `frozen_snapshot` declared — build needs the "
            "milestone snapshot to know which symbols belong to %r"
            % (batch_name, dist)).exit
    snap_dir = port_batch_reader.snapshot_path(port_root, batch.frozen_snapshot)
    try:
        idx = port_batch_reader.read_snapshot_dir(port_root, snap_dir)
    except codegraph_snapshot_reader.SnapshotIndexError as e:
        return Verdict.broken(str(e)).exit

    ledger_path = port_root / ".port" / "ledger.jsonl"
    try:
        ledger = Ledger.load_checked(ledger_path)
    except BrokenLedgerError as e:
        return Verdict.broken("; ".join(msg for _code, msg in e.findings)).exit

    try:
        assigned = port_batch_reader.read_assigned(batch)
    except ValueError as e:
        return Verdict.broken(str(e)).exit

    apply = bool(getattr(opts, "apply", False))
    exit_code, plan, run_problems, handoff = build_dist(
        dist, spec, port_root=port_root, source_repo=source_repo, source=source,
        descriptor=descriptor, idx=idx, ledger=ledger, apply=apply, assigned=assigned)
    if exit_code == 2:
        payload = {"schema": "port.build/1", "dist": dist, "apply": apply,
                  "ok": False, "plan": plan, "problems": run_problems, "handoff": handoff}
        if getattr(opts, "json", False):
            print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        else:
            print("\n".join(plan))
        return Verdict.broken("; ".join(run_problems)).exit

    payload = {"schema": "port.build/1", "dist": dist, "apply": apply,
              "ok": not run_problems, "plan": plan, "problems": run_problems,
              "handoff": handoff}
    if getattr(opts, "json", False):
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        return int(Verdict(exit_code, payload).exit)

    print("\n".join(plan))
    for p in run_problems:
        print("    RED %s" % p)
    if run_problems:
        print("floor: RED")
        return Verdict.red(payload).exit
    if not apply:
        print("floor: OK (dry-run; --apply writes)")
        return Verdict.ok(payload).exit
    print("floor: written")
    return Verdict.ok(payload).exit


BUILD = Command(add_args=_build_args, run=_build)
