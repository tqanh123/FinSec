#!/usr/bin/env python3
"""dev_override_wiring — detector for a `.harness-dev/<name>.yaml` override that
NO `HARNESS_*` env points to, so the loader silently falls back to the shipped
default and the override does nothing.

This is the "I made a dev override but forgot to wire its env" failure mode:
most localized configs resolve `explicit --config > $HARNESS_XXX > shipped`, so
the dev file is inert until an explicit `HARNESS_XXX` env in
`.claude/settings.local.json` points at it (a restart then applies it). Auto-
discovery is deliberately NOT done for the caged/security-relevant seams (an
agent could widen its own cage via a writeable-zone file), so the wiring is
manual — and therefore forgettable. This detector catches the gap; it owns no
side effects and never writes anything.

Dev-only by construction: it returns None (silent) when `.harness-dev/` is
absent, so a shipped install never trips it.
"""
import json
import os
import re
from pathlib import Path
from typing import Optional

# Dev override files consumed WITHOUT a HARNESS_* env — absence of an env is
# correct for these, not a wiring bug:
#   dev-off-skills.yaml — read directly by dev_skill_farm.py (the farm off-list)
_NO_ENV_NEEDED = frozenset({"dev-off-skills.yaml"})

_DEV_DIRNAME = ".harness-dev"

_ENV_MAP_RE = re.compile(r"_ENV_MAP = \{(.*?)\n\}", re.S)


def declared_envs(root=None) -> dict:
    """`<file>.yaml -> HARNESS_*`, as `scripts/dev_init.py` declares it.

    Read from the harness tree, not from the project under inspection: the map is the
    product's statement about which env carries which file, and a project does not get
    to disagree with it. Fail-soft to `{}` — an unreadable map means no opinion, which
    the positive control in the gate is what keeps honest.
    """
    base = Path(root) if root else Path(__file__).resolve().parents[2]
    try:
        m = _ENV_MAP_RE.search((base / "scripts" / "dev_init.py")
                               .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not m:
        return {}
    return dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', m.group(1)))


def _env_sources(project: Path):
    """The env maps to scan for HARNESS_* wiring, most-authoritative first:
    settings.local.json (the per-machine tier, and the one that wins when both
    declare a key), then settings.json (the committed tier — where a checkout
    that must work at first launch declares its wiring, since a fresh session
    binds env at startup and cannot be restarted), then the live process env.

    Both settings files are read because `env` is valid in either; a detector
    that knew only the local tier would call a committed wiring unwired."""
    sources = []
    for name in ("settings.local.json", "settings.json"):
        try:
            data = json.loads(
                (project / ".claude" / name).read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("env"), dict):
                sources.append(data["env"])
        except (OSError, ValueError):
            pass
    sources.append(dict(os.environ))
    return sources


def _wiring(project: Path, dev: Path):
    """Three facts about the HARNESS_* envs aimed into `.harness-dev/`:
    `(wired_basenames, {env_key: raw_value}, {env_key: (file, wanted_env)})` — the
    second holds the DEAD wirings (an env whose target does not exist), the third the
    MISLAYERED ones (the file exists and the env names it, but through a variable
    `dev_init` does not declare for that file).

    A mislayered wiring is counted as `wired` for the unwired question — the file does
    have an env aimed at it — and reported on its own line, because the fix is different:
    rename the variable, not add one. It earns its own class for the reason `dead` does:
    the two ends of `HARNESS_HOOK_CONFIG` vs `HARNESS_HOOK_OVERLAY` are a replacement and
    a layer, so the wrong one silently drops every shipped value the file omits.

    A dead wiring is deliberately NOT counted as wired. Every one of these
    loaders fails open, so an env pointing at nothing degrades to the shipped
    default in exactly the same silence as no env at all — while *looking*
    configured. That is what a settings.local.json carrying another checkout's
    absolute paths produces, so it needs its own name and its own fix (repoint
    the env) rather than hiding inside the wired set.

    First writer wins per key, matching `_env_sources` precedence.
    """
    wired, dead, mis, seen = set(), {}, {}, set()
    declared = declared_envs()
    wired_ok = set()
    for env in _env_sources(project):
        for key, val in env.items():
            if not (isinstance(key, str) and key.startswith("HARNESS_")
                    and isinstance(val, str) and val):
                continue
            if key in seen:
                continue
            try:
                p = Path(val)
                p = p.resolve() if p.is_absolute() else (project / p).resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            seen.add(key)
            if p.suffix != ".yaml":
                continue  # this detector governs the .yaml override tier only
            if p.parent == dev:
                if p.is_file():
                    wired.add(p.name)
                    want = declared.get(p.name)
                    if want and want != key:
                        mis[key] = (p.name, want)
                    else:
                        wired_ok.add(p.name)
                else:
                    dead[key] = val  # report as WRITTEN — the string to go fix
            elif p.parent.name == _DEV_DIRNAME and not p.is_file():
                # names a .harness-dev/ override, but under a prefix that does not
                # exist here — a settings file carried over from another checkout.
                # Calling this "unwired" would send the reader hunting for a
                # missing env line that is sitting right in front of them.
                dead[key] = val
            # anything else is aimed outside .harness-dev — another owner's business
    # A file that ALSO has its declared env aimed at it is correctly wired; the extra
    # variable is then somebody else's arrangement, not a silent drop.
    for key in [k for k, (name, _) in mis.items() if name in wired_ok]:
        del mis[key]
    return wired, dead, mis


def collect(project_dir: Optional[str] = None) -> Optional[dict]:
    """Return the classes of broken dev wiring present, else None:
    `unwired` (no env aims at the file), `dead` (an env aims at nothing) and
    `mislayered` (an env aims at the file through a variable dev_init does not declare
    for it). Fail-soft: any error or a non-dev tree returns None (the nudge stays
    silent) rather than raising."""
    try:
        project = Path(project_dir).resolve() if project_dir else Path.cwd()
        dev = project / ".harness-dev"
        if not dev.is_dir():
            return None
        present = {f.name for f in dev.glob("*.yaml")}
        wired, dead, mis = _wiring(project, dev)
        # A file already reported dead is not ALSO unwired: one root cause earns
        # one line. Listing both doubles a 12-file advisory and implies two fixes.
        claimed = {Path(v).name for v in dead.values()}
        unwired = sorted(present - wired - claimed - _NO_ENV_NEEDED)
        out = {}
        if unwired:
            out["unwired"] = unwired
        if dead:
            out["dead"] = sorted("%s=%s" % kv for kv in dead.items())
        if mis:
            out["mislayered"] = sorted(
                "%s=%s wants %s" % (key, name, want)
                for key, (name, want) in mis.items())
        return out or None
    except (OSError, RuntimeError):
        return None
