"""hs-code check owners — which declared channel owns each command-line module, and on
what evidence.

Nothing imports an entry point, so the import graph cannot say where one belongs; the
lines that NAME it can — a command in a skill file, a hook registry entry, a CI step.
This reads the snapshot's two reference fields (`invoked_by`, `path_drivers`) and sorts
them into the channels `codemap.yaml` `owners:` declares.

THE RANK LAWS
    * A line that RUNS a module outranks a line that merely names it.
    * A declared ledger never owns (it names everything).
    * A `test_channels` claim loses to any product channel's, at any rank; it still owns
      a module nobody else claims.
    * With no reference at all, an in-tree importer's channel claims it (fallback only).
    * Several homes at the top rank are NAMED, never broken by guess: `shared` from
      `shared_at` claimants up, `contested` below.

THE KEEP LAWS (outcome only; the measured home and candidates survive in the entry)
    1 a declared `owners.stay` glob · 2 already home (its path matches its owner's
    pattern) · 3 depended on: a live line (code token or data value, never prose) outside
    the owner channel, not a test, not a ledger — or an importer — pins it · 4 contested
    · 5 already home in a test channel · 6 its channel's `owners.layout` entry is `stay`.

The counts `unclaimed`, `unmatched`, `contested` ratchet both ways against `--baseline`.

WHAT IT CANNOT SEE
    * Intent: a channel owns a module because its files name it.
    * A caller that leaves no line (a wrapper, a path built from a variable): `unclaimed`
      is an upper bound on "nothing uses this", never a finding on its own.
    * Law 3 reads only files it can classify: Python (tokenize: comments and docstrings
      are prose), hash-comment kinds (.yaml/.sh/.toml/.txt/no extension), .json. A
      mention in .md counts as nothing.
    * Every blind spot of the snapshot's driver stage, including its include list.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import attrs

from codegraph._core.testfiles import build_test_layout
from codegraph.check.tree import CheckError, add_pin_args, measure, pin_of
from hs_family.cli import Command

BASELINE_KEYS = ("unclaimed", "unmatched", "contested")


@attrs.define(frozen=True)
class Claim:
    channel: str
    owner: Optional[str]
    kind: str
    source: str

    @property
    def home(self) -> str:
        return "%s:%s" % (self.channel, self.owner) if self.owner else self.channel


@attrs.define(frozen=True, kw_only=True)
class Assignment:
    module: str
    home: Optional[str]
    kind: str          # invoked | cited | shared | contested | unclaimed | stay
    candidates: Tuple[str, ...] = ()
    sources: Tuple[str, ...] = ()
    path: Optional[str] = None
    stay_reason: Optional[str] = None
    depended_on: Tuple[str, ...] = ()


@attrs.define(frozen=True)
class OwnerSpec:
    channels: Tuple[Tuple[str, Any], ...]
    ledger: Tuple[Any, ...]
    stay: Tuple[Tuple[str, str], ...] = ()
    shared_at: int = 4
    test_channels: Tuple[str, ...] = ()
    no_home_channels: Tuple[str, ...] = ()

    @classmethod
    def from_doc(cls, doc: Optional[dict]) -> "OwnerSpec":
        doc = doc or {}

        def rx(where, pat):
            try:
                return re.compile(pat)
            except re.error as e:
                raise CheckError("owners.%s: %r does not compile: %s" % (where, pat, e))

        chans = tuple((cid, rx("channels." + cid, p)) for cid, p in (doc.get("channels") or {}).items())
        if not chans:
            raise CheckError("codemap.yaml declares no `owners.channels`: nothing can own a module")
        led = tuple(rx("ledger", p) for p in doc.get("ledger") or ())
        stay = []
        for i, s in enumerate(doc.get("stay") or ()):
            if not isinstance(s, dict) or not s.get("match") or not s.get("why"):
                raise CheckError("owners.stay[%d]: needs non-empty `match` and `why`" % (i + 1))
            stay.append((str(s["match"]), str(s["why"])))
        at = doc.get("shared_at", 4)
        if not isinstance(at, int) or isinstance(at, bool) or at < 2:
            raise CheckError("owners.shared_at must be an int >= 2, got %r: at 1 every assigned "
                             "module would read as shared" % (at,))
        tests = tuple(doc.get("test_channels") or ())
        unknown = [c for c in tests if c not in dict(chans)]
        if unknown:
            raise CheckError("owners.test_channels names %s, not declared in owners.channels" % unknown)
        no_home = tuple(sorted(c for c, d in (doc.get("layout") or {}).items() if d == "stay"))
        return cls(chans, led, tuple(stay), at, tests, no_home)

    def ledgered(self, source: str) -> bool:
        return any(r.search(source) for r in self.ledger)

    def channel_of(self, source: str) -> Optional[str]:
        return next((cid for cid, r in self.channels if r.search(source)), None)

    def claim(self, source: str, kind: str) -> Optional[Claim]:
        if self.ledgered(source):
            return None
        for cid, r in self.channels:
            m = r.search(source)
            if m:
                return Claim(cid, m.groupdict().get("owner"), kind, source)
        return None


@attrs.define(kw_only=True)
class OwnerReport:
    homes: Dict[str, Assignment] = attrs.Factory(dict)
    ledger_dropped_by_file: Dict[str, int] = attrs.Factory(dict)
    unmatched: List[dict] = attrs.Factory(list)
    unused_channels: Tuple[str, ...] = ()
    unused_stay: Tuple[str, ...] = ()
    shared_at: int = 4

    def of(self, kind: str) -> List[str]:
        return sorted(m for m, a in self.homes.items() if a.kind == kind)

    @property
    def assigned(self) -> List[str]:
        return sorted(m for m, a in self.homes.items() if a.home and a.kind != "stay")

    @property
    def ledger_dropped(self) -> int:
        return sum(self.ledger_dropped_by_file.values())

    def counts(self) -> Dict[str, int]:
        return {"unclaimed": len(self.of("unclaimed")), "unmatched": len(self.unmatched),
                "contested": len(self.of("contested"))}


def _importers(idx: dict, layout: Any) -> Dict[str, Set[str]]:
    """module -> shipped modules importing it or a dotted child (tests import everything)."""
    out: Dict[str, Set[str]] = {}
    for ms in idx.get("units", {}).values():
        for m in ms:
            if m.get("path") and layout.in_test_tree(m["path"]):
                continue
            for key in ("imports_module_level", "imports_function_local"):
                for tgt in m.get(key) or ():
                    parts = tgt.split("::", 1)[0].split(".")
                    for i in range(len(parts), 0, -1):
                        out.setdefault(".".join(parts[:i]), set()).add(m["module"])
    return out


_CFG_BARE_RE = re.compile(r"""^\s*(?:-\s*|[\w.\-]+\s*:\s*)["']?([A-Za-z_]\w*)["']?\s*,?\s*$""")
_CFG_EXT = (".yaml", ".yml", ".json", ".toml", ".cfg", ".ini")


def _ref_lines(repo: Path, rel: str, mod_path: str, dotted: str, cache: dict) -> List[int]:
    """Lines of `rel` carrying a LIVE reference to the module: a >=2-segment path suffix,
    `-m dotted`, or (config only) the bare stem as a value — the spellings the driver
    stage resolves, read through its own prose/comment classifiers."""
    from codegraph._core.snapshot.path_drivers import (HASH_COMMENT_EXT, py_prose_lines,
                                                         strip_hash_comment, strip_var_prefix,
                                                         yaml_value_lines)
    if rel not in cache:
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            cache[rel] = None
            return []
        ext = Path(rel).suffix
        ylines = None
        if ext in (".yaml", ".yml"):
            lmap, _run, why = yaml_value_lines(text)
            ylines = lmap if why is None else None
        cache[rel] = (text, ext, py_prose_lines(text) if ext == ".py" else None, ylines)
    hit = cache[rel]
    if hit is None:
        return []
    text, ext, prose, ylines = hit
    stem = mod_path.rsplit("/", 1)[-1][:-3]
    parts = mod_path.split("/")
    suffixes = ["/".join(parts[i:]) for i in range(len(parts) - 2, -1, -1)]
    if ylines is not None:
        return sorted({n for name in (stem, dotted, *suffixes) for n in ylines.get(name, ())})
    rxs = [re.compile(r"(?<![\w./-])" + re.escape(s) + r"(?![\w])") for s in suffixes]
    drx = re.compile(r"(?<![\w.-])" + re.escape(dotted) + r"(?![\w])")
    out: List[int] = []
    for n, line in enumerate(text.splitlines(), 1):
        line = strip_var_prefix(line)
        if ext == ".py":
            if prose is not None:
                if n in prose:
                    continue
            else:
                line = strip_hash_comment(line)
        elif ext in HASH_COMMENT_EXT:
            line = strip_hash_comment(line)
        elif ext != ".json":
            return []
        m = _CFG_BARE_RE.match(line) if ext in _CFG_EXT else None
        if any(r.search(line) for r in rxs) or drx.search(line) or (m and m.group(1) == stem):
            out.append(n)
    return out


def _depended_on(idx: dict, spec: OwnerSpec, homes: Dict[str, Assignment], repo: Path) -> Dict[str, Tuple[str, ...]]:
    layout = build_test_layout(repo)
    recs = {m["module"]: m for ms in idx.get("units", {}).values() for m in ms}
    importers = _importers(idx, layout)

    def dropped(rel, owner):
        return layout.in_test_tree(rel) or spec.ledgered(rel) or spec.channel_of(rel) == owner

    cache: dict = {}
    out: Dict[str, Tuple[str, ...]] = {}
    for mod, a in homes.items():
        if not a.home:
            continue
        owner = a.home.partition(":")[0]
        refs = ["%s (import)" % recs[i]["path"] for i in sorted(importers.get(mod, set()) - {mod})
                if recs.get(i, {}).get("path") and not dropped(recs[i]["path"], owner)]
        if a.path:
            rec = recs.get(mod, {})
            runs, named = set(rec.get("invoked_by") or ()), set(rec.get("path_drivers") or ())
            for rel in sorted(runs | named):
                if not dropped(rel, owner):
                    refs += ["%s:%d (%s)" % (rel, ln, "run" if rel in runs else "named")
                             for ln in _ref_lines(repo, rel, a.path, mod, cache)]
        if refs:
            out[mod] = tuple(refs)
    return out


def assign(idx: dict, spec: OwnerSpec, repo: Path, entrypoints: Any = None,
           prefix: Optional[str] = None, cli_only: bool = True) -> OwnerReport:
    from codegraph._core.entrypoints import check_entrypoints_declared, is_cli
    rep = OwnerReport(shared_at=spec.shared_at)
    seen: Set[str] = set()
    importers = _importers(idx, build_test_layout(repo))
    records = [m for ms in idx.get("units", {}).values() for m in ms]
    path_of = {m["module"]: m.get("path") for m in records}
    if entrypoints is not None:
        check_entrypoints_declared(entrypoints, set(path_of))
    for rec in records:
        mod = rec["module"]
        if (prefix and not mod.startswith(prefix)) or (cli_only and not is_cli(rec, entrypoints)):
            continue
        claims: List[Claim] = []
        # `invoked_by` is a subset of `path_drivers`: a running line read twice would
        # count twice in `unmatched`/`ledger_dropped` and move the ratchet.
        runs = list(rec.get("invoked_by") or ())
        named = [s for s in rec.get("path_drivers") or () if s not in set(runs)]
        for sources, kind in ((runs, "invoked"), (named, "cited")):
            for src in sources:
                c = spec.claim(src, kind)
                if c is not None:
                    claims.append(c)
                    seen.add(c.channel)
                elif spec.ledgered(src):
                    rep.ledger_dropped_by_file[src] = rep.ledger_dropped_by_file.get(src, 0) + 1
                else:
                    rep.unmatched.append({"source": src, "module": mod, "kind": kind})
        if not claims:
            for imp in sorted(importers.get(mod, set()) - {mod}):
                c = spec.claim(path_of[imp], "cited") if path_of.get(imp) else None
                if c is not None:
                    claims.append(c)
                    seen.add(c.channel)
        if spec.test_channels and any(c.channel not in spec.test_channels for c in claims):
            claims = [c for c in claims if c.channel not in spec.test_channels]
        ranked = [c for c in claims if c.kind == "invoked"] or claims
        homes = sorted({c.home for c in ranked})
        srcs = tuple(sorted({c.source for c in ranked}))
        path = rec.get("path")
        if not homes:
            rep.homes[mod] = Assignment(module=mod, home=None, kind="unclaimed", path=path)
        elif len(homes) == 1:
            rep.homes[mod] = Assignment(module=mod, home=homes[0], kind=ranked[0].kind,
                                        candidates=tuple(homes), sources=srcs, path=path)
        else:
            rep.homes[mod] = Assignment(module=mod, home=None, candidates=tuple(homes), sources=srcs,
                                        path=path, kind="shared" if len(homes) >= spec.shared_at else "contested")
    deps = _depended_on(idx, spec, rep.homes, repo)
    rx = dict(spec.channels)
    matched: Set[int] = set()
    for mod, a in list(rep.homes.items()):
        hit = next((i for i, (m, _w) in enumerate(spec.stay) if a.path and fnmatch.fnmatch(a.path, m)), None)
        dep = deps.get(mod, ())
        chan = a.home.partition(":")[0] if a.home else None
        home_now = bool(a.path and chan and rx[chan].search(a.path))
        test_home = next((tc for tc in spec.test_channels if a.path and rx[tc].search(a.path)), None)
        why = None
        if hit is not None:
            matched.add(hit)
            why = spec.stay[hit][1]
        elif a.home and home_now:
            why = "already home"
        elif a.home and dep:
            why = "depended on by " + ", ".join(dep)
        elif a.kind == "contested":
            why = "contested by " + ", ".join(a.candidates)
        elif a.home and test_home:
            why = "already home in test channel %s" % test_home
        elif a.home and chan in spec.no_home_channels:
            why = "channel %s has no code home of its own" % chan
        if why is not None:
            rep.homes[mod] = attrs.evolve(a, kind="stay", stay_reason=why, depended_on=dep)
    rep.unused_stay = tuple(spec.stay[i][0] for i in range(len(spec.stay)) if i not in matched)
    rep.unused_channels = tuple(sorted({c for c, _ in spec.channels} - seen))
    return rep


def payload(rep: OwnerReport, snapshot: str) -> Dict[str, Any]:
    return {"schema": "hs-code.check.owners/1", "snapshot": snapshot, "modules": len(rep.homes),
            "by_home": dict(Counter(a.home for a in rep.homes.values() if a.home and a.kind != "stay")),
            "assigned": rep.assigned, "shared": rep.of("shared"), "contested": rep.of("contested"),
            "shared_at": rep.shared_at, "unclaimed": rep.of("unclaimed"), "stayed": rep.of("stay"),
            "unused_channels": list(rep.unused_channels), "unused_stay": list(rep.unused_stay),
            "ledger_dropped": rep.ledger_dropped,
            "ledger_dropped_by_file": dict(sorted(rep.ledger_dropped_by_file.items())),
            "unmatched_count": len(rep.unmatched), "unmatched": rep.unmatched,
            "homes": {m: {"home": a.home, "kind": a.kind, "candidates": list(a.candidates), "path": a.path,
                          "stay_reason": a.stay_reason, "depended_on": list(a.depended_on)}
                      for m, a in sorted(rep.homes.items())}}


def render(rep: OwnerReport, snapshot: str) -> None:
    print("owners  %s  ·  %d module(s)" % (snapshot, len(rep.homes)))
    for home, n in Counter(a.home for a in rep.homes.values() if a.home and a.kind != "stay").most_common():
        print("  %-28s %3d" % (home, n))
    for label, kind, gloss in (("SHARED", "shared", "claimed by %d+ channels — shared infrastructure" % rep.shared_at),
                               ("CONTESTED", "contested", "claimed at the same rank by a few — a tie")):
        if rep.of(kind):
            print("%s — %s (%d):" % (label, gloss, len(rep.of(kind))))
            for m in rep.of(kind):
                print("  %-44s %s" % (m, ", ".join(rep.homes[m].candidates)))
    if rep.of("unclaimed"):
        print("UNCLAIMED — no declared channel names them (%d):" % len(rep.of("unclaimed")))
        for m in rep.of("unclaimed"):
            print("  %s" % m)
    if rep.of("stay"):
        print("STAY — measured but kept in place (%d):" % len(rep.of("stay")))
        for m in rep.of("stay"):
            print("  %-44s %s" % (m, rep.homes[m].stay_reason))
    if rep.unused_stay:
        print("STAY DECLARED BUT MATCHING NOTHING: %s" % ", ".join(rep.unused_stay))
    if rep.unused_channels:
        print("DECLARED BUT UNUSED: %s" % ", ".join(rep.unused_channels))
    if rep.unmatched:
        by_src: Dict[str, List[str]] = {}
        for u in rep.unmatched:
            by_src.setdefault(u["source"], []).append("%s (%s)" % (u["module"], u["kind"]))
        print("UNMATCHED — no declared channel names these lines (%d across %d file(s)):"
              % (len(rep.unmatched), len(by_src)))
        for src in sorted(by_src):
            print("  %s: %s" % (src, ", ".join(sorted(by_src[src]))))
    if rep.ledger_dropped_by_file:
        print("LEDGER — lines a declared ledger drops, by file (%d across %d file(s)):"
              % (rep.ledger_dropped, len(rep.ledger_dropped_by_file)))
        for src in sorted(rep.ledger_dropped_by_file):
            print("  %s: %d" % (src, rep.ledger_dropped_by_file[src]))


def read_baseline(path: Path) -> Dict[str, int]:
    import yaml_io
    doc = yaml_io.yaml_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return {k: int((doc or {}).get(k, 0)) for k in BASELINE_KEYS}


def write_baseline(counts: Dict[str, int], path: Path) -> None:
    head = ("# hs-code check owners baseline — a two-way ratchet: --strict reds on a count that\n"
            "# GREW past this file or one that SHRANK without --write-baseline catching up.\n"
            "# --write-baseline refuses when any count grew.\n")
    path.write_text(head + "".join("%s: %d\n" % (k, counts[k]) for k in BASELINE_KEYS), encoding="utf-8")


def _owners_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--prefix", metavar="DOTTED", help="only modules under this dotted prefix")
    ap.add_argument("--all", action="store_true", help="every module, not only command-line ones")
    ap.add_argument("--fail-on-unclaimed", action="store_true", help="an unclaimed module is red")
    ap.add_argument("--baseline", metavar="FILE", help="the ratchet file for unclaimed/unmatched/contested")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--strict", action="store_true", help="red when a count grew or shrank past --baseline")
    mode.add_argument("--write-baseline", action="store_true", help="shrink --baseline to today's counts")
    mode.add_argument("--freeze", action="store_true", help="write the FIRST --baseline")
    add_pin_args(ap)


def _owners(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    try:
        if (args.strict or args.write_baseline or args.freeze) and not args.baseline:
            raise CheckError("--strict/--write-baseline/--freeze need --baseline")
        tree = measure(Path(root), pin_of(args, opts))
        spec = OwnerSpec.from_doc(tree.descriptor.owners)
        try:
            rep = assign(tree.idx, spec, tree.repo, tree.descriptor.entrypoints, args.prefix, not args.all)
        except Exception as e:  # DescriptorError from the entrypoints check
            if type(e).__name__ != "DescriptorError":
                raise
            raise CheckError(str(e))
        counts = rep.counts()
        bl = Path(args.baseline or "")
        check = None
        if args.freeze:
            if bl.exists():
                raise CheckError("%s exists — --freeze only writes the FIRST baseline" % bl)
            write_baseline(counts, bl)
        elif args.write_baseline:
            old = read_baseline(bl)
            grew = {k: [old[k], counts[k]] for k in BASELINE_KEYS if counts[k] > old[k]}
            if grew:
                raise CheckError("--write-baseline refused, count(s) grew (was, now): %s" % grew)
            write_baseline(counts, bl)
        elif args.strict:
            old = read_baseline(bl)
            check = {"grew": {k: [old[k], counts[k]] for k in BASELINE_KEYS if counts[k] > old[k]},
                     "shrank": {k: [old[k], counts[k]] for k in BASELINE_KEYS if counts[k] < old[k]}}
    except CheckError as e:
        print("hs-code check owners: %s" % e)
        return 2
    data = payload(rep, str(tree.idx.get("snapshot")))
    data["counts"], data["baseline_check"] = counts, check
    if getattr(opts, "json", False):
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        render(rep, data["snapshot"])
        if check and (check["grew"] or check["shrank"]):
            print("owners: GREW %s · SHRANK %s (was, now)" % (check["grew"], check["shrank"]))
    red = bool(check and (check["grew"] or check["shrank"])) or bool(args.fail_on_unclaimed and rep.of("unclaimed"))
    return 1 if red else 0


OWNERS = Command(add_args=_owners_args, run=_owners)
