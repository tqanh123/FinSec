"""A verb's own earlier output read back as input (`--against`, `--pairs`,
`--manifest`, `--upstream-red`): one mapping, or a ValueError the verb turns
into its refusal. A list or a scalar there is a wrong file, not an empty one.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import yaml_io


def mapping(doc: object, where: object) -> dict:
    if not isinstance(doc, dict):
        raise ValueError(f"{where}: must be a mapping, got {type(doc).__name__}")
    return doc


def load_json(path: Path) -> dict:
    return mapping(json.loads(Path(path).read_text(encoding="utf-8")), path)


def load_yaml(path: Path) -> dict:
    try:
        doc = yaml_io.yaml_load(Path(path).read_text(encoding="utf-8"))
    except yaml_io.YAMLError as e:
        raise ValueError(f"{path}: not parseable YAML: {e}") from e
    return mapping({} if doc is None else doc, path)


def dist_namer(root: "Path | str") -> "Callable[[str], str]":
    """`path -> dist` for the repo at `root`, through the ONE layout door
    (`codegraph._core.layout.dist_of_path`) off its `codemap.yaml`. A path
    outside every declared root, or a repo with no `codemap.yaml`, is `"?"` —
    never a guessed first segment. A `codemap.yaml` that does not parse is the
    verb's refusal (`ValueError`), not a silent `"?"`."""
    from codegraph._core.descriptor import DescriptorError, load
    from codegraph._core.layout import dist_of_path

    codemap = Path(root) / "codemap.yaml"
    if not codemap.is_file():
        return lambda _path: "?"
    try:
        descriptor = load(codemap, None).descriptor
    except DescriptorError as e:
        raise ValueError(str(e)) from e
    return lambda path: dist_of_path(descriptor, path) or "?"
