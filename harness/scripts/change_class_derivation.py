#!/usr/bin/env python3
"""change_class_derivation.py — derive a change-class from CODE FACTS, never a
self-declared label.

derive(...) returns a `Derivation(cls, signals, ambiguous)` namedtuple (also
tuple-unpackable). The design rejects a fabricated confidence-% (a float the
harness invents, then gates on → circular + false precision): instead it returns
the SET of fact-signals that fired plus an `ambiguous` flag. Enforcement
downstream keys off SIGNAL AGREEMENT, not a number.

Hard rules:
  - a commit-message TYPE ("feat:"/"fix:") is 0% trusted — it is accepted as an
    argument purely so a caller can pass it, and is then IGNORED. It never moves
    the verdict.
  - src-only-no-test → refactor, ambiguous=True (soft-only; no differentiator).
  - src + an empty test (no assertions) does not lift to a hard feature.
  - an explicit override (HARNESS_CHANGE_CLASS env or a git trailer the caller
    extracts) WINS but is recorded as a `change_class_override` trace (actor+ts)
    so the bypass is git-visible.

A changed file is a test when the repo's code-map snapshot records it as one (the
role its language pack gave it -- a story is not a test); the naming heuristics
below cover every file no fresh snapshot holds.
"""

import os
import re
import sys
from collections import namedtuple
from pathlib import Path

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

Derivation = namedtuple("Derivation", ["cls", "signals", "ambiguous"])

# A change-class the override path will accept (a typo'd override must not
# silently become a real class).
_KNOWN_CLASSES = ("feature", "bugfix", "refactor", "dep_bump_major", "release")

# Test-file conventions across stacks: pytest (test_*.py / *_test.py / tests/),
# go (*_test.go), node (*.test.js / *.spec.ts / __tests__/), java (Surefire
# *Test/*Tests, Failsafe *IT/*ITCase — by filename, even outside a /test/ dir).
_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__)/|(?:^|/)test_[^/]+\.py$|_test\.(?:py|go)$"
    r"|\.(?:test|spec)\.[cm]?[jt]sx?$|(?:Test|Tests|IT|ITCase)\.java$")
# Manifests whose version-range edits signal a dependency bump.
_MANIFEST_NAMES = ("package.json", "pyproject.toml", "go.mod", "Cargo.toml",
                   "requirements.txt", "Pipfile", "setup.py", "setup.cfg")
# Non-code paths that are neither source nor test (docs/config) — they do not
# count as a `src_changed` signal on their own.
_NONCODE_RE = re.compile(r"\.(?:md|rst|txt|cfg|ini|toml|ya?ml|json|lock)$"
                         r"|(?:^|/)docs?/", re.IGNORECASE)


#: `derive(role=...)` not given: read the snapshot's roles itself
_UNREAD = object()


def _is_test_path(p: str, role=None) -> bool:
    """`role`: `_snapshot_roles(root)` -- its answer wins where it gives one."""
    decided = role(p) if role is not None else None
    if decided is not None:
        return decided == "test"
    return bool(_TEST_PATH_RE.search(p))


def test_path_predicate(root):
    """`path -> bool`: is it a test in the repo at `root` -- its snapshot's roles first,
    the naming heuristics for what no snapshot holds. Every caller asking "is this a test
    path" goes through here, so two of them cannot answer differently."""
    role = _snapshot_roles(root)
    return lambda p: _is_test_path(p, role)


def _snapshot_roles(root):
    """`rel -> "test" | "story" | None`: the `role` the repo's fresh code-map snapshot
    recorded for `rel`; None where it records none (a pack that names no roles -- the
    Python pack's ordinary modules -- or a path it does not hold).
    Read through `plan_graph.resolve_snapshot_units` -- the snapshot door that imports
    no `codegraph`. No fresh snapshot of HEAD: no role reader, and the naming
    heuristics decide; a derivation hiccup never blocks."""
    if root is None:
        return None
    try:
        import plan_graph
        units, _warning = plan_graph.resolve_snapshot_units(root)
    except Exception:  # noqa: BLE001 -- see the docstring
        return None
    if not units:
        return None

    def role(rel):
        return (units.get(rel) or {}).get("role") or None
    return role


def _is_manifest(p: str) -> bool:
    return Path(p).name in _MANIFEST_NAMES


def _has_src_change(diff_paths, role=None) -> bool:
    """True when a non-test, non-manifest, non-doc source file changed."""
    for p in diff_paths or []:
        if _is_test_path(p, role) or _is_manifest(p):
            continue
        if _NONCODE_RE.search(p):
            continue
        return True
    return False


def _trace_override(cls: str, source: str) -> None:
    try:
        hooks = str(Path(__file__).resolve().parent.parent / "hooks")
        if hooks not in sys.path:
            sys.path.append(hooks)
        import trace_log
        trace_log.append_event("change_class_derivation", "change_class_override",
                               target=cls, note="source=%s" % source)
    except Exception:  # noqa: BLE001 — tracing never breaks derivation
        pass


def _override_class(trailer_class):
    """The override class + its source, or (None, None). env wins over a caller-
    supplied git trailer; an unknown class is ignored (a typo must not open a
    new class)."""
    env = os.environ.get("HARNESS_CHANGE_CLASS")
    if env and env in _KNOWN_CLASSES:
        return env, "env:HARNESS_CHANGE_CLASS"
    if trailer_class and trailer_class in _KNOWN_CLASSES:
        return trailer_class, "git-trailer"
    return None, None


def derive(diff_paths, manifest_changed=False, tests_added=False,
           coverage_artifact=None, *,
           tests_have_assertions=None, regression_tests=False,
           manifest_version_changed=False, version_bump=False,
           tag_exists=False, net_code_change=True,
           commit_message=None, trailer_class=None, root=None, role=_UNREAD):
    """Derive (cls, signals, ambiguous) from change facts.

    diff_paths: changed file paths. manifest_changed: a manifest file changed.
    tests_added: a test file was added. tests_have_assertions: those tests carry
    real assertions (None = unknown → treated as not-asserting for safety).
    regression_tests: an added test is a regression test. manifest_version_-
    changed / version_bump / tag_exists / net_code_change: release/dep signals
    the caller extracts from git. coverage_artifact: a coverage report path (a
    real one is an extra agreement signal). commit_message: ACCEPTED AND
    IGNORED — labels are not facts.
    """
    # 1) explicit override wins, but is traced as a git-visible bypass.
    ov_cls, ov_src = _override_class(trailer_class)
    if ov_cls is not None:
        _trace_override(ov_cls, ov_src)
        return Derivation(ov_cls, ["override", ov_src], False)

    signals = []
    src_changed = _has_src_change(diff_paths, _snapshot_roles(root) if role is _UNREAD else role)
    if src_changed:
        signals.append("src_changed")
    if manifest_changed:
        signals.append("manifest_changed")
    if tests_added:
        signals.append("test_added")
    if tests_added and tests_have_assertions:
        signals.append("test_has_assertions")
    if regression_tests:
        signals.append("regression_test")
    if coverage_artifact:
        signals.append("coverage_artifact")

    # 2) release: a version bump on an existing tag with no net code change —
    # all fact-based, strong enough to gate hard.
    if version_bump and tag_exists and not net_code_change:
        return Derivation("release", signals + ["version_bump", "tag_exists"], False)

    # 3) dep-bump: a manifest changed AND its version range changed (fact-based).
    if manifest_changed and manifest_version_changed:
        return Derivation("dep_bump_major",
                          signals + ["manifest_version_changed"], False)

    # 4) bugfix: a regression test landed alongside a src change. Hard only when
    # the regression test is real (has assertions); else ambiguous → soft.
    if regression_tests and src_changed:
        ambiguous = not bool(tests_have_assertions)
        return Derivation("bugfix", signals, ambiguous)

    # 5) feature: src + a test that ACTUALLY asserts (two-signal agreement). A
    # test file with no assertions is not enough — stays ambiguous → soft.
    if src_changed and tests_added:
        if tests_have_assertions:
            return Derivation("feature", signals, False)
        return Derivation("feature", signals, True)  # empty test → soft

    # 6) refactor: src changed, no test signal at all → ambiguous, soft-only.
    if src_changed:
        return Derivation("refactor", signals, True)

    # 7) no source change (docs/config/manifest-only without a version edit) →
    # refactor-ambiguous so nothing hard-gates on a non-code edit.
    return Derivation("refactor", signals, True)


def _git_name_status(root):
    """[(status, path)] for the commits about to leave (vs the upstream), with a
    working-tree fallback. Best-effort: any git failure → [] so the caller
    degrades to a soft refactor, never a crash.

    `-z` (NUL-separated, no trailing per-record newline): plain `--name-status`
    C-quotes any non-ASCII byte in a path under git's default
    `core.quotePath=true`, and the quoted string never resolves back to a real
    file — `_added_tests_assert`'s own `(Path(root) / rel).read_text()` and
    `_is_test_path`'s naming match both silently miss it. A `-z` record is a
    flat NUL-separated token stream, not one line per path: a plain add/modify/
    delete is `status\\0path\\0`, a rename/copy is `status\\0old\\0new\\0` (one
    extra token) -- so records are walked by CONSUMING tokens, never by
    counting `\\t`-split fields per line.
    """
    import subprocess
    for args in (["diff", "--name-status", "-z", "@{upstream}..HEAD"],
                 ["diff", "--name-status", "-z", "HEAD"]):
        try:
            out = subprocess.run(["git", "-C", str(root), *args],
                                 capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        tokens = [t for t in out.stdout.decode("utf-8", "surrogateescape").split("\0") if t]
        rows = []
        i = 0
        while i < len(tokens):
            status = tokens[i]
            if status[:1] in ("R", "C"):  # rename/copy: status, old-path, new-path
                if i + 2 >= len(tokens):
                    break
                rows.append((status.strip(), tokens[i + 2].strip()))
                i += 3
            else:
                if i + 1 >= len(tokens):
                    break
                rows.append((status.strip(), tokens[i + 1].strip()))
                i += 2
        if rows:
            return rows
    return []


def repo_changed_paths(root):
    """The changed file paths the gate should judge (component-glob matching
    needs them). HARNESS_CHANGED_PATHS (comma/newline separated) overrides for
    tests + explicit scoping; else the git diff. Best-effort: [] on failure."""
    env = os.environ.get("HARNESS_CHANGED_PATHS")
    if env:
        return [p.strip() for p in re.split(r"[,\n]", env) if p.strip()]
    return [p for _s, p in _git_name_status(root)]


def _added_tests_assert(root, added_paths) -> bool:
    """True when any ADDED test file carries an assertion-like token. A cheap
    content sniff (assert / expect / should / t.Error) — enough to tell a real
    test from an empty stub for the feature-vs-ambiguous split."""
    # incl. Mockito verify() — a command-service test may assert only via
    # verify(repo.save()) / verify(publisher.publish()) with no assertThat.
    tokens = ("assert", "expect(", "should", "t.error", ".to.", "verify(")
    for rel in added_paths:
        try:
            text = (Path(root) / rel).read_text(encoding="utf-8",
                                                errors="replace").lower()
        except OSError:
            continue
        if any(tok in text for tok in tokens):
            return True
    return False


def derive_from_repo(root, **overrides):
    """Derive a change-class straight from the repo's git diff — the HONEST gate
    path (the gate derives from facts, it does not trust a declared class). An
    explicit HARNESS_CHANGE_CLASS still short-circuits inside derive(). Any git
    failure degrades to a soft refactor. `overrides` pass through to derive()."""
    rows = _git_name_status(root)
    changed = [p for _s, p in rows]
    added = [p for s, p in rows if s.startswith("A")]
    manifest_changed = any(_is_manifest(p) for p in changed)
    role = _snapshot_roles(root)
    tests = [p for p in added if _is_test_path(p, role)]
    tests_added = bool(tests)
    regression = any("regress" in p.lower() for p in tests)
    has_assert = _added_tests_assert(root, tests)
    kw = dict(manifest_changed=manifest_changed, tests_added=tests_added,
              tests_have_assertions=has_assert, regression_tests=regression,
              root=root, role=role)
    kw.update(overrides)
    return derive(changed, **kw)
