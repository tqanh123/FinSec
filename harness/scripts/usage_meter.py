#!/usr/bin/env python3
"""usage_meter.py — THE one usage calculator. `hs-usage` (CLI), the
ccstatusline segment (`hs-usage statusline`) and the PreToolUse nudge
(`harness/hooks/usage_nudge.py`) all import THIS module rather than each
re-deriving a token count of their own — one reader, three callers.

Local-only by default (no network): every function here reads local
Claude Code (`~/.claude/projects/**/*.jsonl`) and Codex
(`~/.codex/sessions/**/*.jsonl`) transcripts and checks the total against a
sample plan preset declared in `harness/data/usage-plans.yaml` — an ESTIMATE,
never the provider's own accounting. `fetch_provider_usage()` is the one path
that reaches the network, and it refuses unless the caller has explicitly
opted in (`usage-plans.yaml`: `provider_fetch.enabled: true`).

Same reader for per-skill cost: `claude_skill_invocations()`/`skill_cost_summary()` slice
the identical transcripts by which `Skill` tool call was active, so a
per-skill/per-call cost view and a 5h/7d window total never disagree about
what one token event counted as.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml_io

_SCRIPTS = Path(__file__).resolve().parent
DEFAULT_PLANS_PATH = _SCRIPTS.parent / "data" / "usage-plans.yaml"

#: Window name -> its timedelta. The two windows a provider's own rate limit
#: is usually expressed in (a rolling 5h burst ceiling, a rolling 7-day one).
WINDOWS = {"5h": timedelta(hours=5), "7d": timedelta(days=7)}
PROVIDERS = ("claude-code", "codex")


# --------------------------------------------------------------------- plans

class UsagePlansError(ValueError):
    """`usage-plans.yaml` is missing, unreadable, or names an unknown plan."""


def load_plans(path: Optional[Path] = None) -> dict:
    p = Path(path) if path else Path(os.environ.get("HARNESS_USAGE_PLANS_PATH") or DEFAULT_PLANS_PATH)
    if not p.is_file():
        raise UsagePlansError("%s is missing" % p)
    try:
        doc = yaml_io.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 — a malformed file is a named refusal
        raise UsagePlansError("%s is not valid YAML: %s" % (p, e)) from e
    if not isinstance(doc, dict) or not isinstance(doc.get("plans"), dict):
        raise UsagePlansError("%s must declare a top-level 'plans' mapping" % p)
    return doc


def resolve_plan(provider: str, doc: dict, name: Optional[str] = None) -> dict:
    plans = (doc.get("plans") or {}).get(provider) or {}
    name = name or (doc.get("default_plan") or {}).get(provider)
    if not name or name not in plans:
        raise UsagePlansError(
            "no plan %r declared for provider %r in usage-plans.yaml (known: %s)"
            % (name, provider, ", ".join(sorted(plans)) or "(none)"))
    return dict(plans[name], name=name)


def nudge_threshold(doc: dict) -> float:
    return float(doc.get("nudge_threshold_percent", 80))


def provider_fetch_enabled(doc: dict) -> bool:
    return bool((doc.get("provider_fetch") or {}).get("enabled", False))


# ------------------------------------------------------------- transcript IO

def default_roots() -> dict:
    """Where each runtime keeps its local transcripts, honoring the same env
    overrides Claude Code / Codex themselves use."""
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return {"claude-code": [claude],
            "codex": [codex_home / "sessions", codex_home / "archived_sessions"]}


def _load_jsonl(path: Path) -> list:
    events = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return events


def _parse_ts(value) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _claude_token_total(usage: dict) -> int:
    """Claude Code's own usage field names (session_usage.py's `add_usage`)."""
    return (int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
            + int(usage.get("output_tokens") or 0))


@dataclass(frozen=True)
class UsageEvent:
    ts: datetime
    tokens: int
    skill: Optional[str] = None


def claude_events(root: Path) -> list:
    """One `UsageEvent` per distinct assistant-turn `usage` block in every
    transcript under `root` (recursive `*.jsonl`), tagged with whichever
    `Skill` tool call was most recently invoked in that same file (the
    per-skill attribution — `None` before the first one)."""
    root = Path(root)
    out: list = []
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*.jsonl")):
        current_skill = None
        seen_ids: set = set()
        for event in _load_jsonl(path):
            if event.get("type") != "assistant":
                continue
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("name") == "Skill"):
                    name = (block.get("input") or {}).get("skill")
                    if name:
                        current_skill = str(name)
            usage = message.get("usage")
            if not usage:
                continue
            mid = message.get("id") or id(event)
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            ts = _parse_ts(event.get("timestamp"))
            if ts is not None:
                out.append(UsageEvent(ts, _claude_token_total(usage), current_skill))
    return out


def codex_events(root: Path) -> list:
    """One `UsageEvent` per cumulative `token_count` DELTA in every Codex
    session rollout under `root`. Codex reports a running session total, not a
    per-turn delta, so consecutive totals are diffed; a total that goes
    backwards (a new session baseline) never yields a negative delta."""
    root = Path(root)
    out: list = []
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*.jsonl")):
        baseline = 0
        for event in _load_jsonl(path):
            if event.get("type") != "event_msg":
                continue
            payload = event.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            usage = info.get("total_token_usage") or {}
            total = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            delta = max(total - baseline, 0)
            baseline = total
            ts = _parse_ts(event.get("timestamp"))
            if ts is not None and delta:
                out.append(UsageEvent(ts, delta, None))
    return out


_READER = {"claude-code": claude_events, "codex": codex_events}


def all_events(provider: str, roots: Optional[dict] = None) -> list:
    roots = roots if roots is not None else default_roots()
    reader = _READER[provider]
    events: list = []
    for root in roots.get(provider, []):
        events.extend(reader(Path(root)))
    return events


def windowed_used(events: list, window: str, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    since = now - WINDOWS[window]
    return sum(e.tokens for e in events if since <= e.ts <= now)


# --------------------------------------------------------------- window rows

@dataclass(frozen=True)
class UsageWindow:
    provider: str
    window: str
    used_tokens: int
    budget_tokens: int
    percent: float
    plan: str
    source: str = "local-estimate"
    note: str = ""

    def as_dict(self) -> dict:
        return {"provider": self.provider, "window": self.window, "used_tokens": self.used_tokens,
                "budget_tokens": self.budget_tokens, "percent": self.percent, "plan": self.plan,
                "source": self.source, "note": self.note}


def compute_window(provider: str, window: str, *, now: Optional[datetime] = None,
                   roots: Optional[dict] = None, plans_doc: Optional[dict] = None,
                   plan_name: Optional[str] = None) -> UsageWindow:
    if provider not in PROVIDERS:
        raise ValueError("unknown provider %r (known: %s)" % (provider, ", ".join(PROVIDERS)))
    if window not in WINDOWS:
        raise ValueError("unknown window %r (known: %s)" % (window, ", ".join(WINDOWS)))
    plans_doc = plans_doc if plans_doc is not None else load_plans()
    plan = resolve_plan(provider, plans_doc, plan_name)
    events = all_events(provider, roots)
    used = windowed_used(events, window, now)
    budget_key = "five_hour_tokens" if window == "5h" else "seven_day_tokens"
    budget = int(plan.get(budget_key) or 0)
    percent = round((used / budget) * 100, 1) if budget else 0.0
    note = "" if events else "no local transcripts found under the configured root(s)"
    return UsageWindow(provider=provider, window=window, used_tokens=used, budget_tokens=budget,
                       percent=percent, plan=plan["name"], note=note)


def compute_all(*, now: Optional[datetime] = None, roots: Optional[dict] = None,
                plans_doc: Optional[dict] = None) -> list:
    plans_doc = plans_doc if plans_doc is not None else load_plans()
    return [compute_window(p, w, now=now, roots=roots, plans_doc=plans_doc)
            for p in PROVIDERS for w in WINDOWS]


# --------------------------------------------------------- statusline / nudge

def render_statusline_segment(rows: list) -> str:
    """One compact line, safe for a ccstatusline `custom-command` widget."""
    if not rows:
        return "usage: n/a"
    return " · ".join("%s %s %.0f%%" % (r.provider, r.window, r.percent) for r in rows)


def nudge_message(rows: list, threshold: float) -> Optional[str]:
    """The advisory string for `usage_nudge.py`, or `None` when every window
    is below `threshold`. Never blocks anything itself — that is the caller's
    hook-class choice, not this function's."""
    hot = [r for r in rows if r.budget_tokens and r.percent >= threshold]
    if not hot:
        return None
    detail = "; ".join("%s %s: ~%.0f%% ước lượng gói %s" % (r.provider, r.window, r.percent, r.plan)
                       for r in hot)
    return ("[nudge] usage: gần chạm trần ước lượng cục bộ (%s) — cân nhắc việc nhẹ hơn trước khi "
            "mở thêm subagent/workflow nặng. Advisory, non-blocking." % detail)


# ---------------------------------------------------------- per-skill cost

@dataclass
class SkillInvocation:
    runtime: str
    skill: str
    started_at: datetime
    tokens: int = 0


def claude_skill_invocations(root: Path) -> list:
    """One `SkillInvocation` per `Skill` tool call in every Claude Code
    transcript under `root`, holding every assistant-turn token total from
    that call up to (not including) the NEXT `Skill` call in the same file."""
    root = Path(root)
    out: list = []
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*.jsonl")):
        current: Optional[SkillInvocation] = None
        seen_ids: set = set()
        for event in _load_jsonl(path):
            if event.get("type") != "assistant":
                continue
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("name") == "Skill"):
                    name = (block.get("input") or {}).get("skill")
                    if name:
                        if current is not None:
                            out.append(current)
                        ts = _parse_ts(event.get("timestamp")) or datetime.now(timezone.utc)
                        current = SkillInvocation(runtime="claude-code", skill=str(name), started_at=ts)
            usage = message.get("usage")
            if usage and current is not None:
                mid = message.get("id") or id(event)
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    current.tokens += _claude_token_total(usage)
        if current is not None:
            out.append(current)
    return out


_LIMITATION_NO_INVOCATIONS = {
    "codex": "codex per-skill/per-call cost is not yet derived here — only the "
             "windowed token total is measured for codex (see compute_window); "
             "codex has no equivalent 'Skill' tool-call boundary to slice on.",
}


def skill_cost_summary(provider: str, *, roots: Optional[dict] = None) -> dict:
    """`{provider, invocations, by_skill: [{skill, calls, tokens, mean_tokens}],
    limitations: [...]}` — per-skill/per-call token cost from the SAME
    transcripts `compute_window` reads, sorted by total tokens desc.
    An unsupported provider names the gap in `limitations` rather than
    fabricating rows for it (counting-discipline: absence over invention)."""
    roots = roots if roots is not None else default_roots()
    invocations: list = []
    limitations: list = []
    if provider == "claude-code":
        for root in roots.get(provider, []):
            invocations.extend(claude_skill_invocations(Path(root)))
    else:
        limitations.append(_LIMITATION_NO_INVOCATIONS.get(
            provider, "%s per-skill/per-call cost is not derived here" % provider))
    totals: Counter = Counter()
    calls: Counter = Counter()
    for inv in invocations:
        totals[inv.skill] += inv.tokens
        calls[inv.skill] += 1
    rows = [{"skill": s, "calls": calls[s], "tokens": totals[s],
             "mean_tokens": round(totals[s] / calls[s], 1) if calls[s] else 0.0}
            for s in sorted(totals, key=lambda k: -totals[k])]
    return {"provider": provider, "invocations": len(invocations), "by_skill": rows,
           "limitations": limitations}


# ------------------------------------------------------------- provider fetch

class ProviderFetchDisabled(RuntimeError):
    """`fetch_provider_usage()` refused: opted out, or no usable credentials."""


def _read_claude_access_token() -> Optional[str]:
    path = Path.home() / ".claude" / ".credentials.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    token = (data.get("claudeAiOauth") or {}).get("accessToken")
    return str(token) if token else None


def fetch_provider_usage(*, plans_doc: Optional[dict] = None, timeout: float = 5.0) -> dict:
    """Live 5h/7d usage percentages from Anthropic's own OAuth usage endpoint.

    Refuses (`ProviderFetchDisabled`) unless `provider_fetch.enabled: true` in
    usage-plans.yaml (opt-in, off by default) — checked here again even
    though every caller in this tree already checks `provider_fetch_enabled()`
    first, so a caller cannot reach the network by skipping that check. Never
    called by a test; the one integration point (`hs-usage limits --live`) is
    itself gated the same way.
    """
    plans_doc = plans_doc if plans_doc is not None else load_plans()
    if not provider_fetch_enabled(plans_doc):
        raise ProviderFetchDisabled(
            "provider_fetch.enabled is false in usage-plans.yaml — opt in there first "
            "(local-only by default)")
    token = _read_claude_access_token()
    if not token:
        raise ProviderFetchDisabled("no local Claude Code OAuth credentials found "
                                    "(~/.claude/.credentials.json)")
    import httpx  # deferred: every other path in this module never touches the network

    resp = httpx.get(
        "https://api.anthropic.com/api/oauth/usage",
        headers={"Accept": "application/json", "Authorization": "Bearer %s" % token,
                 "anthropic-beta": "oauth-2025-04-20", "User-Agent": "hs-usage/1"},
        timeout=timeout)
    resp.raise_for_status()
    return dict(resp.json())


__all__ = [
    "WINDOWS", "PROVIDERS", "DEFAULT_PLANS_PATH",
    "UsagePlansError", "load_plans", "resolve_plan", "nudge_threshold",
    "provider_fetch_enabled", "default_roots", "UsageEvent", "claude_events",
    "codex_events", "all_events", "windowed_used", "UsageWindow", "compute_window",
    "compute_all", "render_statusline_segment", "nudge_message",
    "SkillInvocation", "claude_skill_invocations", "skill_cost_summary",
    "ProviderFetchDisabled", "fetch_provider_usage",
]
