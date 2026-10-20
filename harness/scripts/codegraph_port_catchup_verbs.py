"""hs-code's `port` domain — `catch-up`: open the batch that follows a source
which moved on. A FLAT module under `harness/scripts/` (same `hs-code-registry.d`
reachability reason as every other `codegraph_port_*_verbs.py`).

`hs-code port catch-up <from-batch> --to <ref> [--name N] [--source ID] [--apply]`
resolves `<ref>` to a 40-hex commit in the source repo (`.port/port.yaml`
`sources[].repo`) and writes `.port/batches/<N>/batch.yaml` pinned there, with
`lane: catch-up` and `from_batch: <from-batch>` — the lane `hs-run port next`
then walks (`port-steps.yaml` `lanes.catch-up`). `frozen_snapshot` is named
`<source id>-<commit8>`, `new_snapshot` (the target tree once the drift landed,
which `judge-nodes`/`judge-removed` compare upstream bodies against) `<N>-new`,
`dists:` is the predecessor's. The predecessor's
`map/assignment.yaml` and `specs/*.yaml` are copied as the new batch's starting
point: human-edited config the person adjusts, never a live reference to the old
batch's files.

Nothing of the predecessor or of the ledger is rewritten. The ledger is
checkpointed at this moment (`port_ledger_writer.write_checkpoint`): the rows the
new batch carries forward are the rows as they stood here, and an old line
edited afterwards reads as a broken ledger.

Refusals: a `<ref>` the source does not have, or a predecessor with no
`frozen_snapshot` (nothing to carry from) is exit 2; a `<ref>` that resolves to
the commit already pinned (nothing to catch up) or a batch name already taken is
exit 3. Dry-run (no `--apply`) reports the batch it would open and writes nothing.

GIT: `rev-parse` only, read-only, on the source repo.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_config  # noqa: E402
import port_ledger_reader  # noqa: E402
import port_workspace  # noqa: E402
import yaml_io  # noqa: E402
from codegraph._core.git import GitError, git_text  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from hs_family.cli import Command  # noqa: E402
from port_ledger_writer import BrokenLedgerError, write_checkpoint  # noqa: E402

_HEADER = ("# written by `hs-code port catch-up` — the batch that follows %s to the\n"
           "# source's %s; edit freely (dists, steps) before the lane runs.\n")


def _catch_up_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("from_batch")
    ap.add_argument("--to", required=True, dest="to_ref")
    ap.add_argument("--name", default=None)
    ap.add_argument("--source", default=None, dest="source_id")


def _copied(from_dir: Path) -> "list[Path]":
    """The predecessor's human config the new batch starts from, relative paths."""
    out = [Path("map") / "assignment.yaml"] if (from_dir / "map" / "assignment.yaml").is_file() else []
    specs = from_dir / "specs"
    if specs.is_dir():
        out += [p.relative_to(from_dir) for p in sorted(specs.glob("*.yaml"))]
    return out


def _catch_up(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit
    try:
        old = port_batch_reader.load_batch(port_root, args.from_batch)
    except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
        return Verdict.unresolved(e).exit
    if not old.frozen_snapshot:
        return Verdict.broken(
            "port catch-up: batch %r declares no `frozen_snapshot` — nothing to carry "
            "the ledger forward from" % old.name).exit

    source, problem = port_batch_reader.find_source(old, args.source_id)
    if problem:
        return Verdict.unresolved(problem).exit
    cfg, cfg_problems = port_config.load_and_validate(port_workspace.port_yaml_path(port_root))
    if cfg_problems:
        return Verdict.broken("port catch-up: `.port/port.yaml` is invalid: %s"
                              % "; ".join(cfg_problems)).exit
    source_cfg, source_problem = port_config.find_source(cfg, source.get("id"))
    if source_problem:
        return Verdict.unresolved(source_problem).exit
    repo_rel = source_cfg.get("repo") if isinstance(source_cfg, dict) else None
    if not isinstance(repo_rel, str) or not repo_rel:
        return Verdict.broken("port catch-up: source %r has no `repo:` in `.port/port.yaml`"
                              % source.get("id")).exit
    source_repo = (port_root / repo_rel).resolve()
    try:
        to_sha = git_text(source_repo, "rev-parse", "--verify", "--quiet",
                          "%s^{commit}" % args.to_ref)
    except GitError:
        to_sha = ""
    if len(to_sha) != 40:
        return Verdict.broken("port catch-up: source %s has no commit %r"
                              % (source_repo, args.to_ref)).exit

    from_sha = str(source.get("pinned_sha"))
    if to_sha == from_sha:
        raise hs_family.RefusedError(
            "port catch-up: %r is already pinned at %s — nothing to catch up"
            % (old.name, to_sha[:8]))
    name = args.name or "%s-catchup-%s" % (old.name, to_sha[:8])
    new_dir = port_batch_reader.batches_dir(port_root) / name
    if new_dir.exists():
        raise hs_family.RefusedError(
            "port catch-up: a batch named %r already exists (%s) — pick another --name"
            % (name, new_dir))

    doc = {"version": 1, "sources": [{"id": source.get("id"), "pinned_sha": to_sha}],
           "frozen_snapshot": "%s-%s" % (source.get("id"), to_sha[:8]),
           "new_snapshot": "%s-new" % name, "lane": "catch-up", "from_batch": old.name}
    if old.dists:
        doc["dists"] = list(old.dists)
    copied = _copied(old.dir)
    data = {"batch": name, "from_batch": old.name, "from_sha": from_sha, "to_sha": to_sha,
            "frozen_snapshot": doc["frozen_snapshot"], "copied": [p.as_posix() for p in copied],
            "apply": bool(getattr(opts, "apply", False))}

    as_json = bool(getattr(opts, "json", False))
    if not as_json:
        print("catch-up  : %s -> %s (%s -> %s)" % (old.name, name, from_sha[:8], to_sha[:8]))
        print("freeze    : %s" % doc["frozen_snapshot"])
        print("copied    : %s" % (", ".join(data["copied"]) or "(nothing)"))
    if not data["apply"]:
        _report(data, as_json, "\ndry-run: nothing written (--apply opens the batch)")
        return int(Verdict.ok(data).exit)

    ledger = port_ledger_reader.ledger_path(port_root)
    try:
        data["checkpoint"] = write_checkpoint(
            ledger, why="catch-up %s from %s @ %s" % (name, old.name, to_sha[:8]))
    except BrokenLedgerError as e:
        return Verdict.broken("port catch-up: %s" % e).exit
    new_dir.mkdir(parents=True)
    for rel in copied:
        (new_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(old.dir / rel, new_dir / rel)
    (new_dir / "batch.yaml").write_text(
        _HEADER % (old.name, to_sha[:8]) + yaml_io.safe_dump(doc, sort_keys=False),
        encoding="utf-8")
    port_batch_reader.load_batch(port_root, name)
    _report(data, as_json, "\nopened %s — next: `hs-run port next`" % new_dir)
    return int(Verdict.ok(data).exit)


def _report(data: dict, as_json: bool, closing: str) -> None:
    if as_json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print(closing)


CATCH_UP = Command(add_args=_catch_up_args, run=_catch_up)
