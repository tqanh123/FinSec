"""hs-code's `graph` domain — the 5 verbs ported from the reference tool's
rules engine + devkit clones: `rules`, `drift`, `incremental`, `init`,
`adapter-blindspots`.

Each verb is a `Command` constant (`add_args`/`run`) named by its registry row's
`entry:`; the family core parses the declared argv and hands `run` a namespace
carrying `ctx.root` (the target repo) and `opts` (`hs_code.ShellOptions`, the
shell's own flags).

SEVERITY DRIVES THE EXIT CODE, NOT VIOLATION COUNT (both `rules` and
`incremental`): a rule declared `severity: warn` reports its violations —
printed, and present in the envelope's `data` — but never turns exit 0 into exit 1. Only
`RuleResult.is_red` (severity `error` AND at least one violation) counts
toward `red`; everything else counts toward `warned`. See
`codegraph._core.rules.base`'s own docstring for the measured bug this
fixes (the reference tool's `severity: nonsense` silently read like `warn`
for the verdict while still being summed into the exit-driving count).

`init` is NON-INTERACTIVE: the ported CLI's own `input()` prompt loop
assumes a TTY an agent never has, so it is not ported — `--layout`/
`--project` resolve from argv, falling back to the auto-discover suggestion
and the target directory's own name, and a still-unresolved layout is an
honest exit 2 naming the available presets, never a hang on stdin.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[2]   # codegraph/<pkg>/x.py -> scripts
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import codemap_adoption  # noqa: E402
import hs_family.base_drift as base_drift  # noqa: E402
import codegraph_snapshot_reader  # noqa: E402

from codegraph._core.descriptor import (DescriptorError, discover_roots, load,  # noqa: E402
                                        root_paths)
from codegraph._core.drift import banner, detail_lines, drift_report, drifted  # noqa: E402
from codegraph._core.graph.model import Graph  # noqa: E402
from codegraph._core.incremental import (IncrementalError, changed_files,  # noqa: E402
                                         run_incremental, touched_from_snapshots,
                                         touched_modules)
from codegraph._core.presets import available_presets, dump_yaml, resolve_doc  # noqa: E402
from codegraph._core.lang import registry as lang_registry  # noqa: E402
from codegraph._core.rules import RuleContext, RulesEngine, RulesError  # noqa: E402
from codegraph._core.snapshot.pipeline import SnapshotError, SnapshotRefused  # noqa: E402
from codegraph._core.snapshot.store import SnapshotMissing, load_dir, resolve_snapshot  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from hs_family.cli import Command  # noqa: E402

from codegraph_graph_freshness import SnapshotResolveError, resolve_idx  # noqa: E402
from codegraph.graph.verbs import SnapshotBaseUnresolved, _snapshot_base  # noqa: E402


def _load_descriptor(root: Path):
    return load(root / "codemap.yaml", root)


# ── rules ────────────────────────────────────────────────────────────────────

def _rules_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--snapshot", default=None, metavar="DIR")
    ap.add_argument("--only", action="append", default=None, metavar="ID",
                    help="repeatable — restrict to these declared rule ids")


def _rules(args: argparse.Namespace) -> int:
    """`rules [--snapshot DIR] [--only ID]...` — every DECLARED rule, over the
    whole latest snapshot (or `--snapshot`) — and only a FRESH one.

    Exit: 0 clean · 1 at least one `severity: error` rule is red · 2
    cannot-run — a bad descriptor, no declared rules, an unknown `--only` id,
    a `severity` outside `{error, warn}`, a stale snapshot (`--fresh`
    measures the on-disk tree; `--allow-stale` overrides), or a `layer_direction` rule
    that decided NOTHING (every edge undecided — green over a plain,
    dist-less tree would check nothing while looking clean).
    """
    root, opts = args.ctx.root, args.opts
    snapshot_dir_arg, only = args.snapshot, args.only

    try:
        descriptor = _load_descriptor(root).descriptor
    except DescriptorError as e:
        return Verdict.broken(str(e)).exit
    if not descriptor.rules:
        return Verdict.broken(
            f"{descriptor.path} declares no `rules:` — nothing to run").exit
    rules = list(descriptor.rules)
    if only:
        want = set(only)
        rules = [r for r in rules if r["id"] in want]
        missing = sorted(want - {r["id"] for r in rules})
        if missing:
            return Verdict.broken(
                f"no rule id {', '.join(missing)} in {descriptor.path}").exit

    as_json = getattr(opts, "json", False)
    if not root_paths(descriptor) and not as_json:
        print("⚠ no layout.roots declared — freshness check runs "
              "un-scoped (whole repo)", file=sys.stderr)

    try:
        _, snap_dir, idx, scratch = resolve_idx(root, descriptor, opts, None, None, "rules",
                                                fresh=bool(getattr(opts, "fresh", False)),
                                                snap_dir=snapshot_dir_arg)
    except SnapshotResolveError as e:
        return Verdict.broken(str(e)).exit
    try:
        return _run_declared_rules(opts, root, descriptor, rules, snap_dir, idx)
    finally:
        if scratch is not None:
            import shutil
            shutil.rmtree(scratch, ignore_errors=True)


RULES = Command(add_args=_rules_args, run=_rules)


def judge(repo: Path, descriptor, rules: list, idx: dict) -> "tuple[list, dict[str, str]]":
    """Run declared `rules` over one snapshot -> (results in rule order, {rule
    id: why it checked nothing}). Every caller that turns a rule into a verdict
    goes through here, so "ran" means the same thing for `graph rules` and for
    the gate book's `code` page. Raises `RulesError` for a rule that cannot be
    built or run."""
    engine = RulesEngine(rules)
    graph = Graph.from_snapshot(idx, groups=descriptor.groups)
    results = engine.run(RuleContext(repo=repo, graph=graph, idx=idx, descriptor=descriptor))
    cannot: dict[str, str] = {}
    for rule, res in zip(rules, results):
        if rule["kind"] == "layer_direction":
            total = sum(res.metrics.get("edges", {}).values())
            undecided = sum(res.metrics.get("undecided", {}).values())
            if total and undecided == total:
                cannot[rule["id"]] = (
                    f"rule {rule['id']!r}: every one of its {total} edges is "
                    f"undecided — no group resolved on either end, so this "
                    f"rule checked nothing (declare `groups:` or widen "
                    f"`tiers:`)")
    return results, cannot


def _run_declared_rules(opts: Any, repo: Path, descriptor, rules: list,
                        snap_dir: Path, idx: dict) -> int:
    try:
        results, cannot_by_rule = judge(repo, descriptor, rules, idx)
    except RulesError as e:
        return Verdict.broken(str(e)).exit
    cannot = list(cannot_by_rule.values())
    red = warned = 0
    for res in results:
        if res.is_red:
            red += len(res.violations)
        else:
            warned += len(res.violations)

    as_json = getattr(opts, "json", False)
    if not as_json:
        print(f"snapshot {snap_dir.name}  ({snap_dir})")
        for rule, res in zip(rules, results):
            print(f"rule {rule['id']} ({rule['kind']}, {res.severity}): "
                  f"{len(res.violations)} violation(s)")
            for v in res.violations:
                print(f"  {v.cause}  {v.label}")
            for n in res.notes:
                print(f"  ⚠ {n}")
        print(f"{red} red violation(s), {warned} warned violation(s) "
              f"across {len(rules)} rule(s)")
        for why in cannot:
            print(f"hs-code graph rules: {why}", file=sys.stderr)

    data = {"snapshot": snap_dir.name, "dir": str(snap_dir),
            "red": red, "warned": warned,
            "rules": [{"id": r["id"], "kind": r["kind"], "severity": res.severity,
                       "violations": [{"cause": v.cause, "label": v.label,
                                       "detail": v.detail} for v in res.violations],
                       "notes": list(res.notes)}
                      for r, res in zip(rules, results)],
            "cannot_run": cannot}
    if as_json:
        for why in cannot:
            print(why, file=sys.stderr)
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    code = 2 if cannot else (1 if red else 0)
    return code


# ── drift ────────────────────────────────────────────────────────────────────

def _drift_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--snapshot", default=None, metavar="DIR")
    ap.add_argument("--strict", action="store_true")


def _drift(args: argparse.Namespace) -> int:
    """`drift [--snapshot DIR] [--strict]` — is the measured tree still the
    snapshot. `--snapshot` defaults to the latest in this project's own
    store (hs-code's `--repo` already names the measured tree, so drift
    never takes a second `--repo` of its own the way the ported CLI did).
    `--strict` turns GRAPH drift into exit 1; cosmetic-only edits never
    redden (see `codegraph._core.drift`'s own module docstring)."""
    root, opts = args.ctx.root, args.opts
    snapshot_dir_arg, strict = args.snapshot, args.strict

    try:
        descriptor = _load_descriptor(root).descriptor
    except DescriptorError as e:
        return Verdict.broken(str(e)).exit

    if snapshot_dir_arg:
        snap_dir = Path(snapshot_dir_arg)
        if not snap_dir.is_dir():
            return Verdict.broken(f"drift: no snapshot dir {snap_dir}").exit
    else:
        try:
            base = _snapshot_base(descriptor.project, None, root)
        except SnapshotBaseUnresolved as e:
            return Verdict.broken(str(e)).exit
        try:
            # Deliberately NOT `store.resolve_current`: drift's whole job is
            # comparing the last MEASURED snapshot against the tree as it
            # stands now, so it must read whatever was actually written
            # last -- a content-aware "pick the one matching the current
            # tree" resolver would make an out-of-date snapshot invisible
            # to the one command that exists to report it.
            snap_dir = resolve_snapshot(base, None)
        except SnapshotMissing as e:
            return Verdict.broken(str(e)).exit
    try:
        idx = load_dir(snap_dir)
    except SnapshotMissing as e:
        return Verdict.broken(str(e)).exit

    try:
        report = drift_report(root, idx, descriptor)
    except SnapshotRefused as e:
        return Verdict.refused("drift: %s" % e).exit
    except SnapshotError as e:
        return Verdict.broken("drift: %s" % e).exit
    if report.get("engine_mismatch"):
        return Verdict.broken("drift: snapshot %s was measured with another engine (engine_mismatch: %s) -- "
                              "a new engine is a new measurement, not drift: run `hs-code --apply graph "
                              "snapshot`" % (idx.get("snapshot"), ", ".join(
                                  "%s %s, codemap.yaml declares %s" % (lang, v["snapshot"], v["declared"])
                                  for lang, v in sorted(report["engine_mismatch"].items())))).exit
    is_drifted = drifted(report)
    as_json = getattr(opts, "json", False)
    if not as_json:
        if is_drifted:
            print(banner(report))            # level (a): always first, never buried
        for ln in detail_lines(report):      # level (c)
            print(ln)

    data = dict(report)
    data["changed"] = [[p, r] for p, r in report["changed"]]
    data["drifted"] = is_drifted
    if as_json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    if is_drifted and strict:                # level (b): CI red
        print("drift: STRICT — snapshot is stale, exit 1", file=sys.stderr)
        return 1
    return 0


DRIFT = Command(add_args=_drift_args, run=_drift)


# ── incremental ──────────────────────────────────────────────────────────────

def _incremental_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--base", default=None, metavar="COMMIT")
    ap.add_argument("--old-snap", default=None, metavar="DIR")
    ap.add_argument("--new-snap", default=None, metavar="DIR")
    ap.add_argument("--snapshot", default=None, metavar="DIR")
    ap.add_argument("--rules-file", default=None, metavar="JSON")


def _incremental(args: argparse.Namespace) -> int:
    """`incremental (--base COMMIT | --old-snap DIR --new-snap DIR)
    [--snapshot DIR] [--rules-file JSON]` — run declared rules over only the
    CHANGED modules; the repo positional the ported CLI took is DROPPED
    (hs-code's shared `--repo` already names it). `--base` warns (never
    blocks) when the LOCAL branch it names has drifted from its
    `origin/<base>` counterpart — see `hs_family.base_drift`'s own contract.
    """
    root, opts = args.ctx.root, args.opts
    base_rev, old_snap, new_snap = args.base, args.old_snap, args.new_snap
    snapshot_dir_arg, rules_file = args.snapshot, args.rules_file

    stamp_warn: "str | None" = None
    try:
        if old_snap and new_snap:
            a, b = load_dir(Path(old_snap)), load_dir(Path(new_snap))
            stamp_warn = codegraph_snapshot_reader.stamp_mismatch(
                a, b, labels=(f"--old-snap {Path(old_snap).name}", f"--new-snap {Path(new_snap).name}"))
            touched = touched_from_snapshots(a, b)
        elif base_rev:
            base_drift.warn_if_base_drifted(root, base_rev)
            module_at = None
            if (root / "codemap.yaml").is_file():
                from codegraph._core.snapshot.pipeline import import_context
                try:
                    module_at = import_context(root, _load_descriptor(root).descriptor).module_at
                except SnapshotRefused as e:
                    return Verdict.refused(f"incremental: {e}").exit
            touched = touched_modules(changed_files(root, base_rev), module_at)
        else:
            return Verdict.broken(
                "incremental: give --base <commit> or --old-snap/--new-snap").exit
    except (IncrementalError, SnapshotMissing, DescriptorError, SnapshotError) as e:
        return Verdict.broken(f"incremental: {e}").exit

    as_json = getattr(opts, "json", False)
    if stamp_warn:
        print(f"⚠ {stamp_warn}", file=sys.stderr)

    def _emit_touched_only(rules_run: bool) -> int:
        data = {"touched": sorted(touched), "rules_run": rules_run}
        if stamp_warn:
            data["notes"] = [stamp_warn]
        if as_json:
            print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        return 0

    if not touched:
        # Nothing changed is a REAL answer, distinct from "could not tell".
        if not as_json:
            print("incremental: no .py module touched — nothing to re-check")
        return _emit_touched_only(False)

    if not as_json:
        print(f"incremental: {len(touched)} touched module(s)")
        for m in sorted(touched):
            print(f"  {m}")

    if not rules_file:
        if not as_json:
            print("pass --rules-file + --snapshot to actually run the "
                  "declared rules")
        return _emit_touched_only(False)
    if not snapshot_dir_arg:
        return Verdict.broken("incremental: --rules-file needs --snapshot DIR").exit

    try:
        rules = json.loads(Path(rules_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return Verdict.broken(
            f"incremental: cannot read --rules-file {rules_file}: {e}").exit
    try:
        idx = load_dir(Path(snapshot_dir_arg))
    except SnapshotMissing as e:
        return Verdict.broken(f"incremental: {e}").exit

    try:
        report = run_incremental(idx, rules, root, touched)
    except (IncrementalError, RulesError) as e:
        return Verdict.broken(f"incremental: {e}").exit

    if not as_json:
        for r in report["rules"]:
            note = (f" (dropped {r.get('dropped', 0)} outside diff)"
                    if r["mode"] == "narrowed" else " (full: global kind)")
            print(f"  {r['id']:<24} {r['kind']:<18} {r['mode']:<9} "
                  f"{r['severity']:<6} red={r['red']} warned={r['warned']}"
                  f"{note}")

    data = {"touched": sorted(touched), "rules_run": True, **report}
    if stamp_warn:
        data["notes"] = [stamp_warn]
    if as_json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    return 1 if report["red"] else 0


INCREMENTAL = Command(add_args=_incremental_args, run=_incremental)


# ── init ─────────────────────────────────────────────────────────────────────

#: auto-discover rule id -> preset id `init` offers as its default `--layout`.
_SUGGEST: dict[str, str] = {
    "uv-workspace": "monorepo-workspace",
    "sibling-pyprojects": "monorepo-workspace",
    "src-layout": "src-layout",
    "flat-packages": "python-flat",
    "repo-flat-fallback": "python-flat",
}


def _suggest_layout(target: Path) -> "tuple[str | None, str]":
    _roots, rule = discover_roots(target)
    return _SUGGEST.get(rule), rule


def _init_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--layout", default=None, metavar="PRESET")
    ap.add_argument("--project", default=None, metavar="NAME")


def _init(args: argparse.Namespace) -> int:
    """`init [--layout PRESET] [--project NAME]` — write (dry-run by
    default; `--apply` writes for real) a fresh `codemap.yaml`.

    Non-interactive by design: the ported CLI's `input()` prompt loop assumes
    a TTY an agent never has, so it is REPLACED here, never ported — a
    `--layout` not given falls back to the auto-discover suggestion, and a
    still-unresolved layout is exit 2 naming the presets, never a hang on
    stdin. `--project` defaults to the target directory's own name.
    """
    root, opts = args.ctx.root, args.opts
    layout, project = args.layout, args.project

    if not root.is_dir():
        return Verdict.broken(f"init: no such directory {root}").exit
    for existing in ("codemap.yaml", "index.yaml"):
        if (root / existing).is_file():
            return Verdict.broken(
                f"{root / existing} already exists — init writes a fresh "
                f"descriptor; edit the existing one instead (two descriptors "
                f"at one level is a root-detection coin flip)").exit

    adoption = codemap_adoption.status(root)
    if adoption.state == "unsupported":
        return Verdict.refused(f"init: {adoption.reason} — a descriptor here would own "
                               f"nothing", adoption=adoption.as_dict()).exit
    languages, unrunnable = _language_block(root, adoption.packs)
    python = lang_registry.DEFAULT_ID in adoption.packs
    if not python and not languages:
        return Verdict.refused(
            "init: no engine this machine runs reads this repo's %s files — nothing written; "
            "install one, then run init again:\n  %s"
            % (", ".join(adoption.packs), "\n  ".join(unrunnable)),
            adoption=adoption.as_dict(), unrunnable=unrunnable).exit
    if unrunnable:
        print("init: left out of `languages:` (no engine runs here):\n  %s"
              % "\n  ".join(unrunnable), file=sys.stderr)

    presets = available_presets()
    suggestion, rule = _suggest_layout(root) if python else (None, "no-python-files")
    if layout is None and not python:
        layout = ""   # a repo with no Python file: one root over the repo, units from its packages
    if layout is None:
        layout = suggestion
    if layout is None:
        return Verdict.broken(
            f"init: no --layout given and auto-discover found no usable "
            f"suggestion (rule `{rule}`) — choose one of: "
            f"{', '.join(presets)}").exit
    if layout and layout not in presets:
        return Verdict.broken(
            f"init: unknown preset {layout!r}; choose one of: "
            f"{', '.join(presets)}").exit

    project = project or root.name
    if not project:
        return Verdict.broken("init: project name must be non-empty").exit

    # Merge through the SAME door a hand-written `extends:` uses, then write
    # the result SELF-CONTAINED: the generated file keeps no indirection a
    # later human edit has to chase.
    if layout:
        doc = resolve_doc({"schema_version": 2, "project": project, "extends": layout})
        roots = _measured_import_roots(root, doc)
        if roots:
            doc["import_roots"] = roots
    else:
        doc = {"schema_version": 2, "project": project,
               "layout": {"roots": [{"id": project, "path": ".", "kind": "flat"}]}}
    if languages:
        doc["languages"] = languages
    out = root / "codemap.yaml"
    apply = bool(getattr(opts, "apply", False))
    if apply:
        out.write_text(dump_yaml(doc), encoding="utf-8")

    data = {"path": str(out), "descriptor": doc, "suggested": suggestion,
            "discover_rule": rule, "dry_run": not apply}
    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    elif apply:
        print(f"wrote {out}")
        print("next: hs-code graph snapshot --apply   # then: hs-code graph stats")
    else:
        print(f"[dry-run: nothing written — --apply writes {out}]")
        print(dump_yaml(doc), end="")
    return 0


def _language_block(root: Path, packs) -> "tuple[dict, list[str]]":
    """`languages:` for a fresh codemap, and what was left out of it: every pack the
    repo holds beside the default one, with the first engine this machine runs
    (`registry.runnable_engine`); with any of them, the default pack named too, since a
    `languages:` block replaces the default rather than adding to it."""
    block: "dict[str, dict]" = {}
    lines: "list[str]" = []
    by_id = {p.id: p for p in lang_registry.packs()}
    for pid in packs:
        pack = by_id.get(pid)
        if pack is None or pid == lang_registry.DEFAULT_ID:
            continue
        options: "dict[str, str]" = {}
        if pack.engines:
            eng, why = lang_registry.runnable_engine(pack, root)
            if eng is None:
                lines += ["%s: %s" % (pid, w) for w in why]
                continue
            options["engine"] = eng.name
        block[pid] = options
    if block and lang_registry.DEFAULT_ID in packs:
        block = {lang_registry.DEFAULT_ID: {}, **block}
    return block, lines


def _measured_import_roots(root: Path, doc: dict) -> "list[str]":
    """`import_roots` for a fresh descriptor, from the roots its layout declares.

    Without it `layout.dotted_of` cannot turn `src/app/core.py` into `app.core`,
    and every test importing the package reads as an orphan. One entry per
    declared root, by `kind`: `src`/`flat` import from the root's own path; a
    `workspace` imports from `<path>/*/src` when every package there has a
    `src/`, from both `<path>/*/src` and `<path>/*` when only some do, else
    from `<path>/*`. A preset that declares its own keeps it."""
    if doc.get("import_roots"):
        return list(doc["import_roots"])
    out: "list[str]" = []
    for spec in ((doc.get("layout") or {}).get("roots") or []):
        path, kind = str(spec.get("path") or "."), spec.get("kind")
        if kind in ("src", "flat"):
            out.append(path)
        elif kind == "workspace":
            base = root / path
            pkgs = ([d for d in sorted(base.iterdir())
                     if d.is_dir() and not d.name.startswith((".", "__"))]
                    if base.is_dir() else [])
            has_src = [(d / "src").is_dir() for d in pkgs]
            if has_src and all(has_src):
                out.append(f"{path}/*/src")
            elif any(has_src):
                # mixed (a meta dist with no python, a flat dist): the longest
                # match wins, so `<dist>/src/...` still loses its `src.` prefix
                out += [f"{path}/*/src", f"{path}/*"]
            else:
                out.append(f"{path}/*")
    return list(dict.fromkeys(out))


INIT = Command(add_args=_init_args, run=_init)


# ── adapter-blindspots ───────────────────────────────────────────────────────

BLINDSPOT_MARKER = "WHAT IT CANNOT SEE"


def _blindspot_section(module: Any) -> "list[str] | None":
    """The blind-spot contract lines of a module, or None when it has none.

    Works on the RAW docstring (indentation preserved): the section runs from
    the marker line until the NEXT module-level header — a flush-left line
    whose first word is all-uppercase. Continuation prose of the marker's own
    sentence sits flush-left too but starts lowercase, so it stays in; a
    dedented parse would flatten exactly that distinction.
    """
    doc = getattr(module, "__doc__", None) or ""
    lines = doc.split("\n")
    start = next((i for i, ln in enumerate(lines) if BLINDSPOT_MARKER in ln), None)
    if start is None:
        return None
    out: list[str] = [lines[start].rstrip()]     # the marker line carries its own context
    for ln in lines[start + 1:]:
        if ln.strip() and not ln[:1].isspace():
            first = ln.split(None, 1)[0]
            if first.isupper():
                break  # next module-level section header
        out.append(ln.rstrip())
    while out and not out[-1].strip():
        out.pop()
    return out


def _engine_rows(missing: "list[str]", repo: "Path | None" = None) -> "list[dict[str, Any]]":
    """One row per engine a language pack offers (`languages.<id>.engine`): its
    reader's contract, or `not_installed` with the install line when what it needs
    (wheels, Node.js, pinned npm packages) is absent here -- an optional engine is not
    a contract violation."""
    import importlib

    from codegraph._core.lang import registry as lang_registry

    rows: "list[dict[str, Any]]" = []
    for pack in lang_registry.packs():
        for eng in pack.engines:
            row: "dict[str, Any]" = {"ext": " ".join(pack.suffixes()), "adapter": eng.reader_ref[1],
                                     "engine": f"{pack.id}/{eng.name}"}
            if lang_registry.missing_requirements(eng, repo):
                rows.append({**row, "section": None, "status": "not_installed",
                             "install": eng.install})
                continue
            section = _blindspot_section(importlib.import_module(eng.reader_ref[0]))
            if section is None:
                missing.append(f"{row['engine']} -> {row['adapter']}")
            rows.append({**row, "section": section, "status": "ok" if section else "MISSING"})
    return rows


def _adapter_blindspots_args(ap: argparse.ArgumentParser) -> None:
    pass


def _adapter_blindspots(args: argparse.Namespace) -> int:
    """`adapter-blindspots` — print every registered language adapter's
    WHAT-IT-CANNOT-SEE contract; exit 1 if any adapter lacks one (the
    blind-spot contract is law, not an optional docstring nicety)."""
    opts = args.opts

    import inspect

    import codegraph._core.adapter.base as adapter_base
    from codegraph._core.adapter.registry import by_extension

    shared = _blindspot_section(adapter_base)
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for ext, cls in sorted(by_extension().items()):
        mod = inspect.getmodule(cls)
        section = _blindspot_section(mod) if mod else None
        if section is None:
            missing.append(f"{ext} -> {cls.__name__}")
            rows.append({"ext": ext, "adapter": cls.__name__,
                         "section": None, "status": "MISSING"})
        else:
            rows.append({"ext": ext, "adapter": cls.__name__,
                         "section": section, "status": "ok"})
    rows += _engine_rows(missing, Path(args.ctx.root))
    code = 1 if missing else 0
    data = {"shared_contract": shared, "adapters": rows, "missing": missing}
    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print("shared adapter contract (codegraph._core.adapter.base):")
        for ln in shared or ["(MISSING — contract violation)"]:
            print(f"  {ln}")
        for r in rows:
            print(f"\n{r['ext']}  {r['adapter']}:")
            if r["status"] == "MISSING":
                print("  !! no WHAT IT CANNOT SEE section — the blind-spot"
                      " contract is law; refusing to vouch for this adapter")
            elif r["status"] == "not_installed":
                print(f"  engine {r['engine']} not installed here: {r['install']}")
            else:
                for ln in r["section"]:
                    print(f"  {ln}")
    return code


ADAPTER_BLINDSPOTS = Command(add_args=_adapter_blindspots_args, run=_adapter_blindspots)
