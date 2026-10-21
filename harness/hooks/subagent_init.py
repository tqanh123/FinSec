#!/usr/bin/env python3
"""subagent_init.py — SubagentStart context injector (telemetry, fail-open).

When a subagent is spawned, inject the FULL resolved register — the same block the
main session receives at SessionStart, built by the shared register_block builder so
the two surfaces cannot drift — followed by the existing pointer at the harness rule
layer, the standards tree, and an ownership reminder, the RBAC write-scope reminder
(hoisted here so it reaches default agents that carry no authored .md), and a one-line
reinforcement that extends the register scope-fence over ALL the subagent's output
(reasoning, reports, and messages back to the lead — not only the journal/critique
cases the fence names), and a memory-scope line: a brief that limits writes also
rules out agent-memory writes; spawned below the project root, it is also told the
root's memory dir, since Claude Code names one relative to the spawn cwd.

Why the full register: any subagent can generate code, so the code_style/audience
directives must reach it too; gating by agent_type would buy a branch for little —
the code_style body is inert for a report-only agent. The scope-fence travels with
the register verbatim, so the scope-fence invariant (a knob never touches evidence)
holds by construction on this path too.

Alongside the register it also carries a standards read-DIRECTIVE (a read-order, never
the file body): a task that writes/modifies code or tests is told to open
docs/code-standards.md first, and docs/system-architecture.md for a structural change.
The directive self-gates on the task ("IF you write/modify …") so no agent-type
allowlist is needed, and it rides BOTH return paths like the RBAC reminder.

Posture: telemetry, fail-OPEN — emits at most an additionalContext string and ALWAYS
exits 0. Disabled or any exception → the generic pointer (or no context), never a
blocked subagent. The register build is wrapped so an import/read failure degrades to
the pointer alone rather than dropping the spawn.
"""
import json
import sys
from pathlib import Path

# Resolve scripts/ by resolved path, not CWD-relative
# so both context hooks find the shared builder identically.
_HOOKS_DIR = Path(__file__).resolve().parent
for _p in (str(_HOOKS_DIR), str(_HOOKS_DIR.parent / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

HOOK_CLASS = "telemetry"
_HOOK = "subagent_init"

# Extends the register's scope-fence over the whole subagent surface. The
# fence text names the deliverable; a subagent also reasons and messages the lead,
# so spell out that the fence covers all of it, and that the agent's designed task
# behaviour outranks any register knob.
_SCOPE_REINFORCEMENT = (
    "Subagent-scope: the scope-fence above applies to ALL your output — reasoning, "
    "reports, AND messages back to the lead. Your designed task behaviour wins over "
    "any register knob."
)

# The write-scope reminder, hoisted here so it reaches EVERY subagent — including the
# built-in default agents (claude, general-purpose, Explore, ...) that have no authored
# .md to carry it. It rides BOTH return paths (full register and the degraded pointer)
# so a fail-open spawn still carries it.
#
# This is information, not a threat: a spawned agent cannot see the delegating turn, so
# it has no way of knowing the tree has a protected core until something tells it. The
# per-role lane table is gone — every agent writes anywhere outside that core.
_RBAC_SCOPE = (
    "Write scope: you may write anywhere in the tree EXCEPT the protected core (the "
    "harness binary and its policy files). Those are shared machinery your task almost "
    "never needs to touch; if it genuinely does, say so and return the intended change "
    "rather than applying it, so the lead can decide."
)


# Agent memory is a write like any other, and the one write an agent is TOLD to make
# by something other than its brief: every agent declaring `memory:` gets Claude
# Code's own instructions to save into its memory directory, and that switch is per
# agent definition (or session-global), never per spawn. So a caller whose brief
# says "write nothing outside X" has no lever to turn it off — only this line, which
# makes the brief's limit outrank those instructions. Prose, not enforcement: the
# brief is natural language, and no tool-call payload carries a machine-readable
# write scope a hook could check. Rides both return paths, like the RBAC reminder.
_MEMORY_SCOPE = (
    "Agent memory: saving to your memory directory (.claude/agent-memory/ or "
    ".claude/agent-memory-local/) is a write into the repo. When your brief limits where "
    "you may write (read-only, report-only, or only under a named path), make NO memory "
    "writes — the brief outranks the memory instructions; put anything worth keeping in "
    "your final report instead."
)

# The standards read-directive, hoisted here so it reaches EVERY subagent on both
# return paths. It is self-gating on the task ("IF … writes or modifies code or tests")
# so no agent-type allowlist is needed — a report-only task is told it may skip. This
# carries a READ ORDER, not the file body: the subagent opens the file itself, so the
# additionalContext channel stays small (the ~10000c cap is silent-truncating).
_STANDARDS_DIRECTIVE = (
    "Standards: IF your delegated task writes or modifies code or tests, you MUST read "
    "docs/code-standards.md BEFORE writing — the shared standard the harness enforces. "
    "For a structural/architectural change also read docs/system-architecture.md. This "
    "is a read directive, not content: open the file yourself. A report-only task may skip it."
)


def _memory_location(payload) -> str:
    """The root's memory dir, named when the spawn cwd sits below the project root.

    Claude Code names a `memory: project` agent's directory relative to the session's
    cwd and says it already exists; below the root that is neither true nor the
    project's memory, and a save there strands a dir `agent_memory_stray` sweeps."""
    import os
    try:
        root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or "").resolve()
        cwd = Path((payload or {}).get("cwd") or "").resolve()
        agent = str((payload or {}).get("agent_type") or "")
    except Exception:  # noqa: BLE001 - fail-open: no correction
        return ""
    if not agent or not str(root) or root not in cwd.parents:
        return ""
    name = agent.replace(":", "-")
    return ("Agent memory location: this agent was started in %s, below the project root, "
            "so a memory directory named under %s/.claude/ does not hold this project's "
            "memory. If you read or save agent memory, use %s instead."
            % (cwd, cwd, root / ".claude" / "agent-memory" / name))


def _pointer(agent: str) -> str:
    return (
        "[Harness subagent: %s] You are running inside a file-based SDLC harness. "
        "Follow the shared rule layer in harness/rules/ (load on demand; routing in "
        "CLAUDE.md), the standards in docs/ (system-architecture.md, code-standards.md) "
        "and the structured rule tree in harness/standards/. Stay within your delegated "
        "file ownership and acceptance criteria — report findings rather than mutating "
        "outside your scope unless explicitly tasked. Generated reports follow "
        "harness/data/output.yaml." % agent
    )


def context_text(payload) -> str:
    """The additionalContext injected into the subagent. Pure; never raises.

    Full register (shared builder) + pointer + scope reinforcement when the register
    builds; the generic pointer alone if register_block/output_config are unavailable
    (fail-open — a degraded inject is still a spawned subagent)."""
    agent = "subagent"
    try:
        agent = (payload or {}).get("agent_type") or "subagent"
    except Exception:
        pass
    pointer = _pointer(agent)
    try:
        import register_block
        import output_config
        register = register_block.build_register(
            output_config.resolve_all(), surface="subagent")
    except Exception:  # noqa: BLE001 - degrade to the generic pointer, never raise
        return "\n".join(x for x in (pointer, _RBAC_SCOPE, _MEMORY_SCOPE,
                                      _memory_location(payload), _STANDARDS_DIRECTIVE) if x)
    return "\n".join(x for x in (register, pointer, _RBAC_SCOPE, _MEMORY_SCOPE,
                                  _memory_location(payload), _STANDARDS_DIRECTIVE,
                                  _SCOPE_REINFORCEMENT) if x)


def _emit(text: str) -> None:
    # Route through the shared co-emit chokepoint (subagent_start event) so this surface
    # stays on the same config layer as the main-session injectors. A subagent has no
    # human watching, so subagent_start ships system_message:false (model-only) — but the
    # channel now lives in ONE place. Fail-open to the raw additionalContext.
    try:
        import context_surface_config as _cs
        _cs.emit("subagent_start", text)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return
    except Exception:  # noqa: BLE001 — register injection must never break a subagent
        pass
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": text,
        }
    }))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> None:
    try:
        import hook_runtime
        if not hook_runtime.hook_enabled(_HOOK, HOOK_CLASS):
            sys.exit(0)
        payload = hook_runtime.read_stdin_json()
        _emit(context_text(payload))
    except Exception as e:
        # fail-open: a broken injector degrades to no context, never blocks a
        # subagent — but record the crash so the failure leaves an audit trail.
        try:
            import hook_runtime
            hook_runtime.log_hook_error(_HOOK, e)
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
