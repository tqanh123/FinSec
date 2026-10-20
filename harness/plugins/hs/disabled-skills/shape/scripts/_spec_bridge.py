#!/usr/bin/env python3
"""_spec_bridge — the ONE isolated-loader dance hs:shape uses to reach into
hs:spec's scripts (`spec_graph_port`, `frontmatter_parser_port`,
`id_grammar`).

The `_port` suffix is what makes those names unambiguous: each is carried by
exactly one file in the tree, so a bare-name import can no longer resolve to
the harness-internal docs-governance copy depending on which directory reached
`sys.path` first. That hazard is retired; this loader is no longer the thing
standing between hs:shape and the wrong module.

What it still does: hs:spec's scripts are a flat directory, not a package, so
reaching them means putting that directory on `sys.path`. Doing it under a
save/restore of `sys.modules`/`sys.path` keeps the reach scoped to the call
rather than leaving hs:spec's modules resident in whatever process imported
hs:shape, and keeps the cross-skill coupling at the one relative path below
instead of one copy of this dance per reader.

Every hs:shape reader that touches hs:spec's frontmatter/serves grammar goes
through this one loader instead of re-rolling the save/restore dance per
caller (`serves_resolver.py` and `experiment_spec.py` used to each carry a
near-identical private copy of this — collapsed here).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Sequence

# Dependency order matters: a module's own sibling imports must already be in
# sys.modules by the time it is exec'd (frontmatter_parser_port needs
# encoding_utils_port; spec_graph_port needs id_grammar and
# frontmatter_parser_port).
_ALL_NAMES = ("encoding_utils_port", "id_grammar", "frontmatter_parser_port",
              "spec_graph_port")


def spec_scripts_dir() -> Path:
    # .../shape/scripts/_spec_bridge.py -> .../spec/scripts
    return Path(__file__).resolve().parent.parent.parent / "spec" / "scripts"


def load_spec_modules(names: Sequence[str] = _ALL_NAMES):
    """Load `names` (in dependency order) from hs:spec's scripts dir under an
    isolated sys.modules/sys.path scope, then restore. Returns the LAST named
    module -- callers pass just the names they need (e.g. `("id_grammar",)`
    or `("encoding_utils_port", "frontmatter_parser_port")`) plus whatever those depend
    on."""
    scripts_dir = spec_scripts_dir()
    saved_mods = {n: sys.modules.get(n) for n in names}
    saved_path = list(sys.path)
    sys.path.insert(0, str(scripts_dir))
    for n in names:
        sys.modules.pop(n, None)
    try:
        for n in names:
            src = scripts_dir / (n + ".py")
            spec = importlib.util.spec_from_file_location(n, src)
            if spec is None or spec.loader is None:
                raise ImportError("cannot load hs:spec module %s from %s" % (n, src))
            module = importlib.util.module_from_spec(spec)
            sys.modules[n] = module
            spec.loader.exec_module(module)
        return sys.modules[names[-1]]
    finally:
        for n in names:
            if saved_mods[n] is not None:
                sys.modules[n] = saved_mods[n]
            else:
                sys.modules.pop(n, None)
        sys.path[:] = saved_path


# The three fixed module-set wrappers every hs:shape reader needs. Homed here
# (not re-defined per reader) so the dependency tuple for each is stated ONCE —
# a reader adding one is a one-line import, not another byte-identical copy of
# the isolated-load dance. Peer readers stay uncoupled from each other: they
# depend only on this bridge, which they already import.
def load_frontmatter_parser():
    """hs:spec's frontmatter_parser_port (+ its encoding_utils_port dep)."""
    return load_spec_modules(("encoding_utils_port", "frontmatter_parser_port"))


def load_spec_graph():
    """hs:spec's spec_graph_port (+ its id_grammar/frontmatter/encoding deps)."""
    return load_spec_modules(("encoding_utils_port", "id_grammar",
                              "frontmatter_parser_port", "spec_graph_port"))


def load_id_grammar():
    """hs:spec's id_grammar (the parent-scoped ID grammar SSOT), isolated-loaded."""
    return load_spec_modules(("id_grammar",))
