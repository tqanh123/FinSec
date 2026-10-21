"""A project's own tool (its vitest, jest, tsc, eslint): found where the project installed
it, run with every door to an installer shut.

Where it is looked for
    `node_modules/.bin/<bin>` in the unit's directory, then each parent up to the repo
    root -- never above it, never through a package manager (`pnpm exec`, `npx`, `yarn`
    and `npm exec` can install or download on the way: measured). Not found is a refusal
    naming every directory searched. A Yarn Plug'n'Play tree (`.pnp.cjs` on the way)
    has no `.bin` at all and is refused by name.
    Unlike `toolchain.find_tool` -- which refuses a binary inside the repo, right for the
    harness's own pinned tools -- this one runs the repo's code on purpose: a caller
    checks the repo is trusted first.
The environment it runs in
    `toolchain.child_env` (PATH without empty, relative or in-repo entries), then the
    `.bin` directories of the walk prepended (what `pnpm exec` does), and: `CI=1` (no
    snapshot is written on a first run), `COREPACK_ENABLE_NETWORK=0`,
    `pnpm_config_verify_deps_before_run=false` and `npm_config_yes=false` -- a test that
    itself calls pnpm or npx installs nothing.
"""
from __future__ import annotations

import os
import posixpath
from pathlib import Path

from . import toolchain

INSTALLER_GUARDS = {"CI": "1", "COREPACK_ENABLE_NETWORK": "0",
                    "pnpm_config_verify_deps_before_run": "false", "npm_config_yes": "false"}


def _walk(repo: Path, unit: str) -> "list[Path]":
    """The unit's directory and each parent up to `repo`, nearest first."""
    out = []
    d = posixpath.normpath(unit or ".")
    while True:
        out.append(repo / d if d != "." else repo)
        if d == ".":
            return out
        d = posixpath.dirname(d) or "."


def bin_dirs(repo: Path, unit: str) -> "list[Path]":
    return [d / "node_modules" / ".bin" for d in _walk(Path(repo), unit)]


def find_bin(repo: Path, unit: str, name: str) -> Path:
    import hs_family
    repo = Path(repo)
    pnp = next((d for d in _walk(repo, unit) if (d / ".pnp.cjs").is_file()), None)
    if pnp is not None:
        raise hs_family.RefusedError(
            "%s: Yarn Plug'n'Play (%s) keeps no node_modules/.bin to run %s from -- not supported; "
            "nothing run" % (unit, (pnp / ".pnp.cjs").relative_to(repo), name))
    dirs = bin_dirs(repo, unit)
    for d in dirs:
        p = d / name
        if p.is_file() and os.access(p, os.X_OK):
            return p
    raise hs_family.RefusedError(
        "%s is not installed for %s: no %s -- install the project's dependencies (its own "
        "install command), then run again; nothing run"
        % (name, unit, ", ".join(str(d.relative_to(repo) / name) for d in dirs)))


def child_env(repo: Path, unit: str) -> "dict[str, str]":
    env = toolchain.child_env(repo)
    bins = [str(d) for d in bin_dirs(Path(repo), unit) if d.is_dir()]
    env["PATH"] = os.pathsep.join(bins + [p for p in env["PATH"].split(os.pathsep) if p])
    env.update(INSTALLER_GUARDS)
    return env
