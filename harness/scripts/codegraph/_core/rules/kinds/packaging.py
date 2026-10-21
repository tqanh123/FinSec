"""`packaging` — does the packages tree package the way it declares.

Ported (algorithm-faithful, narrowed) from
`ob-tools@60075cb:packages/portkit/src/portkit/gates/packaging.py`.

WHY THIS KIND EXISTS
    PEP 420 implicit namespace packages fail QUIETLY: an `__init__.py` sitting at the
    namespace root turns it into a REGULAR package. A regular package cannot be
    assembled from several distributions — exactly ONE wins on `sys.path` and every
    `import ns.<other>` raises `ModuleNotFoundError`. The worst part: it WORKS in the
    dev checkout (everything sits in one tree) and only dies once installed from a
    wheel, in somebody else's environment.

THREE LAWS, EACH ABLE TO RED ALONE
    1. `__init__.py` at the namespace root                -> RED (see above)
    2. dist directory <-> `[project].name` <-> import path -> RED on mismatch;
       `acme-mode-deep` must live at `acme/mode_deep` and its `[project].name` must
       equal the directory name, or `pip install` installs a package under another
       name.
    3. `[tool.<ns>] tier` present AND inside the declared ladder -> RED when absent
       or unknown: without it, layer rules treat every edge of this dist as
       UNDECIDABLE, and a tree of undecidable edges is a green tree.

NOT PORTED HERE, ON PURPOSE: upstream's own fourth law (`[project.dependencies]` vs
measured import edges, both ways) is dropped — `hs-code check pkgsync`'s own eight laws
(`missing`/`stale`/`third_missing`/`third_stale`/`ns_unknown`/`ext_stale`/
`version_conflict`/`plugin_undeclared`) already cover exactly that ground, off the same
measured edges, over the same packages. Two independently maintained implementations
of one law is not completeness, it is a way for the two to quietly disagree; a project
wanting it under `hs-code check gates` runs `pkgsync` as its own gate row instead of a
second copy here.

WHERE PACKAGES AND THE NAMESPACE COME FROM — the T13 fix this port carries alongside
`pkg_door`. Upstream's own `packaging.py` probed a dist's code dir with a SECOND,
differently-shaped function (`code_dir`) than `pkg_door.py`'s own `package_roots` — two
package detectors judging two different trees at the same moment. This kind reads
`codegraph.check.tree.workspace()` instead, the one probe (`package_code_dir`) every
reader of a workspace package's code already shares. The namespace is the project's OWN
`namespace_root` (`codemap.yaml`), read the same way `hs-code check pkgsync` reads it —
never a rule param, never a hard-coded name; a project declaring none has NO SUBJECT
for these three laws (a note, never a green pretending to have checked something).

WHAT THIS KIND CANNOT SEE
    * It installs NOTHING: a `pyproject.toml` with a broken `[build-system]` passes
      here and breaks at `pip install`.
    * Whether the dist directory really is the workspace's ONLY copy of that name.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from ..base import RuleContext, RuleResult, RulesError, Violation, register_kind, require_params


def pkg_path_of(dist_id: str, ns: str) -> "str | None":
    """`ns-mode-deep` -> `ns/mode_deep`; `None` when the dist name carries no
    namespace prefix. One direction on purpose: deriving the path FROM the name is
    binding, then the path is checked to exist."""
    if not ns or not dist_id.startswith(f"{ns}-"):
        return None
    return f"{ns}/" + dist_id[len(ns) + 1:].replace("-", "_")


def load_toml(p: Path) -> dict:
    if not p.is_file():
        raise RulesError(f"{p} missing — a workspace package must be a dist")
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise RulesError(f"{p} does not parse: {e}") from e


class PackagingCheck:
    CAUSES = ("namespace-init", "path-mismatch", "tier-missing", "tier-unknown")

    def __init__(self, rule_id: str, params: "dict[str, Any]") -> None:
        self.rule_id = rule_id
        self.ladder: "set[str]" = set(params["tiers"])

    def check(self, ctx: RuleContext) -> RuleResult:
        from codegraph.check.tree import workspace
        descriptor, pkgs = workspace(ctx.repo)
        ns = descriptor.namespace_root or ""
        notes: "list[str]" = []
        if not pkgs:
            notes.append("0 packages under any `workspace` root — the three packaging "
                         "laws have NO SUBJECT here")
            return RuleResult(self.rule_id, "error", (), tuple(notes))
        if not ns:
            notes.append("no `namespace_root` declared in codemap.yaml — the three "
                         "packaging laws have NO SUBJECT here")
            return RuleResult(self.rule_id, "error", (), tuple(notes))

        v: "list[Violation]" = []
        for pkg in pkgs:
            toml = load_toml(pkg.dir / "pyproject.toml")
            proj = toml.get("project") or {}

            # 1 — an `__init__.py` at the namespace root
            ns_init = pkg.code / ns / "__init__.py"
            if ns_init.is_file():
                rel = ns_init.relative_to(ctx.repo)
                v.append(Violation(
                    self.rule_id, "namespace-init",
                    f"{rel} — namespace root `{ns}` has an `__init__.py`: it becomes "
                    f"a REGULAR package and only ONE dist wins on sys.path",
                    {"dist": pkg.name, "path": str(rel)}))

            # 2 — dist directory <-> package path <-> [project] name
            want_path = pkg_path_of(pkg.name, ns)
            if want_path is None:
                v.append(Violation(
                    self.rule_id, "path-mismatch",
                    f"{pkg.name} — carries no namespace prefix `{ns}-`",
                    {"dist": pkg.name}))
            elif not (pkg.code / want_path).is_dir():
                v.append(Violation(
                    self.rule_id, "path-mismatch",
                    f"{pkg.name} — with that name the package must live at "
                    f"`{(pkg.code / want_path).relative_to(ctx.repo)}`; no such directory",
                    {"dist": pkg.name, "want": want_path}))
            if proj.get("name") and proj["name"] != pkg.name:
                v.append(Violation(
                    self.rule_id, "path-mismatch",
                    f"{pkg.name} — `[project] name = {proj['name']!r}` differs from "
                    f"the dist directory name; `pip install {pkg.name}` installs a "
                    f"package under another name",
                    {"dist": pkg.name, "declared": proj["name"]}))

            # 3 — [tool.<ns>] tier present and inside the ladder
            tier = ((toml.get("tool") or {}).get(ns) or {}).get("tier")
            if not tier:
                v.append(Violation(
                    self.rule_id, "tier-missing",
                    f"{pkg.name} — no `[tool.{ns}] tier`; layer rules will treat "
                    f"every edge of this dist as UNDECIDABLE, and that is a green tree",
                    {"dist": pkg.name}))
            elif tier not in self.ladder:
                v.append(Violation(
                    self.rule_id, "tier-unknown",
                    f"{pkg.name} — `tier = {tier!r}` is not in the ladder "
                    f"({', '.join(sorted(self.ladder))})",
                    {"dist": pkg.name, "tier": tier}))
        return RuleResult(self.rule_id, "error", tuple(v), tuple(notes))


register_kind("packaging", languages=("python",))(require_params("tiers")(PackagingCheck))
