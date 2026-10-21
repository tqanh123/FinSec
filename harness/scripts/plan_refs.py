"""A plan section whose body lives in a reference file.

A deep plan keeps its heading skeleton in plan.md and the phase files and moves a
section's body to a file under the plan dir, leaving one pointer line in its place:

    ## Related Code Files
    → [refs/p1-03-related.md](../refs/p1-03-related.md) (42 lines)

A reader that parses a section reads the pointed-to file as that section's body.
Only a line that starts with `→` (or `->`) and then a markdown link is a pointer; a
link elsewhere in prose is not. The target must be a `.md` file the approval hash pins
(plan.md, a phase file, a file under a reference dir), other than the file holding the
pointer; anything else leaves the line as it is. The target's frontmatter and leading
headings are dropped, and a pointer inside it is not followed.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

_POINTER = re.compile(r"^(?:→|->)\s*\[[^\]]*\]\(\s*<?([^)>\s#]+)(?:#[^)\s>]*)?>?\s*\)")


def pinned(plan_dir: Path) -> set:
    """The resolved files the plan's approval hash covers."""
    import plan_files
    return {f.resolve() for f in plan_files._plan_files(Path(plan_dir))}


def pointer_target(line: str, doc: Path, plan_dir: Path,
                   pins: "set | None" = None) -> "Path | None":
    """The pinned markdown file `line` points to, or None when it is no pointer."""
    m = _POINTER.match(line.strip())
    if not m:
        return None
    try:
        target = (Path(doc).parent / unquote(m.group(1))).resolve()
        here = Path(doc).resolve()
    except OSError:
        return None
    if target.suffix != ".md" or target == here:
        return None
    return target if target in (pinned(plan_dir) if pins is None else pins) else None


def numbered_body(path: Path) -> list:
    """[(1-based line in `path`, text)] of a reference file as a section body."""
    try:
        lines = list(enumerate(Path(path).read_text(encoding="utf-8", errors="replace")
                               .splitlines(), 1))
    except OSError:
        return []
    if lines and lines[0][1].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i][1].strip() == "---"), None)
        if end is not None:
            lines = lines[end + 1:]
    while lines and (not lines[0][1].strip() or lines[0][1].lstrip().startswith("#")):
        lines = lines[1:]
    return lines


def body(path: Path) -> list:
    """The lines of a reference file as a section body."""
    return [t for _, t in numbered_body(path)]


def expand(lines: list, doc: Path, plan_dir: Path) -> list:
    """`lines` with every pointer line replaced by the body it points to."""
    out: list = []
    pins = pinned(plan_dir)
    for line in lines:
        target = pointer_target(line, doc, plan_dir, pins)
        out.extend(body(target) if target is not None else [line])
    return out
