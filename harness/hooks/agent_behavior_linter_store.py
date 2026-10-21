#!/usr/bin/env python3
"""agent_behavior_linter_store.py — fold a session's redacted event records
into the aggregate the detectors read.

Deliberate departure from AK's `store.cjs`: AK persists the FOLDED aggregate
and read-modify-writes it on every event. This harness's store rule forbids
that (`harness-contract.md`: "append-only JSONL, no read-modify-write"), so
the main hook instead APPENDS one record per PostToolUse/PreCompact call to a
per-session JSONL journal (its own concern, in agent_behavior_linter.py) and
this module re-folds the WHOLE journal at Stop time — a read, never a
rewrite. A session's own event count bounds the journal size; nothing here
evicts or rotates it.
"""


def empty_state() -> dict:
    return {
        "counters": {"reads": 0, "searches": 0, "commands": 0, "mutations": 0,
                      "output_bytes": 0, "compactions": 0},
        "files": {}, "commands": {}, "searches": [], "full_suite_command": None,
    }


def _mark_mutated(state: dict, path, documentation: bool) -> None:
    """One project file changed: every earlier read of it is void, and any
    command that failed before may now behave differently."""
    entry = state["files"].get(path) or {
        "reads": 0, "last_mtime": None, "last_size": None, "ranges": [], "mutated_at": None}
    entry["reads"] = 0
    entry["last_mtime"] = None
    entry["last_size"] = None
    entry["mutated_at"] = True
    entry["documentation"] = documentation
    state["files"][path] = entry
    for cmd_entry in state["commands"].values():
        cmd_entry["mutated_since"] = True


def apply_event(state: dict, record) -> dict:
    """Fold one record into `state` IN PLACE, and return it (for chaining)."""
    if not isinstance(state, dict) or not isinstance(record, dict):
        return state
    kind = record.get("kind")

    if kind == "compaction":
        # Re-reading a file after compaction is correct behavior, not waste —
        # the evidence that would accuse it is dropped, not carried forward.
        state["counters"]["compactions"] += 1
        state["files"] = {}
        state["commands"] = {}
        state["searches"] = []
        return state

    state["counters"]["output_bytes"] += record.get("output_bytes") or 0

    if kind == "read":
        state["counters"]["reads"] += 1
        if not record.get("inside"):
            return state
        path = record.get("path")
        entry = state["files"].get(path) or {
            "reads": 0, "last_mtime": None, "last_size": None, "ranges": [], "mutated_at": None}
        unchanged = (entry["last_mtime"] == record.get("mtime")
                     and entry["last_size"] == record.get("size"))
        entry["reads"] = entry["reads"] + 1 if unchanged else 1
        entry["last_mtime"] = record.get("mtime")
        entry["last_size"] = record.get("size")
        if record.get("range") not in entry["ranges"]:
            entry["ranges"] = (entry["ranges"] + [record.get("range")])[-8:]
        state["files"][path] = entry
        return state

    if kind == "mutation":
        state["counters"]["mutations"] += 1
        if not record.get("inside"):
            return state
        _mark_mutated(state, record.get("path"), record.get("documentation") is True)
        return state

    if kind == "search":
        state["counters"]["searches"] += 1
        state["searches"] = (state["searches"] + [{
            "root": record.get("root"), "tool": record.get("tool"),
            "broad": record.get("broad") is True}])[-200:]
        return state

    if kind == "command":
        state["counters"]["commands"] += 1
        if record.get("mutates"):
            # A shell change: a mutation like any Edit. Its paths, when the text
            # names them, mark those files; one it cannot name (a commit, a
            # script) leaves the change's KIND unknown -- `opaque_mutation`.
            state["counters"]["mutations"] += 1
            paths = record.get("mutated_paths") or []
            docs = record.get("mutated_documentation") or []
            for k, path in enumerate(paths):
                _mark_mutated(state, path, bool(k < len(docs) and docs[k]))
            if not paths:
                state["opaque_mutation"] = True
                for cmd_entry in state["commands"].values():
                    cmd_entry["mutated_since"] = True
        if record.get("full_suite"):
            state["full_suite_command"] = {"class": record.get("command_class")}
        if not record.get("failed"):
            state["commands"].pop(record.get("command_hash"), None)
            return state
        h = record.get("command_hash")
        previous = state["commands"].get(h)
        same_failure = bool(
            previous and previous.get("error_sig_hash") == record.get("error_sig_hash")
            and previous.get("mutated_since") is not True)
        state["commands"][h] = {
            "class": record.get("command_class"),
            "attempts": (previous["attempts"] + 1) if same_failure else 1,
            "error_sig_hash": record.get("error_sig_hash"),
            "nondeterministic": record.get("nondeterministic") is True,
            "mutated_since": False,
        }
        return state

    return state


def fold(records) -> dict:
    """The aggregate state for an ordered iterable of records (oldest first)."""
    state = empty_state()
    for rec in records:
        apply_event(state, rec)
    return state
