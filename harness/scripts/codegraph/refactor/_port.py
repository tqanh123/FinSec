"""The port a refactor verb works for: the target's port root, the source it
ports from, and both trees' declared layouts — read the way the `port` verbs
read them (`.port/port.yaml` through `port_config`, the target's
`codemap.yaml` through `layout.target_descriptor`), so `rebind` and
`rewrite-refs` never keep a second opinion of either.

The old package name is the source's `package`, the new namespace the
target's `namespace_root`; neither is a literal anywhere in the verbs.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import hs_family

from codegraph._core import layout as _layout


@dataclass(frozen=True)
class PortContext:
    port_root: Path
    package: str
    namespace_root: str
    descriptor: Any
    upstream: Path
    ledger: Path
    specs_dir: Optional[Path]
    #: `sources[].descriptor` resolved against the port root, when declared
    source_descriptor_path: Optional[Path] = None

    def source_descriptor(self) -> Any:
        """The source's layout — `sources[].descriptor` when declared, else the
        source tree's own `codemap.yaml` (the rule `port snapshot` reads it by):
        where an old module lives is ITS layout's answer, never an assumed `src/`."""
        from codegraph._core.descriptor import DescriptorError, load
        path = self.source_descriptor_path or self.upstream / "codemap.yaml"
        if not path.is_file():
            raise hs_family.UsageError(
                "no source descriptor at %s — the source's module layout is read "
                "from it, never assumed" % path)
        try:
            return load(path, self.upstream).descriptor
        except DescriptorError as e:
            raise hs_family.UsageError("source `codemap.yaml`: %s" % e) from e


def port_context(root: Path, verb: str, source_id: Optional[str] = None,
                 batch_name: Optional[str] = None) -> PortContext:
    import port_batch_reader
    import port_config
    import port_ledger_reader
    import port_workspace

    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        raise hs_family.UsageError(str(e)) from e
    cfg_path = port_workspace.port_yaml_path(port_root)
    if not cfg_path.is_file():
        raise hs_family.UsageError("no `.port/port.yaml` under %s — refactor %s works for a port"
                                   % (port_root, verb))
    cfg, problems = port_config.load_and_validate(cfg_path)
    if problems:
        raise hs_family.UsageError("`.port/port.yaml` is invalid:\n"
                                   + "\n".join("  · %s" % p for p in problems))
    source, problem = port_config.find_source(cfg, source_id)
    if problem:
        raise hs_family.UsageError(str(problem))
    package, repo_rel = source.get("package"), source.get("repo")
    if not package or not repo_rel:
        raise hs_family.UsageError(
            "source %r declares no `%s` — refactor %s needs the old package name and "
            "the source tree" % (source.get("id"), "package" if not package else "repo", verb))
    descriptor, layout_problem = _layout.target_descriptor(port_root)
    if layout_problem:
        raise hs_family.UsageError(layout_problem)
    specs_dir = None
    if batch_name:
        try:
            specs_dir = port_batch_reader.load_batch(port_root, batch_name).specs_dir
        except (port_batch_reader.BatchNotFoundError, port_batch_reader.BatchConfigError) as e:
            raise hs_family.UsageError(str(e)) from e
    return PortContext(port_root=port_root, package=package,
                       namespace_root=descriptor.namespace_root, descriptor=descriptor,
                       upstream=(port_root / repo_rel).resolve(),
                       ledger=port_ledger_reader.ledger_path(port_root), specs_dir=specs_dir,
                       source_descriptor_path=((port_root / source["descriptor"]).resolve()
                                               if source.get("descriptor") else None))
