"""codegraph.scaffold -- `hs-code scaffold`: `new-module`, `new-dist`,
`slice-docs`, `agents-doc`, `codeowners`.

Ported from ob-tools @`60075cb89c` (tag `frozen-260924`): `devkit.docs.scaffold`
(new-dist/new-module), `devkit.analysis.codeowners` (codeowners),
`devkit.docs.agentsdoc` (agents-doc), `portkit.doc.slice_docs` (slice-docs).
See `docs/STANDARDIZE.md` for the PORT/ADAPT rows and `engine.py` for why
`new-dist`/`new-module` do not port upstream's content generators verbatim.
"""
from __future__ import annotations
