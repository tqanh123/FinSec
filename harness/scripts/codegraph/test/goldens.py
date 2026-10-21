"""hs-code test goldens -- golden drift: a source changed but its golden was
not regenerated, so the test still compares against the OLD answer.

Port of ob-tools' `testkit.analysis.goldens` (pinned commit `60075cb`).

WHY THE PAIR MUST BE DECLARED OR EARNED
    A golden file is just a file: nothing in its bytes says what produced it.
    A pair is accepted from exactly two sources: (1) a declaration file
    (`--pairs`, JSON) naming golden -> sources, or (2) a convention that is
    checkable: `X.golden` sitting next to `X.py`. Anything else is BLIND —
    named, counted, never assumed clean.

WHY HASHES, NOT MTIME
    mtime is a function of checkout order, not of editing. The manifest
    records the SOURCE hash each golden was generated from, so the verdict
    survives a clone, a rebase, and a restore from backup. With no manifest
    the check degrades to mtime and SAYS SO.

    hs-code test goldens tests --pairs pairs.json --check
    hs-code test goldens tests --write-manifest .golden-manifest.json --apply

`--write-manifest` writes only under `--apply` (a dry run reports how many
pairs it would record).

Exit: 0 clean, or `--write-manifest` computed · 1 `--check` and something
drifted/vanished/BLIND · 2 the tree given is not a directory, or `--pairs`/
`--manifest` could not be read.

WHAT IT CANNOT SEE
    * A golden whose source changed and whose manifest entry was regenerated
      without re-running the generator reads clean — the manifest records
      what a human told it, not what the generator did.
    * Sources that are not files are invisible.
    * Generated files written outside the tree, and goldens consumed by a
      test that never names them.
    * Only the declared edges are followed — one hop, not a transitive closure.
    * Binary goldens are hashed, not inspected: drift is detected, not shown.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import hs_family

from codegraph._core import walk
from codegraph.test.inputs import load_json, mapping
from hs_family.cli import Command

#: Suffixes that mark a file as "an expected answer" by convention.
GOLDEN_SUFFIXES = (".golden", ".snap", ".expected", ".approved")
#: Where a convention-discovered golden's generator is looked for, in order.
GENERATOR_SUFFIXES = (".py",)


@dataclass(frozen=True)
class Pair:
    """One golden and the files whose change should have regenerated it."""

    golden: str                       # path relative to root
    sources: "tuple[str, ...]"        # paths relative to root
    origin: str                       # "declared" | "convention"

    @property
    def key(self) -> str:
        return self.golden


@dataclass
class Drift:
    """One pair's verdict."""

    pair: Pair
    verdict: str                      # ok | drifted | missing | unknown
    detail: str = ""

    @property
    def red(self) -> bool:
        return self.verdict in ("drifted", "missing")


@dataclass
class Report:
    pairs: "list[Pair]" = field(default_factory=list)
    drifts: "list[Drift]" = field(default_factory=list)
    blind: "list[str]" = field(default_factory=list)
    basis: str = "hash"               # "hash" | "mtime" — printed, never hidden

    def red(self) -> "list[Drift]":
        return [d for d in self.drifts if d.red]


# ── discovery ────────────────────────────────────────────────────────────────

def load_pairs(path: Path) -> "list[Pair]":
    """Read a declaration file: {"pairs": [{golden, sources}, ...]}. JSON only,
    deliberately: this tool must not depend on a YAML parser to decide
    whether a gate is red."""
    doc = load_json(path)
    out: "list[Pair]" = []
    for i, entry in enumerate(doc.get("pairs") or []):
        entry = mapping(entry, f"{path}: pair #{i}")
        golden = entry.get("golden")
        sources = entry.get("sources") or []
        if not golden or not sources:
            raise ValueError(
                f"{path}: pair #{i} needs both `golden` and a non-empty `sources`")
        out.append(Pair(str(golden), tuple(str(s) for s in sources), "declared"))
    return out


def discover_pairs(root: Path) -> "tuple[list[Pair], list[str]]":
    """Convention pairs, plus the reasons anything else stayed blind."""
    pairs: "list[Pair]" = []
    blind: "list[str]" = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or walk.skipped(path.parts):
            continue
        if not path.name.endswith(GOLDEN_SUFFIXES):
            continue
        rel = path.relative_to(root).as_posix()
        stem = path.name
        for suf in GOLDEN_SUFFIXES:
            if stem.endswith(suf):
                stem = stem[: -len(suf)]
                break
        candidates = [path.parent / f"{stem}{s}" for s in GENERATOR_SUFFIXES]
        found = [c for c in candidates if c.is_file()]
        if not found:
            blind.append(f"{rel}: no sibling generator `{stem}.py` — pair unknown")
            continue
        pairs.append(Pair(rel, tuple(f.relative_to(root).as_posix() for f in found), "convention"))
    return pairs, blind


# ── checking ─────────────────────────────────────────────────────────────────

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: Path) -> "dict[str, dict[str, str]]":
    """{"<golden>": {"<source>": "<sha256>", ...}} — the recorded generation."""
    doc = mapping(load_json(path).get("manifest") or {}, path)
    return {str(k): {str(s): str(v) for s, v in mapping(d or {}, path).items()}
            for k, d in doc.items()}


def check(root: Path, pairs: "list[Pair]",
          manifest: "dict[str, dict[str, str]] | None" = None) -> Report:
    """Verdict per pair. Manifest hashes when available, mtime when not."""
    rep = Report(pairs=list(pairs), basis="hash" if manifest is not None else "mtime")
    for pair in pairs:
        golden = root / pair.golden
        if not golden.is_file():
            rep.drifts.append(Drift(pair, "missing", f"golden not found: {pair.golden}"))
            continue
        recorded = (manifest or {}).get(pair.golden)
        if manifest is not None and recorded is None:
            rep.drifts.append(Drift(
                pair, "unknown", "not in the manifest — its source hash was never recorded"))
            continue
        if manifest is not None:
            known = recorded or {}
            changed = []
            for src in pair.sources:
                want = known.get(src)
                sp = root / src
                if not sp.is_file():
                    changed.append(f"{src}: source file is gone")
                    continue
                got = _sha256(sp)
                if want is None:
                    changed.append(f"{src}: no hash recorded")
                elif got != want:
                    changed.append(f"{src}: hash differs from the manifest")
            rep.drifts.append(Drift(pair, "drifted" if changed else "ok", "; ".join(changed)))
            continue
        # mtime fallback — weaker, and said to be weaker.
        gt = golden.stat().st_mtime
        newer = [s for s in pair.sources
                 if (root / s).is_file() and (root / s).stat().st_mtime > gt]
        rep.drifts.append(Drift(
            pair, "drifted" if newer else "ok",
            ("source newer than golden (mtime — weaker than a manifest): "
             + ", ".join(newer)) if newer else ""))
    return rep


def write_manifest(root: Path, pairs: "list[Pair]", path: Path) -> int:
    """Record the CURRENT source hashes as the generation point."""
    manifest: "dict[str, dict[str, str]]" = {}
    for pair in pairs:
        entry: "dict[str, str]" = {}
        for src in pair.sources:
            sp = root / src
            if sp.is_file():
                entry[src] = _sha256(sp)
        manifest[pair.golden] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"manifest": manifest}, ensure_ascii=False,
                               indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return len(manifest)


def summary(rep: Report) -> dict:
    red = rep.red()
    return {
        "pairs": len(rep.pairs),
        "ok": sum(1 for d in rep.drifts if d.verdict == "ok"),
        "drifted": sum(1 for d in rep.drifts if d.verdict == "drifted"),
        "missing": sum(1 for d in rep.drifts if d.verdict == "missing"),
        "unknown": sum(1 for d in rep.drifts if d.verdict == "unknown"),
        "red": len(red), "blind": len(rep.blind), "basis": rep.basis,
    }


def render(rep: Report, top: int = 20) -> str:
    s = summary(rep)
    lines = [
        f"golden drift: {s['pairs']} pair(s), basis={s['basis']}",
        f"  ok {s['ok']} · drifted {s['drifted']} · missing {s['missing']} "
        f"· unknown {s['unknown']}",
    ]
    if s["basis"] == "mtime":
        lines.append("  NO MANIFEST: verdicts use mtime, which a fresh clone or a "
                     "`touch` defeats.")
        lines.append("  Run --write-manifest --apply once, then commit it, for a real answer.")
    red = rep.red()[:top]
    if red:
        lines.append("")
        lines.append(f"  RED ({len(rep.red())}):")
        for d in red:
            lines.append(f"    {d.verdict:8} {d.pair.golden}")
            if d.detail:
                lines.append(f"             {d.detail}")
    unknown = [d for d in rep.drifts if d.verdict == "unknown"][:top]
    if unknown:
        lines.append("")
        lines.append(f"  UNKNOWN ({s['unknown']}) — not a pass, not a fail:")
        for d in unknown:
            lines.append(f"    {d.pair.golden}")
    if rep.blind:
        lines.append("")
        lines.append(f"  BLIND ({len(rep.blind)}) — no pair could be established:")
        for line in rep.blind[:top]:
            lines.append(f"    {line}")
    if not rep.pairs:
        lines.append("")
        lines.append("  no pair found. That is NOT a clean bill: every golden whose")
        lines.append("  generator cannot be established is invisible to this check.")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def add_arguments(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("path", help="tree to check, repo-relative")
    sp.add_argument("--pairs", type=Path, default=None,
                    help="declaration JSON: {pairs: [{golden, sources}]}")
    sp.add_argument("--manifest", type=Path, default=None,
                    help="manifest of source hashes recorded at generation")
    sp.add_argument("--write-manifest", type=Path, default=None, metavar="FILE",
                    help="record current source hashes as the generation point (--apply writes it)")
    sp.add_argument("--check", action="store_true",
                    help="exit 1 when any pair drifted, vanished, or is BLIND")
    sp.add_argument("--top", type=int, default=20)


def _goldens(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    root = Path(root).resolve()
    apply = bool(getattr(opts, "apply", False))
    as_json = bool(getattr(opts, "json", False))
    scan_root = (root / args.path).resolve()
    if not scan_root.is_dir():
        raise hs_family.UsageError(f"goldens: not a directory: {scan_root}")

    try:
        pairs, blind = ((load_pairs(args.pairs), []) if args.pairs
                        else discover_pairs(scan_root))
    except (OSError, ValueError) as e:
        raise hs_family.UsageError(f"goldens: {e}") from e

    if args.write_manifest:
        if apply:
            try:
                n = write_manifest(scan_root, pairs, args.write_manifest)
            except OSError as e:
                raise hs_family.UsageError(f"goldens: {e}") from e
            payload = {"wrote": str(args.write_manifest), "pairs": n, "dry_run": False}
        else:
            payload = {"wrote": None, "pairs": len(pairs), "dry_run": True,
                      "would_write": str(args.write_manifest)}
        if as_json:
            print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        elif payload["dry_run"]:
            print(f"goldens: [dry-run] --apply would write a manifest for "
                  f"{payload['pairs']} pair(s) -> {payload['would_write']}")
        else:
            print(f"goldens: manifest written for {payload['pairs']} pair(s) -> {payload['wrote']}")
            print("  commit it: the manifest is what makes the check survive a clone.")
        return 0

    manifest = None
    if args.manifest:
        if not args.manifest.is_file():
            raise hs_family.UsageError(f"goldens: no manifest at {args.manifest}")
        try:
            manifest = load_manifest(args.manifest)
        except (OSError, ValueError) as e:
            raise hs_family.UsageError(f"goldens: {e}") from e

    rep = check(scan_root, pairs, manifest)
    rep.blind = blind
    if as_json:
        print(json.dumps({
            "summary": summary(rep),
            "drifts": [{"golden": d.pair.golden, "sources": list(d.pair.sources),
                        "verdict": d.verdict, "detail": d.detail} for d in rep.drifts],
            "blind": rep.blind,
        }, ensure_ascii=False, sort_keys=True))
    else:
        print(render(rep, top=args.top))

    if args.check:
        red = rep.red()
        if red or rep.blind:
            return 1
        return 0
    return 0


GOLDENS = Command(add_args=add_arguments, run=_goldens)
