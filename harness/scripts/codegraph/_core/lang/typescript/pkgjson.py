"""One `package.json` as the TypeScript/JavaScript pack reads it.

`exports`/`imports` keep the file's key order (`json` keeps it): which condition comes
first is what Node and TypeScript resolve by. `deps` names every declared dependency
with the field that declares it, the first field in `DEP_FIELDS` order when two do; a
bundled name carries no spec. An object `browser` field is kept but not applied (a
note says so). A manifest that does not parse, or is not an object, is a note and no
package -- the files under it are still measured.
"""
from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEP_FIELDS = ("dependencies", "peerDependencies", "optionalDependencies", "devDependencies")
BUNDLE_FIELDS = ("bundleDependencies", "bundledDependencies")


@dataclass(frozen=True)
class Pkg:
    name: "str | None"
    dir: str                      # repo-relative, "." for the repo root
    type: str = "commonjs"        # module | commonjs
    exports: Any = None
    imports: Any = None
    main: "str | None" = None
    types: "str | None" = None
    typesVersions: Any = None
    browser: Any = None
    deps: "dict[str, tuple[str, str]]" = field(default_factory=dict)


def manifest(rel_dir: str) -> str:
    return posixpath.normpath(posixpath.join(rel_dir, "package.json"))


def load(repo: Path, rel_dir: str) -> "tuple[Pkg | None, list[str]]":
    """The package at `rel_dir` (repo-relative) and the notes reading it left."""
    rel = manifest(rel_dir)
    path = repo / rel
    if not path.is_file():
        return None, []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, ["package_json_unreadable: %s (%s)" % (rel, e)]
    if not isinstance(raw, dict):
        return None, ["package_json_unreadable: %s (not an object)" % rel]
    deps: "dict[str, tuple[str, str]]" = {}
    for f in DEP_FIELDS:
        for name, spec in (raw.get(f) or {}).items() if isinstance(raw.get(f), dict) else ():
            deps.setdefault(str(name), (f, str(spec)))
    for f in BUNDLE_FIELDS:
        for name in raw.get(f) or () if isinstance(raw.get(f), list) else ():
            deps.setdefault(str(name), (f, ""))
    notes = []
    if isinstance(raw.get("browser"), dict):
        notes.append("package_json_browser_object_ignored: %s" % rel)
    name = raw.get("name")
    return Pkg(name=name if isinstance(name, str) and name else None,
               dir=posixpath.normpath(rel_dir), type="module" if raw.get("type") == "module" else "commonjs",
               exports=raw.get("exports"), imports=raw.get("imports"), main=raw.get("main"),
               types=raw.get("types", raw.get("typings")), typesVersions=raw.get("typesVersions"),
               browser=raw.get("browser"), deps=deps), notes
