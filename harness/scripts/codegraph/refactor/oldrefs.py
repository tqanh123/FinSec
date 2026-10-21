"""old_mentions -- every tracked line that still names an old path (or name) after a move.

A move or rename rewrites what it can reach; this lists what still says the old name so a
person decides. It LISTS, it never rewrites. `refactor move` and `refactor rename` print
it on dry-run and `--apply` alike.

MATCHING
    A line is split into tokens `[\\w.\\-/|,=]+`; a leading `./` and trailing `.`/`,` are
    dropped. A file matches when a token EQUALS the old path; a directory when a token
    equals it or starts with `old/`, and is neither the new path nor under it. Never a
    stem: `leak.out`'s stem `leak` is an English word (304 false hits, measured on this
    harness's own history at the `702124a4` rename).

RELATIVE SPELLINGS
    A file under a `relative_roots` entry (`<root><owner>/<rel>`, e.g. a skill's
    `references/x.md`) is also spelled `<rel>` by its owner's files and registry rows. That
    spelling counts when exactly one owner carries `<rel>`; with two owners the bare
    `<rel>` could mean either.

HISTORY
    `history` globs name ledgers whose old names are the record, not a stale reference --
    this harness's own are `plans/**`, `docs/decisions.*`, `CHANGELOG*`. They are this
    function's data, never a filter this module bakes in for every repo: `history` and
    `relative_roots` both default to `()` here, and `declared()` reads a target repo's own
    choice from ITS OWN `codemap.yaml` (`domains.oldrefs.history`/`.relative_roots`) --
    `move`/`rename` pass that straight through rather than this module guessing a directory
    convention for a repo it has never seen. A caller passes `history=()` explicitly to see
    everything regardless of what the repo declares.

KINDS
    `code` (`.py`), `md`, `yaml-json` (`.yaml`/`.yml`/`.json`/`.jsonl`), `other`.

WHAT IT CANNOT SEE
    * A path built at runtime (`base / "x.py"`) or split across tokens (`"a" "/b.py"`).
    * An untracked file; binary files.
    * A relative spelling outside `relative_roots` (a doc's `../x.md`).
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import attrs

import secret_scan_before_ship as secret_scan
from codegraph._core.git import run_git, source_files


HISTORY: Tuple[str, ...] = ()
RELATIVE_ROOTS: Tuple[str, ...] = ()
KINDS: Tuple[str, ...] = ("code", "md", "yaml-json", "other")
_TOKEN = re.compile(r"[\w.\-/|,=]+")
_YAML_JSON = {".yaml", ".yml", ".json", ".jsonl"}


@attrs.frozen
class Mention:
    path: str
    line: int
    kind: str
    text: str


def kind_of(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return "code"
    if suffix == ".md":
        return "md"
    return "yaml-json" if suffix in _YAML_JSON else "other"


def in_history(path: str, history: Sequence[str] = HISTORY) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in history)


def declared(repo: Path) -> "Tuple[Tuple[str, ...], Tuple[str, ...]]":
    """`(history, relative_roots)` this repo's own `codemap.yaml` declares under
    `domains.oldrefs.history` / `domains.oldrefs.relative_roots` -- `((), ())` when the
    file declares neither key, or declares no codemap.yaml at all. `move`/`rename` call
    this on the repo they are ACTUALLY running against and pass the result straight into
    `old_mentions`, rather than this module assuming a directory convention -- a
    `plans/**`-shaped history or a `harness/plugins/hs/skills/`-shaped relative root
    belongs to the repo that declares one, never to a hard default every target inherits
    unasked. A malformed codemap.yaml still raises (broken, never silently ignored);
    a missing one is not malformed -- most `--flat` repos never need one at all."""
    from ._layout import optional_descriptor
    descriptor = optional_descriptor(Path(repo))
    if descriptor is None:
        return (), ()
    block = (descriptor.domains or {}).get("oldrefs") or {}
    return (tuple(block.get("history") or ()),
            tuple(block.get("relative_roots") or ()))


def _tracked(repo: Path, at: Optional[str], under: str = "") -> List[str]:
    """`-z` (NUL-separated, no quoting): plain `ls-files`/`ls-tree` C-quote a non-ASCII
    path under git's default `core.quotePath=true` -- the same class of bug H1 fixed in
    `moveflat.tracked`. Here the quoted string is not what `spellings()` slices an owner
    name out of, so a non-ASCII-named file's ownership silently resolves to nobody and
    its otherwise-unambiguous relative spelling is dropped."""
    if not at:
        # the working tree: the files git counts as source, untracked included --
        # the universe `movemodule` rewrites (`source_files`, the one listing door)
        files = source_files(repo) or []
        base = under.rstrip("/")
        return [p for p in files if not base or p == base or p.startswith(base + "/")]
    spec = ["--", under] if under else []
    raw = run_git(repo, "ls-tree", "-r", "--name-only", "-z", at, *spec, check=False)
    return [p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p]


def _is_dir(repo: Path, old: str, at: Optional[str]) -> bool:
    if old.endswith("/"):
        return True
    if at:
        raw = run_git(repo, "cat-file", "-t", "%s:%s" % (at, old), check=False)
        return raw.decode("utf-8", "replace").strip() == "tree"
    return (repo / old).is_dir()


def spellings(repo: Path, old: str, at: Optional[str] = None,
              relative_roots: Sequence[str] = RELATIVE_ROOTS) -> List[str]:
    """`old` itself, plus its owner-relative spelling when exactly one owner carries it."""
    own = old.rstrip("/")
    out = [own]
    for root in relative_roots:
        if not own.startswith(root):
            continue
        owner, _, rel = own[len(root):].partition("/")
        if "/" not in rel:
            continue
        owners = set()
        for f in _tracked(repo, at, root):
            o, _, r = f[len(root):].partition("/")
            if r == rel or r.startswith(rel + "/"):
                owners.add(o)
        if owners == {owner}:
            out.append(rel)
    return out


def _matches(token: str, spell: str, is_dir: bool, new: Optional[str]) -> bool:
    if not is_dir:
        return token == spell
    if not (token == spell or token.startswith(spell + "/")):
        return False
    return not (new and (token == new or token.startswith(new + "/")))


def _hits(line: str, spells: Iterable[str], is_dir: bool, new: Optional[str]) -> bool:
    for raw in _TOKEN.findall(line):
        t = raw[2:] if raw.startswith("./") else raw
        t = t.rstrip(".,")
        if any(_matches(t, s, is_dir, new) for s in spells):
            return True
    return False


def _grep(repo: Path, needles: Sequence[str], at: Optional[str]) -> Dict[str, List[Tuple[int, str]]]:
    """{path: [(line, text)]} of source lines holding any needle as a substring --
    tracked, plus untracked-not-ignored on the working tree (`--untracked`: what
    `movemodule` rewrites, so the report sees every file the move could leave). Raw
    `subprocess.run`, not `run_git`: a `git grep` exit of 1 means "no match" -- an
    ordinary outcome, never an error -- so the one-code `run_git` contract (0 clean,
    anything else raises) does not fit; `check.f821.scan_scope` keeps the same shape
    for the same reason. `repo` is caller-supplied, so this still carries `run_git`'s
    own hardening (`secret_scan_before_ship`'s constants), never a private copy."""
    args = ["grep", "-n", "-I", "-z", "--full-name", "-F", *([] if at else ["--untracked"])]
    for n in needles:
        args += ["-e", n]
    r = subprocess.run(
        ["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(repo), *args,
         *([at] if at else []), "--"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=secret_scan.git_safe_env())
    if r.returncode not in (0, 1):
        raise RuntimeError("git grep failed in %s: %s" % (repo, r.stderr.strip()))
    out: Dict[str, List[Tuple[int, str]]] = {}
    for rec in r.stdout.split("\n"):
        parts = rec.split("\0", 2)
        if len(parts) != 3:
            continue
        path = parts[0].split(":", 1)[1] if at else parts[0]
        out.setdefault(path, []).append((int(parts[1]), parts[2]))
    return out


def old_mentions(repo: Path, old: str, new: Optional[str] = None, *, at: Optional[str] = None,
                 texts: Optional[Dict[str, str]] = None, history: Sequence[str] = HISTORY,
                 relative_roots: Sequence[str] = RELATIVE_ROOTS,
                 kinds: Sequence[str] = KINDS) -> List[Mention]:
    """Every tracked line naming `old`, in path then line order.

    `at` reads a commit instead of the working tree. `texts` ({path: text}) stands in for
    those files' contents -- a move's planned rewrite, so a dry-run reports what the
    `--apply` would leave. `old` itself (and, for a directory, what is under it) is never
    its own mention."""
    repo = Path(repo)
    own = old.rstrip("/")
    is_dir = _is_dir(repo, old, at)
    spells = spellings(repo, old, at, relative_roots)
    new_own = new.rstrip("/") if new else None
    texts = texts or {}
    found = {p: ls for p, ls in _grep(repo, spells, at).items() if p not in texts}
    for p, text in texts.items():
        found[p] = [(n, ln) for n, ln in enumerate(text.split("\n"), 1) if any(s in ln for s in spells)]
    out: List[Mention] = []
    for path in sorted(found):
        if path == own or path.startswith(own + "/") or in_history(path, history):
            continue
        kind = kind_of(path)
        if kind not in kinds:
            continue
        for n, ln in found[path]:
            if _hits(ln, spells, is_dir, new_own):
                out.append(Mention(path, n, kind, ln.strip()[:200]))
    return out


def as_rows(old: str, mentions: List[Mention]) -> List[dict]:
    return [dict(attrs.asdict(m), old=old) for m in mentions]


def print_report(rows: List[dict]) -> None:
    """The text block `move` and `rename` print under their plan."""
    if not rows:
        return
    files = {r["path"] for r in rows}
    print("  still name the old path -- listed, not rewritten (%d line(s) in %d file(s)):"
          % (len(rows), len(files)))
    for r in rows:
        print("    %s:%d  %-9s %s" % (r["path"], r["line"], r["kind"], r["text"]))
