"""The suite recipe: `ROPS_SUITE_LANE` + `ROPS_CMD_<NAME>` + `ROPS_JUNIT_<NAME>`
— one resolver both `hs-run suite next` (`hs_run_suite.py`) and `doctor`'s own
config check read, so a repo's suite recipe is validated by exactly one rule,
never two copies that can drift.

"The alias exists" is more than the config key being non-empty. `ROPS_CMD_
<NAME>` only declares the COMMAND; whether `hs-rops <name>` actually
dispatches to it depends on hs-rops's command table (`rops.rops.HsRopsCli`),
which ranks the shipped commands ahead of `ROPS_CMD_*` aliases
(`rops.ropslib.alias.modules`) and treats a name two sources declare as a hard
error. `ROPS_SUITE_LANE=LANE` + `ROPS_CMD_LANE=echo hi` does not make the alias
lose quietly to the built-in `lane` — the table refuses with `command 'lane'
declared twice — [ship] … and [alias] ROPS_CMD_LANE (.rops.env)`, which fails
EVERY `hs-rops` invocation (exit 2) before any command is routed. `hs-rops doctor` itself never gets to print its config block.

A name that does not lower to a Python identifier is a second way the alias
never registers: `rops.ropslib.alias.modules` silently skips it (see that
module's own `isidentifier()` check), so `hs-rops <name>` answers "unknown
command" — no verb by that name was ever created, but nothing said so at
config time.

Both are refused HERE, before either caller hands out a chain that ends in
`hs-rops <name>` or checks a program on a PATH that command will never run
on.
"""
from __future__ import annotations

import attrs as _attrs


@_attrs.define(frozen=True, kw_only=True)
class SuiteRecipe:
    """A fully-resolved recipe: enough to build the `suite` domain's chain and
    to print doctor's own suite lines."""

    lane_name: str
    verb: str
    junit_key: str
    junit_remote: str


@_attrs.define(frozen=True, kw_only=True)
class SuiteRecipeProblem:
    """Why the declared recipe is not usable. `key` is the config knob a
    reader should fix; `reason` is a ready-to-print sentence naming both the
    key and its value — never just "not set", which cannot distinguish a
    missing alias from one that collides with a built-in verb."""

    key: str
    reason: str


def _core_verb_names() -> "set[str]":
    """Every command hs-rops ships — its registry's own rows, never a second list."""
    from rops.rops import HsRopsCli
    from hs_family.cli import RegistrySource

    cli = HsRopsCli()
    src = RegistrySource(cli.registry_env(), cli.install_root, depth=1)
    return {row.path[0] for row in src.rows() if row.moved_to is None}


def resolve_suite_recipe(cfg) -> "SuiteRecipe | SuiteRecipeProblem | None":
    """`None` when `ROPS_SUITE_LANE` itself is unset — no suite declared,
    which a caller may treat as "nothing to check" (doctor) or as its own
    problem (the `suite` domain, which needs a lane to do anything).

    Otherwise either a `SuiteRecipe` or the exact `SuiteRecipeProblem` a
    caller must refuse on — checked in this order, each one a reason
    `hs-rops <verb>` would never run the declared alias:
      1. `ROPS_CMD_<lane>` is not declared at all.
      2. the lowercased lane name is not a Python identifier — `alias.
         modules` would silently skip it and no such verb is ever created.
      3. the lowercased lane name collides with a built-in verb — the whole
         router refuses to start, not just this one command.
      4. `ROPS_JUNIT_<lane>` (the remote path the run leaves its junit at)
         is not declared.
    """
    lane_name = cfg.get("ROPS_SUITE_LANE")
    if not lane_name:
        return None

    verb = lane_name.lower()
    cmd_key = "ROPS_CMD_%s" % lane_name
    command = cfg.get(cmd_key)
    if not command:
        return SuiteRecipeProblem(
            key="ROPS_SUITE_LANE",
            reason=("ROPS_SUITE_LANE=%s but %s is not declared, so `hs-rops %s` "
                    "is not a verb" % (lane_name, cmd_key, verb)))

    if not verb.isidentifier():
        return SuiteRecipeProblem(
            key="ROPS_SUITE_LANE",
            reason=("ROPS_SUITE_LANE=%s does not lower to a usable alias name "
                    "(%r) — `hs-rops` never registers it as a verb"
                    % (lane_name, verb)))

    if verb in _core_verb_names():
        return SuiteRecipeProblem(
            key="ROPS_SUITE_LANE",
            reason=("ROPS_SUITE_LANE=%s collides with the built-in `hs-rops %s` "
                    "verb — %s would never run the declared alias; rename the "
                    "lane" % (lane_name, verb, cmd_key)))

    junit_key = "ROPS_JUNIT_%s" % lane_name
    junit_remote = cfg.get(junit_key)
    if not junit_remote:
        return SuiteRecipeProblem(
            key=junit_key,
            reason=("ROPS_SUITE_LANE=%s but %s is not set — declare the remote "
                    "junit path `hs-rops %s` leaves behind"
                    % (lane_name, junit_key, verb)))

    return SuiteRecipe(lane_name=lane_name, verb=verb, junit_key=junit_key,
                       junit_remote=junit_remote)
