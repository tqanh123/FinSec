"""hs_family.registry — the generic base+fragments registry loader shared by
every hs-* CLI that dispatches on a data-driven `{domain: {verb: spec}}` table.

Extracted from `hs_run.py` (was `harness/scripts/hs_run.py:1462-1960`) so a
second CLI (hs-code, `harness/data/hs-code-registry.d/`) loads through the
SAME merge/conflict/duplicate-key machinery instead of forking it — two
independent copies of one merge algorithm is exactly the failure
`harness_root.py` was already split out to stop (see that module's own
docstring for the precedent).

What stayed in `hs_run.py`: `step_routing`'s own business logic —
`rules_for`/`refs_for`/`advisory_for`, `StateRouting`/`DomainRouting`, and the
`Registry` value object's field names — because that is step-engine
vocabulary (hs-run only ever answers `next`), not something a read/query CLI
like hs-code shares. What moved HERE is the mechanical half
neither CLI would want to fork: path resolution (single-file override vs
base+fragments), per-file caching, the `.yml`-extension rejection, the two
conflict shapes (nested domain/verb vs. a flat top-level key), the closed
`RegistryError` family every caller maps to its own BROKEN exit code, and the
`module:`/`entry:` lazy-import/resolve pair a dispatcher calls once it has
picked a verb.

A caller supplies a `RegistryEnv` (which env vars, which default filenames)
and gets back a `LoadedRegistry` (`.domains`, `.sources`, `.blocks`) — `blocks`
carries whatever OTHER top-level block names the caller asked to merge
(hs-run asks for `step_routing` + `rules_not_routed`; a caller with no such
concept asks for none and gets an empty `.blocks`).
"""
from __future__ import annotations

import importlib
import itertools
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

import attrs
import yaml_io
from ruamel.yaml.constructor import DuplicateKeyError

from .errors import UsageError

MOVED_KEY = "moved_to"
#: Every family CLI and how many words name one of its commands: `<domain> <verb>` (2) or
#: `<command>` (1). A one-level registry's row sits directly under `domains:`.
FAMILY_DEPTH = {"hs-run": 2, "hs-code": 2, "hs-cli": 1, "hs-rops": 1, "hs-secrets": 1,
                "hs-usage": 1}
FAMILY = tuple(FAMILY_DEPTH)


# --------------------------------------------------------------------- errors

class RegistryError(Exception):
    """Base for a registry load failure. A caller's `main()` maps every
    subclass to its family's BROKEN exit code + a valid JSON error envelope —
    never a raw traceback."""


class RegistryParseError(RegistryError):
    """A registry file (base or fragment) failed to parse. Fatal, not skippable: a
    silently-skipped fragment makes its verbs vanish and the eventual "unknown
    domain/verb" error points the user at the wrong cause entirely."""

    def __init__(self, path: Path, cause: Exception):
        self.path = path
        self.cause = cause
        super().__init__("could not parse registry file %s: %s" % (path, cause))


class RegistryMissingError(RegistryError):
    """A registry file (an explicit single-file override, or a fragment listed
    by an earlier glob that vanished before it could be read) does not exist at
    all. Kept DISTINCT from RegistryParseError: a missing file is not a syntax
    problem, and reporting it as "could not parse" sends the reader hunting
    for a YAML error that is not there. (The default base file and fragments
    directory being ABSENT is not an error at all — see `load_registry()`'s
    own `is_file()`/`is_dir()` guards; this class only fires when a path was
    EXPLICITLY named and then turned out not to exist.)"""

    def __init__(self, path: Path):
        self.path = path
        super().__init__("registry file does not exist: %s" % path)


class RegistryConflictError(RegistryError):
    """Two sources (base file and/or fragments) both declare the same key.

    Used at TWO granularities by `load_registry()` below: `(domain, verb)` for
    the nested `domains:` block, and a flat top-level key (a domain name, a
    rule name, ...) for any other merged block — the constructor's second
    argument doubles as either a verb name or a block label, whichever the
    caller passed. Decision: hard error, not last-wins. Silent last-wins means
    a typo'd or alphabetically-late fragment filename can silently hijack an
    already-registered row — nothing would shout. One-file-per-domain only
    pays off if a genuine collision is treated as the authoring mistake it is."""

    def __init__(self, domain: str, verb: str, first: Path, second: Path):
        self.domain, self.verb, self.first, self.second = domain, verb, first, second
        super().__init__(
            "%s.%s is declared in BOTH %s and %s — a fragment can never silently "
            "override another; rename the verb or delete the duplicate row"
            % (domain, verb, first, second))


class RegistryDuplicateKeyError(RegistryError):
    """A SINGLE registry file (base or fragment) declares the same mapping key twice
    at the same level — two `next:` rows under one `plan:` block, or `plan:` declared
    twice at the top level. This is a DIFFERENT hazard than
    RegistryConflictError above: plain PyYAML's default loader (`yaml.safe_load`)
    treats a repeated mapping key as perfectly legal and silently keeps only the LAST
    one — the duplicate is already gone by the time this module's own cross-file
    conflict detection runs, so a verb (or an entire domain block) vanishes with zero
    trace, and the eventual failure is "unknown verb" — the exact misdirection this
    module's own docstring promises never happens. The shared `yaml_io` loader turns
    this into a loud, path+key-naming error at parse time instead."""

    def __init__(self, path: Path, key):
        self.path = path
        self.key = key
        super().__init__(
            "%s declares the key %r more than once at the same mapping level — a "
            "last-wins loader would silently keep only the LAST one (a verb or an "
            "entire domain block vanishing without a trace); remove the "
            "duplicate row" % (path, key))


class RegistryExtensionError(RegistryError):
    """A `.yml`-suffixed fragment under the registry's `.d/` directory.
    Decision: REJECT loudly, do not silently accept it as a second valid extension.
    Every other file under `harness/data/` is `.yaml` already — silently accepting
    `.yml` too would let two files (`plan.yaml` + `plan.yml`) both claim the same
    domain with no documented rule for which wins, which is a worse hazard than the
    one being fixed. Between "accept both" and "reject", reject matches the existing
    one-extension convention and costs nothing (rename is a one-line fix); silently
    ignoring it — the previous behaviour, `*.yaml`-only glob with no complaint — is
    the one option ruled out."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(
            "%s uses the '.yml' extension — registry fragments must use "
            "'.yaml' (matching every other file under harness/data/); rename it, "
            "do not add a second accepted extension" % path)


# Anchored on the trailing ` with value ` and GREEDY, because ruamel does not escape a
# quote inside the key it reports. A non-greedy `"(.*?)"` stops at the first inner quote,
# so a key spelled `say "hi"` came back as `say ` — a key that does not exist in the file,
# handed to the operator with full confidence. DOTALL because a key may contain a newline,
# which otherwise fails to match at all and dumps ruamel's whole multi-line message
# (documentation URL included) into the middle of a sentence.
_DUP_KEY_RE = re.compile(r'found duplicate key "(?P<key>.*)" with value ', re.DOTALL)


class RegistryMovedTargetError(RegistryError):
    """A `moved_to` row is malformed or points at a door that does not exist."""


class RegistryMovedError(RegistryError, UsageError):
    """The old spelling of a command that moved: exit 2, naming the new door."""

    def __init__(self, old: tuple, target, message: str = ""):
        self.old = old
        self.target = target  # None when no single door answers (a domain split apart)
        super().__init__(message or "`%s` has moved: run `%s`" % (" ".join(old), target))


def _retired_domain_refusal(domain: str, moved: dict):
    """The answer for a bare former domain every one of whose verbs moved, or
    None. A registry cannot hold both a domain-level `moved_to` and verbs in
    one node, so a domain retired verb by verb has no `(domain,)` row and
    would otherwise read as "unknown domain", naming no door. One shared
    `<cli> <dest>` head is the door the whole domain moved to; verbs that went
    to different doors (two fragments each retiring part of one domain) are
    named door by door."""
    by_door: dict = {}
    for key, target in moved.items():
        if len(key) >= 2 and key[0] == domain:
            by_door.setdefault(target, []).append(key[1])
    if not by_door:
        return None
    heads = {" ".join(t.split()[:2]) for t in by_door}
    if len(heads) == 1 and all(len(t.split()) >= 2 for t in by_door):
        head = heads.pop()
        return RegistryMovedError(
            (domain,), head,
            "%r has moved — every verb it once carried now lives under %r "
            "(run `%s <verb>`)" % (domain, head, head))
    doors = "; ".join("%s → `%s`" % (", ".join(sorted(verbs)), target)
                      for target, verbs in sorted(by_door.items()))
    return RegistryMovedError(
        (domain,), None, "%r has moved — its verbs now live elsewhere: %s" % (domain, doors))


def _duplicate_key_name(exc: DuplicateKeyError) -> str:
    """Pull the offending key out of ruamel's DuplicateKeyError for the error message.

    ruamel reports the key inside prose (`found duplicate key "plan" with value ...`)
    rather than as an attribute, so this reads it back out. If that wording ever changes
    the regex stops matching and the fallback hands over the FULL message — less precise,
    but never a confidently wrong key name. The failure mode that shape does NOT cover is
    a regex that matches the wrong SPAN, which is why the pattern is anchored on both
    sides rather than trusting a lazy quantifier to stop in the right place.
    """
    m = _DUP_KEY_RE.search(str(getattr(exc, "problem", "") or exc))
    return m.group("key") if m else str(exc).strip()


@lru_cache(maxsize=None)
def read_registry_file(path: Path) -> dict:
    """Read one registry file whole. Callers pick the block they want.

    Reading whole and selecting AFTERWARDS is the point: a reader that opened the
    file and returned `.get("domains")` in one step would drop any sibling block —
    `step_routing:` for hs-run, whatever a future caller names — inside the reader
    with no caller able to observe it had been there.

    Cached per path for the process's lifetime: a caller merging N blocks out of the
    same file would otherwise re-open and re-parse it once per block — measured on
    hs-run's real registry at ~49ms of a ~58ms `main()` before this cache existed.
    A registry file never changes mid-process, so the second and third read of the
    same path is pure waste. lru_cache also means a raised error
    (missing/duplicate-key/parse failure) is never cached — only a successful read
    is, which is the read worth reusing.
    """
    if not path.is_file():
        raise RegistryMissingError(path)
    try:
        raw = yaml_io.yaml_load(path.read_text(encoding="utf-8"))
    except DuplicateKeyError as e:
        raise RegistryDuplicateKeyError(path, _duplicate_key_name(e)) from e
    except Exception as e:  # noqa: BLE001 — re-raised as a named, path-carrying error
        raise RegistryParseError(path, e) from e
    return raw or {}


def load_named_block(path: Path, block_name: str) -> dict:
    """Read one registry file's top-level `<block_name>:` mapping. An empty/
    comment-only file, or one that simply does not carry this block, is a legal
    no-op ({} back); a MISSING file raises RegistryMissingError (a distinct,
    correctly-named cause — never reported as a parse failure, which sends the
    reader hunting for a YAML syntax problem that does not exist); a file that
    exists but fails to parse (bad syntax, OR a repeated mapping key) raises
    RegistryParseError / RegistryDuplicateKeyError naming THIS path — never
    swallowed."""
    return read_registry_file(path).get(block_name) or {}


def _normalize_domains(domains: dict) -> dict:
    return {str(d): {str(v): spec for v, spec in (verbs or {}).items()}
            for d, verbs in (domains or {}).items()}


@attrs.define(frozen=True, kw_only=True)
class RegistryEnv:
    """Which env vars one registry family's location resolves through, and what
    its default files are named — the parameter that lets `load_registry()`
    below serve hs-run's `harness/data/hs-run-registry.d/` and, unmodified,
    hs-code's own `harness/data/hs-code-registry.d/` (a second `RegistryEnv`
    instance, never a second copy of the merge algorithm).

    `domain_module_candidates` is OPTIONAL and answers a different question
    than the rest of this class: not "where do the registry's OWN files
    live", but "what does a domain's IMPLEMENTATION look like on disk for
    this family". hs-run's shape is a flat `harness/scripts/hs_run_<x>.py`
    file per domain; hs-code's (a directory under its own domain root) will
    be a different callable entirely — the field exists so
    `find_undeclared_domain_modules()` below stays family-agnostic rather
    than hardcoding hs-run's file layout. `None` means "this family has no
    on-disk enumeration story" (an override-only fixture, say), and the
    check below is then silently a no-op — presence is what it looks for,
    never absence-by-default."""
    registry_name: str
    dir_name: str
    override_env: str
    dir_env: str
    domain_module_candidates: Optional[Callable[[Path], set]] = None

    def base_path(self, root: Path) -> Path:
        import config_layers
        return config_layers.resolve_path(self.registry_name, root=root)[0]

    def frag_dir(self, root: Path) -> Path:
        raw = os.environ.get(self.dir_env)
        return Path(raw) if raw else root / "harness" / "data" / self.dir_name


@attrs.define(frozen=True, kw_only=True)
class LoadedRegistry:
    """Everything one registry read produces, as a value rather than a tuple.

    `blocks` is generic on purpose: hs-run asks for `step_routing` and
    `rules_not_routed` and normalizes each into its own richer objects
    afterward; a caller with no such concept asks for `extra_blocks=()` and
    gets an empty mapping back."""
    domains: dict = attrs.field()
    sources: list = attrs.field()
    blocks: dict = attrs.field(factory=dict)
    moved: dict = attrs.field(factory=dict)
    #: `(domain, verb)` -> the file that declared it (empty for a single-file override).
    origins: dict = attrs.field(factory=dict)


def load_registry(env: RegistryEnv, root: Path, *, override=None,
                   extra_blocks: tuple = (),
                   check_moved: bool = True, extra_frag_dirs: tuple = ()) -> LoadedRegistry:
    """Merge the table, then take every `moved_to` row out of it (`.moved`) and, with
    `check_moved`, check each new door exists in the target CLI's own registry files
    (every family CLI has one; `FAMILY_DEPTH` says how many words name a command).

    `extra_frag_dirs` — additional fragment directories merged AFTER the env's own
    (core first, packs after, in the order given): a pack's own `<pack>/hs-code-
    registry.d/` adding a row through the SAME conflict/duplicate-key machinery a
    second core fragment would go through, never a second merge algorithm. Ignored
    when `override`/`env.override_env` is set — a single-file override is a full
    replacement of the whole base+fragments pipeline, packs included."""
    loaded = _load_merged(env, root, override=override, extra_blocks=extra_blocks,
                          extra_frag_dirs=extra_frag_dirs)
    live, moved = _split_moved(loaded.domains, strict=check_moved)
    loaded = attrs.evolve(loaded, domains=live, moved=moved)
    if check_moved and moved:
        _check_moved(env, root, loaded)
    return loaded


def family_env(cli: str) -> RegistryEnv:
    """The registry location of one family CLI, by the family's naming convention."""
    stem = cli.replace("-", "_").upper()
    return RegistryEnv(registry_name="%s-registry.yaml" % cli, dir_name="%s-registry.d" % cli,
                       override_env="HARNESS_%s_REGISTRY" % stem,
                       dir_env="HARNESS_%s_REGISTRY_DIR" % stem)


def _cli_of(env: RegistryEnv) -> str:
    return env.registry_name[:-len("-registry.yaml")]


def _split_moved(domains: dict, strict: bool = True) -> tuple:
    """`(live, moved)`. Not `strict` (a caller that reports registry health itself, such as
    `hs-cli doctor`): a node that is both moved and live stays live, so the caller can
    still run and name the problem from its own strict read."""
    live: dict = {}
    moved: dict = {}
    for domain, verbs in domains.items():
        if MOVED_KEY in verbs:
            rest = sorted(v for v in verbs if v != MOVED_KEY)
            if rest and not strict:
                live[domain] = {v: spec for v, spec in verbs.items() if v != MOVED_KEY}
                continue
            if rest:
                raise RegistryMovedTargetError(
                    "%s: a moved command (moved_to) cannot also hold verbs: %s"
                    % (domain, ", ".join(rest)))
            moved[(domain,)] = _target(domain, verbs[MOVED_KEY])
            continue
        for verb, spec in verbs.items():
            if isinstance(spec, dict) and MOVED_KEY in spec:
                if set(spec) != {MOVED_KEY}:
                    raise RegistryMovedTargetError(
                        "%s %s: a moved_to row carries nothing else, got %s"
                        % (domain, verb, ", ".join(sorted(spec))))
                moved[(domain, verb)] = _target("%s %s" % (domain, verb), spec[MOVED_KEY])
            else:
                live.setdefault(domain, {})[verb] = spec
    return live, moved


def _target(old: str, target) -> str:
    if not isinstance(target, str) or not target.split():
        raise RegistryMovedTargetError("%s: moved_to must name a command, got %r" % (old, target))
    return " ".join(target.split())


def _check_moved(env: RegistryEnv, root: Path, loaded: LoadedRegistry) -> None:
    own = _cli_of(env)
    own_live, own_moved = _table(loaded.domains, loaded.moved, _depth_of(own))
    tables = {own: (own_live, own_moved)}
    # A moved row's OLD name shadowing a still-live command. Live and moved rows are
    # split out of the SAME `domains:` node by `_split_moved` ("cannot also hold
    # verbs" / "carries nothing else"), so the two sets are disjoint by construction;
    # kept as the one place that would say so if a second source of live names came back.
    shadowed = own_moved & own_live
    if shadowed:
        raise RegistryMovedTargetError(
            "%s: a moved_to row's old name shadows a still-live command: %s"
            % (own, ", ".join(" ".join(h) for h in sorted(shadowed))))
    for old, target in loaded.moved.items():
        cli, *cmd = target.split()
        where = "%s moved_to %r" % (" ".join(old), target)
        if cli not in FAMILY:
            raise RegistryMovedTargetError("%s names no hs-* CLI (%s)" % (where, ", ".join(FAMILY)))
        if cli not in tables:
            other = load_registry(family_env(cli), root, check_moved=False)
            tables[cli] = _table(other.domains, other.moved, _depth_of(cli))
        live, moved = tables[cli]
        head = _command_head(cmd, live | moved)
        if head in moved:
            raise RegistryMovedTargetError("%s points at a command that moved itself" % where)
        if head not in live:
            raise RegistryMovedTargetError("%s: no such command" % where)


def _command_head(cmd: list, known: set) -> tuple:
    """The command a target names: its words before the first flag, taking the
    second only when the first has verbs under it. `(domain,)` is live on its own,
    so `live nope` must not pass as `live`; hs-cli's commands are one word, so
    `trust add` is `trust` plus its own argv."""
    words = list(itertools.takewhile(lambda w: not w.startswith("-"), cmd))
    if len(words) >= 2 and any(len(k) == 2 and k[0] == words[0] for k in known):
        return tuple(words[:2])
    return tuple(words[:1])


def _depth_of(cli: str) -> int:
    return FAMILY_DEPTH.get(cli, 2)


def _table(domains: dict, moved: dict, depth: int = 2) -> tuple:
    if depth == 1:
        return {(str(d),) for d in domains}, set(moved)
    # `(domain,)` alongside every `(domain, verb)`: a `moved_to` target may
    # name a bare domain (no verb) — see `refuse_if_moved`'s `domain_targets`
    # for the other half of that shape (appending the caller's own verb).
    # Built as one untyped set rather than two same-shape comprehensions
    # unioned together: a 1-tuple and a 2-tuple literal in one comprehension
    # is a real arity mismatch mypy is right to flag, not a false positive
    # worth silencing.
    live: set = set()
    for d, verbs in domains.items():
        live.add((d,))
        for v in verbs:
            live.add((d, v))
    return live, set(moved)


def refuse_if_moved(argv, loaded: LoadedRegistry, *, domain_targets: bool = False) -> None:
    """Raise `RegistryMovedError` when `argv` starts with a command that moved.

    `domain_targets`, set only by a registry-backed CLI (hs-run/hs-code —
    never hs-cli, whose targets are always fully specified at 2 words by
    construction): when the matched old name is a single word and its
    target names only `<cli> <domain>` (2 words, no verb), the caller's own
    NEXT argv token is appended when it looks like a verb (not a flag) —
    `hs-run oldcook close` answers `hs-run cook close`, not a domain-only
    door that drops whatever verb the caller actually typed. A target that
    already names a verb (3 words) is the author's deliberate, unambiguous
    choice and is never overridden.

    The 2-word heuristic assumes the target CLI is registry-backed
    (hs-run/hs-code), where `<cli> <domain>` is genuinely incomplete without
    a verb. It does NOT hold for a target into hs-cli: hs-cli's commands are
    flat, so `hs-cli <verb>` (2 words) is ALREADY complete — appending the
    caller's own next word would hand it a bogus extra positional (measured:
    `oldtrust: {moved_to: "hs-cli trust"}` made `hs-run oldtrust next` answer
    `hs-cli trust next`, which hs-cli then read as a repo path)."""
    words = list(argv[:2])
    for head in (tuple(words[:2]), tuple(words[:1])):
        if head and head in loaded.moved:
            target = loaded.moved[head]
            target_words = target.split()
            if (domain_targets and len(head) == 1 and len(target_words) == 2
                    and target_words[0] != "hs-cli"):
                extra = argv[1:2]
                if extra and not extra[0].startswith("-"):
                    target = "%s %s" % (target, extra[0])
            raise RegistryMovedError(head, target)
    if argv and not argv[0].startswith("-") and argv[0] not in loaded.domains:
        refusal = _retired_domain_refusal(argv[0], loaded.moved)
        if refusal is not None:
            raise refusal


def _load_merged(env: RegistryEnv, root: Path, *, override=None,
                 extra_blocks: tuple = (), extra_frag_dirs: tuple = ()) -> LoadedRegistry:
    """Resolve one `LoadedRegistry`: verb table, source paths, and whatever
    other top-level blocks `extra_blocks` names.

    `override` (or `env.override_env` in the process's environment) short-
    circuits to a single file, full stop — a MISSING override file is a named
    RegistryMissingError, not a confusing "could not parse". Otherwise: the
    base file (if present) merges with every `*.yaml` fragment under the
    fragments dir (if the dir exists at all — its total absence degrades to
    base-only, never a crash), fragments visited in SORTED FILENAME order
    (never directory iteration order — that order is not part of the OS's
    contract, so a merge depending on it would be a latent, filesystem-
    dependent bug). A `.yml`-suffixed fragment is REJECTED loudly
    (RegistryExtensionError). A `(domain, verb)` declared by more than one
    source raises RegistryConflictError; the same for a top-level key
    repeated across sources within one of `extra_blocks`; a duplicate key
    WITHIN one source raises RegistryDuplicateKeyError — all loud, all naming
    the file(s).

    `extra_frag_dirs` (see `load_registry`'s own docstring) are read the same way,
    right after the env's own fragments — a domain/verb a pack declares that the
    core also declares is a real `RegistryConflictError`, not a silent pack-wins
    or core-wins.

    The returned `.sources` is what a caller's envelope can surface as
    self-declared provenance — which files actually won, so a redirected/
    overridden registry never acts invisibly."""
    ov = override if override is not None else os.environ.get(env.override_env)
    if ov:
        p = Path(ov)
        domains = _normalize_domains(load_named_block(p, "domains"))
        blocks = {b: {str(k): v for k, v in load_named_block(p, b).items()}
                  for b in extra_blocks}
        return LoadedRegistry(domains=domains, sources=[str(p)], blocks=blocks)

    sources = []
    base = env.base_path(root)
    if base.is_file():
        sources.append(base)
    frag_dir = env.frag_dir(root)
    if frag_dir.is_dir():
        sources.extend(_fragment_files(frag_dir))

    # Pack fragment dirs, core first: same stray-`.yml` rejection, same sorted-
    # filename order WITHIN a dir, dirs themselves visited in the order the
    # caller passed (a pack's own declared order, never directory-iteration
    # order — see this function's docstring for why that matters at all).
    for pdir in extra_frag_dirs:
        if pdir.is_dir():
            sources.extend(_fragment_files(pdir))

    return _merge(sources, extra_blocks)


def _fragment_files(pdir: Path) -> list:
    strays = sorted(pdir.glob("*.yml"))
    if strays:
        raise RegistryExtensionError(strays[0])
    return sorted(pdir.glob("*.yaml"))


def load_fragment_dirs(dirs, *, extra_blocks: tuple = ()) -> LoadedRegistry:
    """Merge ONLY `dirs`' `*.yaml` fragments (in the order given, sorted within a dir) —
    a pack's rows as a registry of their own, through the same merge `load_registry` runs.
    Moved rows are split out, never checked: a pack names no door of its own."""
    sources: list = []
    for pdir in dirs:
        if Path(pdir).is_dir():
            sources.extend(_fragment_files(Path(pdir)))
    loaded = _merge(sources, extra_blocks)
    live, moved = _split_moved(loaded.domains)
    return attrs.evolve(loaded, domains=live, moved=moved)


def _merge(sources: list, extra_blocks: tuple) -> LoadedRegistry:
    merged: dict = {}
    origin: dict = {}  # (domain, verb) -> the file that first declared it
    block_merged: dict[str, dict] = {b: {} for b in extra_blocks}
    block_origin: dict[str, dict] = {b: {} for b in extra_blocks}  # block -> {key -> file}
    for src in sources:
        for domain, verbs in load_named_block(src, "domains").items():
            domain = str(domain)
            for verb, spec in (verbs or {}).items():
                verb = str(verb)
                key = (domain, verb)
                if key in origin:
                    raise RegistryConflictError(domain, verb, origin[key], src)
                origin[key] = src
                merged.setdefault(domain, {})[verb] = spec
        # Every other requested block collides at its own TOP-LEVEL key, not at
        # (domain, verb): the block's row is one object, so two fragments both
        # declaring it is the same authoring mistake a duplicate verb is — and
        # last-wins would hide it behind alphabetical filename order.
        for block_name in extra_blocks:
            for block_key, val in load_named_block(src, block_name).items():
                block_key = str(block_key)
                if block_key in block_origin[block_name]:
                    raise RegistryConflictError(block_key, block_name,
                                                block_origin[block_name][block_key], src)
                block_origin[block_name][block_key] = src
                block_merged[block_name][block_key] = val

    return LoadedRegistry(domains=_normalize_domains(merged),
                          sources=[str(s) for s in sources],
                          blocks=block_merged, origins=origin)


def resolve_verb(domain: str, verb: str, registry: dict):
    """Return the callable for (domain, verb), or None if either is unregistered.
    Import happens HERE, at dispatch time, not at registry-load time — a domain
    nobody calls this run never pays an import cost (mirrors hook_dispatch.py's
    _resolve_core lazy-import discipline)."""
    spec = (registry.get(domain) or {}).get(verb)
    if spec is None:
        return None
    module_name = str(spec["module"])
    entry_name = str(spec.get("entry") or ("cmd_" + verb.replace("-", "_")))
    mod = importlib.import_module(module_name)
    return getattr(mod, entry_name, None)


def import_declared_modules(domains: dict) -> dict:
    """`{module name: None-or-exception}` for every DISTINCT module a
    `domains:` table declares — importing each exactly once even when several
    verbs share it (hs-run's `cook` domain points all eight of its verbs at
    `hs_run_cook`).

    Closes a real gap: `resolve_verb()` above imports lazily, on purpose
    — but that means a typo'd `module:` name only ever breaks at the one
    dispatch call that happens to invoke that exact verb. A domain nobody
    called in a given run, or in CI, ships with a silently broken row. This
    walks the whole table eagerly and reports every failure, so a caller can
    turn a nonempty result into a loud failure (`hs-cli doctor`, and a test
    over the shipped registry) instead of waiting for the one user who
    happens to hit it."""
    modules = sorted({str(spec["module"])
                       for verbs in (domains or {}).values()
                       for spec in (verbs or {}).values()
                       if spec and spec.get("module")})
    result: dict = {}
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            result[name] = exc
        else:
            result[name] = None
    return result


def find_undeclared_domain_modules(env: RegistryEnv, root: Path, domains: dict) -> list:
    """The mirror image of `import_declared_modules()` above: that one catches
    a `domains:` row whose `module:` does not exist; this one catches a
    module that EXISTS on disk but no row names — a domain a fragment file
    forgot to declare, silent by construction because nothing ever asked
    "what's on disk that isn't in the table" before.

    `env.domain_module_candidates(root)` (the family-specific callback —
    see `RegistryEnv`'s own docstring) returns the set of module names that
    LOOK like a domain implementation for this family; anything in that set
    with no matching `module:` anywhere in `domains` is reported. Returns a
    sorted list of module names, or `[]` when `env` carries no callback (the
    check is then a deliberate no-op, not a false "all clear")."""
    if env.domain_module_candidates is None:
        return []
    candidates = env.domain_module_candidates(root)
    declared = {str(spec["module"])
                for verbs in (domains or {}).values()
                for spec in (verbs or {}).values()
                if spec and spec.get("module")}
    return sorted(candidates - declared)
