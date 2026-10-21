#!/usr/bin/env python3
"""detect_techstack — read-only tech-stack profile of a target repo.

The harness installs INTO other repos (`sh install.sh <tarball> <repo>`) but its
verification skills default to pytest. This detector lets a skill adapt to the
target's actual stack: it scans a repo root for build/dependency markers and
returns {languages, test command, package manager, CI} — observing the repo,
never mutating it. The read-only Shape-A self-learning surface consumed by
hs:setup / hs:test / hs:insights.

Detection is presence-based (a marker file's existence) and fail-soft: a missing
or unreadable root yields an empty profile rather than raising — a detector that
crashes the host is worse than one that finds nothing. The package manager is
read from the lockfile/marker, not guessed; the test command is the conventional
one for the stack (and for Node, only when a `scripts.test` is actually declared).

CLI:
    detect_techstack.py [--root <dir>]   # prints the JSON profile, exit 0
"""
import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _read(path: Path) -> str:
    """Best-effort text read; unreadable → empty (fail-soft, never raises)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _python_stack(root: Path) -> Optional[Dict[str, Any]]:
    marker = next((m for m in ("pyproject.toml", "setup.py", "setup.cfg",
                               "requirements.txt", "Pipfile")
                   if (root / m).is_file()), None)
    if not marker:
        return None
    # pkg-mgr from a real [tool.poetry] TABLE header (line-anchored), never a
    # commented-out or prose mention — the docstring promises the marker is read, not
    # guessed, so a migration-note comment must not override an actual uv.lock below.
    _pyproject = _read(root / "pyproject.toml") if (root / "pyproject.toml").is_file() else ""
    _has_poetry = any(s == "[tool.poetry]" or s.startswith("[tool.poetry.")
                      for s in (ln.strip() for ln in _pyproject.splitlines()))
    pkg_mgr = "pip"
    if _has_poetry:
        pkg_mgr = "poetry"
    elif (root / "uv.lock").is_file():
        pkg_mgr = "uv"
    elif (root / "Pipfile").is_file():
        pkg_mgr = "pipenv"
    return {"language": "python", "marker": marker, "test_cmd": "pytest", "pkg_mgr": pkg_mgr,
            "test_exts": (".py",)}


def _node_stack(root: Path) -> Optional[Dict[str, Any]]:
    if not (root / "package.json").is_file():
        return None
    language = "typescript" if (root / "tsconfig.json").is_file() else "javascript"
    if (root / "pnpm-lock.yaml").is_file():
        pkg_mgr = "pnpm"
    elif (root / "yarn.lock").is_file():
        pkg_mgr = "yarn"
    elif (root / "bun.lockb").is_file():
        pkg_mgr = "bun"
    else:
        pkg_mgr = "npm"
    # test_cmd only when a test script is actually declared (honest: don't invent one)
    test_cmd = None
    try:
        pkg = json.loads(_read(root / "package.json"))
        if isinstance(pkg, dict) and isinstance(pkg.get("scripts"), dict) \
                and pkg["scripts"].get("test"):
            test_cmd = f"{pkg_mgr} test"
    except (ValueError, TypeError):
        pass  # malformed package.json → stack still detected, just no test_cmd
    return {"language": language, "marker": "package.json",
            "test_cmd": test_cmd, "pkg_mgr": pkg_mgr,
            "test_exts": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")}


def _go_stack(root: Path) -> Optional[Dict[str, Any]]:
    if not (root / "go.mod").is_file():
        return None
    return {"language": "go", "marker": "go.mod",
            "test_cmd": "go test ./...", "pkg_mgr": "go", "test_exts": (".go",)}


def _rust_stack(root: Path) -> Optional[Dict[str, Any]]:
    if not (root / "Cargo.toml").is_file():
        return None
    return {"language": "rust", "marker": "Cargo.toml",
            "test_cmd": "cargo test", "pkg_mgr": "cargo", "test_exts": (".rs",)}


def _java_stack(root: Path) -> Optional[Dict[str, Any]]:
    if (root / "pom.xml").is_file():
        return {"language": "java", "marker": "pom.xml",
                "test_cmd": "mvn test", "pkg_mgr": "maven", "test_exts": (".java",)}
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        marker = "build.gradle" if (root / "build.gradle").is_file() else "build.gradle.kts"
        return {"language": "java", "marker": marker,
                "test_cmd": "gradle test", "pkg_mgr": "gradle", "test_exts": (".java",)}
    return None


# Fixed precedence — also the order `primary` is chosen by.
_DETECTORS = (_python_stack, _node_stack, _go_stack, _rust_stack, _java_stack)


def _detect_ci(root: Path) -> List[str]:
    ci: List[str] = []
    wf = root / ".github" / "workflows"
    if wf.is_dir() and any(wf.iterdir()):
        ci.append("github-actions")
    if (root / ".gitlab-ci.yml").is_file():
        ci.append("gitlab-ci")
    if (root / ".circleci" / "config.yml").is_file():
        ci.append("circleci")
    return ci


def scoped_test_command(stack: Optional[Dict[str, Any]], targets: List[str]) -> Optional[str]:
    """`stack`'s own `test_cmd`, narrowed to just `targets` in that stack's
    OWN idiom -- the one place a caller building a "run just these paths"
    command gains target arguments, so a targeted-test consumer (a defer_
    suite debt-repay message, and `test`/`fix --scope`) never invents a
    second, hardcoded runner. A caller building a scoped
    command from a detected stack goes through here; it never assembles
    `"python3 -m pytest %s" % ...` (or any other runner literal) itself --
    that was the exact defect this function replaces (measured: a Node
    target got a `python3 -m pytest` command it could not run at all).

    `None` when there is nothing to narrow: no `stack` (nothing detected),
    no declared `test_cmd` on it (this module's own "don't invent one"
    contract, e.g. a Node repo with no `scripts.test`), or an empty
    `targets` list (the caller's own concern -- this never guesses "run
    everything" from an empty target list).

    Each stack's own targeting idiom (not exhaustive of every tool that
    stack could use, but the CONVENTIONAL one for its declared `test_cmd`):
    python (pytest) and rust (cargo test) take file/filter args directly;
    node's package-manager `test` script forwards args after `--`; go
    takes its own package-path args verbatim (never file paths); maven
    takes a comma-joined `-Dtest=`; gradle takes one `--tests` per target.
    An unrecognized language still gets a best-effort space-joined append
    rather than silently keeping the unscoped command — this function's own
    job is to NEVER leave a caller holding an unscoped command when targets
    were named."""
    if not stack:
        return None
    test_cmd = stack.get("test_cmd")
    if not test_cmd or not targets:
        return None
    quoted = " ".join(shlex.quote(str(t)) for t in targets)
    language = stack.get("language")
    if language in ("python", "rust"):
        return "%s %s" % (test_cmd, quoted)
    if language in ("javascript", "typescript"):
        return "%s -- %s" % (test_cmd, quoted)
    if language == "go":
        return "go test %s" % quoted
    if language == "java":
        if stack.get("pkg_mgr") == "maven":
            return "mvn test -Dtest=%s" % ",".join(str(t) for t in targets)
        return "gradle test %s" % " ".join(
            "--tests %s" % shlex.quote(str(t)) for t in targets)
    return "%s %s" % (test_cmd, quoted)


def route_targets_to_stacks(
        stacks: List[Dict[str, Any]],
        targets: List[str]) -> "tuple[List[tuple], List[str]]":
    """Assign each of `targets` to the ONE stack in `stacks` that owns its
    file extension, never by POSITION (`stacks[0]`, `stacks[-1]`) -- a
    polyglot repo (Python + Node markers both present) means neither end
    of that list is a safe default, and a fixed index was the actual
    defect this replaces (measured: a `.spec.js` target in a repo whose
    `pyproject.toml` sorts first by detector precedence got handed
    `pytest ... thing.spec.js`, which cannot run).

    Ownership comes from each detector's OWN `test_exts` field (declared
    once, in `_python_stack`/`_node_stack`/`_go_stack`/`_rust_stack`/
    `_java_stack`'s own return dict) -- not a second lookup table kept
    here or anywhere else. `standards_graph.py`'s `_EXT_LANG` (extension ->
    review-scope language label) was read before writing this: it answers
    a DIFFERENT question and cannot be reused as-is -- a Node stack is ONE
    entry here regardless of `language` being `"javascript"` or
    `"typescript"`, so a `.js` target must still resolve to that same
    entry even when the stack's own language field says `"typescript"`
    (a repo with both a `tsconfig.json` and a `.spec.js` test file), which
    `_EXT_LANG`'s per-extension labels cannot express without themselves
    growing a second js/ts-merging table beside this one.

    Returns `(groups, unmatched)`: `groups` is `[(stack, [target, ...]),
    ...]` in first-target-seen order, each stack listed at most once and
    only when it owns >=1 target; `unmatched` is every target whose
    extension no declared stack claims -- named explicitly, never folded
    into whichever stack happens to be present."""
    groups: "list[tuple[Dict[str, Any], List[str]]]" = []
    owned_by_id: "dict[int, List[str]]" = {}
    unmatched: List[str] = []
    for target in targets:
        suffix = Path(str(target)).suffix
        owner = next((s for s in stacks if suffix in (s.get("test_exts") or ())), None)
        if owner is None:
            unmatched.append(str(target))
            continue
        key = id(owner)
        if key not in owned_by_id:
            owned_by_id[key] = []
            groups.append((owner, owned_by_id[key]))
        owned_by_id[key].append(str(target))
    return groups, unmatched


def scoped_test_commands(
        stacks: List[Dict[str, Any]],
        targets: List[str]) -> "tuple[List[str], List[str]]":
    """One `scoped_test_command` per stack that owns at least one of
    `targets` (via `route_targets_to_stacks`), plus every target no command
    covers: unclaimed by any stack, or claimed by a stack that declares no
    `test_cmd`. A polyglot target set (some `.py`, some `.spec.js`) yields
    ONE command per language, never a single command built from the wrong
    stack's `test_cmd`, and never a target dropped without being named."""
    groups, unrun = route_targets_to_stacks(stacks, targets)
    commands = []
    for stack, owned in groups:
        cmd = scoped_test_command(stack, owned)
        if cmd:
            commands.append(cmd)
        else:
            unrun.extend(owned)
    return commands, unrun


def detect(root) -> Dict[str, Any]:
    """Return the tech-stack profile of `root`. Never raises; missing → empty."""
    root = Path(root)
    if not root.is_dir():
        return {"root": str(root), "stacks": [], "ci": [], "primary": None}
    stacks = [s for s in (d(root) for d in _DETECTORS) if s]
    ci = _detect_ci(root)
    return {"root": str(root), "stacks": stacks, "ci": ci,
            "primary": stacks[0]["language"] if stacks else None}


def main(argv=None) -> int:
    try:
        from encoding_utils import configure_utf8_console
        configure_utf8_console()
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Read-only tech-stack profile of a repo.")
    ap.add_argument("--root", default=".", help="repo root to scan (default: cwd)")
    args = ap.parse_args(argv)
    print(json.dumps(detect(args.root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
