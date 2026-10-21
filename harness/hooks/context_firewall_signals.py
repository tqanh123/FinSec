#!/usr/bin/env python3
"""context_firewall_signals.py — deterministic blast-radius signals for a single
PreToolUse call. Pure functions: same (tool_name, tool_input, cwd, thresholds)
always yields the same signals, so a verdict built on them is reproducible and
unit-testable without touching the filesystem beyond a real `os.stat`/glob walk.

Four operation kinds, one signal each family can raise:
  read  — a whole-file Read with no offset/limit/pages window
  glob  — a Glob call sized by its REAL match count (not a pattern-shape guess)
  search — a Grep call in content mode with no head_limit
  shell — a Bash call whose leading pipeline stage dumps a whole file, walks a
          tree with no depth bound, searches recursively with no narrowing
          flag, or prints a full history/diff with no bound

Ported by principle from AK's `context-firewall/signals.cjs`: same operation
taxonomy, rewritten to use a real glob match count instead of AK's pattern-shape
`detectBroadPatternIssue` heuristic (this harness carries no scout-checker
sibling to borrow that from), and to drop the log-stream family (`docker logs`,
`journalctl`) which has no counterpart signal worth a Python rewrite at this
scope. Never duplicates the privacy guard (secret-path gating,
`privacy_read_guard.py`) or a scout guard — this only ever sizes CONTENT
volume, never path SENSITIVITY.
"""
import itertools
import os
import re
from pathlib import Path

WHOLE_FILE_DUMP_COMMANDS = frozenset({"cat", "bat", "less", "more", "nl"})
RECURSIVE_WALK_COMMANDS = frozenset({"find", "tree"})
_BOUNDING_STAGE_VERBS = frozenset({"head", "tail", "wc", "sort", "uniq", "jq", "column"})
_HISTORY_SUBCOMMANDS = frozenset({"log", "diff", "show"})

_QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_CONNECTOR_RE = re.compile(r"&&|\|\||[;\n]")
_ENV_ASSIGN_RE = re.compile(r"^(?:[A-Za-z_]\w*=\S*\s+)+")
_WRAPPER_PREFIX_RE = re.compile(r"^(?:sudo|env|nice|nohup|time|timeout)\s+")


def _is_number(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
            return True
        except ValueError:
            return False
    return False


def file_bytes(target: str):
    """Size of a regular file on disk, or None when it is missing / not a file."""
    try:
        st = os.stat(target)
    except OSError:
        return None
    return st.st_size if os.path.isfile(target) else None


def _resolve_against(target: str, cwd: str) -> str:
    if not target:
        return ""
    return target if os.path.isabs(target) else os.path.join(cwd or os.getcwd(), target)


def classify_operation(tool_name: str, tool_input) -> str:
    """The operation kind whose signal family applies. Runtimes name tools
    differently, so the name is a hint; the payload shape decides for an
    unfamiliar name (mirrors AK's own portability stance)."""
    name = (tool_name or "").lower()
    if "bash" in name or "shell" in name or "terminal" in name:
        return "shell"
    if name == "glob" or ("glob" in name and "grep" not in name):
        return "glob"
    if "grep" in name or "search" in name:
        return "search"
    if "read" in name:
        return "read"
    if isinstance(tool_input, dict):
        if isinstance(tool_input.get("command"), str):
            return "shell"
        if isinstance(tool_input.get("pattern"), str):
            return "glob" if "path" not in tool_input and "output_mode" not in tool_input else "search"
        if isinstance(tool_input.get("file_path"), str) or isinstance(tool_input.get("path"), str):
            return "read"
    return "other"


def read_signals(tool_input: dict, cwd: str, thresholds: dict):
    file_path = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(file_path, str) or not file_path:
        return [], {}
    has_window = (
        _is_number(tool_input.get("limit"))
        or _is_number(tool_input.get("offset"))
        or isinstance(tool_input.get("pages"), str)
    )
    resolved = _resolve_against(file_path, cwd)
    size = file_bytes(resolved)
    facts = {"file_path": file_path, "bytes": size, "windowed": has_window}
    if has_window or size is None:
        return [], facts
    if size >= thresholds["read_block_bytes"]:
        return [{"id": "read.unbounded", "severity": "block", "bytes": size}], facts
    if size >= thresholds["read_warn_bytes"]:
        return [{"id": "read.unbounded", "severity": "warn", "bytes": size}], facts
    return [], facts


def _safe_glob_count(base: str, pattern: str, cap: int):
    """The real number of paths `pattern` matches under `base`, walked lazily and
    stopped at `cap` so a pathological pattern cannot make counting itself
    expensive. Returns None when the pattern cannot be walked at all (an
    unreadable base, a pattern outside pathlib's glob grammar)."""
    try:
        root = Path(base) if base else Path(os.getcwd())
        it = root.glob(pattern)
        return sum(1 for _ in itertools.islice(it, cap))
    except (OSError, ValueError, NotImplementedError):
        return None


def glob_signals(tool_input: dict, cwd: str, thresholds: dict):
    pattern = tool_input.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return [], {}
    base = tool_input.get("path") or cwd or os.getcwd()
    cap = thresholds["glob_breadth_block_count"] + 1
    count = _safe_glob_count(base, pattern, cap)
    facts = {"pattern": pattern, "path": base, "match_count": count}
    if count is None:
        return [], facts
    if count >= thresholds["glob_breadth_block_count"]:
        return [{"id": "glob.breadth", "severity": "block", "count": count}], facts
    if count >= thresholds["glob_breadth_warn_count"]:
        return [{"id": "glob.breadth", "severity": "warn", "count": count}], facts
    return [], facts


def search_signals(tool_input: dict, thresholds: dict):
    pattern = tool_input.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return [], {}
    output_mode = tool_input.get("output_mode") or "files_with_matches"
    head_limit = tool_input.get("head_limit")
    bounded = (head_limit is not None and _is_number(head_limit) and float(head_limit) > 0
               and float(head_limit) <= thresholds["search_head_limit"])
    facts = {"pattern": pattern, "output_mode": output_mode,
             "head_limit": head_limit if bounded else None}
    if output_mode == "content" and not bounded:
        return [{"id": "search.unbounded_content", "severity": "warn"}], facts
    return [], facts


def _strip_prefix(stage: str) -> str:
    s = _ENV_ASSIGN_RE.sub("", stage.strip())
    s = _WRAPPER_PREFIX_RE.sub("", s)
    return s.strip()


def split_pipelines(command: str):
    """Split on `;`, newline, `&&`, `||`, respecting quotes — each pipeline's own
    `|` stages stay intact (split_stages does that split)."""
    pipelines, current, quote = [], "", None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            current += ch
            if ch == quote and (i == 0 or command[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current += ch
            i += 1
            continue
        if ch == "\n" or ch == ";":
            pipelines.append(current)
            current = ""
            i += 1
            continue
        if ch in "&|" and i + 1 < n and command[i + 1] == ch:
            pipelines.append(current)
            current = ""
            i += 2
            continue
        current += ch
        i += 1
    pipelines.append(current)
    return [p.strip() for p in pipelines if p.strip()]


def split_stages(pipeline: str):
    """Split one pipeline into its `|` stages, respecting quotes."""
    stages, current, quote = [], "", None
    for ch in pipeline:
        if quote:
            current += ch
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            current += ch
            continue
        if ch == "|":
            stages.append(current)
            current = ""
            continue
        current += ch
    stages.append(current)
    return [s.strip() for s in stages if s.strip()]


def redirects_to_file(pipeline: str) -> bool:
    if re.search(r"(^|\s)\|\s*tee\b", pipeline):
        return True
    stripped = re.sub(r"2>\S+", "", pipeline)
    return bool(re.search(r"(^|[^0-9&>])>{1,2}\s*[^\s&|]+", stripped))


def is_terminally_bounded(stages) -> bool:
    if len(stages) < 2:
        return False
    last = _strip_prefix(stages[-1])
    tokens = last.split()
    if not tokens:
        return False
    verb = tokens[0]
    if verb in _BOUNDING_STAGE_VERBS:
        return verb not in ("sort", "uniq")
    if re.match(r"^(grep|egrep|rg)$", verb) and any(
            re.match(r"^-[a-zA-Z]*[cl][a-zA-Z]*$", t) for t in tokens[1:]):
        return True
    return False


def _has_bounding_flag(tokens, flags) -> bool:
    for i, tok in enumerate(tokens):
        for flag in flags:
            if tok == flag and i + 1 < len(tokens):
                return True
            if tok.startswith(flag + "=") and len(tok) > len(flag) + 1:
                return True
    return False


def _pipeline_signals(pipeline: str, cwd: str, thresholds: dict):
    if redirects_to_file(pipeline):
        return []
    stages = split_stages(pipeline)
    if is_terminally_bounded(stages):
        return []
    command = _strip_prefix(stages[0])
    if not command:
        return []
    tokens = command.split()
    if not tokens:
        return []
    verb = tokens[0]

    signals = []

    if verb in WHOLE_FILE_DUMP_COMMANDS:
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            size = file_bytes(_resolve_against(tok, cwd))
            if size is None:
                continue
            if size >= thresholds["shell_dump_block_bytes"]:
                signals.append({"id": "shell.file_dump", "severity": "block",
                                 "bytes": size, "target": tok, "verb": verb})
            elif size >= thresholds["shell_dump_warn_bytes"]:
                signals.append({"id": "shell.file_dump", "severity": "warn",
                                 "bytes": size, "target": tok, "verb": verb})

    if verb in RECURSIVE_WALK_COMMANDS and not _has_bounding_flag(tokens, ("-maxdepth", "-L")):
        signals.append({"id": "shell.recursive_walk", "severity": "warn", "verb": verb})

    if re.match(r"^(grep|egrep|rg|ag|ack)$", verb):
        if verb in ("grep", "egrep"):
            recursive = any(re.match(r"^-[a-zA-Z]*[rR][a-zA-Z]*$", t) for t in tokens[1:])
        else:
            recursive = True
        narrowed = (
            any(re.match(r"^-[a-zA-Z]*[clm][a-zA-Z]*$", t) for t in tokens[1:])
            or any(t.startswith("--include") or t.startswith("--exclude")
                   or t.startswith("-m") or t.startswith("--max-count") for t in tokens[1:])
        )
        if recursive and not narrowed:
            signals.append({"id": "shell.recursive_search", "severity": "warn", "verb": verb})

    if verb == "git" and len(tokens) > 1 and tokens[1] in _HISTORY_SUBCOMMANDS:
        bounded = any(
            re.match(r"^-\d+$", t) or t.startswith("--stat") or t.startswith("--name-only")
            or t.startswith("--oneline") or t.startswith("-n") or t.startswith("--max-count")
            for t in tokens[2:]
        )
        if not bounded and len(tokens) <= 2:
            signals.append({"id": "shell.verbose_history", "severity": "warn",
                             "verb": "git %s" % tokens[1]})

    return signals


def shell_signals(tool_input: dict, cwd: str, thresholds: dict):
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return [], {}
    pipelines = split_pipelines(command)
    signals = []
    for pipeline in pipelines:
        signals.extend(_pipeline_signals(pipeline, cwd, thresholds))
    return signals, {"command": command, "pipelines": len(pipelines)}


def compute_signals(tool_name: str, tool_input, cwd: str, thresholds: dict):
    """(kind, signals, facts) for one PreToolUse call."""
    kind = classify_operation(tool_name, tool_input)
    if kind == "other" or not isinstance(tool_input, dict):
        return kind, [], {}
    if kind == "read":
        signals, facts = read_signals(tool_input, cwd, thresholds)
    elif kind == "glob":
        signals, facts = glob_signals(tool_input, cwd, thresholds)
    elif kind == "search":
        signals, facts = search_signals(tool_input, thresholds)
    else:
        signals, facts = shell_signals(tool_input, cwd, thresholds)
    return kind, signals, facts
