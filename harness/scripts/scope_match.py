#!/usr/bin/env python3
"""scope_match.py — the canonical path-glob matcher for the rule subsystem.

Two gates already share a fnmatch matcher (`path_glob.py`: the deny matcher +
component-policy). The RULE side needs different, richer semantics: gitignore-style
`**` plus the relation predicates `globs_overlap` / `glob_subsumes` that
conflict-detect and coverage-derive rely on. fnmatch cannot answer "do these two
globs intersect", so this is a separate module with a regex/automaton model — it
does NOT change path_glob's semantics for the other two gates.

Glob semantics (identical to the old review_rules/risk_rubric `_glob_to_re`):
  `**/` zero-or-more leading path segments, `**` anything (incl `/`),
  `*` within one segment (no `/`), `?` one non-slash char.

The duplicated `_glob_to_re` copies (review_rules.py + risk_rubric.py) re-point
here. Case-sensitivity is NOT unified into one default: review-rules/consumer use
case-sensitive (gitignore), risk-rubric keeps case-insensitive on purpose (a
case-sensitive match would miss AuthService.java / AuthGuard.tsx and silently
under-rate security risk). The CODE is unified; the `case_insensitive=` knob keeps
the two semantics distinct.
"""

import functools
import re


def glob_to_regex(glob: str, case_insensitive: bool = False) -> "re.Pattern":
    """Compile a path glob to an anchored regex with gitignore-style `**`.

    `**/` -> `(?:.*/)?` (zero-or-more leading segments), `**` -> `.*`,
    `*` -> `[^/]*` (within a segment), `?` -> `[^/]`.
    """
    out = []
    i, n = 0, len(glob)
    while i < n:
        if glob[i:i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif glob[i:i + 2] == "**":
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    flags = re.IGNORECASE if case_insensitive else 0
    return re.compile("^" + "".join(out) + "$", flags)


def scope_matches(globs, changed_files, case_insensitive: bool = False) -> bool:
    """True when >=1 changed file matches at least one glob in `globs` (OR-fan)."""
    for glob in globs or []:
        rx = glob_to_regex(glob, case_insensitive=case_insensitive)
        for f in changed_files or []:
            if rx.match(f):
                return True
    return False


# --- declared-entry conflict (grounded, not abstract) --------------------------
#
# A `files_to_*`-style sidecar entry is a path glob in THIS module's semantics —
# not only an exact path or a `dir/` prefix. Two callers used to compare such
# entries as plain literal strings (a set intersection, or `.is_file()` on the
# entry itself): `plan_graph.find_parallel_conflicts` and
# `graph_test_scope.resolve_test_scope`. Both need the same normalization
# (`normalize_dir_prefix`) and the same three-way split below (literal/literal,
# literal/glob, glob/glob) — one helper, so a third caller never copies either
# bug back in.

_GLOB_CHARS = frozenset("*?")


def has_glob_chars(entry: str) -> bool:
    """True when `entry` contains a glob metacharacter in this module's dialect
    (`*`/`?`; `**` is just two consecutive `*`) — i.e., it names more than the
    one exact path it spells."""
    return any(c in entry for c in _GLOB_CHARS)


def normalize_dir_prefix(entry: str) -> str:
    """A trailing `/` names everything under it — the same rule
    `hs_run_git.py`'s `_matches_declared` applies to a staged path's declared
    set, pulled out here so every reader of a `files_to_*`-style entry agrees
    on what a directory prefix means."""
    return entry + "**" if entry.endswith("/") else entry


def declared_conflict(entry_a: str, entry_b: str, universe=()) -> set:
    """Concrete paths that BOTH `entry_a` and `entry_b` claim.

    This is the GROUNDED answer a conflict report needs — an actual path to
    name — as opposed to `globs_overlap`'s abstract "could some string satisfy
    both patterns" answer, which has no single path to point at.

    - identical (normalized) entries: always a conflict, reported as that one
      entry — two phases declaring the exact same literal path, or the exact
      same glob, need no grounding to know they collide.
    - literal vs literal, unequal: never a conflict — two different exact
      paths cannot be the same file. The pre-existing `_owned(a) & _owned(b)`
      behavior for the common case, preserved without any glob machinery.
    - literal vs glob: the literal path is the only candidate there is —
      matched directly against the glob with `scope_matches`, no `universe`
      needed. (`pkg/` vs `pkg/a.py`, normalized to `pkg/**` vs `pkg/a.py`, is
      this case.)
    - glob vs glob (and not identical): grounded against `universe` — the
      candidate concrete paths (e.g. `git ls-files` of the repo, plus any
      literal `files_to_create` entry a phase declared, since a file that
      does not exist YET is still a real, specific future path) — a path
      both regexes match. Empty when nothing in `universe` instantiates the
      overlap: an abstract "these shapes COULD collide" is not evidence of a
      real conflict, and the per-file report shape has no slot for a claim
      with no path behind it.
    """
    a = normalize_dir_prefix(entry_a)
    b = normalize_dir_prefix(entry_b)
    if a == b:
        return {a}
    lit_a, lit_b = not has_glob_chars(a), not has_glob_chars(b)
    if lit_a and lit_b:
        return set()
    if lit_a:
        return {a} if scope_matches([b], [a]) else set()
    if lit_b:
        return {b} if scope_matches([a], [b]) else set()
    rx_a, rx_b = glob_to_regex(a), glob_to_regex(b)
    return {p for p in universe if rx_a.match(p) and rx_b.match(p)}


# --- glob relation predicates (overlap / subsumption) -------------------------
#
# Implemented as a small token automaton over the path alphabet. fnmatch/regex
# alone can decide "does a string match a glob"; deciding "do two globs share any
# string" / "does one glob's language contain another's" needs the automaton.
# Path globs are short, so the powerset product stays tiny and bounded.
#
# Token kinds after parsing:
#   ('LIT', c)  exactly char c (c may be '/')
#   ('Q',)      one non-slash char            (`?`)
#   ('STAR',)   zero+ non-slash chars         (`*`)
#   ('DSTAR',)  zero+ any chars incl '/'      (`**`)
# `**/` -> `(?:.*/)?` is an OPTIONAL "(.* then /)", so it is expanded at parse
# time into two alternative token lists: the empty branch, and `[DSTAR, '/']`.

def _tokenize_alts(glob: str):
    """Parse a glob into a list of token-lists, expanding each `**/` into its two
    branches (empty | `.*` then `/`). Usually one alt; >1 only with `**/`."""
    alts = [[]]
    i, n = 0, len(glob)
    while i < n:
        if glob[i:i + 3] == "**/":
            nxt = []
            for seq in alts:
                nxt.append(list(seq))                                  # empty branch
                nxt.append(list(seq) + [("DSTAR",), ("LIT", "/")])     # `.*/` branch
            alts = nxt
            i += 3
        elif glob[i:i + 2] == "**":
            for seq in alts:
                seq.append(("DSTAR",))
            i += 2
        elif glob[i] == "*":
            for seq in alts:
                seq.append(("STAR",))
            i += 1
        elif glob[i] == "?":
            for seq in alts:
                seq.append(("Q",))
            i += 1
        else:
            for seq in alts:
                seq.append(("LIT", glob[i]))
            i += 1
    return alts


def _can_empty_from(toks, k: int) -> bool:
    """Can toks[k:] match the empty string? (STAR/DSTAR can, LIT/Q cannot.)"""
    for t in toks[k:]:
        if t[0] in ("LIT", "Q"):
            return False
    return True


def _consume(toks, k: int, ch: str):
    """Next token indices after consuming char `ch` at position k (no epsilon)."""
    t = toks[k]
    kind = t[0]
    if kind == "LIT":
        return [k + 1] if ch == t[1] else []
    if kind == "Q":
        return [k + 1] if ch != "/" else []
    if kind == "STAR":
        return [k] if ch != "/" else []   # stay in the star (exit via epsilon)
    if kind == "DSTAR":
        return [k]                          # stay in the star (any char)
    return []


def _eps_closure(toks, states):
    """Expand STAR/DSTAR k -> k+1 (matching empty) to closure."""
    out = set(states)
    stack = list(states)
    while stack:
        k = stack.pop()
        if k < len(toks) and toks[k][0] in ("STAR", "DSTAR") and (k + 1) not in out:
            out.add(k + 1)
            stack.append(k + 1)
    return frozenset(out)


def _step(toks, states, ch: str):
    nxt = set()
    for k in states:
        if k < len(toks):
            nxt.update(_consume(toks, k, ch))
    return _eps_closure(toks, nxt)


def _accepts(toks, states) -> bool:
    return any(_can_empty_from(toks, k) for k in states)


def _probe_chars(*alt_groups):
    """A sound finite alphabet partition: every literal that appears, plus one
    representative non-slash char not among them, plus '/'."""
    lits = set()
    for alts in alt_groups:
        for toks in alts:
            for t in toks:
                if t[0] == "LIT" and t[1] != "/":
                    lits.add(t[1])
    probes = list(lits)
    gen = None
    for c in "abcdefghijklmnopqrstuvwxyz0123456789_.-":
        if c not in lits:
            gen = c
            break
    if gen is not None:
        probes.append(gen)
    probes.append("/")
    return probes


@functools.lru_cache(maxsize=4096)
def globs_overlap(a: str, b: str) -> bool:
    """True when some path matches BOTH globs (their languages intersect).

    Memoized because the answer is a pure function of the two glob strings and
    the callers repeat themselves structurally: the rule conflict audit compares
    every rule's scope against every other's, and rules share scopes, so it asked
    8085 overlap questions over 1459 distinct pairs — 5.5 repeats each. Each
    answer is a product-automaton reachability search, which is what made that
    audit cost 17.8s cold.

    Bounded rather than unbounded: the keys are globs read from rule files, a set
    that does not grow with input size, and a ceiling keeps a long-lived process
    from holding every pair it ever saw.
    """
    a_alts = _tokenize_alts(a)
    b_alts = _tokenize_alts(b)
    probes = _probe_chars(a_alts, b_alts)
    for ta in a_alts:
        for tb in b_alts:
            if _literals_compatible(ta, tb) and _alts_intersect(ta, tb, probes):
                return True
    return False


def _literal_run(toks, from_end: bool = False) -> str:
    seq = reversed(toks) if from_end else toks
    out = []
    for t in seq:
        if t[0] != "LIT":
            break
        out.append(t[1])
    return "".join(reversed(out) if from_end else out)


def _literals_compatible(ta, tb) -> bool:
    """False when no string can carry both literal heads, or both literal tails.

    A match of `ta` starts with its leading LIT run and ends with its trailing one, so a
    shared string needs each head to be a prefix of the other and each tail a suffix.
    Measured on the rule conflict audit: 2188 distinct pairs, 4.0s cold -> 0.19s, the
    same answer for every pair, because most scope pairs part at their first character."""
    ha, hb = _literal_run(ta), _literal_run(tb)
    if not (ha.startswith(hb) or hb.startswith(ha)):
        return False
    ea, eb = _literal_run(ta, True), _literal_run(tb, True)
    return ea.endswith(eb) or eb.endswith(ea)


def _alts_intersect(ta, tb, probes) -> bool:
    """Product reachability: is there a string accepted by both token lists?"""
    start = (_eps_closure(ta, {0}), _eps_closure(tb, {0}))
    seen = {start}
    stack = [start]
    while stack:
        sa, sb = stack.pop()
        if _accepts(ta, sa) and _accepts(tb, sb):
            return True
        for ch in probes:
            nxt = (_step(ta, sa, ch), _step(tb, sb, ch))
            if nxt[0] and nxt[1] and nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def glob_subsumes(a: str, b: str) -> bool:
    """True when every path matching `b` also matches `a` (L(b) subset of L(a)).

    Checked by product of b's automaton with the UNION of a's alternatives: if a
    reachable product state accepts on the b side but not on the a side, there is
    a witness in L(b) outside L(a), so a does not subsume b.
    """
    a_alts = _tokenize_alts(a)
    b_alts = _tokenize_alts(b)
    probes = _probe_chars(a_alts, b_alts)
    for tb in b_alts:
        if not _alt_included_in_union(tb, a_alts, probes):
            return False
    return True


def _alt_included_in_union(tb, a_alts, probes) -> bool:
    """L(tb) subset of union(L(a) for a in a_alts)."""
    b_start = _eps_closure(tb, {0})
    a_start = tuple(_eps_closure(a, {0}) for a in a_alts)
    start = (b_start, a_start)
    seen = {start}
    stack = [start]
    while stack:
        sb, sa = stack.pop()
        if _accepts(tb, sb) and not any(_accepts(a_alts[k], sa[k]) for k in range(len(a_alts))):
            return False    # witness accepted by b, rejected by every a-alt
        for ch in probes:
            nb = _step(tb, sb, ch)
            if not nb:
                continue    # no b-string continues this way; nothing to cover
            na = tuple(_step(a_alts[k], sa[k], ch) for k in range(len(a_alts)))
            nxt = (nb, na)
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return True
