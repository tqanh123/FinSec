"""hs-code's `port` domain — the NAMED snapshot write verb, as one FLAT
module under `harness/scripts/` (same `hs-code-registry.d` reachability
reason as `codegraph_port_batch_verbs.py`).

`hs-code port snapshot <name> [--rebuild]` builds a snapshot through the
EXISTING kernel pipeline (`codegraph._core.snapshot.pipeline.take_snapshot`
— never copied) into `<port_root>/.port/snapshots/<name>/`, the location
every other port reader already resolves through
`port_batch_reader.snapshot_path` (`codegraph_port_batch_verbs.py`,
`codegraph_port_judge_verbs.py`, `port_batch_reader.snapshot_commit`
itself). It then records `{name: {commit, sha256}}` in the shared
`.port/snapshots/index.json` and ensures a `.gitignore` there that keeps
only `index.json` and itself tracked — a snapshot is a measurement, pinned
by its hash, never committed wholesale (user decision: rebuild on demand
from the recorded commit, verify by hash, never diff a tree of generated
YAML).

WHICH COMMIT — PROBED, not assumed: `codegraph._core.snapshot.pipeline.
commit_stamp` labels a snapshot with `git rev-parse HEAD` of the `repo` path
it is handed, but `take_snapshot` itself scans that path's plain FILESYSTEM
(`descriptor.model.scan_py` reads real files) — it never runs `git show`.
Measured directly (two commits, then an UNCOMMITTED third edit on top): the
written `_index.json` still stamped `commit` as the second commit's sha,
while the unit file's own `bytes`/`file_sha` reflected the THIRD,
uncommitted edit. A `commit=` argument to `take_snapshot` only overrides the
printed LABEL — it never causes a checkout. So building a snapshot pinned to
a commit that is not `repo`'s current HEAD (a batch's `frozen_snapshot`,
built at the batch's `pinned_sha`) needs a real, separate checkout of that
commit on disk.

`materialize_pin` below does that WITHOUT writing anything under the
SOURCE repo — measured (`<src>/.git` snapshotted by path+size+mtime_ns, and
`git worktree list --porcelain`, both before/after, byte-identical): a
plain `git worktree add --detach` registers the new worktree under
`<source_repo>/.git/worktrees/…`, and a crash between add and remove would
leave it there. Instead: `git clone --shared --no-checkout <source_repo>
<scratch>` (a same-machine clone that shares `<source_repo>`'s OBJECT
STORE via `objects/info/alternates` rather than copying it — this writes
files only under `<scratch>`, never `<source_repo>`) then `git checkout
--detach <pinned_sha>` INSIDE that clone. `git archive` was the other
candidate PROBED against and rejected: it applies `export-ignore`/
`export-subst` `.gitattributes` filters, which can drop or rewrite file
bytes — the opposite of what a byte-measuring snapshot needs. The clone
being a real (if disposable) git repo also means `take_snapshot`'s own
default `commit=None` behaviour needs no override: its `git rev-parse HEAD`
reads the clone's own HEAD, which the explicit `checkout --detach
<pinned_sha>` set to the pin exactly — the checkout, not a compare after
the fact, is what makes the built commit equal the pin (see `SNAPSHOT`,
no post-build anchor/pin check remains: after this construction there is
nothing left for one to catch that the checkout step did not already
guarantee). Cleanup is the scratch dir's own `TemporaryDirectory` context
manager — no separate `worktree remove` step is needed once nothing is
registered against the source repo in the first place.

BATCH OWNERSHIP: `<name>` is looked up against every batch's own
`frozen_snapshot` (`port_batch_reader.list_batches`/`load_batch`) — a
match puts this build in PINNED mode (source `.port/port.yaml` repo path +
batch's own `pinned_sha`). No batch names `<name>` -> PLAIN mode, `root`
itself at its own HEAD (the common case: a target/new-tree snapshot, or an
ad hoc named snapshot with no batch at all).

WHICH DESCRIPTOR, in pinned mode: the SOURCE's own `descriptor:` key in
`.port/port.yaml`'s `sources[]` (`port_config._ALLOWED_SOURCE_KEYS`, the
single config door — never a batch.yaml key), a path relative to the port
root (the same base `repo:` already resolves against), loaded against the
materialized pin — a REAL pre-`schema_version` descriptor is a project-local
artifact describing how to read that source's old layout; it does not live
inside the source tree itself (upstream `portkit/legacy.py:333-337`'s own
`load_batch` reads it from the PLAN dir, never the source repo). Falls back
to the source tree's own `codemap.yaml` (today's only shape) when
`descriptor:` is undeclared on the source. Plain mode always reads `root`'s
own `codemap.yaml` — there is no source entry in play to carry an alternate
descriptor path — and holds it to what every other port verb reads off the
target: schema_version 2 (a legacy descriptor there is refused, pointing at
`sources[].descriptor`), and no uncommitted code inside the measured roots
(the snapshot stamps HEAD's commit, the anchor every `to` written against it
names). A plain-mode name some batch declares as its own `new_snapshot` is
that batch's TARGET-TREE snapshot and measures only the roots at or under
`layout.packages_dir` (see `_target_tree_roots`); the dirty-tree refusal then
looks only at those roots too, since nothing else is measured.

LEGACY SOURCES: before loading either mode's descriptor, this verb calls
`port_legacy_snapshot.register_all()` (idempotent) — the ONE real caller
that lets a pre-`schema_version` source descriptor (an old porting
`index.yaml`-shaped tree) be claimed by the port domain's own codec instead
of refusing outright. An unclaimed legacy doc still refuses honestly, exit
2, naming what was tried (`codegraph._core.descriptor.store._load_legacy`).

WRITES are `--apply`-gated like every other write verb in this domain
(`harness/tests/hs_code/test_hs_code_write_flag.py`'s own convention):
without `--apply` this verb measures into a scratch directory and reports
what it WOULD do, touching nothing under `.port/snapshots/`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from ambiguous_input import Ambiguous  # noqa: E402
import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_config  # noqa: E402
import port_legacy_snapshot  # noqa: E402
import port_workspace  # noqa: E402
from codegraph._core.descriptor import DescriptorError, load as load_descriptor  # noqa: E402
from codegraph._core.git import GitError, run_git  # noqa: E402
from codegraph._core.snapshot.freshness import dirty_paths  # noqa: E402
from codegraph._core.snapshot.pipeline import SnapshotError, take_snapshot  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from hs_family.cli import Command  # noqa: E402

_GITIGNORE_TEXT = "*\n!.gitignore\n!%s\n" % port_batch_reader.SNAPSHOT_REGISTRY_NAME


def _snapshot_args(ap: argparse.ArgumentParser) -> None:
    """`name` is the sole positional; `--rebuild` is the only flag this
    verb declares of its own (`--repo`/`--apply` are hs-code's
    shell flags)."""
    ap.add_argument("name")
    ap.add_argument("--rebuild", action="store_true")


def _hash_snapshot_dir(d: Path) -> str:
    """sha256 over EVERY file under `d` — `_index.json` plus every unit
    file, sorted by relative path, hashing path AND content so a rename
    (same bytes, different name) changes the hash too."""
    h = hashlib.sha256()
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(d).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _write_index(idx_path: Path, data: dict) -> None:
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx_path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _ensure_gitignore(snapshots_dir: Path) -> None:
    gi = snapshots_dir / ".gitignore"
    if not gi.is_file() or gi.read_text(encoding="utf-8") != _GITIGNORE_TEXT:
        gi.write_text(_GITIGNORE_TEXT, encoding="utf-8")


def _find_pin_owner(port_root: Path, name: str):
    """`(batch, source_cfg, source_entry)` for the batch whose own
    `frozen_snapshot == name`, or `(None, None, None)` when none does.
    More than one batch naming the same `frozen_snapshot` is ambiguous —
    refused rather than silently picking one. When a batch or
    `.port/port.yaml` itself declares several sources with no selector to
    say which one, `find_source` hands back an `Ambiguous` instead of a
    plain string; that gets re-raised AS an `Ambiguous` (never wrapped in
    `UsageError`) so `SNAPSHOT` can route it through the same
    JSON-carrying refusal every other `Ambiguous` lookup in this
    family uses, candidate list intact."""
    owners = []
    for bname in port_batch_reader.list_batches(port_root):
        batch = port_batch_reader.load_batch(port_root, bname)
        if batch.frozen_snapshot == name:
            owners.append(batch)
    if not owners:
        return None, None, None
    if len(owners) > 1:
        raise hs_family.UsageError(
            "port snapshot: %d batches (%s) declare %r as their own "
            "`frozen_snapshot` — ambiguous which pin to build at"
            % (len(owners), ", ".join(b.name for b in owners), name))
    batch = owners[0]
    source, problem = port_batch_reader.find_source(batch, None)
    if problem:
        if isinstance(problem, Ambiguous):
            raise problem
        raise hs_family.UsageError(problem)
    cfg, cfg_problems = port_config.load_and_validate(port_workspace.port_yaml_path(port_root))
    if cfg_problems:
        raise hs_family.UsageError(
            "`.port/port.yaml` is invalid:\n"
            + "\n".join("  · %s" % p for p in cfg_problems))
    source_cfg, source_problem = port_config.find_source(cfg, source.get("id"))
    if source_problem:
        if isinstance(source_problem, Ambiguous):
            raise source_problem
        raise hs_family.UsageError(source_problem)
    return batch, source_cfg, source


def _names_a_new_snapshot(port_root: Path, name: str) -> bool:
    """Whether some batch declares `name` as its own `new_snapshot`."""
    return any(port_batch_reader.load_batch(port_root, b).new_snapshot == name
               for b in port_batch_reader.list_batches(port_root))


def materialize_pin(source_repo: Path, pinned_sha: str, dest: Path, *,
                    shared: bool = True) -> Path:
    """`source_repo`'s tree at `pinned_sha`, materialized at `dest` — WITHOUT
    writing anything under `source_repo` itself (`port snapshot` builds from one
    in a scratch dir, `port source` keeps one under `.port/sources/`).
    See the module docstring's own WHICH COMMIT section for the probe this
    is built on (`git worktree add` registers under `<source_repo>/.git/
    worktrees/…`; this clones instead, sharing `source_repo`'s object store
    via `--shared` rather than copying it, so the only writes land under the
    returned scratch directory). The clone's own HEAD is left exactly at
    `pinned_sha` (an exact 40-hex `checkout --detach`, never a ref that
    could resolve to something else) — the caller relies on this: no
    post-build compare is needed, the checkout step already IS the
    guarantee. `shared=False` copies (hardlinks) the objects instead
    (`--local`): a checkout meant to outlive the scratch dir must not lose its
    pin when the source rewrites history and prunes."""
    try:
        run_git(dest.parent, "clone", "--shared" if shared else "--local", "--no-checkout",
               str(source_repo), str(dest))
        run_git(dest, "checkout", "--detach", pinned_sha)
    except GitError as exc:
        raise hs_family.UsageError(
            "could not check out %s at %s (%s)"
            % (source_repo, pinned_sha, exc)) from exc
    return dest


def _build(repo: Path, descriptor_path: Path, scratch: Path, *, target: bool = False,
           new_tree: bool = False):
    """`register_all()` (idempotent) THEN load THEN take_snapshot — this
    verb's one real caller of both the codec and the legacy format/payload
    provider. Returns the pipeline's own `SnapshotResult` and the format's
    registry name (`None` for the kernel's own); raises
    `DescriptorError`/`SnapshotError` unwrapped, same shape every sibling
    verb here already returns `Verdict.broken` from. `new_tree` narrows the
    pass to `_target_tree_roots`."""
    port_legacy_snapshot.register_all()
    loaded = load_descriptor(descriptor_path, repo)
    payload = port_legacy_snapshot.WorkspacePayloadProvider() if loaded.legacy else None
    fmt = loaded.descriptor.snapshot_format
    if target:
        if loaded.legacy:
            raise DescriptorError(
                "%s: a legacy descriptor describes a SOURCE's old layout — declare it as "
                "that source's `sources[].descriptor` in .port/port.yaml; the target's own "
                "codemap.yaml must be schema_version 2 (every port verb reads the target "
                "layout off it)" % descriptor_path)
    root_filter = _target_tree_roots(loaded.descriptor, descriptor_path) if new_tree else None
    if target:
        measured = [r.path for r in loaded.descriptor.roots
                    if root_filter is None or r.id in root_filter]
        _refuse_uncommitted_code(repo, measured)
    return take_snapshot(
        repo, scratch, loaded.descriptor, mode="port", force=True,
        root_filter=root_filter, fmt=fmt, payload=payload), fmt


def _target_tree_roots(descriptor, descriptor_path: Path) -> "list[str]":
    """Ids of the roots the dists live in — at or under `layout.packages_dir`.
    A batch's `new_snapshot` is what `check-ledger` holds every new node to (an
    old node's destination, or declared born-new); in a port done in place the
    old code still sits in the repo beside the dists, and measuring it would red
    every one of its nodes. Nothing to narrow to is a broken layout, never a
    silent fall back to every root."""
    pdir = (descriptor.packages_dir or "").strip("/")
    if not pdir:
        raise DescriptorError(
            "%s: a batch's new_snapshot measures the tree the dists live in, but "
            "layout.packages_dir is not declared" % descriptor_path)
    ids = [r.id for r in descriptor.roots
           if r.path.strip("/") == pdir or r.path.strip("/").startswith(pdir + "/")]
    if not ids:
        raise DescriptorError(
            "%s: layout.packages_dir %r is not at or above any declared root (%s) — "
            "the dists' tree is not measured by this layout"
            % (descriptor_path, pdir, ", ".join(r.path for r in descriptor.roots)))
    return ids


def _refuse_uncommitted_code(repo: Path, roots: "list[str]") -> None:
    """A target snapshot stamps `repo`'s HEAD, and that commit is the `new@<hash8>`
    anchor of every `to` written against it — but the pipeline measures the files
    on disk. Uncommitted code inside the measured roots would give an anchor naming
    a commit that never held those bytes, so it is refused before anything is
    measured. A pinned build never reaches this: it measures a clean checkout of
    the pin. Committing is `hs:cook`'s step (the machine never commits)."""
    paths = dirty_paths(repo, roots)
    if paths:
        shown = ", ".join(paths[:10]) + (" (+%d more)" % (len(paths) - 10) if len(paths) > 10 else "")
        raise hs_family.RefusedError(
            "port snapshot: %d uncommitted path(s) inside the measured roots: %s — the "
            "snapshot would stamp HEAD's commit over bytes HEAD does not hold; commit "
            "them first, then re-run" % (len(paths), shown))


def _snapshot(args: argparse.Namespace) -> int:
    """`hs-code port snapshot <name> [--rebuild] [--apply]` — see module
    docstring for the batch-pin / plain-mode split and the legacy-descriptor
    integration."""
    root, opts, name, rebuild = args.ctx.root, args.opts, args.name, args.rebuild

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    try:
        batch, source_cfg, source = _find_pin_owner(port_root, name)
    except Ambiguous as e:
        return e.refuse(opts)
    except hs_family.UsageError as e:
        return Verdict.broken(str(e)).exit

    apply = bool(getattr(opts, "apply", False))
    as_json = bool(getattr(opts, "json", False))

    with tempfile.TemporaryDirectory(prefix="hs-code-port-snapshot-") as scratch_s:
        scratch = Path(scratch_s)
        if batch is not None:
            repo_rel = source_cfg.get("repo") if isinstance(source_cfg, dict) else None
            if not repo_rel or not isinstance(repo_rel, str):
                return Verdict.broken(
                    "port snapshot: source %r has no `repo:` path in "
                    "`.port/port.yaml`" % source.get("id")).exit
            source_repo = (port_root / repo_rel).resolve()
            if not source_repo.is_dir():
                return Verdict.broken(
                    "port snapshot: source repo %s does not exist" % source_repo).exit
            pinned_sha = str(source.get("pinned_sha"))
            try:
                build_repo = materialize_pin(source_repo, pinned_sha, scratch / "pinned-src")
            except hs_family.UsageError as e:
                return Verdict.broken(str(e)).exit

            descriptor_rel = source_cfg.get("descriptor") if isinstance(source_cfg, dict) else None
            if descriptor_rel:
                descriptor_path = (port_root / descriptor_rel).resolve()
            else:
                descriptor_path = build_repo / "codemap.yaml"
        else:
            build_repo = root
            descriptor_path = build_repo / "codemap.yaml"

        try:
            res, fmt = _build(build_repo, descriptor_path, scratch / "measure",
                              target=batch is None,
                              new_tree=batch is None and _names_a_new_snapshot(port_root, name))
        except (DescriptorError, SnapshotError) as e:
            return Verdict.broken(str(e)).exit

        digest = _hash_snapshot_dir(res.out_dir)

        idx_path = port_batch_reader.snapshot_registry_path(port_root)
        try:
            idx = port_batch_reader.read_snapshot_registry(port_root)
        except hs_family.UsageError as e:
            return Verdict.broken(str(e)).exit
        existing = idx.get(name)

        data = {"name": name, "commit": res.commit, "sha256": digest,
                "batch": batch.name if batch is not None else None,
                "roots": list(res.roots_kept),
                "out_dir": str(port_batch_reader.snapshot_path(port_root, name))}

        if rebuild:
            if existing is None:
                return Verdict.broken(
                    "port snapshot: --rebuild given but %r is not in %s — "
                    "nothing to compare; build it first without --rebuild"
                    % (name, idx_path)).exit
            recorded_sha = existing.get("sha256") if isinstance(existing, dict) else None
            if recorded_sha != digest:
                return Verdict.broken(
                    "port snapshot: rebuild of %r did not reproduce the "
                    "recorded snapshot — recorded sha256 %s, rebuilt sha256 "
                    "%s (a rebuild that does not reproduce is broken state)"
                    % (name, recorded_sha, digest)).exit
        elif existing is not None:
            recorded_sha = existing.get("sha256") if isinstance(existing, dict) else None
            if recorded_sha != digest:
                raise hs_family.RefusedError(
                    "port snapshot: %r is already built (commit %s, sha256 %s) and this "
                    "build gives sha256 %s — never a silent overwrite; --rebuild "
                    "compares the two and names what changed"
                    % (name, existing.get("commit") if isinstance(existing, dict) else "?",
                       recorded_sha, digest))
            # Same bytes as the recorded measurement: the same snapshot, not an
            # overwrite — the step lane re-runs this exact command to turn green.

        entry = {"commit": res.commit, "sha256": digest}
        if fmt is not None:
            entry["format"] = fmt
        data["index_updated"] = apply and existing != entry

        if not apply:
            if as_json:
                print(json.dumps(data, sort_keys=True, ensure_ascii=False))
            else:
                print("snapshot %r  commit %s  sha256 %s  [dry-run: nothing written — "
                     "--apply writes]" % (name, res.commit, digest))
            return int(Verdict.ok(data).exit)

        target_dir = port_batch_reader.snapshot_path(port_root, name)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(res.out_dir, target_dir)
        idx[name] = entry
        _write_index(idx_path, idx)
        _ensure_gitignore(port_batch_reader.snapshots_dir(port_root))

        if as_json:
            print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        else:
            print("snapshot %r  commit %s  sha256 %s  -> %s"
                 % (name, res.commit, digest, target_dir))
        return int(Verdict.ok(data).exit)


SNAPSHOT = Command(add_args=_snapshot_args, run=_snapshot)
