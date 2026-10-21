"""doctor — what rops resolved, where each value came from, and whether it works.

Detection is a silent layer, so this verb's first job is not "is the machine healthy"
but "do you agree with what I inferred". Every value prints its source; on a repo with
no .rops.env the detected set is written out so the next run reads a declared file.
"""
from __future__ import annotations

import argparse
import re
import shlex

from rops.ropslib import config as config_mod
from rops.ropslib import hosts
from rops.ropslib import suite as suite_mod
from rops.ropslib.prog import PROG
from rops.ropslib.tmux import TMUX_MIN
from rops.ropslib.transport import Host
from shell_unwrap import top_level_segments


# Printed even when unset, because an absent value is a finding.
SHOWN = ["ROPS_HOST", "ROPS_LANE", "ROPS_REMOTE_DIR", "ROPS_LOG_DIR", "ROPS_PATH_PREPEND",
         "ROPS_ENV"]

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def resolve_alias_program(command: str) -> "tuple[list[str], str | None, bool]":
    """`(setup_segments, program, unparsable)` for an alias's declared command
    text — no hard-coded prefix list, so a repo's own venv-activation shape
    never has to be special-cased here.

    Split at every top-level separator (`shell_unwrap.top_level_segments`,
    quote-aware). A leading segment that is empty or made ONLY of `export`/
    `NAME=value` words is SETUP — text a caller must re-run before checking
    the program, so an alias that puts its own venv on PATH is checked on
    THAT PATH. The first segment that is not pure setup is the command: its
    own leading `NAME=value` words are skipped (inline assignments do not
    name a program), and the first word left is the program.

    `unparsable=True` (program always `None`) only when a segment's own
    words cannot be split at all — unbalanced quotes. That is a genuinely
    different finding from "this alias runs nothing to check" (a blank
    command, or one made only of setup segments, `program=None,
    unparsable=False`): the caller decides, but it must not silently guess a
    program out of text it could not parse.
    """
    setup: "list[str]" = []
    for segment in top_level_segments(command):
        stripped = segment.strip()
        if not stripped:
            continue
        try:
            words = shlex.split(stripped)
        except ValueError:
            return setup, None, True
        if words and all(w == "export" or _ASSIGNMENT_RE.match(w) for w in words):
            setup.append(stripped)
            continue
        i = 0
        while i < len(words) and _ASSIGNMENT_RE.match(words[i]):
            i += 1
        program = words[i] if i < len(words) else None
        return setup, program, False
    return setup, None, False


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--measure", action="store_true",
                   help="also check the machine is QUIET enough to trust a number")
    p.add_argument("--write-env", action="store_true",
                   help="write detected values to .rops.env even if it exists is refused")


def _shell_line(cfg, host) -> str:
    """Which shell answers ssh here, vs which shell every command actually runs
    under (`ROPS_SHELL`, default `bash`) -- `wrap` (ropslib/exec.py) now deliberately
    makes the two differ: the login shell here is zsh (measured on homelab), and a
    command written in bash syntax means something else there or nothing at all."""
    login = host.out("echo $0") or "?"
    resolved = cfg.get("ROPS_SHELL") or "bash"
    src = cfg.source("ROPS_SHELL") if cfg.get("ROPS_SHELL") else "default"
    return f"[info] shell         login={login}  commands run under={resolved} [{src}]"


def _venv_prepend_note(cfg, host):
    """Warn when an alias expects `$PWD/<dir>/bin` on PATH and neither that
    directory nor a python from `ROPS_PATH_PREPEND` exists on the remote yet.

    A freshly built lane has no venv until `lane` (or `run --lane`) runs its own
    sync step: without this, the alias fails with a plain "command not found",
    easy to misread as a broken alias rather than a lane that has never synced.
    """
    from rops.ropslib import detect

    names = detect.venvs_in(cfg)
    if not names:
        return None
    remote = cfg.get("ROPS_REMOTE_DIR", "~")
    missing = [n for n in names if not host.ok(f"test -d {remote}/{n}/bin", timeout=15)]
    if not missing:
        return None
    from rops.ropslib.transport import prelude

    prepend = cfg.get("ROPS_PATH_PREPEND")
    if prepend and host.ok(f"{prelude(cfg)} && command -v python3 >/dev/null 2>&1", timeout=15):
        return None
    where = ", ".join(f"$PWD/{n}/bin" for n in missing)
    return (f"[warn] venv          {where} not on {remote} yet (a fresh lane has none until "
            f"`{PROG} lane`/`run --lane` runs its sync) and ROPS_PATH_PREPEND does not supply "
            f"a python either")


def _print_config(cfg) -> None:
    print(f"repo      {cfg.repo}")
    shown = SHOWN + [k for k in cfg.keys() if k.startswith("ROPS_CMD_")]
    # Only meaningful once a lane is declared -- an undeclared suite recipe
    # is not a config finding this block reports on (see the `[FAIL] suite`
    # line in `run()`, which is where an INCOMPLETE one is).
    lane_name = cfg.get("ROPS_SUITE_LANE")
    if lane_name:
        shown = shown + ["ROPS_SUITE_LANE", "ROPS_JUNIT_%s" % lane_name]
    for key in shown:
        val = cfg.get(key)
        src = cfg.source(key) if val else "unset"
        label = key[len("ROPS_"):].lower()
        print(f"  {label:<14} {val or '—':<52} [{src}]")
    ex = cfg.csv("ROPS_RSYNC_EXCLUDES")
    print(f"  {'excludes':<14} {len(ex)} patterns{'':<43} [{cfg.source('ROPS_RSYNC_EXCLUDES')}]")


def _tmux_too_old(version_line: str) -> bool:
    """`tmux 1.8` / `tmux 2.0` -> True; `tmux 3.4`, `tmux next-3.6`, `tmux master` -> False:
    a version it cannot read is not called old."""
    m = re.search(r"(\d+)\.(\d+)", version_line)
    return bool(m) and (int(m[1]), int(m[2])) < TMUX_MIN


def _stale_aliases(cfg) -> "list[tuple[str, str]]":
    """[(key, detected now)] for each alias a .rops.env declares that detection now
    answers differently. Detection runs once, into the file; a fix to it never
    reaches a repo whose file already holds the old guess."""
    from rops.ropslib import detect as detect_mod
    try:
        now = detect_mod.aliases(cfg.repo)
    except Exception:  # noqa: BLE001 — a note, never the reason doctor fails
        return []
    return [(k, now[k]) for k in sorted(now)
            if cfg.source(k) in (config_mod.SRC_FILE, config_mod.SRC_MAIN_FILE)
            and cfg.get(k) != now[k]]


def run(args: argparse.Namespace) -> int:
    cfg = args.cfg
    _print_config(cfg)

    env_path = cfg.repo / config_mod.ENV_FILENAME
    # a linked worktree already reads its main checkout's file; a second one here
    # would shadow the declared host with whatever detection found (never a host)
    if not env_path.exists() and config_mod.main_env_file(cfg.repo) is None:
        written = config_mod.write_env_file(cfg, env_path)
        if written:
            added = config_mod.git_exclude(cfg.repo, config_mod.ENV_FILENAME)
            print(f"\nwrote {env_path} ({len(written)} lines) — read it before running anything heavy"
                  + ("\nadded .rops.env to .git/info/exclude (local ignore, not .gitignore)" if added else ""))
    elif env_path.exists():
        # a legacy file predates the quoted shape and still executes its command
        # tails when sourced — upgrade it in place when that is lossless
        migrated, refused = config_mod.maybe_migrate_env_file(env_path)
        if migrated:
            bak = config_mod.env_backup_path(env_path)
            config_mod.git_exclude(cfg.repo, bak.name)
            print(f"\nmigrated {env_path} to quoted values — now safe to source "
                  f"(the previous bytes are in {bak})")
        elif refused:
            print(f"\nleft {env_path} as-is: re-quoting would change the meaning of "
                  f"{', '.join(refused)} (its bare value contains ' #'); edit by hand")

    for key, now in _stale_aliases(cfg):
        print(f"[note] {key[len('ROPS_CMD_'):].lower():<13} {cfg.source(key)} says "
              f"{cfg.get(key)!r}; detection now gives {now!r} — the file wins, so "
              f"update it if detection wrote that line")

    print()
    bad = 0

    # Local-only: whether the suite recipe resolves needs no ssh round trip,
    # and doctor must say so even when the host below is unreachable or
    # unset. `resolve_suite_recipe` is the SAME rule `hs-run suite next`
    # refuses on -- one definition, read by both callers.
    recipe = suite_mod.resolve_suite_recipe(cfg)
    if isinstance(recipe, suite_mod.SuiteRecipeProblem):
        bad += 1
        print(f"[FAIL] {'suite':<13} {recipe.reason}")

    host_name = getattr(args, "host", None) or cfg.get("ROPS_HOST")
    if not host_name:
        print("[FAIL] host          not set — rops cannot run anything")
        return 1
    host = Host(host_name, cfg.get("ROPS_SSH_OPTS"))

    # Reachability first, the destination second: before the first `lane` the dir does
    # not exist, and every check that `cd`s into it would fail for THAT reason while
    # naming another ('uv' not on PATH, once per alias). One cause, one finding.
    problem = hosts.check_transport(cfg, host, need_dir=False)
    remote = cfg.get("ROPS_REMOTE_DIR", "~")
    missing_dir = not problem and not host.ok(f"test -d {remote}", timeout=15)
    aliases = [(k, cfg.get(k)) for k in cfg.keys() if k.startswith("ROPS_CMD_")]
    if missing_dir:
        bad += 1
        skipped = ", ".join(["git tree", *(k[len("ROPS_CMD_"):].lower() for k, _ in aliases)])
        print(f"[FAIL] remote dir    {host.name}:{remote} does not exist yet (destination "
              f"came from: {cfg.source('ROPS_REMOTE_DIR')}) — run `{PROG} lane` (or "
              f"`{PROG} sync`) first; skipped the checks that run inside it: {skipped}")
    else:
        print(f"[{'ok  ' if not problem else 'FAIL'}] transport     {problem or f'{host.name} reachable, destination exists'}")
        bad += 1 if problem else 0

    checks = [("tmux", "tmux -V")]
    if not missing_dir:
        checks.append(("git tree", f"cd {remote} && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '(not a git tree)'"))
    for label, cmd in checks:
        r = host.run(cmd)
        ok = r.returncode == 0
        first = ((r.stdout or r.stderr).strip().splitlines() or [""])[0]
        if ok and label == "tmux" and _tmux_too_old(first):
            ok, first = False, (f"{first}: the --tmux lane targets `<session>:^.{{top-left}}`, "
                                f"which needs tmux >= {'.'.join(map(str, TMUX_MIN))}")
        bad += 0 if ok else 1
        print(f"[{'ok  ' if ok else 'FAIL'}] {label:<13} {first}")

    print(_shell_line(cfg, host))

    # Every alias names a PROGRAM, not necessarily its first word: an alias
    # that prepends its own venv to PATH (`export PATH=$PWD/.venv-ci/bin:
    # $PATH && bash ...`, the real shape every alias in this repo's own
    # `.rops.env` takes) must be checked on THAT path, not the login shell's
    # bare one -- and its own leading `export`/`NAME=value` words are setup,
    # never the program. A non-interactive ssh shell also often lacks
    # ~/.local/bin, so `uv` is simply missing — and rc=127 "command not
    # found" reads as "uv is not installed on that machine", not as "PATH is
    # wrong". Check it here instead.
    from rops.ropslib.transport import prelude
    for key, command in ([] if missing_dir else aliases):
        name = key[len("ROPS_CMD_"):].lower()
        setup, program, unparsable = resolve_alias_program(command)
        if unparsable:
            bad += 1
            print(f"[FAIL] {name:<13} could not tell which program this alias "
                  f"runs (unbalanced quotes in {command!r})")
            continue
        if not program:
            continue
        check = " && ".join([prelude(cfg), *setup, f"command -v {shlex.quote(program)} >/dev/null 2>&1"])
        found = host.ok(check)
        if not found:
            bad += 1
            hint = ("" if cfg.get("ROPS_PATH_PREPEND")
                    else "  (a non-interactive ssh shell has no ~/.local/bin — try ROPS_PATH_PREPEND=$HOME/.local/bin)")
            print(f"[FAIL] {name:<13} {program!r} not on PATH over there{hint}")
        else:
            print(f"[ok  ] {name:<13} {program} found")

    venv_note = None if missing_dir else _venv_prepend_note(cfg, host)
    if venv_note:
        print(venv_note)

    if args.measure:
        # These two do not ask "can this machine run things" — they ask "is it quiet
        # enough to believe a number". 56 orphaned CPU burners once ran here for 108
        # minutes while every other check stayed green.
        from rops.ropslib import quiet
        bad += quiet.report(cfg, host)

    print(f"\n{'all clear' if not bad else f'{bad} problem(s)'}")
    return 1 if bad else 0
