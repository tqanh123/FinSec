"""vnstock is pinned only in the `capture` extra and installs only through the hash-pinned lock (plan 261022-1147, phase 1).

Reads `pyproject.toml`, `requirements/vnstock/pyproject.toml` and `requirements/vnstock/win-py313.lock` as text or with
`tomllib`. Nothing is resolved or installed: no network, no clock, no subprocess.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PROJECT = REPO_ROOT / "requirements" / "vnstock" / "pyproject.toml"
LOCK_FILE = REPO_ROOT / "requirements" / "vnstock" / "win-py313.lock"

VENDOR_INDEX = "https://vnstocks.com/api/packages"
PYPI_INDEX = "https://pypi.org/simple"
# vnai and vnstock3 carry the 4.x agent-file writer (research vnstock-gen5.md §3); the other two are third-party name squats.
FORBIDDEN_LOCK_NAMES = frozenset({"vnai", "vnstock3", "vnstock-installer", "vnstock-mcp"})

PIN = re.compile(r"^vnstock==(\d+)\.(\d+)\.(\d+)$")
LOCK_BLOCK_START = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+?)\s*\\?$")
HASH_OPTION = re.compile(r"--hash=(\S+)")
SHA256_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
FROM_ANNOTATION = re.compile(r"^\s*#\s*from\s+(\S+)\s*$")


def _normalized(name: str) -> str:
    """PEP 503 name normalization: runs of "-", "_" and "." become "-", lower case."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    """The normalized project name of a PEP 508 requirement string (the text before any extra, version, marker or URL)."""
    return _normalized(re.split(r"[\s\[<>=!~;@(]", requirement.strip(), maxsplit=1)[0])


def _load_toml(path: Path) -> dict[str, Any]:
    # TOML values stay untyped (Any) until each test checks the keys it needs.
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _capture_pin() -> str:
    """The single `vnstock==X.Y.Z` requirement of the repo's `capture` extra."""
    extras = _load_toml(REPO_ROOT / "pyproject.toml")["project"].get("optional-dependencies", {})
    assert "capture" in extras, "pyproject.toml has no `capture` extra"
    pins = [req.strip() for req in extras["capture"] if _requirement_name(req) == "vnstock"]
    assert len(pins) == 1, f"the capture extra must hold exactly one vnstock requirement, got {pins}"
    assert PIN.match(pins[0]), f"vnstock must be pinned with == to an exact X.Y.Z version, got {pins[0]!r}"
    return pins[0]


def _lock_blocks() -> dict[str, dict[str, Any]]:
    """`name -> {"version", "hashes", "indexes"}` for every `name==version` block of the lock, in a line-state machine.

    A line `name==version [\\]` opens a block; `--hash=` options and `# from <url>` annotations belong to the open block,
    wherever uv puts them among its `# via` comments.
    """
    blocks: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line in LOCK_FILE.read_text(encoding="utf-8").splitlines():
        start = LOCK_BLOCK_START.match(line)
        if start:
            name = _normalized(start.group(1))
            assert name not in blocks, f"{name} appears twice in the lock"
            current = blocks[name] = {"version": start.group(2), "hashes": [], "indexes": []}
            continue
        if current is None:
            continue  # the uv header comment
        current["hashes"] += HASH_OPTION.findall(line)
        annotation = FROM_ANNOTATION.match(line)
        if annotation:
            current["indexes"].append(annotation.group(1))
    return blocks


def test_vnstock_is_pinned_only_in_the_capture_extra() -> None:
    project = _load_toml(REPO_ROOT / "pyproject.toml")["project"]
    elsewhere = [f"dependencies: {req}" for req in project.get("dependencies", []) if _requirement_name(req) == "vnstock"]
    for extra, requirements in project.get("optional-dependencies", {}).items():
        if extra != "capture":
            elsewhere += [f"{extra}: {req}" for req in requirements if _requirement_name(req) == "vnstock"]
    assert not elsewhere, f"vnstock must appear only in the capture extra, also found in {elsewhere}"
    _capture_pin()


def test_vnstock_lock_is_hash_pinned_and_split_by_index() -> None:
    assert LOCK_FILE.is_file(), f"{LOCK_FILE.relative_to(REPO_ROOT).as_posix()} is missing; compile it (AGENTS.md)"
    blocks = _lock_blocks()
    assert "vnstock" in blocks, "the lock has no vnstock block"
    problems: list[str] = []
    for name, block in blocks.items():
        if not block["hashes"]:
            problems.append(f"{name}: no --hash")
        problems += [f"{name}: malformed hash {value!r}" for value in block["hashes"] if not SHA256_HASH.match(value)]
        expected = VENDOR_INDEX if name == "vnstock" else PYPI_INDEX
        if block["indexes"] != [expected]:
            problems.append(f"{name}: index annotations {block['indexes']}, expected exactly [{expected!r}]")
    forbidden = sorted(FORBIDDEN_LOCK_NAMES & blocks.keys())
    if forbidden:
        problems.append(f"forbidden packages in the lock: {forbidden}")
    assert not problems, "\n".join(problems)
    pin = _capture_pin()
    assert f"vnstock=={blocks['vnstock']['version']}" == pin, f"lock vnstock {blocks['vnstock']['version']} != capture {pin}"
    assert blocks["vnstock"]["version"].split(".")[0] == "5", "only vnstock Gen 5 (major 5) may be locked"


def test_lock_project_routes_only_vnstock_to_the_vendor_index() -> None:
    assert LOCK_PROJECT.is_file(), f"{LOCK_PROJECT.relative_to(REPO_ROOT).as_posix()} is missing"
    lock_project = _load_toml(LOCK_PROJECT)
    uv = lock_project.get("tool", {}).get("uv", {})
    indexes = uv.get("index", [])
    vendor = [index for index in indexes if index.get("name") == "vnstocks"]
    assert len(vendor) == 1, f"expected one index named vnstocks, got {indexes}"
    assert vendor[0].get("url") == VENDOR_INDEX, vendor[0]
    # A non-explicit index would become a candidate for every package (dependency confusion).
    implicit = [index.get("name") for index in indexes if index.get("explicit") is not True]
    assert not implicit, f"every [[tool.uv.index]] must set explicit = true, these do not: {implicit}"
    sources = uv.get("sources")
    assert sources == {"vnstock": {"index": "vnstocks"}}, f"[tool.uv.sources] must route only vnstock: {sources}"
    assert lock_project["project"]["dependencies"] == [_capture_pin()]
    repo_tool = _load_toml(REPO_ROOT / "pyproject.toml").get("tool", {})
    assert "uv" not in repo_tool, "the repo pyproject.toml must not carry [tool.uv]"
