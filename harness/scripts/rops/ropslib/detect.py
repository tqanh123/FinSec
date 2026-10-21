"""Stack detection: destination dir, rsync excludes, command aliases.

Detection is a silent layer. Everything here is reported by `doctor` with its source
and written out to .rops.env on first run, so a wrong guess is visible rather than
merely wrong. ROPS_HOST is deliberately NOT detected.
"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Callable

# Never sync these regardless of .gitignore. Shipping .venv turns a 2s sync into
# minutes and can break the remote interpreter.
EXCLUDE_FLOOR = [".git/", ".venv/", "__pycache__/", "node_modules/",
                 ".mypy_cache/", ".pytest_cache/", ".ruff_cache/"]

REMOTE_ROOT = "~/rops"

#: A venv on the remote is named by the repo's own commands and NOWHERE else: this repo
#: says `.venv-ci`, the next says `.tox` or `env/`. Measured 260910 — a hardcoded name
#: protected this repo and would have destroyed the next one's venv, silently, one lane
#: rebuild at a time. Read the name where it is already declared.
_VENV_IN_PATH = re.compile(r"\$PWD/([A-Za-z0-9._-]+)(?:/[A-Za-z0-9._-]+)*/bin\b")


def venvs_in(values) -> list[str]:
    """Directory names that a ROPS_CMD_* puts on PATH as `$PWD/<name>/.../bin`.

    Ordered, de-duplicated, and empty when the commands name none — a repo running
    `uv run` has no such directory and inventing one would keep a stray forever.
    """
    items = values.items() if hasattr(values, "items") else []
    out: list[str] = []
    for key, val in items:
        if not str(key).startswith("ROPS_CMD_"):
            continue
        for m in _VENV_IN_PATH.finditer(str(val)):
            if m.group(1) not in out:
                out.append(m.group(1))
    return out



def remote_dir(repo: pathlib.Path) -> str:
    return f"{REMOTE_ROOT}/{repo.name}"


def excludes(repo: pathlib.Path) -> str:
    """Floor + .gitignore patterns git actually honours. Negations (!x) and comments
    are dropped: rsync has no equivalent, and passing one through would exclude a
    literal filename starting with '!'."""
    found = list(EXCLUDE_FLOOR)
    gi = repo / ".gitignore"
    if gi.is_file():
        for raw in gi.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "!")) or "," in line:
                continue  # a comma would split the CSV this ends up in
            if line not in found:
                found.append(line)
    return ",".join(found)


_MYPY_TARGETS = ("files", "modules", "packages")


def _mypy_config(repo: pathlib.Path) -> "dict | None":
    """The mypy section mypy itself reads here, or None when there is none. Measured
    (mypy 2.3): the first of mypy.ini and .mypy.ini (existence alone, `[mypy]` or
    not), pyproject.toml with [tool.mypy], setup.cfg with [mypy]; a file that does
    not parse is skipped for the next, as mypy does."""
    import configparser
    import tomllib

    def ini(path: pathlib.Path, section_required: bool) -> "dict | None":
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(path, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError):
            return None
        if cp.has_section("mypy"):
            return dict(cp.items("mypy"))
        return None if section_required else {}

    for name in ("mypy.ini", ".mypy.ini"):
        if (repo / name).is_file():
            conf = ini(repo / name, section_required=False)
            if conf is not None:
                return conf
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        try:
            section = tomllib.loads(pyproject.read_text(encoding="utf-8")) \
                .get("tool", {}).get("mypy")
        except (ValueError, UnicodeDecodeError, AttributeError):
            section = None
        if isinstance(section, dict):
            return section
    if (repo / "setup.cfg").is_file():
        return ini(repo / "setup.cfg", section_required=True)
    return None


def _pyproject_aliases(repo: pathlib.Path) -> dict[str, str]:
    text = (repo / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
    uv = (repo / "uv.lock").is_file() or "[tool.uv]" in text
    run = "uv run " if uv else ""
    out = {"ROPS_CMD_TEST": f"{run}python -m pytest -q"}
    if "[tool.ruff" in text:
        out["ROPS_CMD_LINT"] = f"{run}ruff check ."
    conf = _mypy_config(repo)
    if conf is not None or "mypy" in text:
        # A target on the command line replaces the config's own `files`/`packages`/
        # `modules`: `.` there re-reads every directory the config left out.
        # Exactly one: mypy refuses a config naming two ("May only specify one of").
        named = sum(bool((conf or {}).get(k)) for k in _MYPY_TARGETS) == 1
        out["ROPS_CMD_TYPECHECK"] = f"{run}python -m mypy" + ("" if named else " .")
    return out


def _package_json_aliases(repo: pathlib.Path) -> dict[str, str]:
    """Read the scripts block. A malformed package.json yields nothing rather than
    raising — detection failing loudly on someone else's broken file helps no one."""
    try:
        data = json.loads((repo / "package.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    scripts = data.get("scripts") or {}
    if not isinstance(scripts, dict):
        return {}
    # `bun.lockb` is checked before `npm`'s catch-all — a bun-only project has no
    # standing guarantee `npm run <script>` even reaches the same script runner.
    runner = "pnpm" if (repo / "pnpm-lock.yaml").is_file() else \
             "yarn" if (repo / "yarn.lock").is_file() else \
             "bun" if (repo / "bun.lockb").is_file() else "npm run"
    out = {}
    for name in ("test", "lint", "build", "typecheck"):
        if name in scripts:
            out[f"ROPS_CMD_{name.upper()}"] = f"{runner} {name}"
    return out


_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_.-]+):(?!=)")


def _makefile_aliases(repo: pathlib.Path) -> dict[str, str]:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        mk = repo / name
        if mk.is_file():
            break
    else:
        return {}
    targets = set()
    for raw in mk.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _MAKE_TARGET.match(raw)
        if m:
            targets.add(m.group(1))
    return {f"ROPS_CMD_{t.upper()}": f"make {t}"
            for t in ("test", "lint", "build", "check") if t in targets}


def aliases(repo: pathlib.Path) -> dict[str, str]:
    """First stack that matches wins per key; earlier sources are not overwritten."""
    out: dict[str, str] = {}
    # Explicit element type: otherwise mypy pins it to the first appended
    # function's signature, and the lambdas below differ in parameter name.
    sources: list[Callable[[pathlib.Path], dict[str, str]]] = []
    if (repo / "pyproject.toml").is_file():
        sources.append(_pyproject_aliases)
    if (repo / "package.json").is_file():
        sources.append(_package_json_aliases)
    if (repo / "go.mod").is_file():
        sources.append(lambda r: {"ROPS_CMD_TEST": "go test ./...",
                                  "ROPS_CMD_BUILD": "go build ./..."})
    if (repo / "Cargo.toml").is_file():
        sources.append(lambda r: {"ROPS_CMD_TEST": "cargo test",
                                  "ROPS_CMD_BUILD": "cargo build"})
    if (repo / "build.gradle.kts").is_file() or (repo / "build.gradle").is_file():
        # ./gradlew when the wrapper is committed — a bare `gradle` on the far side is
        # whatever version that machine happens to have, which is not this project's.
        g = "./gradlew" if (repo / "gradlew").is_file() else "gradle"
        sources.append(lambda r: {"ROPS_CMD_TEST": f"{g} test",
                                  "ROPS_CMD_BUILD": f"{g} build"})
    if (repo / "pom.xml").is_file():
        m = "./mvnw" if (repo / "mvnw").is_file() else "mvn"
        sources.append(lambda r: {"ROPS_CMD_TEST": f"{m} test",
                                  "ROPS_CMD_BUILD": f"{m} package"})
    sources.append(_makefile_aliases)
    for fn in sources:
        for k, v in fn(repo).items():
            out.setdefault(k, v)
    return out


def detect(repo: pathlib.Path) -> dict[str, str]:
    out = {
        "ROPS_REMOTE_DIR": remote_dir(repo),
        "ROPS_RSYNC_EXCLUDES": excludes(repo),
    }
    out.update(aliases(repo))
    return out
