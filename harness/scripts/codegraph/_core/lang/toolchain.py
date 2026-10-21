"""The tools outside Python an engine runs: `node`/`npm` found safely, and the pinned
npm packages the harness installs once per user.

Where a tool is looked for
    `HARNESS_NODE` (an absolute path) names `node` outright. Otherwise PATH is searched
    with every entry that is empty, `.`, relative, or inside the repo being measured
    dropped: a repo nobody trusted must not get to stand in for `node` by shipping one
    in `node_modules/.bin` or its root. A found path inside the repo is refused, and a
    hit is kept only when it sits in the entry it was searched in (Windows' `which`
    looks in the current directory first).

Where a package lives
    `<lang-tools>/<name>-<version>/node_modules/<name>`, `<lang-tools>` being
    `HARNESS_LANG_TOOLS_DIR` (absolute) or `<engine home>/lang-tools` -- shared by every
    project of this user, never the bin (read-only under a global install), never a
    repo. A package counts as installed when its `package.json` says the pinned version
    and npm's own `node_modules/.package-lock.json` carries the pinned integrity.
"""
from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shutil
import subprocess
from pathlib import Path
from typing import Mapping

from .base import LanguageError, NpmPackage
from .errors import AdapterRefused

ENV_TOOLS_DIR = "HARNESS_LANG_TOOLS_DIR"
ENV_NODE = "HARNESS_NODE"
_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def _pathmod(os_name: str):
    return ntpath if os_name == "nt" else posixpath


def _inside(entry: str, repo: str, os_name: str) -> bool:
    mod = _pathmod(os_name)
    real = os.path.realpath if os_name == os.name else (lambda p: p)
    e, r = mod.normcase(mod.normpath(real(entry))), mod.normcase(mod.normpath(real(repo)))
    try:
        return mod.commonpath([e, r]) == r
    except ValueError:   # another drive
        return False


def usable_path_entries(path: str, repo: "Path | str | None", os_name: str) -> "list[str]":
    """The entries of `path` a tool may be found in, in order (see the module doc)."""
    mod = _pathmod(os_name)
    out = []
    for entry in path.split(";" if os_name == "nt" else ":"):
        if not entry or entry == "." or not mod.isabs(entry):
            continue
        if repo is not None and _inside(entry, str(repo), os_name):
            continue
        out.append(entry)
    return out


def find_tool(name: str, repo: "Path | str | None", path: "str | None" = None,
              env: "Mapping[str, str] | None" = None) -> "Path | None":
    """The absolute path of `name` (`node`, `npm`) to run, or None. Raises
    `AdapterRefused` for an override that is relative or inside the repo."""
    env = os.environ if env is None else env
    override = env.get(ENV_NODE) if name == "node" else None
    if override:
        if not os.path.isabs(override):
            raise AdapterRefused("%s=%s must be an absolute path" % (ENV_NODE, override))
        if repo is not None and _inside(override, str(repo), os.name):
            raise AdapterRefused("%s=%s lies inside the repo %s; a tool the repo ships is not run"
                                 % (ENV_NODE, override, repo))
        return Path(override)
    for entry in usable_path_entries(env.get("PATH", "") if path is None else path, repo, os.name):
        hit = shutil.which(name, path=entry)
        if hit and os.path.normcase(os.path.dirname(os.path.abspath(hit))) == \
                os.path.normcase(os.path.abspath(entry)):
            return Path(os.path.abspath(hit))
    return None


def child_env(repo: "Path | str | None" = None) -> "dict[str, str]":
    """The environment a tool runs in: this one, its PATH cut to the entries a tool may
    be found in (`usable_path_entries`: no empty, `.`, relative or in-`repo` entry --
    npm's `#!/usr/bin/env node` searches it too), and Windows told not to search the
    current directory for the commands the tool itself starts."""
    path = os.pathsep.join(usable_path_entries(os.environ.get("PATH", ""), repo, os.name))
    return {**os.environ, "PATH": path, "NoDefaultCurrentDirectoryInExePath": "1"}


def node_version(node: Path) -> "tuple[int, int, int] | None":
    import process_group
    try:
        out = process_group.run([str(node), "--version"], capture_output=True, text=True, timeout=30,
                                env=child_env()).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = _VERSION.search(out)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _floor(spec: str) -> "tuple[int, int, int]":
    parts = [int(x) for x in spec[2:].split(".")] + [0, 0]
    return parts[0], parts[1], parts[2]


def node_problems(spec: str, repo: "Path | str | None", path: "str | None" = None,
                  env: "Mapping[str, str] | None" = None) -> "list[str]":
    """[] when a `node` meeting `spec` (`>=X.Y`) is found, else what is wrong."""
    try:
        node = find_tool("node", repo, path, env)
    except AdapterRefused as e:
        return [str(e)]
    if node is None:
        return ["Node.js (`node` %s) -- none on PATH (entries that are empty, `.`, relative or "
                "inside the repo are not searched), and %s is not set" % (spec, ENV_NODE)]
    have = node_version(node)
    if have is None:
        return ["Node.js (`node` %s) -- %s does not answer `--version`" % (spec, node)]
    if have < _floor(spec):
        return ["Node.js %s -- %s is %s" % (spec, node, ".".join(map(str, have)))]
    return []


def tools_root(env: "Mapping[str, str] | None" = None) -> Path:
    env = os.environ if env is None else env
    raw = env.get(ENV_TOOLS_DIR)
    if raw:
        if not os.path.isabs(raw):
            raise AdapterRefused("%s=%s must be an absolute path" % (ENV_TOOLS_DIR, raw))
        return Path(raw)
    import harness_paths
    return harness_paths.engine_home() / "lang-tools"


def package_dir(pkg: NpmPackage, root: "Path | None" = None) -> Path:
    """The directory `pkg` is installed in (its own npm prefix)."""
    return (tools_root() if root is None else root) / ("%s-%s" % (pkg.name.replace("/", "+"), pkg.version))


def module_dir(pkg: NpmPackage, root: "Path | None" = None) -> Path:
    return package_dir(pkg, root) / "node_modules" / pkg.name


def installed_problems(pkg: NpmPackage, prefix: Path) -> "list[str]":
    """[] when `prefix` holds `pkg` at its pinned version and integrity."""
    want = "%s@%s" % (pkg.name, pkg.version)
    try:
        version = json.loads((prefix / "node_modules" / pkg.name / "package.json")
                             .read_text(encoding="utf-8")).get("version")
        lock = json.loads((prefix / "node_modules" / ".package-lock.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["the npm package %s, not installed under %s" % (want, prefix)]
    integrity = ((lock.get("packages") or {}).get("node_modules/" + pkg.name) or {}).get("integrity")
    if version != pkg.version:
        return ["the npm package %s; %s holds version %s" % (want, prefix, version)]
    if integrity != pkg.integrity:
        return ["the npm package %s; %s was installed with integrity %s, the book pins %s"
                % (want, prefix, integrity, pkg.integrity)]
    return []


def npm_problems(pkg: NpmPackage) -> "list[str]":
    try:
        return installed_problems(pkg, package_dir(pkg))
    except (AdapterRefused, LanguageError) as e:
        return [str(e)]
