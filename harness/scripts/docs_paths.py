r"""What in a doc is a path, and does it resolve -- the rule the docs reference gate runs.

Where a path is taken from (`kind`):
    code    an inline code span (`...`), split on whitespace and `=,;()[]"'`
    link    a markdown link target `[text](target)`
    prose   the rest of a line, once code spans, links and URLs are cut out
    fence   a fenced block, unless its info string carries the word `example`

What counts as a path: a token holding a `/` whose segments are all `[\w.@+-]+` and
whose last one is a file name with an extension; in a code span, link or fence also a
directory spelled `dir/`. A `:12`/`:3-9` line suffix and a `#anchor` are dropped. So
not a path: a placeholder or glob (`<x>`, `{x}`, `*`, `$X`, `...`), a URL, a home (`~`)
or absolute (`/`) path, a Windows path, `read/write`.

Which tree a path names (`status`), from what git knows (tracked or would-track files):
    live / dead   anchored in this repo -- a link target or `./`/`../` token (relative to
                  the doc's directory), or a token whose first segment git knows (tracks,
                  or ignores as a directory) at one of the ANCHORS -- and git does / does
                  not know it under some anchor
    runtime       anchored, not known, and git ignores it: a tree the harness writes
    unanchored    anything else (`evals/`, `src/app.py`): another tree (a user's project),
                  so its presence here proves nothing; a token that resolves against the
                  doc's own directory is live instead
    removed       dead, but its line says `đã gỡ ở commit X` / `removed in commit X` and
                  git shows X removing it (in X's parent, not in X)
A path that merely ENDS like a tracked file is not live: `scripts/x.py` for
`harness/scripts/x.py` names the wrong file.

A bare token with no `/` (`skill-routing.md`) fails the "holds a `/`" rule above and is
not a path -- except inside a code span, where it is checked one more way: if this
repo's history ever deleted a file with that exact basename, and no file git tracks (or
would track) now carries that basename, it is `dead` (or `removed`, same marker rule as
above, checked by basename instead of by path). Any other bare name -- one never tracked
here, or whose basename some live file still carries -- stays unclassified, in a code
span exactly as everywhere else.

The gate that counts and fails on these is `docs_refs.py`.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

from codegraph._core.git import GitError, source_files

#: where a path may be read from: the repo root, then the product tree (docs abbreviate
#: `harness/data/x.yaml` as `data/x.yaml`, `harness/state/...` as `state/...`)
ANCHORS: Tuple[str, ...] = ("", "harness/")

_FENCE = re.compile(r"^\s*(```+|~~~+)(.*)$")
_SPAN = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
_SPLIT = re.compile(r"[\s\"'(),;=\[\]|]+")
_FILE = re.compile(r"^[\w.@+-]*\w\.[A-Za-z][A-Za-z0-9]{0,7}$")
_SEG = re.compile(r"^[\w.@+-]+$")
_MARK = re.compile(r"(?:đã gỡ ở commit|removed in commit)\s+`?([0-9a-f]{7,40})\b")
_SUFFIX = re.compile(r"(:\d+(-\d+)?)+$")


@dataclass(frozen=True)
class Ref:
    doc: str
    line: int
    kind: str
    token: str
    path: str
    status: str          # live | dead | runtime | unanchored | removed


def _clean(tok: str) -> str:
    tok = tok.strip().rstrip(".:,")
    tok = tok.split("#", 1)[0]
    return _SUFFIX.sub("", tok)


def is_path(tok: str, allow_dir: bool) -> bool:
    if "/" not in tok or "..." in tok:
        return False
    segs = tok.rstrip("/").split("/")
    if not all(s in (".", "..") or _SEG.match(s) for s in segs):
        return False
    if tok.endswith("/"):
        return allow_dir and bool(segs[-1]) and segs[-1] not in (".", "..")
    return bool(_FILE.match(segs[-1]))


def _norm(p: str) -> Optional[str]:
    n = os.path.normpath(p)
    return None if n.startswith("..") else n.replace(os.sep, "/")


class Index:
    """What git knows under `root`: files it tracks or would track, their directories, the
    top-level entries, and whether it ignores a path."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        # `codegraph._core.git.source_files` runs the one `-z`-terminated `ls-files
        # --cached --others --exclude-standard` every reader here shares (a plain
        # whitespace-split, or git's default `core.quotePath=true` C-quoting a non-ASCII
        # byte, would otherwise leave the real path out of `self.files`, reading as
        # untracked everywhere `has`/`knows` ask) -- reused as-is, not a second listing.
        files = source_files(root)
        if files is None:
            raise GitError("%s is not inside a git work tree" % root)
        self.files = set(files)
        self.dirs = {"/".join(f.split("/")[:i]) for f in self.files for i in range(1, f.count("/") + 1)}
        self.top = {f.split("/")[0] for f in self.files}
        self._ignored: dict = {}
        self._commits: dict = {}
        self._removed: dict = {}
        self._names: dict = {}
        self._live_basenames: Optional[Set[str]] = None
        self._deleted_basenames: Optional[Set[str]] = None

    def has(self, path: str) -> bool:
        return path.rstrip("/") in (self.dirs if path.endswith("/") else self.files | self.dirs)

    def ignored(self, path: str) -> bool:
        # a `dir/` pattern only matches a path git knows is a directory; the trailing `/`
        # says so for a tree that does not exist on this checkout
        if path not in self._ignored:
            self._ignored[path] = subprocess.run(
                ["git", "-C", str(self.root), "check-ignore", "-q", "--no-index", path],
                capture_output=True).returncode == 0
        return self._ignored[path]

    def prefetch(self, paths: Sequence[str]) -> None:
        """Ask git about every path in one call, not one process per path."""
        # one path outside the tree (`../x`) makes git abort the whole batch: those are left
        # to `ignored`, and a failed batch caches nothing; -z keeps names unquoted
        todo = sorted({p for p in paths
                       if p not in self._ignored and not p.startswith("/") and _norm(p) not in (None, ".")})
        if not todo:
            return
        r = subprocess.run(["git", "-C", str(self.root), "check-ignore", "--no-index", "--stdin", "-z"],
                           input="\0".join(todo) + "\0", capture_output=True, text=True)
        if r.returncode not in (0, 1):
            return
        hit = set(r.stdout.split("\0"))
        self._ignored.update((p, p in hit) for p in todo)

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True)

    def is_commit(self, sha: str) -> bool:
        if sha not in self._commits:
            self._commits[sha] = self._git("cat-file", "-e", sha + "^{commit}").returncode == 0
        return self._commits[sha]

    def removed_in(self, sha: str, path: str) -> bool:
        """`path` (file or directory) is in `sha`'s parent and gone in `sha`."""
        def has(rev: str) -> bool:
            # git-listing-exempt: existence-only -- `path` is a pathspec git matches
            # itself; only stdout's EMPTINESS is read, never a printed name, so a
            # C-quoted non-ASCII match still leaves stdout non-empty and this correct.
            r = self._git("ls-tree", "-r", "--name-only", rev, "--", path.rstrip("/"))
            return r.returncode == 0 and bool(r.stdout.strip())
        key = (sha, path)
        if key not in self._removed:
            self._removed[key] = self.is_commit(sha) and has(sha + "^") and not has(sha)
        return self._removed[key]

    def removed_names(self, sha: str) -> Set[str]:
        """File names (last segment) `sha` deleted or renamed away."""
        if sha not in self._names:
            # `-z`: plain `--name-status` C-quotes a non-ASCII path, and the quoted
            # stand-in never matches a ledger row's real name -- a `-z` record is a flat
            # NUL-separated token stream (one status token, then one path token, two for
            # a rename: old, new), never one `\t`-split line per path.
            r = self._git("diff", "--name-status", "--diff-filter=DR", "-M", "-z",
                          sha + "^", sha)
            names: Set[str] = set()
            if r.returncode == 0:
                tokens = [t for t in r.stdout.split("\0") if t]
                i = 0
                while i < len(tokens):
                    status = tokens[i]
                    if status[:1] == "R":
                        if i + 2 >= len(tokens):
                            break
                        names.add(tokens[i + 1].rsplit("/", 1)[-1])  # old (removed) name
                        i += 3
                    else:
                        if i + 1 >= len(tokens):
                            break
                        names.add(tokens[i + 1].rsplit("/", 1)[-1])
                        i += 2
            self._names[sha] = names
        return self._names[sha]

    def knows(self, entry: str) -> bool:
        """An entry git tracks, or a directory it ignores."""
        return entry in self.files or entry in self.dirs or self.ignored(entry + "/")

    def live_basenames(self) -> Set[str]:
        """Last segment of every file git tracks or would track, now."""
        if self._live_basenames is None:
            self._live_basenames = {f.rsplit("/", 1)[-1] for f in self.files}
        return self._live_basenames

    def deleted_basenames(self) -> Set[str]:
        """Last segment of every file some commit in this repo's history deleted or
        renamed away -- one `git log --diff-filter=D` call, not one per bare token.
        Empty (never raises) when git history cannot answer this, which is also the
        pre-existing behaviour for a bare token: unclassified."""
        if self._deleted_basenames is None:
            r = self._git("log", "--diff-filter=D", "--name-only", "--format=", "-z")
            names: Set[str] = set()
            if r.returncode == 0:
                for tok in r.stdout.split("\0"):
                    tok = tok.strip()
                    if tok:
                        names.add(tok.rsplit("/", 1)[-1])
            self._deleted_basenames = names
        return self._deleted_basenames


def classify(index: Index, doc: str, tok: str, kind: str) -> Tuple[str, str]:
    """(repo-relative spelling, live | dead | runtime | unanchored)."""
    here = str(Path(doc).parent)
    trail = "/" if tok.endswith("/") else ""
    if kind == "link" or tok.startswith(("./", "../")):
        rel = _norm(os.path.join(here, tok))
        if rel is None:
            return tok, "unanchored"
        cands = [rel + trail]
    else:
        path = (_norm(tok) or tok) + trail
        head = path.split("/")[0]
        cands = [a + path for a in ANCHORS if index.knows(a + head)]
        if not cands:
            near = _norm(os.path.join(here, tok))
            if near and index.has(near + trail):
                return near + trail, "live"
            return path, "unanchored"
    for c in cands:
        if index.has(c):
            return c, "live"
    for c in cands:
        if index.ignored(c):
            return c, "runtime"
    return cands[0], "dead"


def is_bare_file(tok: str) -> bool:
    """A file-name-shaped token with no `/` -- the shape `is_path` never admits, checked
    only for a code span, only against git history (`classify_bare`)."""
    return "/" not in tok and "..." not in tok and bool(_FILE.match(tok))


def classify_bare(index: Index, tok: str) -> Optional[str]:
    """`dead` when history shows `tok`'s basename deleted and no live file carries it now;
    `None` (unclassified, exactly like today) otherwise."""
    if tok in index.live_basenames():
        return None
    if tok in index.deleted_basenames():
        return "dead"
    return None


def _tokens(text: str) -> Iterator[str]:
    for raw in _SPLIT.split(text):
        tok = _clean(raw)
        if tok:
            yield tok


def refs_in(text: str) -> Iterator[Tuple[int, str, str]]:
    """(line, kind, token) for every path-shaped token; fenced `example` blocks skipped."""
    fence: Optional[str] = None
    example = False
    for n, line in enumerate(text.splitlines(), 1):
        m = _FENCE.match(line)
        if fence is None and m:
            fence, example = m.group(1)[0] * 3, "example" in m.group(2).split()
            continue
        if fence is not None:
            if line.strip().startswith(fence):
                fence = None
            elif not example:
                for tok in _tokens(line):
                    if is_path(tok, allow_dir=True):
                        yield n, "fence", tok
            continue
        rest = _URL.sub(" ", line)
        for m2 in _LINK.finditer(rest):
            tok = _clean(m2.group(1))
            if tok and "://" not in tok and not tok.startswith(("mailto:", "#")) and is_path(
                    tok if "/" in tok else "./" + tok, allow_dir=True):
                yield n, "link", tok if "/" in tok else "./" + tok
        rest = _LINK.sub(" ", rest)
        for m2 in _SPAN.finditer(rest):
            for tok in _tokens(m2.group(1)):
                if is_path(tok, allow_dir=True) or is_bare_file(tok):
                    yield n, "code", tok
        for tok in _tokens(_SPAN.sub(" ", rest)):
            if is_path(tok, allow_dir=False):
                yield n, "prose", tok


def markers(line: str) -> List[str]:
    return _MARK.findall(line)


def scan_doc(root: Path, doc: str, index: Optional[Index] = None) -> List[Ref]:
    """Every path in `doc`; a dead one whose line carries a marker naming the commit that
    removed it is `removed`. A bare code-span token (`is_bare_file`, no `/`) classifies by
    basename (`classify_bare`) instead, and is dropped entirely (not even `unanchored`)
    when that leaves it unclassified."""
    index = index or Index(root)
    lines = (Path(root) / doc).read_text(encoding="utf-8", errors="replace").splitlines()
    found = list(refs_in("\n".join(lines)))
    pathlike = [(n, kind, tok) for n, kind, tok in found if "/" in tok]
    here = str(Path(doc).parent)

    def spell(p: str, tok: str) -> str:
        return (_norm(p) or "") + ("/" if tok.endswith("/") else "")
    index.prefetch([a + x for _, _, tok in pathlike for a in ANCHORS
                    for x in (spell(tok, tok).split("/")[0] + "/", spell(tok, tok))]
                   + [spell(os.path.join(here, tok), tok) for _, _, tok in pathlike])
    out = []
    for n, kind, tok in found:
        if "/" not in tok:
            status = classify_bare(index, tok)
            if status is None:
                continue
            if any(tok in index.removed_names(x) for x in markers(lines[n - 1])):
                status = "removed"
            out.append(Ref(doc, n, kind, tok, tok, status))
            continue
        path, status = classify(index, doc, tok, kind)
        if status == "dead" and any(index.removed_in(x, path) for x in markers(lines[n - 1])):
            status = "removed"
        out.append(Ref(doc, n, kind, tok, path, status))
    return out
