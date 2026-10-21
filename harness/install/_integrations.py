#!/usr/bin/env python3
"""_integrations.py — opt-in host integrations for the installer (extracted from
install.py): the ccstatusline terminal status bar and the on-PATH
launchers. install.py re-exports these names, so callers and tests that reach
them through the `install` module see no change.
"""
import os
import shlex
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _settings import (  # noqa: E402
    _settings_path, _load_settings, _write_settings)


def _statusline_config_home(override) -> Path:
    """Where ccstatusline keeps its config. The documented default is
    ~/.config/ccstatusline; the override is an explicit install() param so the
    home write is testable without touching the real ~/.config."""
    if override is not None:
        return Path(override)
    return Path.home() / ".config" / "ccstatusline"


def _wire_statusline(source_root, target_root, local, result, dry_run, home_override):
    """Opt-in ccstatusline onboarding. Two no-clobber writes:
    (1) a `statusLine` block in the target's settings.json (the npx command
    auto-installs ccstatusline on first run), and (2) a shipped default config
    copied into the user's config home. An existing statusLine or an existing
    config file is left exactly as the user had it."""
    path = _settings_path(target_root, local)
    settings = _load_settings(path)
    if "statusLine" in settings:
        result["actions"].append(
            "statusLine already set in %s — left as-is" % path.name)
    else:
        settings["statusLine"] = {
            "type": "command",
            "command": "npx -y ccstatusline@latest",
            "padding": 0,
        }
        _write_settings(path, settings, dry_run)
        result["actions"].append(
            "wire statusLine (ccstatusline) into %s" % path.name)

    cfg_dir = _statusline_config_home(home_override)
    cfg = cfg_dir / "settings.json"
    asset = source_root / "harness" / "data" / "ccstatusline-default.json"
    if cfg.is_file():
        result["actions"].append(
            "ccstatusline config exists at %s — left as-is" % cfg)
    elif not asset.is_file():
        result["warnings"].append(
            "ccstatusline default config asset missing: %s" % asset)
    elif dry_run:
        result["actions"].append("ccstatusline config -> %s (dry-run)" % cfg)
    else:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(asset, cfg)
        result["actions"].append("copy default ccstatusline config -> %s" % cfg)


def _cli_bindir(override) -> Path:
    """Where the on-PATH launchers are dropped. Default ~/.local/bin (the
    XDG user-bin convention, already on PATH in most shells); an explicit
    override keeps it testable."""
    if override:
        return Path(override)
    return Path.home() / ".local" / "bin"


def _load_launchers(root, result):
    scripts = Path(root) / "harness" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        import launchers
        return launchers.load(root)
    except Exception as e:  # noqa: BLE001 — a broken list is a warning, not a failed install
        result["warnings"].append("launchers.yaml unreadable, no CLI linked: %s" % e)
        return []


def _wire_cli(source_root, target_root, result, dry_run, bindir_override, windows=None,
              engine_root=None, bin_root_export=None):
    """Opt-in: put every launcher declared in harness/data/launchers.yaml on PATH.
    POSIX → a symlink per launcher in ~/.local/bin to harness/bin/<name>.
    Windows → a <name>.cmd shim in %USERPROFILE%\\.local\\bin that calls
    harness\\bin\\<name>.cmd (Windows has no reliable user symlink).
    Never clobbers an existing file; warns when the directory is not on PATH.

    `engine_root` is where the launcher's OWN `harness/bin/<name>` wrapper
    actually lives — default `target_root` (the per-project install copies
    harness/ there). Under a global install nothing is copied into
    `target_root`; the caller passes `source_root` (the shared bin) instead,
    same `engine_root` concept `install.py`'s runtime-wiring already uses.

    `bin_root_export`, when given, means the on-PATH entry must export
    `HARNESS_BIN_ROOT=<value>` before delegating — the shared-bin (global)
    case. A plain symlink resolves the shared bin fine on its own (from its
    own file location), but a bare terminal call then has no way to tell
    `harness_paths.data_root()` that this bin serves MANY projects: the
    self-host branch fires (`HARNESS_BIN_ROOT` unset) and every invocation's
    state lands in the SHARED bin's own `.harness/`, not the caller's project
    — measured, `hs-run plan next` from an installed project wrote its
    `runs.jsonl` into the bin's tree. A generated shim (not a symlink, which
    cannot carry an export) fixes the one thing a plain symlink cannot: the
    scripts it delegates to (`harness_paths.tool_data_root()`/
    `tool_state_dir()`) then resolve the CALLER's own project from CWD
    instead. A per-project install passes `None` and keeps the existing
    plain symlink — that copy's `harness/` tree already sits INSIDE the one
    project it serves, so the self-host branch is already correct there."""
    windows = (os.name == "nt") if windows is None else windows
    bindir = _cli_bindir(bindir_override)
    engine_root = Path(engine_root) if engine_root else target_root
    for entry in _load_launchers(engine_root, result):
        name = entry["name"]
        wrapper = engine_root / "harness" / "bin" / name
        if not wrapper.is_file():
            result["warnings"].append("%s wrapper missing: %s" % (name, wrapper))
            continue
        if not dry_run:
            try:  # tar can drop the exec bit — restore it so the launcher runs
                wrapper.chmod(0o755)
            except OSError:
                pass
        if windows:
            _write_cmd_shim(bindir, name, wrapper.parent / (name + ".cmd"), result, dry_run,
                            bin_root_export=bin_root_export)
            continue
        if bin_root_export:
            _write_sh_shim(bindir, name, wrapper, result, dry_run, bin_root_export)
            continue
        link = bindir / name
        if link.exists() or link.is_symlink():
            result["actions"].append("%s already at %s — left as-is" % (name, link))
            continue
        if dry_run:
            result["actions"].append("%s symlink -> %s (dry-run)" % (name, link))
            continue
        try:
            bindir.mkdir(parents=True, exist_ok=True)
            link.symlink_to(wrapper)
            result["actions"].append("link %s -> %s" % (name, wrapper))
        except OSError as e:  # noqa: BLE001 — a launcher we could not link is a warning
            result["warnings"].append("could not link %s (%s)" % (name, e))
    if not dry_run and bindir.is_dir() and not _on_path(bindir):
        result["warnings"].append(
            "%s is not on PATH — add it to call the harness CLIs directly" % bindir)


def _write_sh_shim(bindir, name, target, result, dry_run, bin_root_export):
    """POSIX on-PATH entry for the shared-bin (global) case — see `_wire_cli`'s
    `bin_root_export` doc. Never clobbers an existing file, symlink or not."""
    shim = bindir / name
    if shim.exists() or shim.is_symlink():
        result["actions"].append("%s already at %s — left as-is" % (name, shim))
        return
    if dry_run:
        result["actions"].append("%s shim -> %s (dry-run)" % (name, shim))
        return
    body = (
        "#!/bin/sh\n"
        "# %s — written by install.py --cli under a global install; exports\n"
        "# HARNESS_BIN_ROOT so the shared binary resolves THIS call's own\n"
        "# project from CWD instead of defaulting to the shared bin's own tree.\n"
        "export HARNESS_BIN_ROOT=%s\n"
        'exec "%s" "$@"\n' % (name, shlex.quote(str(bin_root_export)), target))
    try:
        bindir.mkdir(parents=True, exist_ok=True)
        shim.write_text(body, encoding="utf-8")
        shim.chmod(0o755)
        result["actions"].append(
            "shim %s -> %s (HARNESS_BIN_ROOT=%s)" % (shim, target, bin_root_export))
    except OSError as e:  # noqa: BLE001 — a launcher we could not write is a warning
        result["warnings"].append("could not write %s (%s)" % (shim, e))


def _on_path(directory) -> bool:
    want = os.path.normcase(os.path.normpath(str(directory)))
    return any(os.path.normcase(os.path.normpath(p)) == want
               for p in (os.environ.get("PATH") or "").split(os.pathsep) if p)


def _write_cmd_shim(bindir, name, target, result, dry_run, bin_root_export=None):
    shim = bindir / (name + ".cmd")
    if shim.exists():
        result["actions"].append("%s already at %s — left as-is" % (name, shim))
        return
    if dry_run:
        result["actions"].append("%s shim -> %s (dry-run)" % (name, shim))
        return
    export_line = ('set "HARNESS_BIN_ROOT=%s"\r\n' % bin_root_export
                   if bin_root_export else "")
    body = ('@echo off\r\nrem %s — written by install.py --cli; the launcher is %s\r\n'
            '%scall "%s" %%*\r\nexit /b %%errorlevel%%\r\n'
            % (name, target, export_line, target))
    try:
        bindir.mkdir(parents=True, exist_ok=True)
        shim.write_text(body, encoding="utf-8", newline="")
        result["actions"].append("shim %s -> %s" % (shim, target))
    except OSError as e:  # noqa: BLE001 — a launcher we could not write is a warning
        result["warnings"].append("could not write %s (%s)" % (shim, e))
