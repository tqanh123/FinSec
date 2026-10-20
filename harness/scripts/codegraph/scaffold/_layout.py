"""load_descriptor -- the target repo's own `codemap.yaml`, or a named
refusal. Mirrors `codegraph.refactor._layout.load_descriptor` (same doctrine,
kept as its own small copy per that module's own convention of one loader
per family rather than a cross-package import)."""
from __future__ import annotations

from pathlib import Path

from codegraph._core.descriptor import DescriptorError, load

from .errors import ScaffoldError


def load_descriptor(repo: Path):
    doc_path = Path(repo) / "codemap.yaml"
    if not doc_path.is_file():
        import codemap_adoption
        raise ScaffoldError(
            "%s: scaffold needs a layout schema -- no codemap.yaml under %s -- %s"
            % (doc_path, repo, codemap_adoption.REMEDY))
    try:
        return load(doc_path, repo).descriptor
    except DescriptorError as e:
        raise ScaffoldError(str(e)) from None
