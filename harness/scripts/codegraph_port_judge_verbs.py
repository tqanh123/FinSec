"""hs-code's `port` domain — the carry-forward + judge verbs. A FLAT module
under `harness/scripts/`, sibling of `codegraph_port_ledger_verbs.py` (same
reason: the `hs-code-registry.d` reachability channel matches a dot-free
`module:` name).

`CARRY_FORWARD` is ported (algorithm-faithful) from `ob-tools@60075cb:
packages/portkit/src/portkit/ledger/carry_forward.py` (`plan`/`build_events`/
`run`): a batch that shares its predecessor's ledger starts with ZERO rows
under its own freeze key, so most symbols that never changed would read as
`unported` all over again. For every symbol that has a row under the FROM
anchor and whose snapshot fingerprint (`sha`) is EQUAL in the FROM and TO
snapshots, this verb appends a new row under the TO anchor: same status,
same destinations/reason/blockers, plus `carried_from=<old node key>`.
`node:` blockers naming another carried node are re-keyed with it. Everything
else is classified by cause (`changed`/`gone`/`new-in-target`/`unmapped`) and
never carried.

WHAT UPSTREAM'S OWN CLI DID NOT NEED, AND WHAT REPLACES IT HERE:
  * `--from-snap` (a bare directory path, the PREVIOUS freeze) becomes
    `--from-snapshot NAME`: a snapshot name under `.port/snapshots/`,
    resolved the same way `INIT`/`MAP` resolve a batch's own
    snapshot (`port_batch_reader.snapshot_commit`) — it need not belong to
    any declared batch at all (a predecessor ledger may come with only its
    snapshot, no batch of its own). `--to-snap` (the batch's OWN freeze)
    becomes the batch's own `frozen_snapshot` — the tree carried rows land
    on is the batch's own freeze, not the batch's `new_snapshot`.
  * `extra["src"]` on a carried row is NOT copied verbatim from the FROM row
    (upstream's own `build_events` does exactly that — its ledger carries no
    `src` rule at all). The real OpsBrain ledger this reader is compatible
    with predates this harness's `src` rule entirely (`port_ledger_reader.
    py`'s own docstring: 25,608 nodes, none carrying `src`), so a FROM row
    copied byte-for-byte would refuse at the write door under `L0`
    (`port_ledger_writer.validate_src`) for a reason that has nothing to do
    with carry-forward's own job. `src` is instead OVERWRITTEN with the TO
    commit `port_batch_reader.pinned_frozen_commit` hands back — the batch's
    own frozen snapshot, checked against `source`'s pin before this verb
    trusts it as anything's `src.sha` (the ONE anchor/pin door every port
    verb shares, `test_port_anchor_pin_one_door.py`'s own AST gate). The
    FROM snapshot (`--from-snapshot`) carries no such check — it is not the
    batch's promise, only the ledger's own history.
  * `--out DIR` (upstream: one `<category>.txt` per non-carried cause) gets
    ONE addition upstream never needed: a path that resolves OUTSIDE the
    target repo (the resolved port root) is refused outright (`exit 3`,
    nothing written) rather than silently written wherever the caller
    pointed it.
  * the envelope's `data` is upstream's own `data` dict (`schema: "portkit.
    carry_forward/1"`, `from_key`/`to_key`/`counts`/`l1_after`/`dry_run`)
    as one document, the same `Verdict.data` upstream's own `run()` already
    returns — the text report below is derived from that SAME dict, not a
    second, independently-computed one.

EXIT CODES follow the family table (`codegraph_port_ledger_verbs.py`'s own
docstring), not upstream's (upstream answered every refusal with rc 2): an
unknown/misconfigured batch, a batch missing `frozen_snapshot`, an unknown
`--from-snapshot`, a broken/partial ledger, or a FROM anchor with no row at
all is exit 2 (already-broken state); a frozen snapshot whose own commit is
NOT the batch's pin, a `--from-key`/`--to-key` assertion that disagrees with
the resolved snapshot, two snapshots anchored at the same commit, or an
`--out` outside the repo is exit 3 (`hs_family.RefusedError` — a well-formed
request a batch/ledger rule declines); a run with nothing
left to carry is exit 0 EVEN when nothing was already carried either —
upstream answers that one case with rc 1 (`ledger/carry_forward.py`'s own
`run`), deliberately widened here to match this family's own "a write verb
with nothing left to write is done", same as `INIT`'s bulk-declare
"nothing to declare".

GIT: none. Every commit this verb needs comes from the batch's own pinned
source or a snapshot's own commit stamp — never a live git resolve, so this
module never imports `codegraph._core.git` at all (`--batch` is required,
unlike `MAP`'s own optional batch path).

`plan`, `_body` and `CATEGORIES` are module-level on purpose (not nested
inside `CARRY_FORWARD`): `judge-nodes`/`judge-removed`, defined further
down in this same module, import them directly — a carried node is exactly
the input those two verbs judge next.

`JUDGE_NODES`/`JUDGE_REMOVED` settle exactly the residue
`carry-forward` could not carry (its own `changed`/`new-in-target` and
`gone` categories) — ported (algorithm-faithful, cited per function) from
`ob-tools@60075cb:packages/portkit/src/portkit/ledger/{judge_common,
judge_nodes,judge_removed}.py`. Auto-write is deliberately narrow and never
widened past what the body evidence proves:
  * `judge-nodes` auto-`ported` ONLY when the upstream body at the TARGET
    commit is syntax-tree-equal (docstrings/imports stripped, `normal_body`)
    to a port-tree symbol's own body — a name-only match (no body compare)
    is never enough, and is exactly the mutation this file's own
    mutation-proof test plants.
  * `judge-removed` auto-`dropped` ONLY when the upstream MODULE itself is
    gone from the target snapshot, `git log` names the removing commit, AND
    no destination the FROM row already named is still present in the port
    — a module that is still there is never auto-dropped even when its
    symbol vanished (that is `vanished`, a packet).
  Everything else (`moved`/`orphan`/`vanished`/multi-candidate/no-candidate)
  is written to a packet (one markdown per upstream module for judge-nodes,
  one jsonl for judge-removed) for a human, never auto-written.

GIT (read-only, `show`/`log` only): `Tree.text`/`git_last_commit` go through
`codegraph._core.git.run_git`/`git_text` — a non-zero exit raises `GitError`,
caught and answered `None` ("the path/commit does not exist"), the case
`judge-nodes`/`judge-removed` must tell apart from "the path exists and is
byte-empty" (`git show` succeeds with empty stdout, `""`). This module's
own git write-scan AST gate (`test_port_verb_modules_never_write_git.py`)
still holds it to the same law every other port module obeys — it flags
`commit`/`push` among any call's string args; only `show`/`log` cross here.

`--verdicts FILE` (upstream: apply a reviewer's answered packet rows,
bypassing auto-judgement) IS ported — `load_verdicts` (jsonl of `{node,
status, to?, reason?}`, a malformed line named by `path:line`) reads the
rows, each becomes an `Event` (`src` stamped from the batch's own pin, the
same house addition every other written row here carries) and is written
through the SAME validated `append_event` door the auto path uses:
validate the WHOLE file first (`apply=False`), write all-or-nothing only
if every row clears (`apply=True`). `judge-nodes` skips a verdict row whose
`node` already has a ledger row (idempotent re-run, matching upstream);
`judge-removed` does not (upstream's own asymmetry — a verdict correcting
an ALREADY-`dropped` FROM row is exactly how `may_transition` earns its
own `L9` refusal there). A verdict file resolving outside the target repo
is refused (exit 3) before it is even opened — the same containment `--out`
already enforces; upstream never checks this. Exit table deviates from
upstream's blanket `die`: a row `append_event` refuses (unknown/malformed
node, illegal transition, any other law) is exit 3 (`RefusedError`, a
well-formed request a ledger law declines), never upstream's undifferentiated
"cannot run honestly"; zero new rows (an empty file, or every row already
stated) is exit 0 ("nothing to write" is done, not upstream's exit 1).
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import sys
import textwrap
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
_HOOKS = _SCRIPTS.parent / "hooks"
for _p in (_SCRIPTS, _HOOKS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from hook_runtime import resolve_actor  # noqa: E402

import hs_family  # noqa: E402
import port_batch_reader  # noqa: E402
import port_config  # noqa: E402
import port_ledger_reader  # noqa: E402
import port_ledger_spec  # noqa: E402
import port_open  # noqa: E402
import port_workspace  # noqa: E402
from codegraph._core.git import GitError, git_text, run_git  # noqa: E402
from codegraph._core.verdict import Verdict  # noqa: E402
from codegraph_port_ledger_verbs import _not_written, write_all  # noqa: E402
from hs_family.cli import Command  # noqa: E402
from port_ledger_reader import Event  # noqa: E402
from port_ledger_spec import STATUSES_DONE  # noqa: E402
from port_ledger_writer import BrokenLedgerError, Ledger, now_ts  # noqa: E402

#: The four causes a row under the FROM anchor (or a node only the TO
#: snapshot has) can be classified under when it is NOT carried — ported
#: verbatim (name-for-name) from `ob-tools@60075cb:packages/portkit/src/
#: portkit/ledger/carry_forward.py:44` (`CATEGORIES`). `judge-nodes`/
#: `judge-removed` import this constant directly (see the module docstring).
CATEGORIES = ("changed", "gone", "new-in-target", "unmapped")

#: How many lines of a category list a plain (non `--list`) run prints.
_SHOWN = 10


def _body(key: str) -> str:
    """`<module>::<name>` — a node key with its anchor stripped. Ported
    verbatim from `ob-tools@60075cb:...carry_forward.py:66-67` (`_body`).
    Module-level: `judge-nodes`/`judge-removed` import this directly."""
    return key.split("::", 1)[1]


def _rekey_blockers(blocked_by: "tuple[str, ...]", carried: "dict[str, str]") -> "tuple[str, ...]":
    """`node:<old key>` -> `node:<new key>` when that node is carried too.
    Ported (algorithm-faithful) from `ob-tools@60075cb:...carry_forward.
    py:70-77` (`_rekey_blockers`)."""
    out = []
    for b in blocked_by:
        kind, _, body = b.partition(":")
        out.append("node:%s" % carried[body.strip()]
                   if kind == "node" and body.strip() in carried else b)
    return tuple(out)


def plan(state: "dict[str, Event]", old: "dict[str, str]", new: "dict[str, str]",
         from_key: str, to_key: str) -> dict:
    """Pure classification: no I/O. Ported (algorithm-faithful) from
    `ob-tools@60075cb:...carry_forward.py:80-97` (`plan`), trimmed to the
    ONE field `old`/`new` carry here (`sha`, a plain string keyed by node —
    `_snapshot_node_shas` below builds it): upstream's own `old[body]["sha"]`
    / `new[body]["sha"]` become plain `old[body]` / `new[body]`.

    `state` is the ledger's own `replay()` map; `old`/`new` are node-key ->
    sha maps of the FROM/TO snapshot, both keyed WITH their own anchor
    prefix (`_snapshot_node_shas`'s own output shape). Module-level:
    `judge-nodes`/`judge-removed` import this directly (see the module
    docstring)."""
    rows = {_body(k): ev for k, ev in state.items() if k.startswith(from_key + "::")}
    old_b = {_body(k): v for k, v in old.items()}
    new_b = {_body(k): v for k, v in new.items()}
    out: "dict[str, Any]" = {"carry": [], "already": [], **{c: [] for c in CATEGORIES}}
    for body in sorted(rows):
        if body not in new_b:
            out["gone"].append("%s::%s" % (from_key, body))
        elif body not in old_b or old_b[body] != new_b[body]:
            # a row whose node the FROM snapshot never had cannot be judged
            # equal: changed-by-default.
            out["changed"].append("%s::%s" % (from_key, body))
        elif "%s::%s" % (to_key, body) in state:
            out["already"].append("%s::%s" % (to_key, body))
        else:
            out["carry"].append(body)
    out["new-in-target"] = ["%s::%s" % (to_key, b) for b in sorted(set(new_b) - set(old_b))]
    out["unmapped"] = ["%s::%s" % (to_key, b) for b in sorted(set(new_b) & set(old_b))
                       if b not in rows and old_b[b] == new_b[b]]
    out["rows"] = rows
    return out


def build_events(p: dict, from_key: str, to_key: str, actor: str, stamp: str,
                 src: dict) -> "list[Event]":
    """Ported (algorithm-faithful) from `ob-tools@60075cb:...carry_forward.
    py:100-110` (`build_events`), with ONE house addition: every carried
    row's `extra["src"]` is OVERWRITTEN with `src` rather than copied from
    the FROM row's own `extra` — see the module docstring for why."""
    carried = {"%s::%s" % (from_key, b): "%s::%s" % (to_key, b) for b in p["carry"]}
    evs = []
    for b in p["carry"]:
        prev = p["rows"][b]
        extra = dict(prev.extra)
        if extra.get("blocked_by"):
            extra["blocked_by"] = _rekey_blockers(tuple(extra["blocked_by"]), carried)
        extra["carried_from"] = "%s::%s" % (from_key, b)
        extra["src"] = dict(src)
        evs.append(Event(ts=stamp, node="%s::%s" % (to_key, b), status=prev.status,
                         extra=extra, by=prev.by, actor=actor,
                         note="carry-forward %s -> %s" % (from_key, to_key)))
    return evs


def _snapshot_nodes(idx: dict, anchor: str) -> "dict[str, dict]":
    """Every node key of one snapshot -> `{sha, path, lines, parent,
    module}`, at `anchor` — ported (algorithm-faithful) from
    `ob-tools@60075cb:packages/portkit/src/portkit/ledger/ledger_io.
    py:81-95` (`snapshot_nodes`), trimmed to the fields a caller on THIS
    tree's paths actually reads (`unit`/`dist`/`kind` have none: `plan()`
    only ever wants `sha`, `judge-nodes`/`judge-removed` additionally want
    `path`/`lines`/`parent`/`module` to read source and match twins).
    Mirrors `codegraph_port_ledger_verbs._snapshot_node_keys`'s own
    traversal."""
    out: "dict[str, dict]" = {}
    for mods in (idx.get("units") or {}).values():
        for m in mods:
            for s in m.get("symbols") or []:
                for holder, parent in [(s, None)] + [
                        (mm, s["name"]) for mm in s.get("methods") or []]:
                    key = "%s::%s::%s" % (anchor, m["module"], holder["name"])
                    out[key] = {"sha": holder.get("sha"), "path": m.get("path"),
                               "lines": holder.get("lines"), "parent": parent,
                               "module": m["module"]}
    return out


def _snapshot_node_shas(idx: dict, anchor: str) -> "dict[str, str]":
    """Every node key of one snapshot -> its own symbol `sha` — the ONE
    field `plan()`'s `old`/`new` fingerprint compare reads. Derived from
    `_snapshot_nodes` rather than a second traversal."""
    return {k: v["sha"] for k, v in _snapshot_nodes(idx, anchor).items()}


def _carry_forward_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--from-snapshot", required=True, dest="from_snapshot")
    ap.add_argument("--source", default=None, dest="source_id")
    ap.add_argument("--from-key", default=None)
    ap.add_argument("--to-key", default=None)
    ap.add_argument("--out", default=None, dest="out_dir")
    ap.add_argument("--list", action="store_true", dest="list_all")


def _carry_forward(args: argparse.Namespace) -> int:
    """`hs-code port carry-forward --batch <name> --from-snapshot <name>
    [--source <id>] [--from-key <hash8>] [--to-key <hash8>] [--out DIR]
    [--list]` — carry every fingerprint-equal node from `--from-snapshot`
    (a PREVIOUS freeze, any snapshot under `.port/snapshots/`) onto the
    batch's own `frozen_snapshot` (the TO tree — the batch's OWN freeze).
    Dry-run by default (`--apply` writes for real); ALL planned rows are
    validated before the first one is written (mirrors `MAP`'s bulk
    path)."""
    root, opts = args.ctx.root, args.opts
    (batch_name, from_snapshot_name, source_id, from_key_assert, to_key_assert,
     out_dir, list_all) = (args.batch_name, args.from_snapshot, args.source_id,
                           args.from_key, args.to_key, args.out_dir, args.list_all)

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    out_abs = None
    if out_dir is not None:
        raw = Path(out_dir)
        out_abs = (raw if raw.is_absolute() else port_root / raw).resolve()
        if not out_abs.is_relative_to(port_root):
            raise hs_family.RefusedError(
                "port carry-forward: --out %s resolves to %s, outside the "
                "target repo %s — refusing to write category lists there"
                % (out_dir, out_abs, port_root))

    try:
        batch = port_batch_reader.load_batch(port_root, batch_name)
    except (port_batch_reader.BatchNotFoundError,
            port_batch_reader.BatchConfigError) as e:
        return Verdict.unresolved(e).exit

    frozen_snapshot = batch.frozen_snapshot
    if not frozen_snapshot:
        return Verdict.broken(
            "batch %r has no `frozen_snapshot` declared — carry-forward "
            "needs the batch's own freeze as the TO tree" % batch.name).exit

    source, source_problem = port_batch_reader.find_source(batch, source_id)
    if source_problem:
        return Verdict.unresolved(source_problem).exit

    # FROM is a PREVIOUS freeze, never checked against the batch's own pin —
    # it may not belong to any batch at all (module docstring).
    from_commit, from_idx = port_batch_reader.read_snapshot(
        port_root, from_snapshot_name, caller_named=True)
    # TO must BE the batch's own pin: refuse a frozen snapshot that is not
    # the batch's own pin BEFORE any listing/dry-run output — a mismatch
    # writes nothing, ever (mirrors INIT's own order).
    to_commit, to_idx = port_batch_reader.pinned_frozen_snapshot(port_root, batch, source)
    from_key, to_key = from_commit[:8], to_commit[:8]

    for flag, want, got, snap in (("--from-key", from_key_assert, from_key, from_snapshot_name),
                                  ("--to-key", to_key_assert, to_key, frozen_snapshot)):
        if want and want[:8] != got:
            raise hs_family.RefusedError(
                "port carry-forward: %s %r is not the anchor of snapshot %r (%s)"
                % (flag, want, snap, got))
    if from_key == to_key:
        raise hs_family.RefusedError(
            "port carry-forward: both snapshots anchor at %s — nothing to "
            "carry between one tree and itself" % from_key)

    old_shas = _snapshot_node_shas(from_idx, from_key)
    new_shas = _snapshot_node_shas(to_idx, to_key)

    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        return Verdict.broken(_not_written(e.findings)).exit
    state = ledger.replay()

    have = Counter(k.split("::", 1)[0] for k in state)
    if not have.get(from_key):
        return Verdict.broken(
            "port carry-forward: the ledger holds NO row keyed %s (anchors "
            "present: %s) — wrong batch, or the frozen snapshot's own batch "
            "never wrote its rows"
            % (from_key, ", ".join("%s (%d)" % (a, n) for a, n in sorted(have.items())) or "none")
        ).exit

    p = plan(state, old_shas, new_shas, from_key, to_key)
    src = {"id": source.get("id"), "sha": to_commit}
    evs = build_events(p, from_key, to_key, resolve_actor(), now_ts(), src)

    counts: "dict[str, int]" = {"carried": len(evs), "already-carried": len(p["already"]),
                                 **{c: len(p[c]) for c in CATEGORIES}}
    keyed = {_body(k) for k in state if k.startswith(to_key + "::")} | {_body(e.node) for e in evs}
    l1_after = len({_body(k) for k in new_shas} - keyed)
    apply = bool(getattr(opts, "apply", False))
    data: "dict[str, Any]" = {"schema": "portkit.carry_forward/1", "from_key": from_key,
                              "to_key": to_key, "counts": counts, "l1_after": l1_after,
                              "dry_run": not apply}
    want_json = bool(getattr(opts, "json", False))

    if not want_json:
        print("ledger  : %s" % ledger.path)
        print("from    : %s  key %s  (%d node(s), %d row(s))"
              % (from_snapshot_name, from_key, len(old_shas), len(p["rows"])))
        print("to      : %s  key %s  (%d node(s))" % (frozen_snapshot, to_key, len(new_shas)))
        print()
        print("carry (fingerprint equal)      : %d" % data["counts"]["carried"])
        print("already carried / own row      : %d" % data["counts"]["already-carried"])
        for c in CATEGORIES:
            print("%-31s: %d" % (c, data["counts"][c]))
        print("L1 expected after carry        : %d   (target nodes still without a %s row)"
              % (data["l1_after"], to_key))
        limit = None if list_all else _SHOWN
        for c in CATEGORIES:
            shown = p[c] if limit is None else p[c][:limit]
            for n in shown:
                print("  [%s] %s" % (c, n))
            if limit is not None and len(p[c]) > limit:
                print("  [%s] ... %d more (--list / --out)" % (c, len(p[c]) - limit))

    if out_abs is not None:
        out_abs.mkdir(parents=True, exist_ok=True)
        for c in CATEGORIES:
            (out_abs / ("%s.txt" % c)).write_text(
                "".join(n + "\n" for n in p[c]), encoding="utf-8")
        if not want_json:
            print("lists   : %s" % out_abs)

    if not evs:
        text = "\nnothing to carry" + (
            " — everything carriable is already carried" if p["already"] else "")
    elif not apply:
        write_all(ledger, evs, "carry-forward", apply=False)
        text = "\ndry-run: %d row(s) valid, nothing written (--apply appends)" % len(evs)
    else:
        write_all(ledger, evs, "carry-forward", apply=True)
        text = "\nwritten: %d line(s) -> %s" % (len(evs), ledger.path)

    if want_json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print(text)
    return int(Verdict.ok(data).exit)


CARRY_FORWARD = Command(add_args=_carry_forward_args, run=_carry_forward)


# ── judge-nodes / judge-removed: shared core (`judge_common.py`) ───────────


class Tree:
    """A source tree at one commit (or a plain directory when `commit` is
    `None`) — ported (algorithm-faithful) from `ob-tools@60075cb:packages/
    portkit/src/portkit/ledger/judge_common.py:26-49` (`Tree`). Reads
    through `git show <commit>:<path>` via `codegraph._core.git.run_git` —
    a non-zero exit (`GitError`) is caught and answered `None` (the path
    does not exist at this commit), the case `node_text` below must tell
    apart from the path existing but being byte-empty (`""`). Every read is
    cached by path for the tree's own lifetime: a packet and its own
    auto-judgement both read the SAME handful of paths."""

    def __init__(self, repo: Path, commit: "str | None"):
        self.repo, self.commit = Path(repo), commit
        self._files: "dict[str, str | None]" = {}

    def text(self, path: str) -> "str | None":
        if path in self._files:
            return self._files[path]
        out: "str | None"
        if self.commit:
            try:
                out = run_git(self.repo, "show", "%s:%s" % (self.commit, path)).decode(
                    "utf-8", "replace")
            except GitError:
                out = None
        else:
            f = self.repo / path
            out = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else None
        self._files[path] = out
        return out

    def node_text(self, path: str, lines: "list[int] | None") -> "str | None":
        t = self.text(path)
        if t is None or not lines:
            return None
        rows = t.splitlines()
        return "\n".join(rows[lines[0] - 1:lines[1]])


def _strip_docstring(body: list) -> list:
    """Ported verbatim from `ob-tools@60075cb:...judge_common.py:56-60`.
    `body[0].value` is checked by DIRECT attribute access (never `getattr`)
    so mypy narrows the SAME expression across both `isinstance` calls —
    matching `codegraph/_core/rules/kinds/dup_symbol.py`'s own
    `_normalized_dump`, the one other docstring-strip in this tree."""
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        return body[1:] or [ast.Pass()]
    return body


def normal_body(src: str) -> str:
    """Canonical form of one node's source: the AST dump without docstrings
    and import statements — ported verbatim from `ob-tools@60075cb:...
    judge_common.py:62-73` (`normal_body`). Identifiers are NOT normalized
    (unlike `codegraph/_core/rules/kinds/dup_symbol.py`'s own
    `_normalized_dump`, a DIFFERENT, looser equivalence built for a
    different question): a rename still reads as `differs`, which is the
    side `judge-nodes` needs — see the module docstring's AUTO-write
    invariant."""
    try:
        tree = ast.parse(textwrap.dedent(src))
    except SyntaxError:
        return "text:" + " ".join(x.strip() for x in src.splitlines()
                                  if x.strip() and not x.strip().startswith("#"))
    for n in ast.walk(tree):
        body = getattr(n, "body", None)
        if isinstance(body, list):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
                body = _strip_docstring(body)
            # `setattr`, not `n.body = ...`: `n` stays plain `ast.AST` here on
            # purpose (upstream strips imports from EVERY node carrying a
            # list `body` — `if`/`while`/`try` included, not only
            # function/class/module) and `ast.AST` itself has no `.body`.
            setattr(n, "body",
                   [b for b in body if not isinstance(b, (ast.Import, ast.ImportFrom))] or [ast.Pass()])
    return ast.dump(tree)


def split_body(key_body: str) -> "tuple[str, str]":
    """`module::name` -> `(module, name)`. Ported verbatim from
    `ob-tools@60075cb:...judge_common.py:76-79`."""
    mod, _, name = key_body.rpartition("::")
    return mod, name


def twin_index(port_nodes: "dict[str, dict]") -> "dict[tuple, list]":
    """`(name, parent class or None)` -> every node key of `port_nodes`
    carrying it. Ported verbatim from `ob-tools@60075cb:...judge_common.
    py:82-86`."""
    idx: "dict[tuple, list]" = defaultdict(list)
    for k, v in port_nodes.items():
        idx[(k.rsplit("::", 1)[1], v.get("parent"))].append(k)
    return idx


def load_hunks(path: "Path | None") -> "dict[str, list[tuple]]":
    """`hunk-dispositions.jsonl` -> upstream file path -> `[(line, hunk id,
    dest file), ...]` for `applied` rows — ported (algorithm-faithful) from
    `ob-tools@60075cb:...judge_common.py:89-104` (`load_hunks`). This is a
    plain jsonl sidecar the ported drift tool writes, NOT the port ledger
    itself: read directly, never through the checked read door
    (`Ledger.load_checked`) every ledger-deciding verb in this file goes
    through — this sidecar is not the port ledger, so that door does not
    apply to it."""
    out: "dict[str, list[tuple]]" = defaultdict(list)
    if path is None or not Path(path).is_file():
        return out
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        r = json.loads(raw)
        if r.get("status") != "applied":
            continue
        try:
            fpath, line, _ = r["hunk"].rsplit(":", 2)
            out[fpath].append((int(line), r["hunk"], r.get("dest", "")))
        except (KeyError, ValueError):
            continue
    return out


def hunks_in(hunks: dict, path: str, lines: "list[int] | None") -> "list[tuple]":
    """Ported verbatim from `ob-tools@60075cb:...judge_common.py:107-108`."""
    return [h for h in hunks.get(path, []) if lines and lines[0] <= h[0] <= lines[1]]


def unified(a: str, b: str, a_name: str, b_name: str) -> str:
    """Ported verbatim from `ob-tools@60075cb:...judge_common.py:111-113`."""
    return "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), a_name, b_name, n=2))


def git_last_commit(repo: Path, rev_range: str, path: str) -> "str | None":
    """Newest commit in `rev_range` touching `path` (evidence for a
    removal), or `None` when git fails (a bad range) OR succeeds with no
    match (empty stdout) — ported (algorithm-faithful) from
    `ob-tools@60075cb:...judge_common.py:116-119` (`git_last_commit`),
    through `git_text` (same door as `Tree.text` above)."""
    try:
        return git_text(repo, "log", "--format=%h %s", "-n", "1", rev_range, "--", path) or None
    except GitError:
        return None


def load_verdicts(path: Path) -> "list[dict]":
    """`--verdicts FILE`: jsonl of `{node, status, to?, reason?}` — a
    reviewer's answer to a packet, `node` the target/FROM-key node the row
    is written under. Ported (algorithm-faithful) from `ob-tools@60075cb:
    ...judge_common.py:132-140` (`load_verdicts`), widened to name
    `path:line` on a JSON-parse failure too, not only a missing required
    key — upstream leaves that case to raise `json.JSONDecodeError`
    un-annotated; both classes of malformed line are "this line cannot be
    read", so both get the same `path:line` naming."""
    rows: "list[dict]" = []
    for i, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            r = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError("%s:%d: not valid JSON (%s)" % (path, i, e)) from e
        if not isinstance(r, dict) or "node" not in r or "status" not in r:
            raise ValueError("%s:%d: verdict needs `node` and `status`" % (path, i))
        rows.append(r)
    return rows


# ── judge-nodes ──────────────────────────────────────────────────────────

#: Ported verbatim from `ob-tools@60075cb:packages/portkit/src/portkit/
#: ledger/judge_nodes.py` (`AUTO`).
_JUDGE_NODES_AUTO = "auto-body-equal"

def _judge_nodes(state: "dict[str, Event]", old: dict, new: dict, port: dict, from_key: str, to_key: str,
                 up_from: Tree, up_to: Tree, port_tree: Tree, hunks: dict) -> dict:
    """Pure-ish judgement (reads sources through the `Tree`s). Returns
    `{"auto": [...], "packet": [...], "skipped": n}` — ported
    (algorithm-faithful) from `ob-tools@60075cb:...judge_nodes.py:57-121`
    (`judge`). `old`/`new`/`port` are `_snapshot_nodes`'s own FULL shape
    (`sha`/`path`/`lines`/`parent`/`module`), not the flat sha maps `plan()`
    itself takes — this function derives those before calling `plan()`,
    the same way `CARRY_FORWARD` already does."""
    old_shas = {k: v["sha"] for k, v in old.items()}
    new_shas = {k: v["sha"] for k, v in new.items()}
    p = plan(state, old_shas, new_shas, from_key, to_key)
    port_by_body = {_body(k): (k, v) for k, v in port.items()}
    twins = twin_index({_body(k): v for k, v in port.items()})
    port_src: "dict[str, str | None]" = {}

    def psrc(body: str) -> "str | None":
        if body not in port_src:
            k, v = port_by_body[body]
            port_src[body] = port_tree.node_text(v["path"], v["lines"])
        return port_src[body]

    def usrc(tree: Tree, nodes: dict, key: str) -> "str | None":
        v = nodes.get(key)
        return tree.node_text(v["path"], v["lines"]) if v else None

    new_b = {_body(k): (k, v) for k, v in new.items()}
    hint: "dict[str, set]" = defaultdict(set)
    for b, ev in p["rows"].items():
        for t in ev.extra.get("to") or ():
            hint[b.split("::")[0]].add(_body(t).split("::")[0])

    out: "dict[str, Any]" = {"auto": [], "packet": [], "skipped": 0}

    def packet(node, cause, why, **kw):
        out["packet"].append({"node": node, "cause": cause, "why": why, **kw})

    for node in p["changed"]:
        b = _body(node)
        ev = p["rows"][b]
        to_node = "%s::%s" % (to_key, b)
        if to_node in state:
            out["skipped"] += 1
            continue
        dests = [_body(t) for t in (ev.extra.get("to") or ())]
        if ev.status not in STATUSES_DONE or ev.status == "dropped":
            packet(to_node, "changed", "FROM row is %s, not a done port" % ev.status, from_node=node)
            continue
        live = [d for d in dests if d in port_by_body]
        if len(dests) != 1 or len(live) != 1:
            packet(to_node, "changed", "%d destination(s), %d still in the port" % (len(dests), len(live)),
                  from_node=node, dests=dests)
            continue
        new_src = usrc(up_to, new, to_node) if to_node in new else None
        if new_src is None:
            packet(to_node, "changed", "target source unreadable", from_node=node, dests=dests)
            continue
        if normal_body(new_src) == normal_body(psrc(live[0]) or ""):
            out["auto"].append({"node": to_node, "status": ev.status, "to": [port_by_body[live[0]][0]],
                               "carried_from": node, "cause": "changed", "judged": _JUDGE_NODES_AUTO,
                               "reason": ev.extra.get("reason", "") if ev.status == "merged" else "",
                               "note": "upstream drift already present in the port destination"})
            continue
        v = new_b[b][1]
        packet(to_node, "changed", "port destination differs from the new upstream body", from_node=node,
              dests=[port_by_body[live[0]][0]], hunks=[h[1] for h in hunks_in(hunks, v["path"], v["lines"])],
              old_src=usrc(up_from, old, node), new_src=new_src, port_src=psrc(live[0]))

    for node in p["new-in-target"]:
        if node in state:
            out["skipped"] += 1
            continue
        b = _body(node)
        v = new[node]
        mod, name = split_body(b)
        src = up_to.node_text(v["path"], v["lines"])
        cands = twins.get((name, v.get("parent")), [])
        if src is None:
            packet(node, "new-in-target", "target source unreadable")
            continue
        if not cands:
            packet(node, "new-in-target", "no port symbol with this name", new_src=src,
                  hunks=[h[1] for h in hunks_in(hunks, v["path"], v["lines"])])
            continue
        want = normal_body(src)
        eq = [c for c in cands if normal_body(psrc(c) or "") == want]
        if len(eq) > 1:
            near = [c for c in eq if c.split("::")[0] in hint.get(mod, set())]
            eq = near if len(near) == 1 else eq
        if len(eq) == 1:
            out["auto"].append({"node": node, "status": "ported", "to": [port_by_body[eq[0]][0]],
                               "cause": "new-in-target", "judged": _JUDGE_NODES_AUTO,
                               "hinted": eq[0].split("::")[0] in hint.get(mod, set()),
                               "note": "new upstream symbol already present with an equal body"})
        else:
            packet(node, "new-in-target",
                  "%d equal-body candidates" % len(eq) if eq else "same name, body differs (adapted?)",
                  candidates=[port_by_body[c][0] for c in (eq or cands)][:8], new_src=src,
                  equal_body=len(eq),
                  port_src=psrc((eq or cands)[0]) if len(eq or cands) == 1 else None,
                  hunks=[h[1] for h in hunks_in(hunks, v["path"], v["lines"])])
    return out


def write_packets(out_dir: Path, packets: "list[dict]", anchor: "dict | None" = None) -> int:
    """One markdown per upstream module (the reviewer's worklist) plus one
    machine jsonl — ported (algorithm-faithful) from `ob-tools@60075cb:...
    judge_nodes.py:124-152` (`write_packets`), minus the `nodes` param
    upstream's own function body never reads (no-orphan, function level).
    `anchor` is the evidence stamp (port-tree sha + date) printed into
    every packet header, so a verdict written against a packet of another
    tree is visible as such."""
    pk_dir = out_dir / "packets"
    pk_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "nodes-packets.jsonl").write_text(
        "".join(json.dumps({k: v for k, v in x.items() if not k.endswith("_src")}, ensure_ascii=False) + "\n"
                for x in packets), encoding="utf-8")
    by_mod: "dict[str, list[dict]]" = defaultdict(list)
    for x in packets:
        by_mod[_body(x["node"]).rsplit("::", 1)[0]].append(x)
    for mod, items in by_mod.items():
        parts = ["# %s\n\n%d node(s) need a verdict. Answer as jsonl rows "
                "`{\"node\": <target key>, \"status\": ..., \"to\": [...], \"reason\": ...}`.\n"
                % (mod, len(items))]
        if anchor:
            parts.append("\n> evidence anchor: port %s @ %s — a verdict against another tree is stale\n"
                         % (str(anchor.get("source_sha", "?"))[:12], anchor.get("date", "?")))
        for x in items:
            parts.append("\n## `%s`\n\n- cause: %s — %s\n" % (x["node"], x["cause"], x["why"]))
            for f in ("from_node", "dests", "candidates", "hunks"):
                if x.get(f):
                    parts.append("- %s: %s\n" % (f, x[f]))
            if x.get("old_src") and x.get("new_src"):
                parts.append("\n```diff\n" + unified(x["old_src"], x["new_src"], "upstream@from", "upstream@to")
                             + "```\n")
            elif x.get("new_src"):
                parts.append("\nupstream@to:\n```python\n" + x["new_src"] + "\n```\n")
            if x.get("port_src"):
                parts.append("\nport:\n```python\n" + x["port_src"] + "\n```\n")
        (pk_dir / (mod.replace("/", "_") + ".md")).write_text("".join(parts), encoding="utf-8")
    return len(by_mod)


# ── judge-removed ────────────────────────────────────────────────────────

def _judge_removed(state: "dict[str, Event]", old: dict, new: dict, port: dict, from_key: str, to_key: str,
                   up_repo: Path, from_commit: str, to_commit: str) -> "dict[str, list]":
    """Ported (algorithm-faithful) from `ob-tools@60075cb:...judge_removed.
    py:31-63` (`judge`). Unlike `_judge_nodes`, never reads source TEXT —
    only `git log` evidence for the removing commit, so it needs no
    `Tree`, only the upstream repo's own path."""
    old_shas = {k: v["sha"] for k, v in old.items()}
    new_shas = {k: v["sha"] for k, v in new.items()}
    p = plan(state, old_shas, new_shas, from_key, to_key)
    new_mods = {v["module"] for v in new.values()}
    new_twins = twin_index({_body(k): v for k, v in new.items()})
    port_twins = twin_index({_body(k): v for k, v in port.items()})
    port_bodies = {_body(k) for k in port}
    old_b = {_body(k): v for k, v in old.items()}
    out: "dict[str, list]" = {"settled": [], "moved": [], "drop": [], "orphan": [], "vanished": []}
    rev = "%s..%s" % (from_commit, to_commit)
    for node in p["gone"]:
        b = _body(node)
        ev = p["rows"][b]
        mod, name = split_body(b)
        v = old_b.get(b)
        if ev.status == "dropped":
            out["settled"].append({"node": node})
            continue
        parent = v.get("parent") if v else None
        same_up = new_twins.get((name, parent), [])
        if same_up:
            out["moved"].append({"node": node, "now_at": ["%s::%s" % (to_key, k) for k in same_up][:5]})
            continue
        dests = [_body(t) for t in (ev.extra.get("to") or ())]
        live = [d for d in dests if d in port_bodies]
        same_port = port_twins.get((name, parent), [])[:5]
        if mod not in new_mods and not live and not same_port and ev.status in ("ported", "split", "merged"):
            evidence = git_last_commit(up_repo, rev, v["path"]) if v else None
            out["drop"].append({"node": node, "commit": evidence or ("module %s absent at %s" % (mod, to_key)),
                               "path": v["path"] if v else ""})
        elif live:
            out["orphan"].append({"node": node, "live_dests": live, "status": ev.status,
                                  "module_gone": mod not in new_mods})
        else:
            out["vanished"].append({"node": node, "status": ev.status, "module_gone": mod not in new_mods,
                                    "same_name_in_port": same_port, "dests": dests})
    return out


# ── shared CLI plumbing for the two judge verbs ─────────────────────────────

def _resolve_out_dir(out_dir: "str | None", port_root: Path, verb: str) -> "Path | None":
    """`--out DIR` -> its resolved, repo-contained absolute path, or `None`
    when the flag was not given. Same containment check `carry-forward`
    already makes inline (`.resolve()` first, so a symlink pointing
    outside the repo is caught by where it points, not its own in-repo
    path) — factored here since BOTH judge verbs need it verbatim."""
    if out_dir is None:
        return None
    raw = Path(out_dir)
    out_abs = (raw if raw.is_absolute() else port_root / raw).resolve()
    if not out_abs.is_relative_to(port_root):
        raise hs_family.RefusedError(
            "port %s: --out %s resolves to %s, outside the target repo %s "
            "— refusing to write packets there" % (verb, out_dir, out_abs, port_root))
    return out_abs


def _resolve_verdicts_path(verdicts: "str | None", port_root: Path, verb: str) -> "Path | None":
    """`--verdicts FILE` -> its resolved, repo-contained absolute path, or
    `None` when the flag was not given. Same containment check
    `_resolve_out_dir` makes for `--out` — a verdict file is READ, not
    written, but a path outside the target repo is refused for the same
    reason: the repo boundary is the trust boundary every port verb reads
    and writes inside, not only the ones that write."""
    if verdicts is None:
        return None
    raw = Path(verdicts)
    verdicts_abs = (raw if raw.is_absolute() else port_root / raw).resolve()
    if not verdicts_abs.is_relative_to(port_root):
        raise hs_family.RefusedError(
            "port %s: --verdicts %s resolves to %s, outside the target repo "
            "%s — refusing to read it there" % (verb, verdicts, verdicts_abs, port_root))
    return verdicts_abs


def _resolve_judge_batch(port_root: Path, batch_name: str, source_id: "str | None", *, verb: str):
    """`(batch, cfg_source, batch_source, error)` — every check BOTH judge
    verbs share before their own FROM/TO resolution: `.port/port.yaml`
    exists and validates (`cfg_source.repo` names the upstream git
    checkout — `port_batch_reader`'s own `source` never carries `repo`,
    only `pinned_sha`), the named batch loads and declares BOTH
    `frozen_snapshot` (TO) and `new_snapshot` (the port tree's own freeze,
    needed to read/probe the destination), and `source_id` resolves in
    BOTH configs. `error` is `None` on success; every failure mode here is
    a broken/missing-declaration state (exit 2), so the caller always does
    `if error: return Verdict.broken(error).exit` — no exception raised,
    matching `CARRY_FORWARD`'s own inline style for the same class of
    check."""
    config_path = port_workspace.port_yaml_path(port_root)
    if not config_path.is_file():
        return None, None, None, (
            "no `.port/port.yaml` under %s — run /hs:port to plan a port" % port_root)
    cfg, problems = port_config.load_and_validate(config_path)
    if problems:
        return None, None, None, (
            "`.port/port.yaml` is invalid:\n" + "\n".join("  · %s" % p for p in problems))
    cfg_source, cfg_problem = port_config.find_source(cfg, source_id)
    if cfg_problem:
        return None, None, None, cfg_problem

    try:
        batch = port_batch_reader.load_batch(port_root, batch_name)
    except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
        return None, None, None, e
    if not batch.frozen_snapshot:
        return None, None, None, (
            "batch %r has no `frozen_snapshot` declared — %s needs the "
            "batch's own freeze as the TO tree" % (batch.name, verb))
    if not batch.new_snapshot:
        return None, None, None, (
            "batch %r has no `new_snapshot` declared — %s needs the port "
            "tree's own freeze" % (batch.name, verb))
    batch_source, batch_problem = port_batch_reader.find_source(batch, source_id)
    if batch_problem:
        return None, None, None, batch_problem
    return batch, cfg_source, batch_source, None


def _resolve_judge_from_to(port_root: Path, batch: "port_batch_reader.Batch", source: dict,
                          from_snapshot_name: str, *, verb: str) -> "tuple[str, str, str, str, dict, dict]":
    """`(from_commit, to_commit, from_key, to_key, from_idx, to_idx)` — FROM
    is `--from-snapshot` (a PREVIOUS freeze, any snapshot under `.port/
    snapshots/`, no batch of its own required); TO is `batch`'s own
    `frozen_snapshot`, checked against `source`'s pin (`pinned_frozen_
    snapshot`, exit 3 on mismatch — the same pin door `carry-forward`
    already goes through). Hands back both parsed indexes too, read through
    the ONE door alongside each commit — `_judge_trees` never re-opens
    either file."""
    from_commit, from_idx = port_batch_reader.read_snapshot(
        port_root, from_snapshot_name, caller_named=True)
    to_commit, to_idx = port_batch_reader.pinned_frozen_snapshot(port_root, batch, source)
    from_key, to_key = from_commit[:8], to_commit[:8]
    if from_key == to_key:
        raise hs_family.RefusedError(
            "port %s: both snapshots anchor at %s — nothing to do between "
            "one tree and itself" % (verb, from_key))
    return from_commit, to_commit, from_key, to_key, from_idx, to_idx


def _judge_trees(port_root: Path, batch: "port_batch_reader.Batch",
                 from_idx: dict, to_idx: dict, from_key: str, to_key: str):
    """`(old, new, port, port_commit, pk)` — the port tree's own commit +
    `new@<hash8>` anchor (`batch.new_snapshot`), and every snapshot's nodes
    at their own anchor, via `_snapshot_nodes`. `from_idx`/`to_idx` are
    already read by `_resolve_judge_from_to` — passed in rather than
    re-opened. `batch.new_snapshot` is a DECLARED field, never caller-typed,
    so an unreadable one raises `hs_family.UsageError` (exit 2) straight
    out of `read_snapshot`, the same rung this always mapped to."""
    # Narrowed to `str` for mypy: `_resolve_judge_batch` already refused
    # (exit 2) a batch missing either field before any caller reaches here.
    new_snapshot = batch.new_snapshot
    if not batch.frozen_snapshot or not new_snapshot:
        raise AssertionError(
            "_judge_trees: batch %r reached here without frozen_snapshot/"
            "new_snapshot — _resolve_judge_batch must run first" % batch.name)
    port_commit, port_idx = port_batch_reader.read_snapshot(port_root, new_snapshot)
    pk = port_ledger_spec.new_anchor(port_commit)
    old = _snapshot_nodes(from_idx, from_key)
    new = _snapshot_nodes(to_idx, to_key)
    port = _snapshot_nodes(port_idx, pk)
    return old, new, port, port_commit, pk


def _judge_anchor(port_commit: str) -> dict:
    """`{source_sha, date}` printed into every packet header: WHICH port-tree
    commit these packets judge against, read by whoever judges them.
    `stamp_fields(None)` never touches git -- `source_sha` is the PORT commit
    this call already resolved, not a live `HEAD`."""
    from codegraph.test.evidence import stamp_fields
    return {"source_sha": port_commit,
            **{k: v for k, v in stamp_fields(None).items() if k != "source_sha"}}


def _from_key_missing_msg(state: "dict[str, Event]", from_key: str, verb: str) -> "str | None":
    """`None` when the ledger holds at least one row under `from_key`, else
    the SAME refusal message `carry-forward` already prints for this exact
    check — factored here since both judge verbs need it too (they read
    `plan()`'s own `changed`/`new-in-target`/`gone` groups off the same
    state)."""
    have = Counter(k.split("::", 1)[0] for k in state)
    if have.get(from_key):
        return None
    return ("port %s: the ledger holds NO row keyed %s (anchors present: %s) — "
            "wrong batch, or the frozen snapshot's own batch never wrote its rows"
            % (verb, from_key, ", ".join("%s (%d)" % (a, n) for a, n in sorted(have.items())) or "none"))


def _judge_nodes_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--from-snapshot", required=True, dest="from_snapshot")
    ap.add_argument("--source", default=None, dest="source_id")
    ap.add_argument("--hunks", default=None, dest="hunks_path")
    ap.add_argument("--out", default=None, dest="out_dir")
    ap.add_argument("--verdicts", default=None, dest="verdicts_path")
    ap.add_argument("--by", default="human")


def _judge_nodes_verb(args: argparse.Namespace) -> int:
    """`hs-code port judge-nodes --batch <name> --from-snapshot <name>
    [--source <id>] [--hunks FILE] [--out DIR] [--verdicts FILE]
    [--by human|suggest:<sha>] [--apply]` — settle every
    TARGET-key node `carry-forward` left behind (`changed`/`new-in-target`):
    auto-`ported` ONLY when the upstream body at the TARGET commit equals
    (syntax-tree, docstrings/imports stripped) a port-tree symbol's own
    body; everything else is a packet for a human. `--verdicts FILE`
    applies a reviewer's answered packet rows instead of judging — see
    `load_verdicts`. Dry-run by default (`--apply` appends the WHOLE
    validated plan, all-or-nothing, same as `carry-forward`).

    The only verb in this family that reads source TEXT, not only
    snapshot fingerprints — needs `.port/port.yaml`'s `sources[].repo`
    (the upstream git checkout) in addition to the batch's own pin, and
    the port tree's own freeze (`batch.new_snapshot`) to read destination
    source from."""
    root, opts = args.ctx.root, args.opts
    (batch_name, from_snapshot_name, source_id, hunks_path, out_dir,
     verdicts_path, by) = (args.batch_name, args.from_snapshot, args.source_id,
                           args.hunks_path, args.out_dir, args.verdicts_path, args.by)

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    out_abs = _resolve_out_dir(out_dir, port_root, "judge-nodes")
    verdicts_abs = _resolve_verdicts_path(verdicts_path, port_root, "judge-nodes")

    batch, cfg_source, batch_source, err = _resolve_judge_batch(
        port_root, batch_name, source_id, verb="judge-nodes")
    if err:
        return Verdict.unresolved(err).exit

    from_commit, to_commit, from_key, to_key, from_idx, to_idx = _resolve_judge_from_to(
        port_root, batch, batch_source, from_snapshot_name, verb="judge-nodes")

    old, new, port, port_commit, pk = _judge_trees(
        port_root, batch, from_idx, to_idx, from_key, to_key)

    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        return Verdict.broken(_not_written(e.findings)).exit
    state = ledger.replay()

    missing = _from_key_missing_msg(state, from_key, "judge-nodes")
    if missing:
        return Verdict.broken(missing).exit

    if verdicts_abs is not None:
        try:
            rows = load_verdicts(verdicts_abs)
        except ValueError as e:
            return Verdict.broken(str(e)).exit
        stamp, actor = now_ts(), resolve_actor()
        src = {"id": batch_source.get("id"), "sha": to_commit}
        # A row already stated under `node` is skipped (idempotent re-run,
        # matching upstream): the SAME reason a packet exists only for a
        # node with no row yet — see the module docstring's own note that
        # `judge-removed`'s verdicts path deliberately does NOT share this
        # filter.
        evs = [
            Event(ts=stamp, node=r["node"], status=r["status"], by=by, actor=actor,
                 extra={**({"to": tuple(r["to"])} if r.get("to") else {}),
                        **({"reason": r["reason"]} if r.get("reason") else {}),
                        "src": dict(src)},
                 note="judge-nodes verdict")
            for r in rows if r["node"] not in state
        ]
        apply_v = bool(getattr(opts, "apply", False))
        write_all(ledger, evs, "judge-nodes", apply=apply_v)
        v_data = {"schema": "portkit.judge_nodes/1", "verdicts": len(evs), "dry_run": not apply_v}
        if bool(getattr(opts, "json", False)):
            print(json.dumps(v_data, sort_keys=True, ensure_ascii=False))
        else:
            print("ledger  : %s" % ledger.path)
            print("verdicts: %d read, %d new -> %s" % (
                len(rows), len(evs), "written" if apply_v else "dry-run (--apply appends)"))
        return int(Verdict.ok(v_data).exit)

    up_repo = (port_root / str(cfg_source.get("repo", ""))).resolve()
    up_from, up_to = Tree(up_repo, from_commit), Tree(up_repo, to_commit)
    port_tree = Tree(port_root, port_commit)
    hunks = load_hunks(Path(hunks_path) if hunks_path else None)

    res = _judge_nodes(state, old, new, port, from_key, to_key, up_from, up_to, port_tree, hunks)

    stamp, actor = now_ts(), resolve_actor()
    src = {"id": batch_source.get("id"), "sha": to_commit}
    evs = [
        Event(ts=stamp, node=a["node"], status=a["status"], by=by, actor=actor,
             extra={"to": tuple(a["to"]),
                    **({"reason": a["reason"]} if a.get("reason") else {}),
                    **({"carried_from": a["carried_from"]} if a.get("carried_from") else {}),
                    "judged": a["judged"], "src": dict(src)},
             note="judge-nodes %s: %s" % (a["cause"], a["note"]))
        for a in res["auto"]
    ]

    apply = bool(getattr(opts, "apply", False))
    write_all(ledger, evs, "judge-nodes", apply=apply)
    if apply:
        # `two-homes` (`port_open`'s own open-items store): a `new-in-target`
        # packet with >=2 equal-body candidates is a real fork, not a plain
        # "needs a human to write a verdict" packet — raise it so a human
        # can approve one home. Idempotent on `(kind, subject)`
        # (`raise_item`): a re-run never opens a second item for the same
        # node, and once approved the ledger row `approve` writes makes
        # this node land in `state` next run, so it is `skipped` above
        # rather than judged again.
        for p in res["packet"]:
            if p.get("equal_body", 0) >= 2:
                port_open.raise_item(
                    port_root, batch_name, kind="two-homes", subject=p["node"],
                    node=p["node"], candidates=list(p["candidates"]),
                    evidence=["judge-nodes %s: %s" % (p["node"], p["why"])],
                    step="judge-nodes")

    funnel = {"auto": len(res["auto"]), "packet": len(res["packet"]), "already-had-row": res["skipped"],
             "auto-by-cause": {c: sum(1 for a in res["auto"] if a["cause"] == c)
                               for c in ("changed", "new-in-target")},
             "packet-by-cause": {c: sum(1 for a in res["packet"] if a["cause"] == c)
                                 for c in ("changed", "new-in-target")}}
    n_packets = 0
    if out_abs is not None:
        out_abs.mkdir(parents=True, exist_ok=True)
        (out_abs / "nodes-funnel.json").write_text(json.dumps(funnel, indent=2), encoding="utf-8")
        (out_abs / "nodes-auto.jsonl").write_text(
            "".join(json.dumps(a, ensure_ascii=False) + "\n" for a in res["auto"]), encoding="utf-8")
        n_packets = write_packets(out_abs, res["packet"], _judge_anchor(port_commit))

    data: "dict[str, Any]" = {"schema": "portkit.judge_nodes/1", "from_key": from_key, "to_key": to_key,
                              "funnel": funnel, "dry_run": not apply}
    want_json = bool(getattr(opts, "json", False))
    if not want_json:
        print("ledger  : %s" % ledger.path)
        print("from/to : %s -> %s   port tree %s" % (from_key, to_key, port_commit[:8]))
        print("auto (body-equal)     : %d  %s" % (funnel["auto"], funnel["auto-by-cause"]))
        print("packet (needs verdict): %d  %s" % (funnel["packet"], funnel["packet-by-cause"]))
        print("already had a row     : %d" % res["skipped"])
        if out_abs is not None:
            print("packets : %d module file(s) -> %s" % (n_packets, out_abs))
        if res["auto"] or res["packet"]:
            print(("written: %d line(s)" % len(evs)) if apply
                 else "dry-run: %d row(s) valid, nothing written (--apply appends)" % len(evs))

    if want_json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))

    if not (res["auto"] or res["packet"]):
        return int(Verdict.red(data).exit)
    return int(Verdict.ok(data).exit)


JUDGE_NODES = Command(add_args=_judge_nodes_args, run=_judge_nodes_verb)


def _judge_removed_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--batch", required=True, dest="batch_name")
    ap.add_argument("--from-snapshot", required=True, dest="from_snapshot")
    ap.add_argument("--source", default=None, dest="source_id")
    ap.add_argument("--out", default=None, dest="out_dir")
    ap.add_argument("--verdicts", default=None, dest="verdicts_path")
    ap.add_argument("--by", default="human")


def _judge_removed_verb(args: argparse.Namespace) -> int:
    """`hs-code port judge-removed --batch <name> --from-snapshot <name>
    [--source <id>] [--out DIR] [--verdicts FILE] [--by human|suggest:<sha>]
    [--apply]` — settle every FROM-key row `carry-forward` left
    `gone`: auto-`dropped` ONLY when the upstream MODULE is absent from the
    target snapshot, `git log` names the commit that removed it, and no
    destination the FROM row already named is still in the port; the rest
    (`moved`/`orphan`/`vanished`) is a packet for a human, never
    auto-dropped. `--verdicts FILE` applies a reviewer's answered packet
    rows instead — see `load_verdicts`; unlike `judge-nodes`, a row is NOT
    skipped for a `node` already in state (upstream's own asymmetry: a
    verdict correcting an already-`dropped` FROM row is exactly how
    `may_transition` earns its own refusal here)."""
    root, opts = args.ctx.root, args.opts
    (batch_name, from_snapshot_name, source_id, out_dir, verdicts_path,
     by) = (args.batch_name, args.from_snapshot, args.source_id,
            args.out_dir, args.verdicts_path, args.by)

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        return Verdict.broken(str(e)).exit

    out_abs = _resolve_out_dir(out_dir, port_root, "judge-removed")
    verdicts_abs = _resolve_verdicts_path(verdicts_path, port_root, "judge-removed")

    batch, cfg_source, batch_source, err = _resolve_judge_batch(
        port_root, batch_name, source_id, verb="judge-removed")
    if err:
        return Verdict.unresolved(err).exit

    from_commit, to_commit, from_key, to_key, from_idx, to_idx = _resolve_judge_from_to(
        port_root, batch, batch_source, from_snapshot_name, verb="judge-removed")

    old, new, port, _port_commit, _pk = _judge_trees(
        port_root, batch, from_idx, to_idx, from_key, to_key)

    try:
        ledger = Ledger.load_checked(port_ledger_reader.ledger_path(port_root))
    except BrokenLedgerError as e:
        return Verdict.broken(_not_written(e.findings)).exit
    state = ledger.replay()

    missing = _from_key_missing_msg(state, from_key, "judge-removed")
    if missing:
        return Verdict.broken(missing).exit

    if verdicts_abs is not None:
        try:
            rows = load_verdicts(verdicts_abs)
        except ValueError as e:
            return Verdict.broken(str(e)).exit
        stamp, actor = now_ts(), resolve_actor()
        src = {"id": batch_source.get("id"), "sha": to_commit}
        evs = [
            Event(ts=stamp, node=r["node"], status=r["status"], by=by, actor=actor,
                 extra={**({"to": tuple(r["to"])} if r.get("to") else {}),
                        **({"reason": r["reason"]} if r.get("reason") else {}),
                        "src": dict(src)},
                 note="judge-removed verdict")
            for r in rows
        ]
        apply_v = bool(getattr(opts, "apply", False))
        write_all(ledger, evs, "judge-removed", apply=apply_v)
        v_data = {"schema": "portkit.judge_removed/1", "verdicts": len(evs), "dry_run": not apply_v}
        if bool(getattr(opts, "json", False)):
            print(json.dumps(v_data, sort_keys=True, ensure_ascii=False))
        else:
            print("ledger  : %s" % ledger.path)
            print("verdicts: %d read, %d new -> %s" % (
                len(rows), len(evs), "written" if apply_v else "dry-run (--apply appends)"))
        return int(Verdict.ok(v_data).exit)

    up_repo = (port_root / str(cfg_source.get("repo", ""))).resolve()
    res = _judge_removed(state, old, new, port, from_key, to_key, up_repo, from_commit, to_commit)

    stamp, actor = now_ts(), resolve_actor()
    src = {"id": batch_source.get("id"), "sha": to_commit}
    evs = [
        Event(ts=stamp, node=d["node"], status="dropped", by=by, actor=actor,
             extra={"reason": "upstream removed %s in %s..%s (%s); no destination remains in the port"
                              % (d["path"] or "the module", from_key, to_key, d["commit"]),
                    "src": dict(src)},
             note="judge-removed: module removed upstream")
        for d in res["drop"]
    ]

    apply = bool(getattr(opts, "apply", False))
    write_all(ledger, evs, "judge-removed", apply=apply)

    funnel = {k: len(v) for k, v in res.items()}
    if out_abs is not None:
        out_abs.mkdir(parents=True, exist_ok=True)
        (out_abs / "removed-funnel.json").write_text(json.dumps(funnel, indent=2), encoding="utf-8")
        (out_abs / "removed-packets.jsonl").write_text(
            "".join(json.dumps({"class": c, **x}, ensure_ascii=False) + "\n"
                    for c in ("orphan", "vanished", "moved") for x in res[c]), encoding="utf-8")
        (out_abs / "removed-auto.jsonl").write_text(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in res["drop"]), encoding="utf-8")

    data: "dict[str, Any]" = {"schema": "portkit.judge_removed/1", "from_key": from_key, "to_key": to_key,
                              "funnel": funnel, "dry_run": not apply}
    want_json = bool(getattr(opts, "json", False))
    if not want_json:
        print("ledger  : %s" % ledger.path)
        print("from/to : %s -> %s" % (from_key, to_key))
        for k, c in funnel.items():
            print("%-9s: %d" % (k, c))
        if out_abs is not None:
            print("packets : %s" % out_abs)
        if any(funnel.values()):
            print(("written: %d line(s)" % len(evs)) if apply
                 else "dry-run: %d row(s) valid, nothing written (--apply appends)" % len(evs))

    if want_json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))

    if not any(funnel.values()):
        return int(Verdict.red(data).exit)
    return int(Verdict.ok(data).exit)


JUDGE_REMOVED = Command(add_args=_judge_removed_args, run=_judge_removed_verb)
