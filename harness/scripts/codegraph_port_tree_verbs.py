"""hs-code's `port` domain — the tree-write verbs (files, not ledger lines).
A FLAT module under `harness/scripts/` (same `hs-code-registry.d`
reachability reason every other `codegraph_port_*_verbs.py` sibling is).

`ADD_MODULE` is ported (algorithm-faithful) from `ob-tools@60075cb:
packages/portkit/src/portkit/add_module.py` (`run`/`plan_one`/`rewrite_text`
/`dist_of`/`header`/`symbols_of`) — landing ONE upstream file read from a
separate, READ-ONLY source repo into the target, its imports rewritten
through the batch's own merged spec `module_map` (`port_spec.
merged_spec_and_unit_dist`) plus a leftover fallback through `codegraph.
refactor.move_index.name_index` (upstream's own `_name_index` fallback,
generalised the same way `move_index.py`'s own docstring already is).
`dep_edges`/`measure_deps`/`workspace_dist_map` (upstream's cross-dist
pyproject dependency-cycle check) are NOT ported here — nothing in this
verb's own contract calls them (no acceptance case names a dep-cycle), and
porting them now, with no caller, would be exactly the orphan code the
no-orphan rule exists to keep out; a future port of `hs-code port build` is
their real home.

NO HARD-CODED PACKAGE: upstream's own `_ROOT = "gsm_agent"` (`add_module.py
:45-46`) — the single reason a source whose package was not literally
`gsm_agent` got NO import rewritten at all — is replaced by `.port/
port.yaml sources[].package`, read off the resolved source entry every
call. A source declaring no `package` is refused by name (exit 2): this
verb specifically needs it to tell a leftover FOREIGN import from the
target's own.

`src_module` (the SOURCE file's own dotted identity, used to resolve a
RELATIVE import inside it) calls the one path->dotted-name door,
`codegraph._core.layout.dotted_of` — never a second path -> dotted
derivation (`harness/tests/test_dotted_of_has_one_definition.py` holds
every module outside `codegraph/_core/` to that).

DESTINATION resolution (`_resolve_dest`) generalises upstream's own
`dist_of` (a hard-coded `packages/*/src/<parent>/__init__.py` glob) off the
target's OWN declared `codemap.yaml` layout: a `packages_dir` workspace
picks the unique dist whose `src/<parent>/__init__.py` already exists (or
the caller's `--dist`); a flat tree (`import_roots` only, no `packages_dir`)
picks the unique declared root whose expansion already owns the parent
package. Either way, the resolved path is checked TWICE before any byte is
written: it must resolve inside the target repo (destination-outside-target ->
exit 3), and it must NOT resolve inside the source repo (write-into-source ->
exit 3) — both `hs_family.RefusedError`, a well-formed request a rule
blocks, never a broken-state exit 2.

GIT: none. Every provenance stamp this verb writes comes from the batch's
own PINNED `sources[].pinned_sha` (never a live resolve) — this module
carries no `codegraph._core.git` import at all, matching `codegraph_port_
batch_verbs.py`'s own module docstring; the AST gate in `harness/tests/
port/test_port_verb_modules_never_write_git.py` holds every module in this
glob to that.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_config  # noqa: E402
import port_open  # noqa: E402
import port_spec  # noqa: E402
import port_workspace  # noqa: E402
from codegraph._core.layout import dist_import_root, dotted_of  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from codegraph.refactor import import_rewrite, move_index  # noqa: E402
from codegraph.refactor._layout import _expand_root, load_descriptor  # noqa: E402
from codegraph.refactor.errors import RefactorError  # noqa: E402
from hs_family.cli import Command  # noqa: E402
from hs_family.errors import UsageError as _UsageError  # noqa: E402


def _add_module_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--land", action="append", default=[], metavar="FILE=MODULE",
                    help="repeatable — one upstream file to land per occurrence")
    ap.add_argument("--source", default=None, dest="source_id")
    ap.add_argument("--dist", default=None, dest="forced_dist")
    ap.add_argument("--label", default="drift wave")
    ap.add_argument("--symbols-file", default=None)
    ap.add_argument("--batch", required=True, dest="batch_name")


def _parse_land_pairs(land: "List[str]") -> "List[Tuple[str, str]]":
    if not land:
        raise _UsageError("port add-module: --land FILE=MODULE is required (repeatable)")
    pairs: "List[Tuple[str, str]]" = []
    for spec in land:
        if "=" not in spec:
            raise _UsageError(
                "port add-module: --land wants FILE=TARGET_MODULE, got %r" % spec)
        f, t = spec.split("=", 1)
        pairs.append((f, t))
    return pairs


def _source_import_roots(source: dict) -> "Tuple[str, ...]":
    subdir = source.get("src_subdir")
    return (subdir,) if subdir else (".",)


def src_module(rel: str, source: dict) -> "Optional[str]":
    """The SOURCE file's own dotted identity — ALWAYS through the one
    path->dotted-name door, `dotted_of`, never a second path-to-dotted
    implementation (the exact gap upstream's own hard-coded `src/`-strip
    left when a source repo has no such subdir declared at all)."""
    return dotted_of(rel, _source_import_roots(source))


def landed_module(rel: str, source: dict) -> "Optional[str]":
    """The dotted name `rewrite_landed_text` resolves `rel`'s relative imports
    against: `src_module`, with `.__init__` for a package's own `__init__.py`."""
    here = src_module(rel, source)
    if here is not None and Path(rel).name == "__init__.py":
        here = here + ".__init__"
    return here


class Fork(NamedTuple):
    """A `two-homes` fork a leftover import met, handed back to a caller that
    decides for itself which forks are its own to raise."""
    subject: str
    module: str
    names: Tuple[str, ...]
    candidates: List[str]
    evidence: List[str]


def rewrite_landed_text(text: str, here: str, rel: str, merged, port_root: Path, package: str,
                        problems: "List[str]", *, batch_name: str, apply: bool,
                        forks: "Optional[List[Fork]]" = None) -> str:
    """Upstream text in the target's spelling: the batch spec's `module_map`
    (`import_rewrite.rewrite_imports`), then the name-index fallback for what it
    left. The ONE route a landed file takes — `add-module` lands through it and
    `drift-merge` rewrites both upstream versions through it, so the delta it
    merges is spelled the way the twin was landed. `forks` given: an unapproved
    two-homes fork is appended there instead of raised."""
    out = import_rewrite.rewrite_imports(text, here, merged, rel, problems)
    return _leftover_names_resolved(out, port_root, package, rel, problems,
                                    batch_name=batch_name, apply=apply, forks=forks)


def _resolve_dest(target: str, port_root: Path, descriptor,
                  forced_dist: "Optional[str]", *, batch_name: str, rel: str,
                  apply: bool) -> "Tuple[Optional[Path], Optional[str]]":
    """`(dest, problem)` — the file `target` (a dotted module) would land
    at under the target's OWN declared layout. Generalises upstream's
    `dist_of` (a hard-coded `packages/*/src/<parent>/__init__.py` glob) off
    `codemap.yaml` instead: a `packages_dir` workspace picks the unique
    dist whose `<src_subdir>/<parent>/__init__.py` already exists (or
    `forced_dist`); a flat tree (`import_roots` only) picks the unique
    declared root whose expansion already owns the parent package.
    Ambiguous or missing is a `problem` (an ordinary red, never exit 3 on
    its own — see the caller's own outside-repo/inside-source checks for
    the two refusals). BOTH the lookup and the final join go through
    `codegraph._core.layout.dist_import_root` — the ONE place the
    src-or-no-src rule is decided (absent `src_subdir` means NO segment at
    all, a real flat-dist layout, never upstream's hard-coded `"src"`); no
    second `if src_subdir` copy lives here.

    TWO REAL HOMES (`port_open`'s own `two-homes` kind): 0
    candidates stays the plain ambiguous-or-missing problem above (nothing
    to CHOOSE between); >=2 candidates is a real fork. `subject` is
    `add-module:<rel>` — stable across a re-run of the same file, so an
    already-`approved` item resolves the SAME fork without asking again.
    Approved choice wins outright, checked against the CURRENT candidate
    set (a stale approval against a candidate list that changed since is
    never trusted silently). Raising is a WRITE, gated behind the caller's
    own `apply` — a dry run reports the ambiguity like any other problem
    and never opens an item for it."""
    parts = target.split(".")
    parent_rel = "/".join(parts[:-1])
    leaf = parts[-1]
    rel_path = Path(parent_rel, leaf + ".py") if parent_rel else Path(leaf + ".py")
    subject = "add-module:%s" % rel
    packages_dir = getattr(descriptor, "packages_dir", None)
    if packages_dir:
        dist: "Optional[str]"
        if forced_dist:
            dist = forced_dist
        elif parent_rel:
            base_dir = port_root / packages_dir
            hits = sorted({
                d.name for d in (base_dir.iterdir() if base_dir.is_dir() else ())
                if d.is_dir()
                and (port_root / dist_import_root(descriptor, d.name)
                    / parent_rel / "__init__.py").is_file()})
            dist = hits[0] if len(hits) == 1 else None
            if dist is None and len(hits) >= 2:
                dist = _two_homes_choice(port_root, batch_name, subject, hits,
                                         evidence=["%s: dist candidates for %r: %s"
                                                   % (rel, target, ", ".join(hits))],
                                         apply=apply)
        else:
            dist = None
        if dist is None:
            return None, ("no unique dist under %r owns the parent of %r "
                          "(pass --dist)" % (packages_dir, target))
        return port_root / dist_import_root(descriptor, dist) / rel_path, None

    candidates: "List[Path]" = []
    for entry in descriptor.import_roots or ():
        for d in _expand_root(port_root, entry):
            if not parent_rel or (d / parent_rel / "__init__.py").is_file():
                if d not in candidates:
                    candidates.append(d)
    if len(candidates) != 1:
        if len(candidates) >= 2:
            rel_candidates = sorted(str(c.relative_to(port_root)) for c in candidates)
            chosen = _two_homes_choice(
                port_root, batch_name, subject, rel_candidates,
                evidence=["%s: import-root candidates for %r: %s"
                         % (rel, target, ", ".join(rel_candidates))],
                apply=apply)
            if chosen is not None:
                return port_root / chosen / rel_path, None
        return None, ("no unique import root owns %r (candidates: %s)"
                      % (target, ", ".join(str(c) for c in candidates) or "none"))
    return candidates[0] / rel_path, None


def _two_homes_choice(port_root: Path, batch_name: str, subject: str,
                      candidates: "List[str]", *, evidence: "List[str]",
                      apply: bool) -> "Optional[str]":
    """The shared `two-homes` fork logic `_resolve_dest`/
    `_leftover_names_resolved` both need: an already-`approved` choice
    (checked against the CURRENT candidate set) wins outright; otherwise,
    on `--apply` only, raise the item (idempotent on `(kind, subject)` —
    `port_open.raise_item` itself never double-writes) and return `None`
    (still ambiguous THIS run — the caller's own ordinary problem message
    fires, and an item now sits in `open.jsonl` for a human to resolve)."""
    approved = port_open.approved_choice(port_root, batch_name, "two-homes", subject)
    if approved is not None and approved in candidates:
        return approved
    if apply:
        port_open.raise_item(port_root, batch_name, kind="two-homes", subject=subject,
                             candidates=candidates, evidence=evidence, step="add-module")
    return None


def _leftover_names_resolved(text: str, port_root: Path, package: str, fname: str,
                             problems: List[str], *, batch_name: str, apply: bool,
                             forks: "Optional[List[Fork]]" = None) -> str:
    """Every remaining `from <package>… import <names>` / `import <package>…`
    this call's `import_rewrite.rewrite_imports` pass did not cover — a
    unique common home for every imported NAME across `move_index.
    name_index(port_root)` resolves it (upstream's own `_name_index`
    fallback); no unique home, a star import, a multi-line block, or a bare
    `import <package>…` is reported instead of guessed.

    `>=2` common homes is the `two-homes` fork (`port_open`'s own open-items
    store): `subject` is `add-module:<fname>:<module>:<names>` — stable
    across a re-run of the SAME leftover import. An approved choice
    rewrites the import exactly like the single-candidate case above;
    otherwise the fork is raised (on `--apply` only), or appended to `forks`
    when the caller holds that decision, and reported as the same ordinary
    problem this always printed."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return text
    lines = text.splitlines(keepends=True)
    idx: "Optional[dict]" = None
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and not n.level and (
                n.module or "").split(".")[0] == package:
            if any(a.name == "*" for a in n.names):
                problems.append(
                    "%s: cannot resolve `from %s import *` (star import)" % (fname, n.module))
                continue
            if n.lineno != (n.end_lineno or n.lineno):
                problems.append(
                    "%s: multi-line `from %s import ...` left unresolved -- rewrite by hand"
                    % (fname, n.module))
                continue
            names = [a.name for a in n.names]
            if idx is None:
                idx = move_index.name_index(port_root)
            common: "Optional[set]" = None
            for nm in names:
                h = idx.get(nm) or set()
                common = set(h) if common is None else (common & h)
            if common and len(common) == 1 and n.module:
                (new_mod,) = common
                lines[n.lineno - 1] = lines[n.lineno - 1].replace(n.module, new_mod, 1)
            elif common and len(common) >= 2 and n.module:
                sorted_common = sorted(common)
                subject = "add-module:%s:%s:%s" % (fname, n.module, ",".join(names))
                evidence = ["%s: `from %s import %s` has %d equally-common homes: %s"
                            % (fname, n.module, ", ".join(names), len(sorted_common),
                               ", ".join(sorted_common))]
                chosen = _two_homes_choice(port_root, batch_name, subject, sorted_common,
                                           evidence=evidence, apply=apply and forks is None)
                if chosen is None and forks is not None:
                    forks.append(Fork(subject, n.module, tuple(names), sorted_common, evidence))
                if chosen is not None:
                    lines[n.lineno - 1] = lines[n.lineno - 1].replace(n.module, chosen, 1)
                else:
                    problems.append(
                        "%s: `from %s import %s` -> no unique home (candidates %s)"
                        % (fname, n.module, ", ".join(names), sorted_common))
            else:
                problems.append(
                    "%s: `from %s import %s` -> no unique home (candidates %s)"
                    % (fname, n.module, ", ".join(names), sorted(common or [])))
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] == package:
                    problems.append(
                        "%s: cannot resolve `import %s` (bare import of the foreign "
                        "package, no alias to rewrite)" % (fname, a.name))
    return "".join(lines)


def upstream_spelled_imports(text: str, package: str) -> "Counter[str]":
    """Each import statement of the source `package` a text still spells the
    upstream way — what `_leftover_names_resolved` leaves when it cannot place
    it. A text that does not parse has none to count."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return Counter()
    out: "Counter[str]" = Counter()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and not n.level and \
                (n.module or "").split(".")[0] == package:
            out[ast.unparse(n)] += 1
        elif isinstance(n, ast.Import) and any(a.name.split(".")[0] == package for a in n.names):
            out[ast.unparse(n)] += 1
    return out


def _symbols_of(text: str) -> "List[str]":
    """Top-level bound names, sorted (class members ride their class)."""
    names: set = set()
    for n in ast.parse(text).body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            names.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    return sorted(names)


def _header(label: str, sha8: str, rel: str) -> str:
    return ("# %s: born-new @%s -- landed by `hs-code port add-module` from %s; "
           "imports rewritten via the port-specs + name-index.\n" % (label, sha8, rel))


def _add_module(args: argparse.Namespace) -> int:
    """`hs-code port add-module --batch NAME --land FILE=MODULE [--land …]
    [--source ID] [--dist NAME] [--label TEXT] [--symbols-file PATH]
    [--apply]` — land one or more upstream files, imports rewritten, into
    the target repo. Dry-run by default (a plan, nothing written); `--apply`
    writes. The envelope's `data` is one document (`schema: port.add_module/1`).

    Exit: 2 for anything malformed (unknown batch/flag, invalid `.port/
    port.yaml`, a source with no `package`/`repo`, a file missing under the
    source); 3 when a resolved destination lands outside the target repo or
    inside the read-only source repo (a well-formed request a rule blocks);
    1 when any file has an unresolved leftover import, an already-landed
    destination, or no unique destination at all (RED — nothing written for
    ANY file in the call, the same all-or-nothing discipline `MAP`'s
    bulk path already holds to); 0 otherwise.
    """
    root, opts = args.ctx.root, args.opts
    pairs = _parse_land_pairs(args.land)
    source_id, forced_dist, label = args.source_id, args.forced_dist, args.label
    symbols_file, batch_name = args.symbols_file, args.batch_name

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
    package = source.get("package")
    if not package:
        return Verdict.broken(
            "source %r declares no `package` — add-module needs it to tell a "
            "leftover foreign import from the target's own" % source.get("id")).exit
    repo_rel = source.get("repo")
    if not repo_rel:
        return Verdict.broken("source %r declares no `repo`" % source.get("id")).exit
    source_repo = (port_root / repo_rel).resolve()

    batch_source, batch_source_problem = port_batch_reader.find_source(
        batch, source_id or source.get("id"))
    if batch_source_problem:   # a caller's --source that names nothing is not found (1);
        # port.yaml's own id missing from the batch is broken config (2)
        return (Verdict.unresolved(batch_source_problem) if source_id
                else Verdict.broken(str(batch_source_problem))).exit
    sha8 = str(batch_source.get("pinned_sha"))[:8]

    try:
        descriptor = load_descriptor(port_root, "port add-module")
    except RefactorError as e:
        return Verdict.broken(str(e)).exit

    try:
        merged, _primary, _spans, _multi, _catch = port_spec.merged_spec_and_unit_dist(
            batch.specs_dir, package)
    except port_spec.SpecError as e:
        return Verdict.broken(str(e)).exit

    port_root_resolved = str(port_root.resolve())
    apply = bool(getattr(opts, "apply", False))
    plans: "List[dict]" = []
    for rel, target in pairs:
        src_path = source_repo / rel
        if not src_path.is_file():
            return Verdict.broken("%s: not found under source %s" % (rel, source_repo)).exit
        text = src_path.read_text(encoding="utf-8")
        here = landed_module(rel, source)
        if here is None:
            return Verdict.broken(
                "%s: source's own layout names it no dotted module (check "
                "`src_subdir`)" % rel).exit

        file_problems: "List[str]" = []
        new_text = rewrite_landed_text(text, here, rel, merged, port_root, package, file_problems,
                                       batch_name=batch_name, apply=apply)

        dest, dest_problem = _resolve_dest(target, port_root, descriptor, forced_dist,
                                           batch_name=batch_name, rel=rel, apply=apply)
        if dest_problem:
            file_problems.append("%s: %s" % (rel, dest_problem))
        if dest is not None:
            dest_resolved = str(dest.resolve())
            if not (dest_resolved == port_root_resolved
                    or dest_resolved.startswith(port_root_resolved + "/")):
                raise hs_family.RefusedError(
                    "port add-module: %s -> %s resolves OUTSIDE the target repo %s"
                    % (rel, target, port_root))
            if (dest_resolved == str(source_repo)
                    or dest_resolved.startswith(str(source_repo) + "/")):
                raise hs_family.RefusedError(
                    "port add-module: %s -> %s resolves INSIDE the read-only source "
                    "repo %s — refusing to write there" % (rel, target, source_repo))
            if dest.exists():
                file_problems.append(
                    "%s: %s already exists — add-module lands NEW files only" % (rel, dest))

        plans.append({
            "file": rel, "target": target, "dest": dest,
            "text": _header(label, sha8, rel) + new_text,
            "symbols": _symbols_of(new_text) if not file_problems else [],
            "problems": file_problems,
        })

    red = any(p["problems"] for p in plans)
    payload = {
        "schema": "port.add_module/1", "apply": apply,
        "files": [{k: (str(v) if isinstance(v, Path) else v)
                  for k, v in p.items() if k != "text"} for p in plans],
        "ok": not red,
    }

    # `--text` changes only the OUTPUT SHAPE, never whether a write happens --
    # the write below runs on the same `red`/`apply` condition regardless of
    # which branch prints the result.
    if not red and apply:
        for p in plans:
            p["dest"].parent.mkdir(parents=True, exist_ok=True)
            p["dest"].write_text(p["text"], encoding="utf-8")
        if symbols_file:
            with Path(symbols_file).open("a", encoding="utf-8") as fh:
                for p in plans:
                    fh.write("\n## %s (%d symbols)\n" % (p["target"], len(p["symbols"]))
                             + ", ".join(p["symbols"]) + "\n")

    if getattr(opts, "json", False):
        print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        return int(Verdict(1 if red else 0, payload).exit)

    for p in plans:
        print("add-module %s -> %s [%s] (%d symbol(s))"
             % (p["file"], p["target"], p["dest"], len(p["symbols"])))
        for pr in p["problems"]:
            print("    RED %s" % pr)
    if red:
        print("floor: RED")
        return Verdict.red(payload).exit
    if not apply:
        print("floor: OK (dry-run; --apply writes)")
        return Verdict.ok(payload).exit
    print("floor: written")
    return Verdict.ok(payload).exit


ADD_MODULE = Command(add_args=_add_module_args, run=_add_module)
