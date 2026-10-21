"""Every path-shaped backtick token in AGENTS.md (ending in .py/.md/.yaml/.yml/.json/.toml/.sh or /) must exist.

Reads AGENTS.md and asks `git check-ignore` about paths that are absent here; no network, no clock.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_MD = REPO_ROOT / "AGENTS.md"
PACKAGE_ROOT = REPO_ROOT / "src" / "finsec_alpharadar"

CODE_SPAN = re.compile(r"`([^`\n]+)`")
LINE_SUFFIX = re.compile(r":\d[\d,\-]*$")
PATH_TOKEN = re.compile(r"^[\w.\-/]+(\.(py|md|yaml|yml|json|toml|sh)|/)$")

# Path-shaped tokens that AGENTS.md does not claim exist, with the reason.
NOT_PATHS: dict[str, str] = {
    "data/": "package name that the Package names rule forbids, not a path",
    "models/": "package name that the Package names rule forbids, not a path",
    "modeling/": "planned package (roadmap section 5.2), not created yet",
}

# Short forms that AGENTS.md uses, mapped to the repository path.
ALIASES: dict[str, str] = {
    # AGENTS.md names only the directory where the harness installer stashes the default-off skills.
    "disabled-skills/": "harness/plugins/hs/disabled-skills/",
}


def _path_tokens(text: str) -> list[str]:
    """Backtick tokens without whitespace that look like repository paths, with any `:<line>` suffix removed."""
    tokens: list[str] = []
    for span in CODE_SPAN.findall(text):
        if any(char.isspace() for char in span):
            continue
        token = LINE_SUFFIX.sub("", span)
        if token.startswith(("http", "$", "/")) or not PATH_TOKEN.match(token):
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def _exists(path: str) -> bool:
    """Look in the repo root, then src/finsec_alpharadar/, then (bare names only) src/finsec_alpharadar/*/<name>."""
    if (REPO_ROOT / path).exists() or (PACKAGE_ROOT / path).exists():
        return True
    return "/" not in path and any(PACKAGE_ROOT.glob(f"*/{path}"))


def _is_ignored(path: str) -> bool:
    """True when git ignores the path: a runtime path that each clone creates (harness/state/, .venv/).

    `git check-ignore` exits 0 on a match and 1 when nothing matches; any other exit means git failed, and the test
    fails with git's stderr. The `-v` output is read because a match does not always mean "ignored"
    (OBSERVED 2026-10-21, Git for Windows 2.55):
    - `-v` also reports negated ("!") patterns, which un-ignore a path;
    - with core.autocrlf=true the working-tree .gitignore has CRLF line endings, and git reads its blank lines as
      an empty pattern that matches every path ending in "/", so `-q` alone would skip any missing directory token.
    """
    result = subprocess.run(
        ["git", "check-ignore", "-v", "-z", "--stdin"],
        cwd=REPO_ROOT,
        input=f"{path}\0",
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode == 1:
        return False
    if result.returncode != 0:
        pytest.fail(f"git check-ignore failed with exit {result.returncode} for {path!r}: {result.stderr.strip()}")
    pattern = result.stdout.split("\0")[2]  # -z fields: source, line number, pattern, path
    return bool(pattern) and not pattern.startswith("!")


def test_agents_md_paths_exist() -> None:
    missing = [
        token
        for token in _path_tokens(AGENTS_MD.read_text(encoding="utf-8"))
        if token not in NOT_PATHS
        and not _exists(ALIASES.get(token, token))
        and not _is_ignored(ALIASES.get(token, token))
    ]
    assert not missing, (
        "AGENTS.md names paths that do not exist; create them, fix the text, or add the token to NOT_PATHS or "
        "ALIASES with a reason: " + ", ".join(missing)
    )
