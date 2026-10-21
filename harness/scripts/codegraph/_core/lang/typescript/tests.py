"""Which TypeScript/JavaScript file is a test, which a story -- the pack's test convention.

`role_of(rel, options)` -> "test" | "story" | None, decided when the pack names its files
(`TypeScriptResolver.name_modules`) and written on the record as `role`; every reader of
"is this a test" reads that field (`codegraph_change_reader.FilesLayout.with_roles`), never
a path rule of its own.

    test    basename `*.test.<ext>` / `*.spec.<ext>`, or under a `__tests__` directory
            (vitest's and jest's defaults); `languages.typescript.tests: {patterns, dirs}`
            REPLACES both lists -- basename globs, directory names
    story   basename `*.stories.<ext>`; `stories_as_tests: true` makes it a test (a
            storybook test-runner runs every story)

WHAT IT CANNOT SEE
    * A runner's own config (`vitest.config.ts` `include`, jest `testMatch`): a project
      whose runner reads other files declares them under `tests:`.
"""
from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath
from typing import Any

from ..base import LanguageError

EXTS = ("ts", "tsx", "mts", "cts", "js", "jsx", "mjs", "cjs")
TEST_PATTERNS = tuple("*.%s.%s" % (k, e) for k in ("test", "spec") for e in EXTS)
TEST_DIRS = ("__tests__",)
STORY_PATTERNS = tuple("*.stories.%s" % e for e in EXTS)
_TESTS_KEYS = frozenset({"patterns", "dirs"})


def _names(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x and "/" not in x for x in v)


def check(options: "dict[str, Any]", where: str) -> None:
    """Refuse a malformed `tests:` / `stories_as_tests:` under `where`."""
    if "stories_as_tests" in options and not isinstance(options["stories_as_tests"], bool):
        raise LanguageError("%s.stories_as_tests must be true or false" % where)
    if "tests" not in options:
        return
    t = options["tests"]
    if not isinstance(t, dict) or not t or set(t) - _TESTS_KEYS \
            or not all(_names(t[k]) for k in t):
        raise LanguageError("%s.tests must map `patterns` (basename globs) and/or `dirs` "
                            "(directory names) to lists of non-empty names" % where)


def role_of(rel: str, options: "dict[str, Any]") -> "str | None":
    p = PurePosixPath(rel)
    declared = options.get("tests")
    patterns, dirs = ((tuple(declared.get("patterns", ())), tuple(declared.get("dirs", ())))
                      if declared else (TEST_PATTERNS, TEST_DIRS))
    if any(fnmatch.fnmatchcase(p.name, g) for g in patterns) or any(d in dirs for d in p.parts[:-1]):
        return "test"
    if any(fnmatch.fnmatchcase(p.name, g) for g in STORY_PATTERNS):
        return "test" if options.get("stories_as_tests") else "story"
    return None
