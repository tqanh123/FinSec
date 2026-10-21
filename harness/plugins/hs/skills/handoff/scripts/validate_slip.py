#!/usr/bin/env python3
"""validate_slip.py — refuse a handoff slip missing a required section rather
than let it dispatch or get consumed half-written.

    validate_slip.py <path-to-handoff.md>

Exit 0 + nothing on stdout when the slip passes. Exit 1 + one problem per
line on stdout when it does not — the caller (an agent following
`hs:handoff`'s dispatch sequence) prints them and stops; it never "repairs"
the slip on its own.

`validate(text)` is the library entry point other callers (a future dispatch
step, a test) use directly instead of shelling out.
"""
import re
import sys

# The nine required H2 sections, in the order the artifact schema fixes.
# Extra H2 sections may appear between them; only a MISSING one, or one out
# of this relative order, is a hard refusal.
REQUIRED_SECTIONS = (
    "Mission and current status",
    "Scope and guardrails",
    "Current state",
    "Decisions and rationale",
    "Work performed",
    "Verification",
    "Open risks and blockers",
    "Exact next actions",
    "Source pointers",
)

# Handoff-version values this validator understands. A slip declaring a
# version not in this set is refused rather than read under the wrong schema.
KNOWN_VERSIONS = (1,)

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$")


def _strip_frontmatter(text: str):
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fields = {}
    for line in m.group(1).splitlines():
        km = _FRONTMATTER_KEY_RE.match(line)
        if km:
            fields[km.group(1)] = km.group(2).strip()
    return fields, text[m.end():]


def _section_body(body_text: str, heading: str) -> str:
    """The text between `## <heading>` and the next `##` heading (or EOF)."""
    headings = list(_H2_RE.finditer(body_text))
    for i, m in enumerate(headings):
        if m.group(1) == heading:
            start = m.end()
            end = headings[i + 1].start() if i + 1 < len(headings) else len(body_text)
            return body_text[start:end]
    return ""


def validate(text: str):
    """`(ok, problems)` — `ok` is True only when `problems` is empty."""
    problems = []
    fields, body = _strip_frontmatter(text or "")

    version_raw = fields.get("handoff-version")
    if version_raw is not None:
        try:
            version = int(version_raw)
        except ValueError:
            version = None
        if version not in KNOWN_VERSIONS:
            problems.append(
                "unsupported handoff-version %r (known: %s)"
                % (version_raw, ", ".join(str(v) for v in KNOWN_VERSIONS)))

    h1 = _H1_RE.search(body)
    if not h1 or not h1.group(1).startswith("HANDOFF:"):
        problems.append("the document must open with an H1 starting 'HANDOFF: <title>'")

    found_headings = [m.group(1) for m in _H2_RE.finditer(body)]
    found_set = set(found_headings)
    missing = [h for h in REQUIRED_SECTIONS if h not in found_set]
    for h in missing:
        problems.append("missing required section: %s" % h)

    if not missing:
        present_required = [h for h in found_headings if h in REQUIRED_SECTIONS]
        if present_required != list(REQUIRED_SECTIONS):
            problems.append(
                "required sections are out of order (found: %s)"
                % ", ".join(present_required))

    if "Exact next actions" in found_set:
        actions_body = _section_body(body, "Exact next actions").strip()
        items = [ln for ln in actions_body.splitlines() if re.match(r"^\d+\.\s", ln)]
        if not items:
            problems.append("Exact next actions has no numbered item")
        elif "**First safe step**" not in items[0]:
            problems.append(
                "the first item of Exact next actions must be bold-prefixed "
                "'**First safe step**'")

    try:
        _handoff_redact = _load_redact_module()
        hits = _handoff_redact.scan_text(body)
    except Exception:
        hits = []
    if hits:
        problems.append(
            "unredacted secret pattern(s) present: %s — run the artifact through "
            "handoff_redact.redact_text before validating" % ", ".join(sorted(hits)))

    return (not problems, problems)


def _load_redact_module():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent / "handoff_redact.py"
    spec = importlib.util.spec_from_file_location("_handoff_redact_for_validate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: validate_slip.py <path-to-handoff.md>", file=sys.stderr)
        return 2
    try:
        text = open(argv[0], encoding="utf-8").read()
    except OSError as exc:
        print("cannot read %s: %s" % (argv[0], exc), file=sys.stderr)
        return 2
    ok, problems = validate(text)
    for p in problems:
        print(p)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
