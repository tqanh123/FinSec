"""codegraph._core.testfiles — TWO different questions about "is this test
code", answered by TWO predicates, not one collapsed into the other.

    in_test_tree(rel)          is this TEST CODE — a helper, a fixture,
                                `conftest.py`, everything living under a
                                test tree, whether or not pytest would ever
                                collect it directly as a test module.
    is_test_file(rel, pats)    is this a FILE PYTEST COLLECTS — `in_test_
                                tree(rel)` AND the basename matches one of
                                the target's own `python_files` glob
                                patterns. `is_test_file` is a SUBSET of
                                `in_test_tree` by construction: nothing this
                                accepts, `in_test_tree` would reject.

Named `testfiles`, not `test_discovery` — the SAME reason `codegraph.change.
selection` is not `test_select`: a flat module whose own name matches
pytest's `test_*`/`*_test` collection glob gets collected as a test module
by any target that (like this repo) declares none, corrupting the count of
FILES containing a `def test_*` with one that has none.

Two ways to answer "is this an ancestor test-tree directory": a DECLARED
`layout.tests_subdir` (`ProjectDescriptor.tests_subdir`) names the ONE exact
directory name that counts, project-wide, and wins outright — the
convention regex below is never even consulted. UNDECLARED (no descriptor,
or its own `tests_subdir` is `None`) falls back to the convention
`^tests?(_.+)?$` — Node/Maven-style projects that declare nothing still get
a singular `test/` recognized. MEASURED HOLE this closes: narrowing the
UNDECLARED convention itself to exclude the singular form (rejected) would
also reject every other project's plain `test/` — the fix for THIS repo's
own `skills/test/` (a product directory, not a test tree) is declaring
`layout.tests_subdir: tests` in `codemap.yaml`, not narrowing what every
OTHER project's silence means.

`FilesLayout` bundles the resolved `tests_subdir` (or its absence) with the
resolved `python_files_patterns` into ONE object, built ONCE per (root,
descriptor) by each caller (`codegraph._core.testmap`, `codegraph.change.
selection`, `codegraph._core.snapshot_bridge`, `codegraph.check.owners`,
`codegraph.check.plan_baseline`, `codegraph._core.rules.kinds.code_shape`)
instead of re-resolving config on every file. `build_test_layout(root,
descriptor=None)` self-loads `root/codemap.yaml` when no descriptor is
handed in (mirrors `python_files_patterns`'s own root-only door) — a caller
already holding a loaded descriptor should pass it instead of paying for a
second load.

Measured on this repo's own tree (`git ls-files`, undeclared convention): a
basename-only rule ("starts with `test_` or ends `_test.py`") false-
positives on 5 PRODUCT modules with no test-tree ancestor at all —
`harness/scripts/test_policy.py`, `hs_run_test.py`, `manual_test.py`,
`test_census.py`, `test_result_readers.py`. `in_test_tree` alone (directory
only, no basename check) is the RIGHT question for "is this test code" (a
`conftest.py` or a bare helper under `tests/` is real test-tooling, not
product shape, even though its own name never matches `test_*.py`);
`is_test_file` is the right (narrower) question for "would pytest actually
run this as a test module".

`codegraph._core.testmap`, `codegraph.change.selection` and
`codegraph._core.snapshot_bridge` all answer the SECOND question
(`is_test_file`) — a test FILE, in the sense a snapshot record or a bridged
record represents one. `codegraph.check.owners`, `codegraph.check.
plan_baseline` and `codegraph._core.rules.kinds.code_shape` all answer the
FIRST (`in_test_tree`/`is_test_dir`) — they exclude test CODE (fixtures,
helpers, `conftest.py` included) from a product-shape denominator, which a
basename-only rule would under-exclude and a name-based OR rule
(`owners`'s old `"tests" in p.parts or p.name.startswith("test_")`) would
OVER-include (every `test_*.py` product module, anywhere).

`python_files_patterns(root)` reads the target's OWN declared basename glob
list, pytest's own precedence: `pyproject.toml`
`[tool.pytest.ini_options].python_files`, else `pytest.ini` `[pytest]`,
else `setup.cfg` `[tool:pytest]`, else `tox.ini` `[pytest]`; undeclared at
every one of those falls back to pytest's own default,
`("test_*.py", "*_test.py")`. A read/parse error at any ONE of those files
is treated as "not declared there" and the search moves to the next —
a hint, never a hard requirement to have a valid config.

WHAT IT CANNOT SEE
    * `is_test_dir`/`in_test_tree`'s own UNDECLARED-convention directory
      rule (`^tests?(_.+)?$`, case-sensitive) is a NAME shape, not a
      semantic one — a directory genuinely holding tests but named
      something else entirely (`spec/`) reads as ordinary product code
      unless `tests_subdir` is declared to say so.
    * `python_files_patterns` reads ONE config file's `python_files` key by
      pytest's OWN precedence order; it does not merge across files, and a
      project passing `--override-ini`/`-o python_files=...` on its actual
      pytest invocation is invisible here (this module reads declared
      CONFIG, not a live pytest run's resolved settings).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import codegraph_change_reader
from codegraph_change_reader import FilesLayout as FilesLayout  # noqa: PLC0414 -- re-export

# `DEFAULT_TEST_PATTERNS`/`_TEST_DIR_RE`/`_is_test_segment`/`is_test_dir`/
# `in_test_tree`/`is_test_file`/`_python_files_from_toml`/
# `_python_files_from_ini`/`python_files_patterns`/`FilesLayout` MOVED to
# `codegraph_change_reader` (a flat module outside this package — see that
# module's own docstring for why: all of these are pure, with no reference
# to `build_test_layout`'s descriptor-loading door, which is what STAYS
# here — resolving a DECLARED `layout.tests_subdir` needs the descriptor
# loader, `FilesLayout` itself does not). Rebound to the SAME objects, not
# reimplemented beside them — the identical pattern `codegraph._core.
# snapshot.worktree_key.worktree_key` already uses. `is_test_file is
# codegraph_change_reader.is_test_file` holds — one definition. `FilesLayout`
# is imported (`from ... import FilesLayout as FilesLayout`) rather than
# assigned like the rest: identity holds exactly the same way (`is` still
# true — a plain `from X import Y` binds the SAME object), but a bare
# module-level assignment (`FilesLayout = codegraph_change_reader.
# FilesLayout`) is not valid as a TYPE ANNOTATION to mypy (`build_test_
# layout`'s own `-> FilesLayout` return type below needs the real import
# form to resolve).
DEFAULT_TEST_PATTERNS = codegraph_change_reader.DEFAULT_TEST_PATTERNS
_TEST_DIR_RE = codegraph_change_reader._TEST_DIR_RE
_is_test_segment = codegraph_change_reader._is_test_segment
is_test_dir = codegraph_change_reader.is_test_dir
in_test_tree = codegraph_change_reader.in_test_tree
is_test_file = codegraph_change_reader.is_test_file
_python_files_from_toml = codegraph_change_reader._python_files_from_toml
_python_files_from_ini = codegraph_change_reader._python_files_from_ini
python_files_patterns = codegraph_change_reader.python_files_patterns


def build_test_layout(root: Path, descriptor: Any = None) -> FilesLayout:
    """ONE `FilesLayout` for `root`, DECLARED `layout.tests_subdir` (read off
    `descriptor.tests_subdir`) winning outright over the UNDECLARED
    convention regex. `descriptor=None` (the default: most callers here
    have `root` but not always a loaded descriptor in hand) loads `root/
    codemap.yaml` itself when present — the same self-contained, root-only
    door `python_files_patterns` already is; a load failure (no file, or a
    malformed one) degrades to the UNDECLARED convention rather than
    raising, matching `python_files_patterns`'s own "a hint, never a hard
    requirement" law. A caller already holding a loaded descriptor should
    pass it instead of paying for a second load."""
    if descriptor is None:
        cm = root / "codemap.yaml"
        if cm.is_file():
            from .descriptor import DescriptorError, load
            try:
                descriptor = load(cm, root).descriptor
            except DescriptorError:
                descriptor = None
    tests_subdir = getattr(descriptor, "tests_subdir", None)
    return FilesLayout(tests_subdir=tests_subdir, patterns=python_files_patterns(root))
