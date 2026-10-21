"""codegraph.refactor.moveflat -- move files between two directories that
both sit directly on sys.path (a "flat" layout): rewrite every text
reference to the old path, leave the bare import name alone -- a flat move
never changes what a caller SAYS, only where the file physically sits.

Port of ob-tools' `devkit.codemod.moveflat` (pinned commit `60075cb`,
`packages/devkit/src/devkit/codemod/moveflat.py`). Adapted for this repo:

    * `--map`/`owners.layout`-driven planning (upstream: `moves_from_map`,
      `load_layout`, `--channel`) is NOT ported here -- this engine only
      ever takes an explicit `{src: dst}` move list. The layout-driven
      ownership loop is wired one door up instead (`move.py`'s
      `_converged_moves`/`--converge` runs `check.owners.assign_owners_
      converged` to a fixed point against the live tree, then turns the
      converged table into exactly such a `{src: dst}` map before calling
      here) -- this module itself stays agnostic of `owners.channels`.
    * the `pathanchors`-based `__file__` bootstrap rewrite (parts a-e of the
      upstream module docstring) IS ported -- `.pathanchors` (this package)
      is a near-verbatim copy of the sibling upstream module, and the five
      passes below (`_rewrite_moved_paths`, `_extend_importers`,
      `_retarget_dead_refs`, `_grant_moved_own_siblings`,
      `_extend_glob_lists`) are wired into `plan_flat_move` in the same
      order upstream ran them. Nothing here reads a layout schema: the
      whole mechanism is a pure static-AST evaluation of `__file__`-derived
      expressions plus a git listing, so there was no namespace guess to
      replace (see `pathanchors.py`'s own docstring for the measurement).
    * `_postmove_commands`/`run_postmove_commands` (regenerating a
      `drivers.generated` artifact after a move) is not ported -- nothing
      that writes a generated artifact calls this door yet.

A flat move's OWN directory-depth change (a move that adds or removes a
path segment, e.g. `tools/user.py` -> `tools/sub/user.py`) needs no
separate `_depth_edits`-style pass here the way `--module` does: part (a)
above (`_rewrite_moved_paths`) already re-derives each `__file__`-relative
anchor's REAL absolute target directory and re-expresses it relative to
the file's NEW location, so a depth change is just one more retarget,
not a distinct arithmetic case. Proven, not assumed: the flat bootstrap
e2e test (`test_move_flat_rewrites_the_moved_files_own_bootstrap_and_
regrants_a_sibling`, `harness/tests/hs_code/
test_hs_code_refactor_move_bootstrap.py`) moves a file one directory
level DEEPER and asserts the moved file still runs.

WHAT IT CANNOT SEE
    * a path spelled some other way than repo-relative POSIX -- absolute,
      `./`-prefixed, or with backslashes;
    * a path assembled from something OTHER than `__file__` at runtime (an
      f-string interpolating a config value, argv, an environment variable,
      an object attribute) -- `pathanchors` classifies such an anchor
      `foreign-root` rather than guessing, and this port does not touch it;
    * files git does not track, which is also why an untracked SOURCE
      refuses rather than moving: the scan that finds references reads the
      tracked list;
    * a generated artifact that names the path -- correct until the next
      regen run puts the old path back.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Dict, List, Tuple

import attrs

from codegraph._core.git import GitError, run_git, source_files

from . import pathanchors as pa
from .errors import RefactorError


@attrs.define
class FlatMove:
    src: str
    dst: str
    status: str = "pending"
    reason: str = ""


@attrs.define
class FlatPlan:
    repo: Path
    moves: List[FlatMove] = attrs.field(factory=list)
    #: original repo-relative path -> new full text (moved files keyed by
    #: their OLD path, matching `apply_flat_plan`'s own `dst_of` remap).
    texts: Dict[str, str] = attrs.field(factory=dict)
    changes: List[Tuple[str, int, str, str]] = attrs.field(factory=list)

    @property
    def refused(self) -> List[FlatMove]:
        return [m for m in self.moves if m.status == "refused"]

    @property
    def pending(self) -> List[FlatMove]:
        return [m for m in self.moves if m.status == "pending"]


def tracked(repo: Path) -> List[str]:
    """Every git-tracked repo-relative path, the reference scan's universe --
    matches the source-must-be-tracked refusal below by construction.

    Reads through `source_files(tracked_only=True)` -- the shared door for
    "list this repo's tracked files", never a private `ls-files -z` call of
    this module's own -- rather than the untracked-inclusive default: an
    untracked file must never enter this reference scan, per this module's
    own "source must be tracked" law. `-z` (NUL-separated, no quoting) under
    the hood: plain `ls-files` prints a non-ASCII path C-quoted under git's
    default `core.quotePath=true` (`"t\\341..."`), a string this module then
    tries to read as a literal path and never finds -- the quoted form does
    not name a file on disk."""
    try:
        files = source_files(repo, tracked_only=True)
    except GitError as e:
        raise RefactorError("not a git repo (git ls-files failed): %s: %s" % (repo, e)) from None
    if files is None:
        raise RefactorError("not a git repo (git ls-files failed): %s" % repo)
    return files


def _rel(repo: Path, spec: str) -> str:
    """Repo-relative POSIX path, or a refusal when it escapes the repo."""
    p = (repo / spec).resolve()
    try:
        return p.relative_to(repo.resolve()).as_posix()
    except ValueError:
        raise RefactorError("outside the repo: %s" % spec) from None


def _path_rx(rel: str) -> "re.Pattern[str]":
    """`rel` where it is a WHOLE path, not a fragment of a longer one -- see
    upstream's own docstring for the two-guard reasoning this regex keeps
    verbatim (`data/mod.py` must not eat `a/mod.py`'s tail, `${VAR}/x.py`
    must not be mistaken for a longer path)."""
    return re.compile(r"(?<![\w.-])(?<![\w./]/)" + re.escape(rel) + r"(?![\w])")


# --------------------------------------------------- the pathanchors bootstrap

def _expr_for(rel: str, style: str) -> str:
    """A path expression, spelled in `style`, that evaluates to `rel` seen from
    the moved file's NEW directory. `rel` is what os.path.relpath produces:
    leading '..' segments then the tail. One '.parent' enters the file's own
    directory, so `ups` parent-steps above it mean `ups` '..' segments."""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    ups = 0
    for p in parts:
        if p == "..":
            ups += 1
        else:
            break
    tails = parts[ups:]
    if style == pa.STYLE_OSPATH:
        segs = ["os.path.dirname(os.path.abspath(__file__))"] \
            + ['".."'] * ups + ['"%s"' % t for t in tails]
        return "os.path.join(" + ", ".join(segs) + ")"
    steps = ".parent" * (ups + 1)
    if not tails:
        return "Path(__file__).resolve()" + steps
    return "Path(__file__).resolve()" + steps \
        + " / " + " / ".join('"%s"' % t for t in tails)


def _retarget(value: Path, repo: Path, moved: dict) -> Path:
    """A path the moved file pointed at, followed to its own new location when
    the pass moves that target too."""
    try:
        rel = value.resolve().relative_to(repo).as_posix()
    except ValueError:
        return value
    if rel in moved:
        return repo / moved[rel]
    return value


def _splice(text: str, edits: List[Tuple[int, int, int, int, str]]) -> str:
    """Apply (line, col, end_line, end_col, replacement) edits, 1-based lines.
    Applied bottom-up so earlier positions stay valid."""
    if not edits:
        return text
    lines = text.split("\n")
    for ln0, col, eln0, ecol, new in sorted(edits, reverse=True):
        if ln0 == eln0:
            line = lines[ln0 - 1]
            lines[ln0 - 1] = line[:col] + new + line[ecol:]
        else:
            first = lines[ln0 - 1][:col] + new
            last = lines[eln0 - 1][ecol:]
            lines[ln0 - 1:eln0] = [first] + lines[ln0:eln0 - 1] + [last]
    return "\n".join(lines)


def _module_level_anchor_line(text: str) -> "int | None":
    """Line of the LAST module-level sys.path anchor, or None. Anchors inside
    functions do not run at import time, so an inserted anchor must sit beside
    the ones that do."""
    import ast as _ast
    try:
        tree = _ast.parse(text)
    except SyntaxError:
        return None
    last = None

    def walk_block(body):
        nonlocal last
        for st in body:
            if isinstance(st, _ast.FunctionDef):
                continue
            for n in _ast.walk(st):
                if isinstance(n, _ast.Call) and _ast.unparse(n.func) in (
                        "sys.path.insert", "sys.path.append"):
                    last = max(last or 0, n.lineno)
            for b in ("body", "orelse", "finalbody"):
                sub = getattr(st, b, None)
                if sub and isinstance(st, (_ast.If, _ast.Try, _ast.With)):
                    walk_block(sub)

    walk_block(tree.body)
    return last


def _insert_before_code(text: str, block: str) -> str:
    """Insert `block` after the module docstring and any __future__ imports and
    BEFORE everything else. An anchor that grants a path must run before the
    import that needs it -- appending after the import run would grant the path
    one line too late and the import still fails."""
    import ast as _ast
    try:
        tree = _ast.parse(text)
    except SyntaxError:
        return block + "\n" + text
    line = 0
    for st in tree.body:
        if isinstance(st, _ast.Expr) and isinstance(
                st.value, _ast.Constant) and isinstance(st.value.value, str):
            line = st.end_lineno or line
            continue
        if isinstance(st, _ast.ImportFrom) and st.module == "__future__":
            line = st.end_lineno or line
            continue
        break
    lines = text.split("\n")
    lines.insert(line, block)
    return "\n".join(lines)


def _anchor_block(repo: Path, receiver_rel: str, target: Path, style: str) -> str:
    rel = _relpath(target, (repo / receiver_rel).parent)
    inner = _expr_for(rel, style)
    if style == pa.STYLE_OSPATH:
        return "sys.path.insert(0, %s)" % inner
    return "sys.path.insert(0, str(%s))" % inner


def _relpath(target: Path, start: Path) -> str:
    import os as _os
    return _os.path.relpath(target, start)


def _normpath(p: str) -> str:
    import os as _os
    return _os.path.normpath(p)


def _rewrite_moved_paths(repo: Path, src: str, dst: str, text: str, moved: dict) -> str:
    """(a): every `__file__`-based path in the moved file keeps pointing where
    it pointed -- the anchor still reaches the old directory, the data file is
    still the one beside the old home (or the target's new home when this pass
    moves it too). Expressions whose value does not change at the new location
    are left byte-identical."""
    exprs = pa.path_exprs_in_file(repo, src)
    if not exprs:
        return text
    after = {e.line: e for e in pa._FileScan(
        repo, src, abs_override=str(repo / dst)).scan()}
    dst_dir = (repo / dst).parent
    edits = []
    for e in exprs:
        if e.kind != pa.KIND_COMPUTED:
            continue  # runtime params are about other trees; not ours to touch
        if e.expr.isidentifier():
            continue  # a bare name; the fix belongs at whatever assignment
            # actually builds its value, already rewritten separately below
            # if that assignment is itself __file__-derived
        new_v = after.get(e.line)
        if new_v is not None and new_v.kind == pa.KIND_COMPUTED \
                and new_v.dir_abs and e.dir_abs \
                and _normpath(new_v.dir_abs) == _normpath(e.dir_abs):
            continue  # same value at the new location -- leave it alone
        target = _retarget(Path(e.dir_abs), repo, moved)
        rel = _relpath(target, dst_dir)
        inner = _expr_for(rel, e.style)
        expr = e.expr
        if expr.startswith("str(") and expr.endswith(")"):
            inner = "str(%s)" % inner
        edits.append((e.line, e.col, e.end_line, e.end_col, inner))
    return _splice(text, edits)


def _grant_anchor(repo: Path, plan: FlatPlan, receiver: str, target_dir: Path,
                  position_rel: "str | None" = None) -> None:
    """Insert a sys.path grant for `target_dir` into `receiver`, in whatever
    style its own anchors already use (pathlib, once any exist), beside the
    last module-level anchor when one exists, or before the receiver's own
    code otherwise. A grant already present is not inserted twice.

    `position_rel` is where the relative path is measured FROM when it
    differs from `receiver` -- a moved file's plan text is still keyed by its
    OLD path (matching part (a) and `apply_flat_plan`'s own convention), but
    the grant must be relative to where the file will actually SIT."""
    text = plan.texts.get(receiver) or (repo / receiver).read_text(
        encoding="utf-8", errors="surrogateescape")
    anchors = pa.anchors_in_file(repo, receiver)
    style = anchors[-1].style if anchors else pa.STYLE_PATHLIB
    block = _anchor_block(repo, position_rel or receiver, target_dir, style)
    if block.split("insert(0, ", 1)[1] in text:
        return  # this grant already exists -- not twice
    last = _module_level_anchor_line(text)
    if last is not None:
        lines = text.split("\n")
        lines.insert(last, block)
        new = "\n".join(lines)
    else:
        # Unconditional: a duplicate `import sys` a few lines above the
        # file's own import block is harmless (imports are idempotent), but
        # checking merely whether the TEXT contains it ANYWHERE is not -- the
        # file may already import sys/Path/os further down, in its OWN
        # ordinary import block, which sits AFTER the docstring where this
        # grant is inserted; the grant needs the name defined at the point
        # it runs, not eventually.
        head = "import sys\n"
        if style == pa.STYLE_PATHLIB:
            head += "from pathlib import Path\n"
        if style == pa.STYLE_OSPATH:
            head += "import os\n"
        new = _insert_before_code(text, head + block)
    plan.texts[receiver] = new


def _extend_importers(repo: Path, plan: FlatPlan, pairs: List[Tuple[str, str]]) -> None:
    """(b): every importer the move strands gets a new sys.path grant reaching
    the new directory -- spelled by the receiving file, and when the importer is
    a test whose path comes from a conftest, the conftest is the receiver, not
    each test file."""
    by_leaf = {Path(s).stem: (s, d) for s, d in pairs}
    for b in pa.broken_imports_after(repo, {s: d for s, d in pairs}):
        src, dst = by_leaf[b.module]
        target_dir = (repo / dst).parent
        receiver = b.importer
        # nearest existing conftest whose anchors grant the OLD directory
        for cf in pa._conftests_upward(repo, b.importer):
            if not (repo / cf).is_file():
                continue
            grants = [_normpath(str(a.dir_abs)) for a in pa.anchors_in_file(repo, cf)
                      if a.kind == pa.KIND_COMPUTED]
            if _normpath(str((repo / src).parent)) in grants:
                receiver = cf
                break
        _grant_anchor(repo, plan, receiver, target_dir)


def _grant_moved_own_siblings(repo: Path, plan: FlatPlan, pairs: List[Tuple[str, str]]) -> None:
    """(d): a moved file's OWN bare import of a left-behind sibling relied on
    Python's own script-mode rule -- the running file's directory sits on
    `sys.path` with no anchor spelled anywhere. That grant moves WITH the
    file; if the sibling did not move too, the new directory does not carry
    it, and the bare import breaks invisibly to (a) (no `__file__` expression
    is involved) and to (b) (the broken importer IS the moved file, which the
    differential deliberately skips as `part (a)'s problem`)."""
    moved_set = {s for s, d in pairs}
    for src, dst in pairs:
        old_dir = (repo / src).parent
        new_dir = (repo / dst).parent
        if old_dir == new_dir:
            continue
        for mod, _ln in pa._bare_imports(repo, src):
            if mod in ("__future__",):
                continue
            if not (old_dir / (mod + ".py")).is_file():
                continue  # never resolved through the OLD own-dir grant
            if (new_dir / (mod + ".py")).is_file():
                continue  # the sibling sits there too -- still resolves
            if (mod + ".py") in {Path(s).name for s in moved_set}:
                continue  # the sibling moved in this same pass -- see (a)/(c)
            _grant_anchor(repo, plan, src, old_dir, position_rel=dst)


def _retarget_dead_refs(repo: Path, plan: FlatPlan, pairs: List[Tuple[str, str]]) -> None:
    """(c): a file that never imports the moved module by name but reaches it
    by a computed `__file__`-based path -- `spec_from_file_location`, a
    subprocess argv -- spells that path straight through the OLD file. Neither
    (a) (that file is not the one moving) nor (b) (there is no bare import for
    the differential to see) touches it, so it is retargeted here: any
    computed expression, in ANY tracked file, whose value IS a moved file's
    old path is rewritten from scratch to the new one, in its own style, from
    its own directory -- however many directories away that now is."""
    moved_abs = {(repo / s).resolve(): (repo / d) for s, d in pairs}
    moved_set = {s for s, d in pairs}
    for rel in tracked(repo):
        if not rel.endswith(".py") or rel in moved_set:
            continue
        exprs = pa.path_exprs_in_file(repo, rel)
        if not exprs:
            continue
        file_dir = (repo / rel).parent
        edits = []
        for e in exprs:
            if e.kind != pa.KIND_COMPUTED or not e.dir_abs:
                continue
            if e.expr.isidentifier():
                continue  # a bare name; its own assignment is the real source
            cur_abs = Path(e.dir_abs).resolve()
            if cur_abs not in moved_abs:
                continue
            new_target = moved_abs[cur_abs]
            rel_path = _relpath(new_target, file_dir)
            inner = _expr_for(rel_path, e.style)
            expr = e.expr
            if expr.startswith("str(") and expr.endswith(")"):
                inner = "str(%s)" % inner
            edits.append((e.line, e.col, e.end_line, e.end_col, inner))
        if not edits:
            continue
        text = plan.texts.get(rel) or (repo / rel).read_text(
            encoding="utf-8", errors="surrogateescape")
        new = _splice(text, edits)
        if new != text:
            plan.texts[rel] = new


_GLOB_LIST_RX = re.compile(
    r'^(?P<indent>[ \t]*)(?P<key>[\w.-]+):[ \t]*\[(?P<body>[^\]\n]*)\][ \t]*$', re.M)


def _glob_dir_prefix(pattern: str) -> str:
    """The literal directory this glob is anchored under -- the text before
    its first wildcard character, up to the last `/` in that text -- or ''
    when the pattern has no directory literal at all (`**/*x.py`). Only a
    pattern anchored this way is safe to retarget: swapping the prefix is
    unambiguous exactly because the rest of the pattern never depended on it."""
    m = re.match(r"^([^*?\[]*)", pattern)
    lit = m.group(1) if m else ""  # the pattern always matches (`*` allows zero)
    return lit.rsplit("/", 1)[0] + "/" if "/" in lit else ""


def _extend_glob_lists(repo: Path, plan: FlatPlan, pairs: List[Tuple[str, str]]) -> None:
    """(e): a tracked `key: ["glob", ...]` config entry whose glob matched a
    moved file's OLD path, and no longer matches its NEW one, gains a sibling
    glob for the new directory -- ADDED, never substituted. A glob with no
    directory literal to anchor on (`**/*x.py`) is left alone rather than
    guessed at."""
    moved_set = {s for s, d in pairs}
    for rel in tracked(repo):
        if not rel.endswith((".yaml", ".yml")) or rel in moved_set:
            continue
        text = plan.texts.get(rel) or (repo / rel).read_text(
            encoding="utf-8", errors="surrogateescape")
        edits = []
        for m in _GLOB_LIST_RX.finditer(text):
            body = m.group("body")
            globs = re.findall(r'"([^"]*)"', body)
            additions = []
            for src, dst in pairs:
                for g in globs:
                    if not any(c in g for c in "*?["):
                        continue  # a literal path -- the plain-text pass covers it
                    if not fnmatch.fnmatch(src, g) or fnmatch.fnmatch(dst, g):
                        continue  # didn't match before, or still matches after
                    prefix = _glob_dir_prefix(g)
                    old_dir = Path(src).parent.as_posix() + "/"
                    if prefix != old_dir:
                        continue  # not anchored on this move's own old directory
                    new_dir = Path(dst).parent.as_posix() + "/"
                    new_g = new_dir + g[len(prefix):]
                    if new_g in globs or new_g in additions:
                        continue
                    additions.append(new_g)
            if additions:
                new_body = body.rstrip()
                if not new_body.rstrip().endswith(","):
                    new_body += ","
                new_body += " " + ", ".join('"%s"' % g for g in additions)
                edits.append((m.start("body"), m.end("body"), new_body))
        if edits:
            new_text = text
            for start, end, new_b in sorted(edits, key=lambda e: -e[0]):
                new_text = new_text[:start] + new_b + new_text[end:]
            if new_text != text:
                plan.texts[rel] = new_text


def plan_flat_move(repo: Path, moves: Dict[str, str]) -> FlatPlan:
    """Refuse (same path, untracked source, existing destination) or plan
    every move, then rewrite every git-tracked text file's literal mentions
    of an old path to the new one. Any refusal makes the WHOLE plan refused
    (`plan.refused`) -- nothing is written for a partially-honest plan."""
    repo = Path(repo).resolve()
    plan = FlatPlan(repo)
    tracked_set = set(tracked(repo))
    pairs: List[Tuple[str, str]] = []
    for src_spec, dst_spec in moves.items():
        src, dst = _rel(repo, src_spec), _rel(repo, dst_spec)
        mv = FlatMove(src, dst)
        plan.moves.append(mv)
        if src == dst:
            mv.status, mv.reason = "refused", "source and destination are the same path"
        elif src not in tracked_set:
            mv.status, mv.reason = "refused", "git does not track the source"
        elif (repo / dst).exists() or dst in tracked_set:
            mv.status, mv.reason = "refused", (
                "%s already exists: two files with the leaf %r on the path "
                "make the bare name ambiguous" % (dst, Path(dst).name))
        else:
            pairs.append((src, dst))
    if plan.refused:
        return plan

    # (a) first, on each moved file's original text: rewrite every
    # __file__-based path so it keeps pointing where it pointed. Doing this
    # before the plain-path rewrite keeps the AST positions honest.
    moved = {s: d for s, d in pairs}
    for src, dst in pairs:
        orig = (repo / src).read_text(encoding="utf-8", errors="surrogateescape")
        rewritten = _rewrite_moved_paths(repo, src, dst, orig, moved)
        if rewritten != orig:
            plan.texts[src] = rewritten

    rxs = [(_path_rx(src), dst) for src, dst in pairs]
    for rel in sorted(tracked_set):
        text = plan.texts.get(rel) or (repo / rel).read_text(
            encoding="utf-8", errors="surrogateescape")
        new = text
        for rx, dst in rxs:
            new = rx.sub(dst, new)
        if new != text or rel in plan.texts:
            plan.texts[rel] = new
            for n, (a, b) in enumerate(zip(text.split("\n"), new.split("\n")), 1):
                if a != b:
                    plan.changes.append((rel, n, a.strip(), b.strip()))

    # (b) the importers the differential says the move strands
    _extend_importers(repo, plan, pairs)
    # (c) any other file that reaches a moved file by a computed path, not a
    # bare import -- a spec_from_file_location, a subprocess argv
    _retarget_dead_refs(repo, plan, pairs)
    # (d) a moved file's OWN bare import of a left-behind sibling, which
    # relied on its OLD own-directory grant with no anchor spelled anywhere
    _grant_moved_own_siblings(repo, plan, pairs)
    # (e) a glob-list config entry that matched the old path and no longer
    # matches the new one gains a sibling entry -- added, never substituted
    _extend_glob_lists(repo, plan, pairs)
    return plan


def apply_flat_plan(plan: FlatPlan) -> List[str]:
    """`git mv` every pending move, then write every rewritten text -- keyed
    by the move's NEW path when the file itself moved, its own path
    otherwise. Refuses (never writes a byte) when the plan carries a
    refusal."""
    if plan.refused:
        raise RefactorError("plan has refusals; nothing is written")
    repo, written = plan.repo, []
    dst_of = {m.src: m.dst for m in plan.pending}
    for m in plan.pending:
        (repo / m.dst).parent.mkdir(parents=True, exist_ok=True)
        try:
            run_git(repo, "mv", m.src, m.dst)
        except GitError as e:
            raise RefactorError("git mv %s %s failed: %s" % (m.src, m.dst, e)) from None
        written.append(m.dst)
    for rel, text in plan.texts.items():
        (repo / dst_of.get(rel, rel)).write_text(text, encoding="utf-8", errors="surrogateescape")
        written.append(dst_of.get(rel, rel))
    return written
