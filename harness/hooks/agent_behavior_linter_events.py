#!/usr/bin/env python3
"""agent_behavior_linter_events.py — redacted event records for the behavior
linter's session journal.

PRIVACY INVARIANT (mirrors AK's own `event-schema.cjs`): a record produced
here never carries file content, tool output text, prompt text, or an
absolute path outside the project root. A path inside `cwd` is stored
relative to it; a path outside it is stored as a digest + extension. A shell
command is stored as a digest plus its executable class and an argument-shape
count, never as text. Output is stored as a byte length; a failure is stored
as a digest of its first normalized line.

Deliberately narrower than AK's port at one point: this module classifies
ONLY the Grep/Glob TOOL calls as "search" (AK also parses a raw shell
`grep`/`find`/`rg` invocation out of Bash commands). The Bash-side search
shape is a shell-parsing job `context_firewall_signals.py` already does for
its own `shell.recursive_search` signal — duplicating a second parser of the
same shell grammar here is not worth the redundancy at this scope; a repo
that leans on shell greps over the Grep/Glob tools gets a thinner (not wrong)
D3 signal until that is worth porting.
"""
import hashlib
import os
import re

SCHEMA_VERSION = 1
_DIGEST_CHARS = 16
_MAX_CLASS_CHARS = 32
_MAX_EXT_CHARS = 12

READ_TOOL_NAMES = frozenset({"Read", "NotebookRead"})
MUTATION_TOOL_NAMES = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
SEARCH_TOOL_NAMES = frozenset({"Grep", "Glob"})

# Command classes whose failure can legitimately differ between two identical
# runs, so a repeat is not evidence of a pointless retry.
NONDETERMINISTIC_COMMAND_CLASSES = frozenset({
    "curl", "wget", "gh", "git", "ssh", "scp", "rsync", "docker", "podman",
    "kubectl", "helm", "terraform", "npm", "pnpm", "yarn", "bun", "pip", "uv",
    "cargo", "go", "brew", "apt", "apt-get", "nc", "ping", "dig", "aws", "gcloud",
})
# For the mixed classes, only the network-reaching subcommand is nondeterministic
# (`go test`/`npm run build` are as reproducible as any local command); None means
# every subcommand of that class reaches the network.
NETWORK_SUBCOMMANDS_BY_CLASS = {
    "git": {"fetch", "pull", "push", "clone", "remote", "submodule", "ls-remote"},
    "gh": None,
    "go": {"mod", "get", "download", "install"},
    "cargo": {"fetch", "publish", "install", "update", "add"},
    "npm": {"install", "i", "ci", "publish", "audit", "outdated", "update", "add", "login"},
    "pnpm": {"install", "i", "add", "update", "publish", "audit", "outdated", "fetch"},
    "yarn": {"install", "add", "upgrade", "publish", "audit"},
    "bun": {"install", "add", "update", "publish"},
    "pip": {"install", "download", "uninstall"},
    "uv": {"pip", "sync", "add", "lock"},
    "docker": {"pull", "push", "build", "login"},
    "podman": {"pull", "push", "build", "login"},
    "brew": None, "apt": None, "apt-get": None, "aws": None, "gcloud": None,
    "kubectl": None, "helm": None, "terraform": None,
}

DOCUMENTATION_EXTENSIONS = frozenset({".md", ".mdx", ".txt", ".rst"})
_DOC_PATH_RE = re.compile(r"^(?:docs|plans)/")

_ENV_ASSIGN_RE = re.compile(r"^(?:[A-Za-z_]\w*=\S*\s+)+")
_WRAPPER_RE = re.compile(r"^(?:sudo|env|nice|nohup|time|timeout)\s+")
_EXE_SUFFIX_RE = re.compile(r"\.(?:exe|cmd|sh)$", re.IGNORECASE)
_HEX_RE = re.compile(r"0x[0-9a-f]+", re.IGNORECASE)
_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m)?\b")

# A test/build invocation with no scoping argument sweeps the whole repo. Kept
# stack-agnostic (any ported repo may be JS/Go/Rust/Java, not just this one's
# Python/pytest) rather than pinned to one runner's exact flags; each pattern
# anchors on "no positional argument followed the runner", which is what makes
# it a full-suite call rather than a scoped one.
_FULL_SUITE_PATTERNS = [
    re.compile(r"(?:^|/)(?:python3?\s+-m\s+)?pytest(?:\s+-{1,2}[\w-]+(?:=\S+)?)*\s*$"),
    re.compile(r"\bgo\s+test\s+\./\.\.\.\s*$"),
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\s*$"),
    re.compile(r"\bcargo\s+test\s*$"),
    re.compile(r"\b(?:mvn|gradle)\s+test\s*$"),
    re.compile(r"\brun_local_tests\.py\b"),
    re.compile(r"\bci_local\.sh\b"),
    re.compile(r"(?:^|/)ci\.sh(?:\s+\S+)?\s*$"),
]


def digest(value) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def strip_command_prefix(command) -> str:
    if not isinstance(command, str) or not command:
        return ""
    s = command.strip()
    s = _ENV_ASSIGN_RE.sub("", s)
    s = _WRAPPER_RE.sub("", s)
    s = _ENV_ASSIGN_RE.sub("", s)
    return s.strip()


def relativize_project_path(target, cwd):
    """{"path", "inside"} for `target` against `cwd`, or None when either is
    unusable. Never returns a raw absolute path outside `cwd` — that case is a
    digest + extension instead."""
    if not isinstance(target, str) or not target or not isinstance(cwd, str) or not cwd:
        return None
    try:
        absolute = target if os.path.isabs(target) else os.path.normpath(os.path.join(cwd, target))
        root = os.path.normpath(cwd)
        rel = os.path.relpath(absolute, root)
    except (ValueError, OSError):
        return None
    inside = not rel.startswith("..") and not os.path.isabs(rel)
    if inside:
        rel_posix = "." if rel == "." else rel.replace(os.sep, "/")
        return {"path": rel_posix, "inside": True}
    ext = os.path.splitext(absolute)[1][:_MAX_EXT_CHARS]
    return {"path": "external:%s%s" % (digest(absolute), ext), "inside": False}


def command_fingerprint(command):
    stripped = strip_command_prefix(command)
    if not stripped:
        return None
    tokens = stripped.split()
    if not tokens:
        return None
    executable = _EXE_SUFFIX_RE.sub("", os.path.basename(tokens[0]))
    flags = sum(1 for t in tokens[1:] if t.startswith("-"))
    positionals = len(tokens) - 1 - flags
    return {"hash": digest(stripped), "class": executable[:_MAX_CLASS_CHARS],
            "arg_shape": {"flags": flags, "positionals": positionals}}


def error_signature(text):
    if not isinstance(text, str) or not text:
        return None
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), None)
    if not first:
        return None
    normalized = _HEX_RE.sub("0xN", first)
    normalized = _NUM_RE.sub("N", normalized)
    normalized = re.sub(r"\s+", " ", normalized).lower()
    return digest(normalized)


def is_nondeterministic_command(fingerprint, command) -> bool:
    if not fingerprint:
        return True
    cls = fingerprint["class"]
    if cls not in NONDETERMINISTIC_COMMAND_CLASSES:
        return False
    subs = NETWORK_SUBCOMMANDS_BY_CLASS.get(cls)
    if subs is None:
        return True
    tokens = strip_command_prefix(command).split()
    sub = tokens[1].lower() if len(tokens) > 1 else ""
    if not sub or sub.startswith("-"):
        return True
    return sub in subs


def is_full_suite_command(command) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    stripped = strip_command_prefix(command)
    return any(p.search(stripped) for p in _FULL_SUITE_PATTERNS)


def is_documentation_path(rel_path) -> bool:
    if not isinstance(rel_path, str) or not rel_path or rel_path.startswith("external:"):
        return False
    if _DOC_PATH_RE.match(rel_path):
        return True
    return os.path.splitext(rel_path)[1].lower() in DOCUMENTATION_EXTENSIONS


def is_broad_search_root(root_rel_path) -> bool:
    """A search rooted at the project root (or given no scoping path at all) is
    broad; a session's own D3 detector treats one narrower search anywhere as
    proof the agent DID narrow, and drops the finding."""
    return root_rel_path in (None, "", ".", "/")


def read_range(tool_input) -> str:
    if not isinstance(tool_input, dict):
        return "full"
    offset = tool_input.get("offset")
    limit = tool_input.get("limit")
    offset = offset if _is_number(offset) else None
    limit = limit if _is_number(limit) else None
    if offset is None and limit is None:
        return "full"
    return "%s:%s" % (0 if offset is None else offset, 0 if limit is None else limit)


def _response_text(tool_response) -> str:
    if isinstance(tool_response, str):
        return tool_response
    if not isinstance(tool_response, dict):
        return ""
    if tool_response.get("content"):
        return str(tool_response["content"])
    out = str(tool_response.get("stdout") or "") + str(tool_response.get("stderr") or "")
    if out:
        return out
    return str(tool_response.get("output") or "")


def _response_failed(tool_response) -> bool:
    if not isinstance(tool_response, dict):
        return False
    if tool_response.get("is_error") is True or tool_response.get("isError") is True:
        return True
    code = tool_response.get("exit_code", tool_response.get("exitCode", tool_response.get("code")))
    return _is_number(code) and code != 0


def _failure_text(tool_response) -> str:
    if isinstance(tool_response, dict) and tool_response.get("stderr"):
        return str(tool_response["stderr"])
    return _response_text(tool_response)


#: git subcommands that change the work tree or history.
_GIT_MUTATING_RE = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+|-c\s+\S+\s+)*"
    r"(?:commit|merge|apply|am|cherry-pick|rebase|revert|checkout|switch|restore|reset|mv|rm|pull)\b")
#: An interpreter fed a script on stdin (`python3 - <<EOF`), and a write call in it.
_HEREDOC_INTERPRETER_RE = re.compile(r"\b(?:python3?|node|ruby|perl)\b[^\n]*<<")
_SCRIPT_WRITE_RE = re.compile(
    r"\.write_(?:text|bytes)\(|open\([^)\n]*,\s*['\"][wax]|\.write\(|os\.(?:replace|rename|remove|unlink)\("
    r"|shutil\.(?:copy\w*|move|rmtree)\(|\.unlink\(|\.mkdir\(|json\.dump\(")


def shell_mutation(command, cwd) -> "tuple[bool, list[str]]":
    """(changed the project?, project-relative paths it named) for one Bash
    command. Paths come from `bash_write_guard`'s own parser (redirects, tee,
    `sed -i`, dd, `python -c` opens, cp/mv destinations) -- one shell grammar,
    not a second copy; a write outside `cwd` is no change to the project. A
    mutating git subcommand or a heredoc-fed script calling a write is a
    change whose paths are not knowable from the text."""
    if not isinstance(command, str) or not command.strip() or not cwd:
        return False, []
    paths: "list[str]" = []
    try:
        import bash_write_guard
        targets = bash_write_guard.shell_write_absolute_targets(
            command, include_copy_move=True, cwd=cwd)
    except Exception:  # noqa: BLE001 — a parser failure must not cost the record
        targets = []
    real_cwd = os.path.realpath(cwd)  # the parser resolves symlinks; so must the anchor
    for t in targets:
        # The parser over-collects (`sed -i` yields every token); the command has
        # already run, so a real target exists now.
        if not os.path.isfile(str(t)):
            continue
        resolved = relativize_project_path(str(t), real_cwd)
        if resolved and resolved["inside"] and resolved["path"] not in paths:
            paths.append(resolved["path"])
    opaque = bool(_GIT_MUTATING_RE.search(command)) or bool(
        _HEREDOC_INTERPRETER_RE.search(command) and _SCRIPT_WRITE_RE.search(command))
    return bool(paths) or opaque, paths


def normalize_event(payload: dict, cwd=None):
    """One redacted record from a raw hook payload, or None when there is
    nothing this linter learns from it (an unrecognised event, an unusable
    path). `cwd` overrides `payload["cwd"]` for callers that already resolved
    it (the main hook); tests pass it directly."""
    if not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name") or ""
    cwd = cwd or payload.get("cwd") or os.getcwd()

    if event == "PreCompact":
        return {"version": SCHEMA_VERSION, "kind": "compaction"}
    if event != "PostToolUse":
        return None

    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    raw_tool_input = payload.get("tool_input")
    tool_input: dict = raw_tool_input if isinstance(raw_tool_input, dict) else {}
    tool_response = payload.get("tool_response")
    output_bytes = len(_response_text(tool_response).encode("utf-8"))

    if tool_name in READ_TOOL_NAMES:
        target = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
        resolved = relativize_project_path(target, cwd)
        if not resolved:
            return None
        mtime = size = None
        if resolved["inside"]:
            try:
                st = os.stat(os.path.join(cwd, resolved["path"]))
                mtime, size = st.st_mtime, st.st_size
            except OSError:
                pass
        return {"version": SCHEMA_VERSION, "kind": "read", "path": resolved["path"],
                "inside": resolved["inside"], "range": read_range(tool_input),
                "mtime": mtime, "size": size, "output_bytes": output_bytes}

    if tool_name in MUTATION_TOOL_NAMES:
        target = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path")
        resolved = relativize_project_path(target, cwd)
        if not resolved:
            return None
        return {"version": SCHEMA_VERSION, "kind": "mutation", "path": resolved["path"],
                "inside": resolved["inside"],
                "documentation": is_documentation_path(resolved["path"]), "output_bytes": 0}

    if tool_name in SEARCH_TOOL_NAMES:
        raw_root = tool_input.get("path") or "."
        resolved = relativize_project_path(raw_root, cwd)
        root_path = resolved["path"] if resolved else "."
        return {"version": SCHEMA_VERSION, "kind": "search", "tool": tool_name,
                "broad": is_broad_search_root(root_path), "root": root_path,
                "output_bytes": output_bytes}

    if tool_name != "Bash":
        return {"version": SCHEMA_VERSION, "kind": "other", "output_bytes": output_bytes}

    command = tool_input.get("command")
    fingerprint = command_fingerprint(command)
    if not fingerprint:
        return {"version": SCHEMA_VERSION, "kind": "other", "output_bytes": output_bytes}

    failed = _response_failed(tool_response)
    mutates, mutated_paths = (False, []) if failed else shell_mutation(command, cwd)
    return {
        "version": SCHEMA_VERSION, "kind": "command",
        "mutates": mutates, "mutated_paths": mutated_paths,
        "mutated_documentation": [is_documentation_path(x) for x in mutated_paths],
        "command_hash": fingerprint["hash"], "command_class": fingerprint["class"],
        "arg_shape": fingerprint["arg_shape"], "failed": failed,
        "error_sig_hash": error_signature(_failure_text(tool_response)) if failed else None,
        "nondeterministic": is_nondeterministic_command(fingerprint, command),
        "full_suite": is_full_suite_command(command), "output_bytes": output_bytes,
    }
