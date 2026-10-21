#!/usr/bin/env python3
"""fs_guard — script-path containment helper for harness script writes.

A harness script that resolves its target through `assert_under(path, zone)`
BEFORE opening the file cannot write outside the zone's declared root(s):
`..` traversal, symlink escape, and prefix look-alikes (`docs-extra`) are all
defeated by resolve-then-contain. The zone table is DATA-DRIVEN from
harness/data/ownership.yaml — adding a zone is a config edit, not a code edit.

Two-root binding (global install): a zone's base root depends on which tree the
zone lives in. Project zones (docs/plans/state) are PER-PROJECT and resolve
against the project root; the bin zone (standards) is SHIPPED and resolves
against the bin root. Under a global layout (HARNESS_BIN_ROOT set) fs_guard is
authoritative — it picks the base per zone regardless of the `root` a caller
passes, so a caller that still hands over bin-root (e.g. plan_approval) cannot
mis-zone a project write; a project zone whose project root cannot resolve fails
CLOSED (FenceError), never widening to the bin. Under a self-hosted / legacy
layout (HARNESS_BIN_ROOT unset, bin == project) the caller's explicit `root` is
honored exactly as before, preserving every existing caller and test.

Honesty caveat (carried from the source corpus, mirrors the helper's real
reach): this is a SCRIPT-path guard ONLY. It raises before a script write
lands outside its zone. It CANNOT stop a raw LLM `Write`, nor an LLM composing
a body directly to disk — those are governed by the prose contract + the
review/CI invariants, not by this assert. It is the harness disciplining its
own scripts, nothing more.

A missing or malformed ownership.yaml fails LOUD at import time: a guard that
silently degrades to "allow everything" protects nothing. Compliance hooks
importing this module turn that ImportError-time crash into exit 2 + guidance
via their fail-closed wrapper.
"""

import os
import subprocess
from pathlib import Path

def _config_path(name, explicit=None):
    """Resolved through the one door (harness/scripts/config_layers.py); the layer stack
    is declared in harness/data/config-layers.yaml, never spelled twice."""
    import config_layers
    return config_layers.resolve_path(name, explicit=explicit)[0]

# Zone → which tree it lives in. Project zones are per-project (resolve against
# the project root); the bin zone is shipped (resolve against the bin root). An
# unknown zone defaults to project-scoped (fail-closed) under a global layout.
_BIN_ZONES = ("standards",)


class FenceError(Exception):
    """Raised when a script-driven write would land outside its zone root(s)."""


def _ownership_path(path=None) -> Path:
    return _config_path("ownership.yaml", explicit=path)


def _load_zones() -> dict:
    """zone name → list of repo-root-relative root strings. Fails LOUD."""
    import yaml_io  # lazy so the error message below owns the missing-dep case too

    p = _ownership_path()
    try:
        raw = yaml_io.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(
            "fs_guard: ownership file missing at %s — the zone table is the "
            "guard's spine; restore harness/data/ownership.yaml" % p
        )
    zones = (raw or {}).get("zones") if isinstance(raw, dict) else None
    if not isinstance(zones, dict) or not zones:
        raise RuntimeError(
            "fs_guard: ownership file %s is malformed — expected a top-level "
            "`zones:` mapping of zone -> [roots]" % p
        )
    table = {}
    for zone, roots in zones.items():
        if isinstance(roots, str):
            roots = [roots]
        if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
            raise RuntimeError(
                "fs_guard: zone %r in %s must map to a list of path strings" % (zone, p)
            )
        table[str(zone)] = [r.strip() for r in roots if r.strip()]
    return table


# Loaded at import: a broken table must surface immediately, not at first write.
ZONES = _load_zones()


def _zone_base(zone: str, root):
    """The base root a zone's declared paths resolve against — bin vs project per
    the two-root binding. See the module docstring for the self-host vs global
    contract."""
    import harness_paths
    if not os.environ.get("HARNESS_BIN_ROOT"):
        # Self-host / legacy: honor the caller's explicit root (bin == project),
        # falling back to bin_root() when none was passed. Preserves every
        # existing caller/test byte-for-byte.
        return Path(root).resolve() if root is not None else harness_paths.bin_root()
    # Global layout: fs_guard picks the base per zone, authoritative over `root`.
    if zone in _BIN_ZONES:
        return harness_paths.bin_root()
    dr, why = harness_paths.data_root_and_reason()
    if harness_paths.data_root_unresolved(dr):
        raise FenceError(
            "zone %r needs a project root but none resolved (%s) — refusing to "
            "widen the fence to the bin" % (zone, why))
    return dr.parent


def allowed_roots(zone: str, root) -> list:
    """Resolved allowed write roots for `zone` under repo `root`.

    Zone roots are declared repo-relative; a declared root that resolves
    OUTSIDE the repo root (absolute path — pathlib drops `base` when joining
    one — or `../` traversal) would silently widen the fence to anywhere on
    the filesystem, so it raises instead of being honored."""
    if zone not in ZONES:
        raise FenceError(
            "unknown zone %r; known zones: %s" % (zone, sorted(ZONES))
        )
    base = Path(root).resolve()
    out = []
    for r in ZONES[zone]:
        resolved = (base / r).resolve()
        if resolved == base or not resolved.is_relative_to(base):
            raise FenceError(
                "zone %r declares root %r which resolves outside the repo "
                "root %s — zone roots must stay repo-relative; fix "
                "harness/data/ownership.yaml" % (zone, r, base)
            )
        out.append(resolved)
    return out


def assert_under(path, zone: str, root=None) -> Path:
    """Return the resolved `path` if contained under one of `zone`'s roots,
    else raise FenceError. Raises BEFORE any write, so a blocked target never
    touches disk.

    `path` may be relative to the zone's base root or absolute; both are
    resolved (collapsing `..`, following symlinks on existing components). The
    boundary directory itself counts as in-fence. The base root is bound per zone
    (project vs bin); an explicit `root` is honored only under a self-hosted
    layout — see the module docstring.
    """
    return _contain(path, zone, _zone_base(zone, root))


def assert_under_root(path, zone: str, root) -> Path:
    """`assert_under` against a project root the OPERATOR named — a register's
    `--root` — instead of the session's own project.

    `assert_under` rebinds a project zone to the session's data home under a
    global layout, so a caller handing over the bin root by accident cannot
    mis-zone a write. A tool whose whole contract is "operate on the tree at
    --root" needs the opposite: a temp copy or a linked worktree is a different
    project from the session's, and measuring its write against the session's
    docs refuses every such write. Same resolve-then-contain; one extra refusal
    keeps the reason for the rebinding: under a global layout a project zone
    never resolves into the shared bin."""
    base = Path(root).resolve()
    if os.environ.get("HARNESS_BIN_ROOT") and zone not in _BIN_ZONES:
        import harness_paths
        bin_root = harness_paths.bin_root().resolve()
        if base == bin_root or base.is_relative_to(bin_root):
            raise FenceError(
                "refusing to write the %r zone under %s: it is inside the shared "
                "bin %s, and a project zone never resolves into the bin"
                % (zone, base, bin_root))
    return _contain(path, zone, base)


def assert_under_plan_tree(path, plan_dir, root=None) -> Path:
    """`assert_under(path, "plans")` for a write into an EXISTING plan named by path: the
    zone is anchored at the working tree the plan lives in, as the plan's other writers
    read it (`plan_graph.root_for_plan_dir`), when that tree is the project or the same
    project in another worktree of its repo -- a plan in the host checkout named from a
    linked worktree (or the reverse). A plan anywhere else gets the project zone, and its
    refusal. `root` is the project hint `assert_under` takes (default: the session's)."""
    import harness_paths
    import plan_graph
    base = _zone_base("plans", root if root is not None else harness_paths.project_root())
    tree = Path(plan_graph.root_for_plan_dir(plan_dir)).resolve()
    if tree != base and _same_project(tree, base):
        return _contain(path, "plans", tree)
    return _contain(path, "plans", base)


def _same_project(a: Path, b: Path) -> bool:
    """Both dirs sit at the same path inside worktrees of one repo: the same git common
    dir and the same prefix below the worktree's top (a monorepo's `sub/` matches only
    another `sub/`). A dir outside git is no worktree of anything."""
    ids = [_git_identity(d) for d in (a, b)]
    return ids[0] is not None and ids[0] == ids[1]


def _git_identity(d: Path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        # no --path-format: a git before 2.31 echoes a flag it does not know as a line
        r = subprocess.run(["git", "-C", str(d), "rev-parse", "--git-common-dir", "--show-prefix"],
                           capture_output=True, text=True, env=env, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = r.stdout.splitlines()
    if r.returncode != 0 or not lines or not lines[0]:
        return None
    return (str((Path(d) / lines[0]).resolve()), lines[1] if len(lines) > 1 else "")


def _contain(path, zone: str, base) -> Path:
    roots = allowed_roots(zone, base)

    target = Path(path)
    if not target.is_absolute():
        target = Path(base) / target
    resolved = target.resolve(strict=False)

    for r in roots:
        if resolved == r or resolved.is_relative_to(r):
            return resolved

    pretty = ", ".join(str(r) for r in roots)
    raise FenceError(
        "refusing to write outside the %r zone: %s is not under any of [%s]"
        % (zone, resolved, pretty)
    )
