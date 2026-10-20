"""The template `new-dist`/`new-module` read off the TREE when `codemap.yaml`
declares no `scaffold.templates` -- the shape a codemap built by `hs:codemap`
has, which left both verbs refusing on every such repo.

Still no project word in code (`engine`'s doctrine): every value comes from
the tree or the descriptor.

    dist    an EXEMPLAR dist under a `kind: workspace` root whose
            `[tool.<namespace_root>] tier` equals `--tier` (the table `check
            pkgsync`/`scc` already read; the tier is DECLARED, never parsed
            from a name); among those, the one sharing the most leading
            `-` tokens with the new name, then by name -- a family's own
            dist over whichever sorts first. Carried: `requires-python`,
            `[build-system]`, every `[tool.*]` table that does not name the
            exemplar itself; `[tool.<ns>]` carries `tier` plus every key
            whose exemplar value another dist also holds (a membership such
            as `release_group`; a value only the exemplar holds is its
            identity), and `version` is the one that group shares, else
            `0.1.0` with the spread in `warnings`. The module
            path follows the exemplar's own name->module shape: its first `k`
            name tokens are package dirs, the rest one `_`-joined leaf
            (`a-b-store-pg` -> `a/b/store_pg`, k=2), learned per exemplar.
    module  one file in the directory that holds the parent package, found
            under the descriptor's `import_roots`.

WHAT IT CANNOT SEE
    * The exemplar's prose files (`AGENTS.md`, `README`...) -- named in
      `warnings`, never copied: their words describe the exemplar.
    * A dependency the new dist needs: `dependencies = []`, always.
    * A dist whose module does not follow the name->module shape: no
      exemplar qualifies, and `--module` is the way through.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from .errors import ScaffoldError

def _docstring(question: str, what: str) -> str:
    text = question.replace('"""', '\\"\\"\\"') if question else f"TODO: the one question this {what} answers."
    return f'"""{text}"""\n'


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {_toml_value(x)}" for k, x in v.items()) + "}"
    raise ScaffoldError(f"cannot carry TOML value of type {type(v).__name__}")


def _toml_tables(prefix: str, table: dict, out: "list[str]") -> None:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    if scalars or not table:
        out.append(f"\n[{prefix}]")
        out.extend(f"{k} = {_toml_value(v)}" for k, v in scalars.items())
    for k, v in table.items():
        if isinstance(v, dict):
            _toml_tables(f"{prefix}.{k}", v, out)


def _exemplar_module(dist: Path) -> "tuple[Path, int, list[str]] | None":
    """(src dir, k, module parts) when `dist`'s module follows the shape."""
    src = dist / "src" if (dist / "src").is_dir() else dist
    tokens = dist.name.split("-")
    for k in range(len(tokens)):
        parts = tokens[:k] + ["_".join(tokens[k:])]
        if src.joinpath(*parts).is_dir():
            return src, k, parts
    return None


def _dists(repo: Path, descriptor: Any) -> "list[tuple[Path, dict]]":
    out = []
    for root in descriptor.roots:
        if root.kind != "workspace":
            continue
        for d in sorted((repo / root.path).iterdir()) if (repo / root.path).is_dir() else ():
            pp = d / "pyproject.toml"
            if not pp.is_file():
                continue
            try:
                out.append((d, tomllib.loads(pp.read_text(encoding="utf-8"))))
            except tomllib.TOMLDecodeError:
                continue
    return out


def workspace_dists(repo: Path, descriptor: Any) -> "list[tuple[Path, dict]]":
    """Every dist directory (with a parseable `pyproject.toml`) under a `kind:
    workspace` root, with its parsed pyproject."""
    return _dists(Path(repo), descriptor)


def module_of(dist: Path) -> "str | None":
    """The dotted import path a dist keeps at the path its name spells
    (`a-b-store-pg` -> `a.b.store_pg`), or None when it keeps it elsewhere."""
    shape = _exemplar_module(Path(dist))
    return ".".join(shape[2]) if shape else None


def prose_warning(exemplar_name: str, prose: "list[str]") -> str:
    return f"not rendered (they describe {exemplar_name}): {', '.join(prose)}"


def infer_dist_plan(repo: Path, descriptor: Any, name: str, *, tier: str = "",
                    question: str = "", module: "str | None" = None) -> "dict[str, Any]":
    """The inferred dist with its parts kept apart: `files`, `warnings` (the
    exemplar's own prose files NOT among them), `exemplar` (repo-relative),
    `exemplar_name`, `prose` (those file names), `dist_dir` and `module` (dotted)."""
    repo = Path(repo)
    ns = descriptor.namespace_root or ""
    dists = _dists(repo, descriptor)
    if not dists:
        raise ScaffoldError(
            "no scaffold.templates declared and no dist under a `kind: workspace` root to "
            "read one off -- declare codemap.yaml scaffold.templates.<key> (kind: dist)")

    def tier_of(doc: dict) -> "str | None":
        return ((doc.get("tool") or {}).get(ns) or {}).get("tier") if ns else None

    pool = [(d, doc) for d, doc in dists if not tier or tier_of(doc) == tier]
    tokens = name.split("-")

    def kin(d: Path) -> int:
        n = 0
        for a, b in zip(d.name.split("-"), tokens):
            if a != b:
                break
            n += 1
        return n

    pool.sort(key=lambda t: (-kin(t[0]), t[0].name))
    if not pool:
        have = sorted({t for _, doc in dists if (t := tier_of(doc))})
        raise ScaffoldError(f"no dist declares [tool.{ns or '<namespace_root>'}] tier = {tier!r} "
                            f"to read a template off (declared: {', '.join(have) or 'none'})")
    for ex, doc in pool:
        shape = _exemplar_module(ex)
        if shape:
            break
    else:
        raise ScaffoldError(
            "no dist of that tier keeps its module at the path its name spells "
            f"({', '.join(d.name for d, _ in pool[:5])}...) -- pass --module DOTTED")
    src, k, ex_parts = shape
    rel_ex = ex.relative_to(repo).as_posix()
    warnings = [f"no scaffold.templates declared: template read off {rel_ex}"
                + f" ({f'tier {tier}' if tier else 'no --tier'}: the {len(pool)} candidate(s) "
                  "ranked by shared leading name tokens, then name)"]

    if module:
        parts = module.split(".")
    else:
        if len(tokens) <= k:
            raise ScaffoldError(f"{name!r} has too few '-' tokens for {rel_ex}'s shape "
                                f"({k} package level(s) + leaf) -- pass --module DOTTED")
        parts = tokens[:k] + ["_".join(tokens[k:])]
        warnings.append(f"module {'.'.join(parts)} spelled from {name!r} the way "
                        f"{ex.name} spells {'.'.join(ex_parts)} -- --module overrides")
    if not all(p.isidentifier() for p in parts):
        raise ScaffoldError(f"module {'.'.join(parts)!r} is not a dotted identifier -- pass --module")

    base = f"{ex.parent.relative_to(repo).as_posix()}/{name}"
    src_rel = f"{base}/src" if src != ex else base
    files: "list[tuple[str, str]]" = [(f"{base}/pyproject.toml",
                                       _pyproject(name, doc, ns, tier, question, ex, ex_parts, warnings,
                                                  [(d, o) for d, o in dists if d != ex]))]
    for i in range(1, len(parts)):
        ns_init = src.joinpath(*ex_parts[:i], "__init__.py")
        if ns_init.is_file():
            files.append((f"{src_rel}/{'/'.join(parts[:i])}/__init__.py",
                          ns_init.read_text(encoding="utf-8")))
    files.append((f"{src_rel}/{'/'.join(parts)}/__init__.py", _docstring(question, "dist")))

    prose = sorted(p.name for p in ex.iterdir()
                   if p.is_file() and p.name != "pyproject.toml")
    if not question:
        warnings.append("no --question: the door docstring is a TODO")
    return {"files": files, "warnings": warnings, "exemplar": rel_ex, "exemplar_name": ex.name,
            "prose": prose, "dist_dir": base, "module": ".".join(parts)}


def _ns_table(doc: dict, ns: str) -> dict:
    t = ((doc.get("tool") or {}).get(ns) or {}) if ns else {}
    return t if isinstance(t, dict) else {}


def _group(ex_doc: dict, others: "list[tuple[Path, dict]]", ns: str,
           warnings: "list[str]") -> "tuple[dict, str | None]":
    """`([tool.<ns>] keys carried, the version the group shares)`.

    A key is carried when its exemplar value is held by at least one OTHER dist
    (a membership, `release_group = "x"`); a value only the exemplar holds is its
    identity and stays behind. The group is every other dist holding all the
    carried values (no carried key: every other dist); its version is carried
    only when every member and the exemplar agree on one."""
    ex_ns = _ns_table(ex_doc, ns)
    carried: dict = {}
    dropped: "list[str]" = []
    for k in sorted(ex_ns):
        if k == "tier":
            continue
        if any(_ns_table(o, ns).get(k) == ex_ns[k] for _, o in others):
            carried[k] = ex_ns[k]
        else:
            dropped.append(k)
    if dropped:
        warnings.append(f"[tool.{ns}] {', '.join(dropped)}: no other dist holds the exemplar's "
                        "value, so it reads as the exemplar's own and is not carried")
    members = [o for _, o in others
               if all(_ns_table(o, ns).get(k) == v for k, v in carried.items())]
    where = (" and ".join(f"[tool.{ns}] {k} = {_toml_value(v)}" for k, v in carried.items())
             or "the workspace")
    versions: "dict[str, int]" = {}
    for d in [ex_doc, *members]:
        v = (d.get("project") or {}).get("version")
        versions[str(v)] = versions.get(str(v), 0) + 1
    if len(versions) == 1 and "None" not in versions:
        (only,) = versions
        warnings.append(f"version {only}: shared by all {len(members) + 1} dist(s) in {where}")
        return carried, only
    spread = ", ".join(f"{v} ({n})" for v, n in sorted(versions.items()))
    warnings.append(f"version 0.1.0 written: the {len(members) + 1} dist(s) in {where} "
                    f"do not share one ({spread})")
    return carried, None


def _pyproject(name: str, doc: dict, ns: str, tier: str, question: str, ex: Path,
               ex_parts: "list[str]", warnings: "list[str]",
               others: "list[tuple[Path, dict]]") -> str:
    proj = doc.get("project") or {}
    carried_ns, version = _group(doc, others, ns, warnings)
    head: dict = {"name": name, "version": version or "0.1.0", "description": question}
    if "requires-python" in proj:
        head["requires-python"] = proj["requires-python"]
    head["dependencies"] = []
    out = ["[project]"] + [f"{k} = {_toml_value(v)}" for k, v in head.items()]
    if doc.get("build-system"):
        _toml_tables("build-system", doc["build-system"], out)
    own = (ex.name, ".".join(ex_parts), "/".join(ex_parts))
    dropped: "list[str]" = []
    for key, table in (doc.get("tool") or {}).items():
        if key == ns or not isinstance(table, dict):
            continue
        if any(s in json.dumps(table, ensure_ascii=False) for s in own):
            dropped.append(f"[tool.{key}]")
            continue
        _toml_tables(f"tool.{key}", table, out)
    if ns:
        ns_out = ({"tier": tier} if tier else {}) | carried_ns
        if not tier:
            warnings.append(f"no --tier: [tool.{ns}] tier not written")
        if ns_out:
            _toml_tables(f"tool.{ns}", ns_out, out)
    if dropped:
        warnings.append(f"not carried from {ex.name}: {'; '.join(dropped)}")
    return "\n".join(out) + "\n"


def infer_module(repo: Path, descriptor: Any, name: str, *,
                 question: str = "") -> "tuple[list[tuple[str, str]], list[str]]":
    """`(files, warnings)` -- one file beside its parent package's modules."""
    repo = Path(repo)
    parts = name.split(".")
    if len(parts) < 2 or not all(p.isidentifier() for p in parts):
        raise ScaffoldError(f"{name!r}: new-module with no declared template needs a dotted "
                            "name under an existing package (pkg.leaf)")
    globs = list(descriptor.import_roots) or [r.path for r in descriptor.roots] + ["."]
    hits: "dict[Path, None]" = {}
    for g in globs:
        for d in sorted(repo.glob(g)):
            cand = d.joinpath(*parts[:-1])
            if d.is_dir() and cand.is_dir() and any(cand.glob("*.py")):
                hits[cand.resolve()] = None
    parent = ".".join(parts[:-1])
    if not hits:
        raise ScaffoldError(f"no package {parent} under import_roots {globs} -- "
                            "declare codemap.yaml scaffold.templates.<key> (kind: module)")
    if len(hits) > 1:
        raise ScaffoldError(f"package {parent} found in several places: "
                            + ", ".join(sorted(p.relative_to(repo.resolve()).as_posix() for p in hits)))
    where = next(iter(hits)).relative_to(repo.resolve()).as_posix()
    return ([(f"{where}/{parts[-1]}.py", _docstring(question, "module"))],
            [f"no scaffold.templates declared: one file in {where} (where {parent} lives)"])
