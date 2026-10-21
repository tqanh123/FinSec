#!/usr/bin/env python3
"""hs-usage — local 5h/7d token-usage windows per runtime, a sample plan
preset, and per-skill/per-call cost, all from ONE calculator
(`usage_meter.py`) — the same module the ccstatusline segment
(`hs-usage statusline --text`) and the PreToolUse nudge (`harness/hooks/
usage_nudge.py`) read. Local-only by default; `limits --live` is the one
opt-in path that reaches the network (usage-plans.yaml: provider_fetch.enabled).

Family member (`hs_family.registry.FAMILY_DEPTH["hs-usage"] = 1`, verbs declared in
`harness/data/hs-usage-registry.d/core.yaml`): every answer is one `hs-envelope/1`
JSON line (`--text` for a human). `statusline` is the one command a ccstatusline
`custom-command` widget calls with `--text` — its `Outcome.text` is the raw
segment, with nothing else on the line.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parents[1]  # harness/scripts -> harness -> repo root (mirrors hs_cli.py)
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import usage_meter  # noqa: E402
from hs_family.cli import CliBase, Command, Outcome  # noqa: E402


def _no_args(_parser) -> None:
    return None


def _add_args_limits(p) -> None:
    p.add_argument("--provider", choices=usage_meter.PROVIDERS,
                   help="restrict to one runtime (default: both)")
    p.add_argument("--window", choices=tuple(usage_meter.WINDOWS),
                   help="restrict to one window (default: both)")
    p.add_argument("--plan", help="plan preset name (default: usage-plans.yaml's default_plan)")
    p.add_argument("--live", action="store_true",
                   help="fetch the provider's own usage %% instead of the local estimate "
                        "(opt-in: usage-plans.yaml provider_fetch.enabled)")


def _add_args_cost(p) -> None:
    p.add_argument("--provider", choices=usage_meter.PROVIDERS, default="claude-code")


def _load_plans():
    """`(doc, error_exit_code_or_None)` — a malformed/missing plans file is
    reported on stderr and answered as `error`, never a traceback."""
    try:
        return usage_meter.load_plans(), None
    except usage_meter.UsagePlansError as e:
        print(str(e), file=sys.stderr)
        return None, 2


def cmd_limits(ns) -> int:
    plans_doc, rc = _load_plans()
    if rc is not None:
        return rc
    if ns.live:
        if not usage_meter.provider_fetch_enabled(plans_doc):
            print("--live needs usage-plans.yaml provider_fetch.enabled: true "
                  "(opt-in, off by default)", file=sys.stderr)
            return 3
        try:
            live = usage_meter.fetch_provider_usage(plans_doc=plans_doc)
        except usage_meter.ProviderFetchDisabled as e:
            print(str(e), file=sys.stderr)
            return 3
        except Exception as e:  # noqa: BLE001 — a network/parse failure is reported, not a crash
            print("live fetch failed: %s" % e, file=sys.stderr)
            return 2
        return Outcome(exit_code=0, state="done", data={"live": live})

    providers = [ns.provider] if ns.provider else list(usage_meter.PROVIDERS)
    windows = [ns.window] if ns.window else list(usage_meter.WINDOWS)
    rows = []
    for provider in providers:
        for window in windows:
            try:
                rows.append(usage_meter.compute_window(
                    provider, window, plans_doc=plans_doc, plan_name=ns.plan))
            except (ValueError, usage_meter.UsagePlansError) as e:
                print(str(e), file=sys.stderr)
                return 2
    return Outcome(exit_code=0, state="done", data={"windows": [r.as_dict() for r in rows]})


def cmd_statusline(ns) -> Outcome:
    plans_doc, rc = _load_plans()
    if rc is not None:
        # A statusline widget must never see an error line where a segment was
        # expected — degrade to a visible placeholder rather than a stderr-only
        # message a ccstatusline column would just drop.
        segment = "usage: n/a"
    else:
        rows = usage_meter.compute_all(plans_doc=plans_doc)
        segment = usage_meter.render_statusline_segment(rows)
    return Outcome(exit_code=0, state="done", data={"segment": segment}, text=segment)


def cmd_cost(ns) -> Outcome:
    summary = usage_meter.skill_cost_summary(ns.provider)
    return Outcome(exit_code=0, state="done", data=summary)


# One `Command` per `hs-usage` command; `harness/data/hs-usage-registry.d/core.yaml`
# names each with its help line.
LIMITS = Command(add_args=_add_args_limits, run=cmd_limits)
STATUSLINE = Command(add_args=_no_args, run=cmd_statusline)
COST = Command(add_args=_add_args_cost, run=cmd_cost)


class HsUsage(CliBase):
    prog, depth = "hs-usage", 1
    description = "local usage-window / plan-preset / per-skill-cost CLI"


def main(argv=None) -> int:
    return HsUsage(install_root=_ROOT).main(argv)


if __name__ == "__main__":
    sys.exit(main())
