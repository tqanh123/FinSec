"""codegraph.refactor -- restructure-the-tree engines behind `hs-code refactor`.

`move` (this slice) merges three sizes of the same idea -- move a file between
two flat directories, move a module inside one dotted package, cut one symbol
from a module into another -- behind one door (`move.py`'s `MOVE`), each
size its own module: `moveflat.py`, `movemodule.py`, `movesymbol.py`. Ported
from ob-tools (pinned commit `60075cb`,
`packages/devkit/src/devkit/codemod/move{flat,module,symbol}.py`) -- each
module's own docstring names exactly what was adapted and what was dropped.
"""
