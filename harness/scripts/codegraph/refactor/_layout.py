"""find_package_dir -- one dotted package name -> its directory, read from
the target repo's OWN `codemap.yaml` `import_roots` (the layout schema slice
2.7 already ships, `codegraph._core.layout`/`codegraph._core.descriptor`).

Replaces upstream `movemodule.find_package`'s hard-coded glob search
(`packages/*/src/{pkg}`, `src/{pkg}`, `{pkg}`): that glob happens to match
this repo's own two shapes but is still a GUESS about the tree's convention,
never a declared one -- a namespace (`ob`/`gsm_agent`/`packages`) baked into
a tool instead of read from the descriptor. A root can carry a `*` segment
(`packages/*/src`, one entry per dist); each concrete directory it expands to
is tried the same way a literal root is.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from codegraph._core.descriptor import DescriptorError, load

from .errors import RefactorError


def _expand_root(repo: Path, entry: str) -> List[Path]:
    """One `import_roots` entry -> the concrete directories it names --
    globbed when it carries a wildcard segment, itself otherwise. Mirrors
    `codegraph.check.tree.Tree.import_paths`'s own expansion (that method
    lives on a full measured `Tree`; this needs only the descriptor, never a
    snapshot, so it is not reused directly)."""
    if any(c in entry for c in "*?["):
        return sorted(p for p in repo.glob(entry) if p.is_dir())
    d = repo / entry
    return [d] if d.is_dir() else []


def optional_descriptor(repo: Path):
    """The repo's own `codemap.yaml` descriptor, or None when it declares none -- for a
    caller whose layout is an enrichment, not a requirement. A malformed file refuses:
    broken is never read as absent."""
    doc_path = Path(repo) / "codemap.yaml"
    if not doc_path.is_file():
        return None
    try:
        return load(doc_path, repo).descriptor
    except DescriptorError as e:
        raise RefactorError(str(e)) from None


def load_descriptor(repo: Path, verb: str = "refactor move --module"):
    """The repo's own `codemap.yaml` descriptor, or a refusal naming `verb`, the command
    that needs the layout -- never a silent fallback."""
    descriptor = optional_descriptor(repo)
    if descriptor is None:
        raise RefactorError("%s: %s reads the layout from codemap.yaml -- none under %s"
                            % (Path(repo) / "codemap.yaml", verb, repo))
    return descriptor


def find_package_dir(repo: Path, descriptor, pkg: str) -> Path:
    """The directory `pkg` (dotted, e.g. `mypkg` or `ns.sub`) resolves to
    under one of `descriptor.import_roots` -- the directory that carries an
    `__init__.py` at the end of the dotted chain. Refuses (never guesses)
    when zero or more than one root's expansion agrees."""
    if not descriptor.import_roots:
        raise RefactorError(
            "codemap.yaml declares no `import_roots` -- refactor move --module "
            "cannot resolve package %r without one real sys.path entry declared" % pkg)
    pkg_rel = pkg.replace(".", "/")
    candidates: List[Path] = []
    for entry in descriptor.import_roots:
        for d in _expand_root(Path(repo), entry):
            cand = d / pkg_rel
            if (cand / "__init__.py").is_file() and cand not in candidates:
                candidates.append(cand)
    if not candidates:
        raise RefactorError(
            "package %r not found under any declared import_roots (%s)"
            % (pkg, ", ".join(descriptor.import_roots)))
    if len(candidates) > 1:
        raise RefactorError(
            "package %r is ambiguous: found under %s -- narrow `import_roots` "
            "or the package's own layout"
            % (pkg, ", ".join(str(c.relative_to(repo)) for c in candidates)))
    return candidates[0]


def find_import_root(repo: Path, descriptor, root: str) -> Path:
    """`root` (repo-relative) when it is one of `descriptor.import_roots`' own concrete
    directories -- a sys.path entry whose top-level modules have no package prefix."""
    want = (Path(repo) / root).resolve()
    for entry in descriptor.import_roots or ():
        for d in _expand_root(Path(repo), entry):
            if d.resolve() == want:
                return d
    raise RefactorError("%r is not one of codemap.yaml's import_roots (%s)"
                        % (root, ", ".join(descriptor.import_roots or ()) or "none declared"))
