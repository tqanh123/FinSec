"""Repository hygiene (roadmap §7 Phase 0b.4): anchored ignore rules, pinned and localhost-only services, no secrets.

Text-level checks of `.gitignore`, `docker-compose.yml`, `.env.example`, `.agents/mcp_config.json` and `Makefile`
(no YAML parser: PyYAML is not a declared dependency), plus `git check-ignore` on file paths. No network, no clock.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _lines(name: str) -> List[str]:
    return (REPO_ROOT / name).read_text(encoding="utf-8").splitlines()


def _ignored(path: str) -> bool:
    """`git check-ignore -q` on a FILE path: exit 0 = ignored, 1 = not ignored, anything else fails the test.

    Only file paths are asked: with core.autocrlf=true a path ending in "/" can match the empty pattern git makes of
    a CRLF blank line (AGENTS.md "Verified Facts", `.gitignore`).
    """
    result = subprocess.run(
        ["git", "check-ignore", "-q", path], cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode not in (0, 1):
        pytest.fail(f"git check-ignore failed with exit {result.returncode} for {path!r}: {result.stderr.strip()}")
    return result.returncode == 0


def test_gitignore_patterns_are_anchored() -> None:
    patterns = [line.strip() for line in _lines(".gitignore")]
    unanchored = [pattern for pattern in patterns if pattern in ("data/", "models/")]
    assert not unanchored, f".gitignore still ignores every directory named {unanchored}; anchor them as /data/ and /models/"
    assert "/data/" in patterns and "/models/" in patterns


def test_src_models_package_is_not_ignored() -> None:
    assert not _ignored("src/finsec_alpharadar/models/x.py"), "a models/ package under src/ would silently stay out of git"


@pytest.mark.parametrize(
    "path",
    [
        "capture/raw/2026-10-21.jsonl.gz",
        "research/x.parquet",
        "warehouse.duckdb",
        "ops.sqlite",
        "ops.db",
        "capture/data/2026-10-21.jsonl.gz",  # a nested data dir is no longer caught by the anchored /data/ (red-team RT12)
    ],
)
def test_data_artefacts_are_ignored(path: str) -> None:
    # The repository is public, and a pushed copy of licensed vnstock data cannot be taken back out of the history.
    assert _ignored(path), f"{path} would be committable; data artefacts must be ignored anywhere in the tree"


def test_compose_has_no_floating_tags_or_redpanda() -> None:
    lines = _lines("docker-compose.yml")
    assert not [line for line in lines if ":latest" in line], "pin every image tag"
    assert not [line for line in lines if "redpanda" in line.lower()], "Redpanda was removed (decision 9.9)"
    assert not [line for line in lines if re.match(r"^version\s*:", line)], "the compose 'version' key is obsolete"


def test_compose_ports_bind_localhost() -> None:
    entries: List[str] = []
    ports_indent = None
    for line in _lines("docker-compose.yml"):
        indent = len(line) - len(line.lstrip())
        if line.strip() == "ports:":
            ports_indent = indent
            continue
        if ports_indent is not None and line.strip() and indent <= ports_indent:
            ports_indent = None
        if ports_indent is not None and line.strip().startswith("- "):
            entries.append(line.strip()[2:].strip().strip("\"'"))
    assert entries, "no published port found: the check would pass vacuously"
    public = [entry for entry in entries if not entry.startswith("127.0.0.1:")]
    assert not public, f"ports published on every interface: {public}; bind them to 127.0.0.1"


def test_compose_password_comes_from_env() -> None:
    values = [
        line.split(":", 1)[1].strip()
        for line in _lines("docker-compose.yml")
        if line.strip().startswith("POSTGRES_PASSWORD:")
    ]
    assert values, "docker-compose.yml no longer sets POSTGRES_PASSWORD"
    literal = [value for value in values if not re.fullmatch(r"\$\{POSTGRES_PASSWORD:\?[^}]+\}", value)]
    assert not literal, f"POSTGRES_PASSWORD must come from the environment and fail when unset, got {literal}"


def test_env_example_has_no_private_key_or_inline_password() -> None:
    lines = _lines(".env.example")
    assert not [line for line in lines if "SSI_PRIVATE_KEY_PATH" in line], "FC Data needs no private key"
    dsn = [line for line in lines if line.startswith("TIMESCALE_DSN=")]
    assert dsn and not [line for line in dsn if re.search(r"://[^:@/]+:[^@]+@", line)], "no password inside TIMESCALE_DSN"
    assert "SSI_API_URL=https://fc-data.ssi.com.vn/v2.0/Market" in lines
    assert "SSI_STREAM_URL=https://fc-datahub.ssi.com.vn/v2.0" in lines


def test_mcp_servers_are_pinned() -> None:
    servers = json.loads((REPO_ROOT / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "wandb" not in servers, "the wandb entry has no installable, signed release"
    problems: List[str] = []
    for name, entry in servers.items():
        args = [str(arg) for arg in entry.get("args") or []]
        problems += [f"{name}: {arg} floats" for arg in args if arg.endswith("@latest")]
        pinned = any(re.search(r"==\d", arg) or re.search(r"@[0-9a-f]{40}$", arg) for arg in args)
        if entry.get("command") == "uvx" and not pinned:
            problems.append(f"{name}: no '==<version>' or '@<40-hex commit>' in {args}")
        if entry.get("command") == "npx" and not any(re.search(r"@\d+\.\d+\.\d+$", arg) for arg in args):
            problems.append(f"{name}: no '@<x.y.z>' package version in {args}")
    assert not problems, "\n".join(problems)


def _recipe(target: str) -> List[str]:
    """The recipe lines (tab-indented) of one Makefile target."""
    lines = _lines("Makefile")
    start = lines.index(f"{target}:")
    recipe: List[str] = []
    for line in lines[start + 1:]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip())
    return recipe


def test_makefile_binds_localhost() -> None:
    run_api = " ".join(_recipe("run-api"))
    assert "--host 127.0.0.1" in run_api and "0.0.0.0" not in run_api, run_api
    assert "python -m pytest" in " ".join(_recipe("test")), "make test must run pytest through the interpreter"
