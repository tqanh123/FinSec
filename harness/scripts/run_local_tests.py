#!/usr/bin/env python3
"""run_local_tests.py — run the SAME pytest command CI runs, from one subcommand.

Reading `scripts/ci.sh` prose and hand-crafting the equivalent pytest invocation drifts
over time (a scheduler/marker/flag changes on one side and not the other). This wrapper
reads `harness/data/test-strategy.yaml` (the SSOT profile table, kept in lockstep with
ci.sh by `test_run_local_tests.py::test_strategy_matches_ci_sh`) and builds the argv —
so a dev or agent runs `--harness` / `--release` / etc. instead of remembering flags.

Matching the flags is not the same job as running the same job: ci.sh also exports
TMPDIR=/dev/shm when a tmpfs has room, and every `tmp_path` lands there instead of on
disk. Measured on a 20-core remote, same tree and commit, harness suite -- bare argv
90.82-97.23s (spread 7.1%), through ci.sh 84.35-84.48s (spread 0.2%). `ci_environment()`
carries that half; the drift gate compares flags only and was green across the gap.

`build_argv` and `ci_environment` are pure (no subprocess) so they are unit-tested
directly; `main` is the only seam that shells out, and `--all` always runs its profiles SEQUENTIALLY — a personal
16-core box does not want two xdist fan-outs contending for the same cores at once.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml_io

def _default_strategy_path(explicit=None):
    """Resolved through the one door (harness/scripts/config_layers.py); the layer stack
    is declared in harness/data/config-layers.yaml, never spelled twice."""
    import config_layers
    return config_layers.resolve_path("test-strategy.yaml", explicit=explicit)[0]


# argparse dest (dashes -> underscores) -> profile key in test-strategy.yaml.
_FLAG_TO_PROFILE = {
    "harness": "harness",
    "harness_fast": "harness-fast",
    "release": "release",
    "plugin_skills": "plugin-skills",
    "hs_code": "hs-code",
    "packs": "packs",
}


def load_strategy(path) -> Dict[str, Dict[str, Any]]:
    """Parse + validate test-strategy.yaml's `profiles` table. Raises ValueError on a
    missing/empty table or a profile entry missing its required `path` key — a caller
    should not get a confusing KeyError three calls deep in build_argv."""
    p = Path(path) if path else _default_strategy_path()
    text = p.read_text(encoding="utf-8")
    data = yaml_io.safe_load(text) or {}
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError(f"{p}: missing or empty 'profiles' table")
    for name, cfg in profiles.items():
        if not isinstance(cfg, dict) or "path" not in cfg:
            raise ValueError(f"{p}: profile '{name}' missing required 'path' key")
    return profiles


def load_all_profiles(path=None) -> Tuple[str, ...]:
    """Parse test-strategy.yaml's top-level `all_profiles` list -- the ONE source for
    which profiles `--all` runs, in run order. `harness-fast` is a single flag away
    and is deliberately never folded in here: it trades coverage for speed on
    purpose. Kept separate from load_strategy() (whose return shape — the `profiles`
    dict — many other callers depend on) rather than piggy-backing a second key onto
    it. Raises ValueError on a missing/empty list, matching load_strategy()'s own
    failure style, rather than a bare KeyError three calls deep in main()."""
    p = Path(path) if path else _default_strategy_path()
    text = p.read_text(encoding="utf-8")
    data = yaml_io.safe_load(text) or {}
    all_profiles = data.get("all_profiles")
    if not isinstance(all_profiles, list) or not all_profiles:
        raise ValueError(f"{p}: missing or empty 'all_profiles' list")
    return tuple(all_profiles)


def build_argv(profile: str, strategy: Dict[str, Dict[str, Any]]) -> List[str]:
    """Pure argv builder — path, -q, xdist scheduler (unless xdist: false),
    -p no:randomly, per-ignore flags. No subprocess call here; that lives in main()."""
    if profile not in strategy:
        raise ValueError(f"unknown profile '{profile}' — known: {sorted(strategy)}")
    cfg = strategy[profile]
    path = cfg["path"]
    argv: List[str] = ["python3", "-m", "pytest", path, "-q"]

    if cfg.get("xdist", True) is not False:
        scheduler = cfg.get("scheduler", "load")
        argv += ["-n", "auto", "--dist", scheduler]


    if cfg.get("norandomly"):
        argv += ["-p", "no:randomly"]

    for name in cfg.get("ignore") or []:
        argv.append(f"--ignore={path}/{name}")

    return argv


def build_targets_argv(profile: str, strategy: Dict[str, Dict[str, Any]],
                       targets: List[str]) -> List[str]:
    """The gate argv `build_argv(profile, ...)` with ONLY the profile's own `path`
    replaced by explicit target node ids -- runner flags and `--ignore=` items
    unchanged, so a proof runs the command the gate runs. pytest never lets an
    `--ignore` drop an explicitly named target, so a node under an ignored subdir
    still collects (measured on `harness/tests/hs_code`)."""
    if not targets:
        raise ValueError("build_targets_argv: at least one target node id is required")
    path = strategy[profile]["path"]
    out: List[str] = []
    for tok in build_argv(profile, strategy):
        out.extend(targets if tok == path else [tok])
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--harness", action="store_true", help="full harness/tests, loadfile")
    group.add_argument("--harness-fast", action="store_true", help="harness subset, skips the heaviest files")
    group.add_argument("--release", action="store_true", help="release/tests, --dist load")
    group.add_argument("--plugin-skills", action="store_true", help="harness/plugins/hs/skills, loadfile")
    group.add_argument("--hs-code", action="store_true", help="harness/tests/hs_code, isolated hs-code suite")
    group.add_argument("--packs", action="store_true", help="packs/*/tests, empty until a pack ships one")
    group.add_argument("--all", action="store_true", help="harness + release + hs-code + packs + rops, sequentially")
    group.add_argument(
        "--targets", nargs="+", default=None, metavar="NODE",
        help="run the `harness` profile's gate argv with its path replaced by explicit "
             "pytest node ids (runner flags and --ignore items unchanged)",
    )
    parser.add_argument("--strategy", default=None, help="path to test-strategy.yaml")
    parser.add_argument(
        "--collect-only", "--dry",
        dest="dry", action="store_true",
        help="pass --collect-only through to pytest so scope/count can be confirmed without running tests",
    )
    return parser


# ci.sh's tmpfs floor, mirrored here rather than shelled out to, because build_argv()
# must stay pure. 10 GiB = one run (measured 6.4-6.7 GiB of temp trees) plus 50%. A box
# that cleared the old 4 GiB floor could not hold a single run, and overrunning it kills
# pytest with INTERNALERROR on a worker whose test passes alone, with nothing in the
# message about space. NOT one run x pytest's retention of 3: that sizing refused a host
# with 20.8 GiB free whose prep had just cleaned it.
# test_the_tmpfs_floor_covers_what_a_run_actually_writes pins both copies.
_TMPFS_FLOOR_KB = 10538188


def _tmpfs_with_room(path="/dev/shm"):
    """True when `path` is a writable tmpfs with ci.sh's floor free. Separate from
    ci_environment so a test can state the machine condition instead of inheriting
    it -- this developer box runs at 1.4 GiB free, below the floor, so a test that
    read the real filesystem could never exercise the branch that matters (measured:
    both mutations of the resolver survived until this seam existed)."""
    # hasattr, not try/except OSError: on Windows os.statvfs does not EXIST, so the call
    # raises AttributeError and an `except OSError` misses it entirely
    # (test_no_product_module_calls_a_posix_only_os_function_unguarded reddens on it).
    # No statvfs also means no /dev/shm, so False is the right answer there anyway.
    if not hasattr(os, "statvfs"):
        return False
    if not (os.path.isdir(path) and os.access(path, os.W_OK)):
        return False
    try:
        st = os.statvfs(path)
    except OSError:
        return False
    return st.f_bavail * st.f_frsize // 1024 >= _TMPFS_FLOOR_KB


def ci_environment(env=None, *, tmpfs_has_room=None):
    """ci.sh's execution ENVIRONMENT, not just its argv.

    Matching the pytest flags is not the same job as running the same job. `ci.sh`
    exports TMPDIR=/dev/shm when a tmpfs has room, and that changes where every
    `tmp_path` lands -- the install family copies a ~28MB tree per test. Measured on a
    20-core remote, same tree and commit, harness suite: bare argv 90.82-97.23s with a
    7.1% spread, through ci.sh 84.35-84.48s with a 0.2% spread. The drift gate
    (test_strategy_matches_ci_sh) compares flags only and was green across all of it."""
    env = dict(os.environ if env is None else env)
    if tmpfs_has_room is None:
        tmpfs_has_room = _tmpfs_with_room()
    if not env.get("TMPDIR") and tmpfs_has_room:
        env["TMPDIR"] = "/dev/shm"
    return env


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    strategy = load_strategy(args.strategy)

    if args.targets:
        cmd = build_targets_argv("harness", strategy, args.targets)
        if args.dry:
            cmd = cmd + ["--collect-only"]
        print(f"$ {' '.join(cmd)}")
        result = subprocess.run(cmd, env=ci_environment())
        return result.returncode

    if args.all:
        profiles = list(load_all_profiles(args.strategy))
    else:
        profiles = [profile for flag, profile in _FLAG_TO_PROFILE.items() if getattr(args, flag)]

    exit_code = 0
    for profile in profiles:
        cfg = strategy[profile]
        # `allow_empty` suites (packs/*/tests -- no pack has shipped one yet) may not
        # even exist on disk. A bare pytest on a missing path exits 4 ("not found"),
        # which is not "0 tests, fine" -- it is "nothing to run against" -- so this is
        # checked BEFORE shelling out rather than inferred from a subprocess result.
        if cfg.get("allow_empty") and not Path(cfg["path"]).is_dir():
            print(f"$ (skipped) {profile}: {cfg['path']} does not exist yet -- "
                  f"treating as an empty suite")
            continue
        cmd = build_argv(profile, strategy)
        if args.dry:
            cmd = cmd + ["--collect-only"]
        print(f"$ {' '.join(cmd)}")
        result = subprocess.run(cmd, env=ci_environment())
        # pytest's own NO_TESTS_COLLECTED (5) on a suite declared `allow_empty` means
        # the directory exists but nothing has landed in it yet -- still not a failure.
        # A profile without allow_empty keeps treating this exit code as a real failure.
        if result.returncode == 5 and cfg.get("allow_empty"):
            print(f"{profile}: 0 tests collected under {cfg['path']} -- allowed empty suite")
            continue
        if result.returncode != 0:
            exit_code = result.returncode
            break
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
