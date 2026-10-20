"""hs_family.verb_help — the shared `<domain> <verb> --help` interception,
used by every hs-* CLI whose domains dispatch through `hs_family.registry`'s
`module:`/`entry:` pair.

Extracted from `hs_run.py` (see that module's own "help" section for the
measured bug this fixes: `-h`/`--help` anywhere in a verb's argv used to fall
straight through to the verb's own entry point, which then RAN FOR REAL —
`cook open --help` opened a plan; `cook close --help` closed one). A second
CLI dispatching through the SAME registry shape hits the identical hole the
moment it grows a real verb with a side effect: measured, `hs-code graph
snapshot --help` ran the snapshot (dry-run, exit 0) instead of printing
usage — `hs-code graph --help` (the DOMAIN level, handled separately by each
shell's own dispatcher) was already correct. Fixed once, here, so a third
`hs-*` CLI dispatching through this same registry shape inherits the fix
rather than needing its own copy.

A caller intercepts BEFORE resolving/calling the verb's own entry — see
`wants_help`'s own docstring for why the scan covers the WHOLE verb argv,
not just its first token. What the help SAYS is the verb's own parser (the
family core, `hs_family.cli.base`), for every hs-* CLI alike.
"""
from __future__ import annotations


def wants_help(verb_args) -> bool:
    """True when `-h`/`--help` appears ANYWHERE in a verb's own argv.

    Anywhere, not just the first token: a flag can precede it
    (`cook open --plan foo --help`), so a first-token-only check would let
    that shape fall through to the verb's real entry — the exact bug this
    module exists to close.
    """
    return any(a in ("-h", "--help") for a in (verb_args or ()))
