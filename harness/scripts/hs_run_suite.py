#!/usr/bin/env python3
"""hs_run_suite.py — the `suite` domain's verb entry point for hs-run (`next`).

Dropping `durations --run` (`hs-code test durations --run`, the old shape) moved
"run the suite somewhere else and read the result back" onto this lane: `hs-run
suite next` reports whether a fresh remote suite run exists for the tree AS IT
STANDS NOW, and prints the exact chain that produces one when it does not.

MECHANICAL ONLY, same boundary `hs_run_security.py` keeps: this verb never runs
the suite itself (no subprocess touches `hs-rops`/pytest from inside this
module) — it reports a STATE and hands back a `next_command` string for the
CALLER to run verbatim. The five-step chain (`hs-rops lane && ... && mv ...`)
is data this module builds from the resolved config, not a command it executes.

HARD CONSTRAINT (`test_codegraph_snapshot_reader.py`'s closure gates cover every
`hs_run*.py`): this module and
everything it imports at module scope must never reach `codegraph`. The
fingerprint it needs is `tree_content_key.content_key(repo)` — the SAME flat,
stdlib+git module `codegraph._core.snapshot.worktree_key.content_key` now
delegates its own dirty branch to (see that module's docstring) — never a
second copy of the algorithm, and never the measurement engine that module
lives beside.

Configuration comes ONLY from `.rops.env`, read through
`rops.ropslib.config.load(repo, detect=False)` — `detect=False` because
`ROPS_SUITE_LANE`/`ROPS_CMD_<NAME>`/`ROPS_JUNIT_<NAME>` are project decisions
(which alias runs the suite, where its junit lands), never something a stack
detector could infer the way `rops.ropslib.detect` infers a build command.
The three-key recipe is resolved by `rops.ropslib.suite.resolve_suite_recipe`
— the SAME function `doctor`'s own config check calls, so the rule for "is
this recipe usable" is defined once, read at call time, never cached across
calls (a `.rops.env` edit must be seen on the very next `next`):

    ROPS_SUITE_LANE=<NAME>   the hs-rops alias that runs the suite.
    ROPS_CMD_<NAME>          must exist — so `hs-rops <name>`, lowercased, is
                             a verb `rops.rops.discover()` would actually
                             dispatch to (never a name colliding with a
                             built-in verb; see `rops.ropslib.suite`'s own
                             docstring for why that crashes the whole router,
                             not just this command).
    ROPS_JUNIT_<NAME>        the remote path that run leaves its junit XML at.

`ROPS_SUITE_LANE` itself missing, or the recipe it names being unusable, is
`config_missing`, EXIT_BROKEN(2), naming both the key and the value that does
not resolve — never a guess at a default alias name (a guessed alias that
happens to exist for an unrelated reason would run the WRONG command with no
one having chosen it), and never a chain handed out before the recipe is
confirmed usable.

Local files live under `<data_root>/suite/<worktree_key>/` (`harness_paths.data_root_for(root)`
— the CALLER-SUPPLIED root's own data home, never this process's own via the
bare `data_root()`, since a verb here is always handed `root` explicitly by the
dispatcher). Linked worktrees share their host's data root, so the directory is
per worktree (`codegraph_snapshot_reader.worktree_key`, the snapshot store's own
split): two worktrees running the chain at once would otherwise swap pending
stamps and pair one tree's key with the other's junit. Files: `junit.xml`, `junit.meta.json`, `junit.meta.pending.json`. The
`.pending` name exists so a run that dies mid-chain (the remote command fails,
or the fetch fails) never leaves a NEW fingerprint paired with an OLD junit —
`_run_chain`'s own step 5 (`mv ... junit.meta.pending.json junit.meta.json`)
only ever runs after both the run and the fetch already returned 0 (`&&`
chained), so a partial run leaves the pending file behind and the paired
`junit.meta.json` untouched.

THE CLASSIFICATION ITSELF (not_run/stale/red/green, and the freshness check
behind them) lives in `suite_status.py`, a flat module this file imports —
`hs-code test status` (`codegraph/test/status.py`) reads the exact same
`suite_status.classify(root)` for its own read-only glance, so the two
readers can never drift on what state a given tree is in. This module still
owns everything `suite_status` does not: resolving the `.rops.env` recipe and
building the runnable chain.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import hs_run  # noqa: E402 — the verb declaration + StepResult + exit ladder
import rops.ropslib.config as rops_config  # noqa: E402
import rops.ropslib.suite as rops_suite  # noqa: E402 — the one suite-recipe resolver
import suite_status  # noqa: E402 — the shared not_run/stale/red/green classification
from hs_run import EXIT_BROKEN, EXIT_DONE, EXIT_FINDINGS, EXIT_STEP_REMAINING  # noqa: E402


def _config_missing(key: str, root: Path) -> dict:
    return hs_run.StepResult(
        state="config_missing", exit_code=EXIT_BROKEN,
        next_action="`%s` is not set — declare it in `.rops.env` (or export it) "
                    "before `hs-run suite next` can resolve which remote lane runs "
                    "the suite." % key,
        artifacts=[], warnings=["missing config key: %s" % key]).as_dict()


def _run_chain(root: Path, junit_path: Path, pending_path: Path, meta_path: Path,
              junit_remote: str, verb: str) -> str:
    """The five-step chain `not_run`/`stale` hand back, joined by ` && ` so the
    whole thing is ONE `next_command` string a driver runs verbatim (the
    `exit_code == 11` contract: a single runnable string, never an argv list).

    Order is load-bearing, and every step depends on the one before it running
    clean:
      1. `hs-rops lane` — syncs the remote tree to this one; a stale remote
         tree refuses the run rather than measuring the wrong code
         (`rops.verbs.run.execute` -> `tree.check`).
      2. the fingerprint is written BEFORE the run starts, not after it
         finishes — a long run whose local tree is edited mid-flight must be
         paired with the tree it was ACTUALLY run against, not the one sitting
         there when the run happens to finish.
      3. the run itself (`hs-rops <name>`).
      4. fetch the junit XML back.
      5. only once run+fetch both returned 0 does the pending fingerprint
         become the real one — a failed run or fetch must never pair a NEW key
         with an OLD junit.xml.
    `--repo` is the repo `next` answered for: a stamp taken from a subdirectory
    would list only the untracked files under it and never match."""
    bin_script = '"${HARNESS_BIN_ROOT:-.}"/harness/scripts/tree_content_key.py'
    return " && ".join([
        "hs-rops lane",
        "python3 %s --repo %s --write-meta %s --command %s"
        % (bin_script, shlex.quote(str(root)), shlex.quote(str(pending_path)),
           shlex.quote("hs-rops %s" % verb)),
        "hs-rops %s" % verb,
        "hs-rops fetch %s %s --yes" % (shlex.quote(junit_remote), shlex.quote(str(junit_path))),
        "mv %s %s" % (shlex.quote(str(pending_path)), shlex.quote(str(meta_path))),
    ])


def _next(ns) -> dict:
    """`hs-run suite next` — reports whether a FRESH remote suite result exists
    for the tree as it stands right now, mechanically only: it never runs the
    suite, it hands back the exact chain that would.

    The freshness classification (not_run/stale/red/green) is
    `suite_status.classify(root)` — the SAME call `hs-code test status` makes
    — so this function's own job is only: resolve the chain, then translate
    that one shared classification into this domain's envelope + exit rung."""
    root = ns.ctx.root
    cfg = rops_config.load(root, detect=False)
    recipe = rops_suite.resolve_suite_recipe(cfg)
    if recipe is None:
        return _config_missing("ROPS_SUITE_LANE", root)
    if isinstance(recipe, rops_suite.SuiteRecipeProblem):
        return hs_run.StepResult(
            state="config_missing", exit_code=EXIT_BROKEN,
            next_action=recipe.reason,
            artifacts=[], warnings=[recipe.reason]).as_dict()

    sdir = suite_status.suite_dir(root)
    junit_path = sdir / "junit.xml"
    meta_path = sdir / "junit.meta.json"
    pending_path = sdir / "junit.meta.pending.json"
    verb = recipe.verb

    chain = _run_chain(root, junit_path, pending_path, meta_path, recipe.junit_remote, verb)
    status = suite_status.classify(root)

    if status.state == "not_run":
        return hs_run.StepResult(
            state="not_run", exit_code=EXIT_STEP_REMAINING,
            next_action="no suite run recorded for this tree yet — run `%s` to "
                        "produce one." % chain,
            next_command=chain, artifacts=[], warnings=[]).as_dict()

    if status.state == "stale":
        return hs_run.StepResult(
            state="stale", exit_code=EXIT_STEP_REMAINING,
            next_action="%s — run `%s` to refresh it." % (status.reason, chain),
            next_command=chain, artifacts=[], warnings=[status.reason]).as_dict()

    artifact = status.artifact
    # Narrowed for mypy: `classify()` only sets `artifact` on `red`/`green`,
    # both handled below — never `None` at either return site.
    assert artifact is not None
    if status.state == "red":
        return hs_run.StepResult(
            state="red", exit_code=EXIT_FINDINGS,
            next_action="the last suite run against this tree has %d failure(s) and "
                        "%d error(s) — run `hs-code test redlist %s` to see them."
                        % (artifact.failed, artifact.errors, artifact.junit),
            next_command="hs-code test redlist %s" % artifact.junit,
            artifacts=[artifact.as_dict()], warnings=[]).as_dict()

    return hs_run.StepResult(
        state="green", exit_code=EXIT_DONE,
        next_action="the last suite run against this tree is clean (%d passed, 0 "
                    "failed, 0 errors)." % artifact.passed,
        artifacts=[artifact.as_dict()], warnings=[]).as_dict()


NEXT = hs_run.verb(_next)


if __name__ == "__main__":  # pragma: no cover - refusal path, matches every sibling domain
    sys.stderr.write(
        "%s is a hs-run domain module, not a command.\n"
        "Verbs are dispatched through the CLI, which resolves the project root, "
        "loads the registry and owns the exit ladder — none of which happens when "
        "this file is run on its own.\n"
        "Run it through the dispatcher instead:  hs-run suite next\n"
        % Path(__file__).name)
    raise SystemExit(EXIT_BROKEN)
