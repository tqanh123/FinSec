"""The policy a gated run holds a static check to: the stricter of the base's and HEAD's.

    mode     all > new > advisory; a check the base declares and HEAD dropped is still
             owed, under the base's row
    grace    HEAD may be laxer only through `grace: {reason, expires, dec}` with
             `expires` not yet past and `dec` in HEAD's decision register; a grace whose
             DEC the register lacks is refused; one past its date is ignored

The base (`resolve_base`):
    in a plan    a `base_repin` line of the plan's check ledgers (newest), else every
                 phase's open mark -- the mark itself when HEAD descends from it, else
                 its fork point with HEAD (a rebase leaves the old mark off HEAD's line)
                 -- and the first parent of each commit a verify line names; the base is
                 the octopus merge-base of those points. None: BLOCKED, naming
                 `--repin-base`. The policy is read at two points -- that base and the
                 marks' own -- the strictest per check (the earlier on a tie): a point
                 only adds to what is owed, so naming an old commit moves the changed set
                 back, never the floor down; a state inside the phase is never a point
    outside one  the fork point of `@{upstream}` and HEAD; no upstream: HEAD's policy,
                 said so in `note`

A file at the base is read as `<sha>:./<path>` from the repo root (the root may sit under
a larger checkout); a path the base lacks reads as None, any other git failure is BLOCKED
-- never "HEAD's policy instead".
"""
from __future__ import annotations

import datetime
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import static_checks

Key = Tuple[str, str]


class PolicyBlocked(Exception):
    """The base, or the policy read at it, cannot be established."""


@dataclass(frozen=True)
class Base:
    sha: Optional[str]
    source: str  # repin | plan | upstream | none
    note: str = ""
    points: Tuple[str, ...] = ()  # where the policy is read, earliest first; () = at `sha`

    def policy_shas(self) -> Tuple[str, ...]:
        return self.points or ((self.sha,) if self.sha else ())


@dataclass(frozen=True)
class Effective:
    spec: static_checks.CheckSpec
    mode: str
    why: str


@dataclass
class Policy:
    base: Base
    checks: Dict[Key, Effective] = field(default_factory=dict)


def _git(root: Path, *argv: str, input_text: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
    import secret_scan_before_ship as secret_scan
    return subprocess.run(["git", *secret_scan.GIT_SAFE_CONFIG, "-C", str(root), *argv], input=input_text,
                          capture_output=True, text=True, env=secret_scan.hardened_git_env())


def _ok(root: Path, *argv: str) -> Optional[str]:
    r = _git(root, *argv)
    return r.stdout.strip() if r.returncode == 0 else None


def _is_commit(root: Path, sha: str) -> bool:
    return _ok(root, "rev-parse", "--verify", "--quiet", "%s^{commit}" % sha) is not None


def read_at(root: Path, sha: str, rel: str) -> Optional[str]:
    """`rel`'s text at commit `sha`; None when the commit lacks the path."""
    root = Path(root)
    if not _is_commit(root, sha):
        raise PolicyBlocked("base %s is not a commit this repo holds" % sha)
    spec = "%s:./%s" % (sha, rel)
    if _git(root, "cat-file", "-e", spec).returncode != 0:
        return None
    r = _git(root, "show", spec)
    if r.returncode != 0:
        raise PolicyBlocked("git show %s failed: %s" % (spec, (r.stderr or "").strip()[:200]))
    return r.stdout


# ------------------------------------------------------------------ the base

def _ledger_repin(plan: Path) -> Optional[str]:
    import harness_checks
    best: "tuple[str, str] | None" = None
    for f in sorted((plan / "artifacts").glob("harness-checks-*.jsonl")):
        phase = f.name[len("harness-checks-"):-len(".jsonl")]
        for row in harness_checks.rows(plan, phase, "base_repin"):
            if best is None or str(row.get("ts", "")) >= best[0]:
                best = (str(row.get("ts", "")), str(row["sha"]))
    return best[1] if best else None


def _octopus(root: Path, points: "list[str]") -> str:
    if len(set(points)) == 1:
        return points[0]
    out = _ok(root, "merge-base", "--octopus", *sorted(set(points)))
    if not out:
        raise PolicyBlocked("the phases' base points share no history: %s" % ", ".join(sorted(set(points))))
    return out


def _plan_base(root: Path, plan: Path, phase: str) -> Base:
    import phase_commits_log
    hint = ("re-pin it: hs-code check run <id> --repin-base <sha> --dec <DEC-id> --plan %s --phase %s"
            % (plan, phase))
    pinned = _ledger_repin(plan)
    if pinned:
        if not _is_commit(root, pinned):
            raise PolicyBlocked("the re-pinned base %s is not a commit this repo holds; %s" % (pinned, hint))
        return Base(pinned, "repin")
    records = phase_commits_log.read_records(plan)
    marks = [str(r["mark"]) for r in records if r.get("kind") == "open" and r.get("mark")]
    import harness_checks
    gone = [] if marks else harness_checks.marks_removed(plan)
    if gone:
        raise PolicyBlocked(harness_checks.marks_removed_reason(plan, gone, phase or "<phase>"))
    if not marks:
        raise PolicyBlocked("no phase of %s was opened with a mark (`hs-run cook next` opens one): "
                            "there is no base to hold the policy to" % plan.name)
    head = _ok(root, "rev-parse", "HEAD")
    points: List[str] = []
    verified: List[str] = []
    for m in marks:
        if not _is_commit(root, m):
            continue
        anc = _git(root, "merge-base", "--is-ancestor", m, "HEAD").returncode == 0
        point = m if anc else _ok(root, "merge-base", m, "HEAD")
        if point is None:
            raise PolicyBlocked("open mark %s shares no history with HEAD %s; %s" % (m, head, hint))
        points.append(point)
    # what the plan's own verifies name is its work, so the base sits before it: a phase
    # opened late (its work committed before the mark) names that work with --sha
    for r in records:
        if r.get("kind") != "verify":
            continue
        for sha in r.get("sha") or ():
            parent = _ok(root, "rev-parse", "--verify", "--quiet", "%s^" % sha)
            if parent:
                verified.append(parent)
    if not points and not verified:
        raise PolicyBlocked("every open mark of %s (%s) is gone from this repo (a rebase + gc, a fresh "
                            "clone) and no verify names a commit it still holds; %s"
                            % (plan.name, ", ".join(m[:12] for m in marks), hint))
    base = _octopus(root, points + verified)
    _held_by_evidence(root, plan, base, hint)
    # the policy is read at the base and at the marks' point -- never at a state between
    # them or past the mark, which is the plan's own work in progress -- earliest first
    held = [base] + ([_octopus(root, points)] if points else [])
    return Base(base, "plan", points=tuple(dict.fromkeys(held)))


def _held_by_evidence(root: Path, plan: Path, base: str, hint: str) -> None:
    """A base only moves back as phases open; one past what the plan's own evidence
    recorded -- the base a check run was held to, a commit a verification names -- is
    marks rewritten or removed, and a plan re-anchored at HEAD owes nothing it changed."""
    import harness_checks
    for f in sorted((plan / "artifacts").glob("harness-checks-*.jsonl")):
        phase = f.name[len("harness-checks-"):-len(".jsonl")]
        for row in harness_checks.rows(plan, phase, "run"):
            held = str(row.get("base") or "")
            if held and held != base and _is_commit(root, held) and not _ancestor(root, base, held):
                raise PolicyBlocked("the base %s's open marks give now, %s, is past %s, the base its own "
                                    "check run was held to: its marks were rewritten or removed; %s"
                                    % (plan.name, base[:12], held[:12], hint))
    import harness_checks
    verified = harness_checks.verified_commits(plan)
    # one walk of the base's history answers every full sha a verification names
    behind = set((_ok(root, "rev-list", base) or "").split()) if verified else set()
    for c in sorted(verified):
        if c in behind if len(c) == 40 else _ancestor(root, c, base):
            raise PolicyBlocked("the base %s's open marks give now, %s, is at or past %s, a commit its own "
                                "verification names: its marks were rewritten or removed; %s"
                                % (plan.name, base[:12], c[:12], hint))


def _ancestor(root: Path, a: str, b: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", a, b).returncode == 0


def resolve_base(root: Path, plan: Optional[Path] = None, phase: Optional[str] = None) -> Base:
    root = Path(root)
    if plan is not None:
        return _plan_base(root, Path(plan), str(phase or ""))
    up = _ok(root, "rev-parse", "--verify", "--quiet", "@{upstream}")
    if not up:
        return Base(None, "none", "no upstream branch: HEAD's own policy, with nothing to hold it to")
    point = _ok(root, "merge-base", up, "HEAD")
    if not point:
        raise PolicyBlocked("@{upstream} %s shares no history with HEAD" % up)
    return Base(point, "upstream")


# ------------------------------------------------------------------ the policy

def _checks_in(text: Optional[str], root: Path) -> "dict[Key, static_checks.CheckSpec]":
    if text is None:
        return {}
    import yaml_io
    try:
        langs = (yaml_io.safe_load(text) or {}).get("languages") or {}
        return {(str(lang), s.id): s for lang, v in langs.items() if isinstance(v, dict)
                for s in static_checks.merge(str(lang), v.get("checks"), None)}
    except (static_checks.CheckSpecError, AttributeError, ValueError) as e:
        raise PolicyBlocked("codemap.yaml at the base is unreadable: %s" % e) from None


def checks_at(root: Path, base: Base) -> "dict[Key, tuple[static_checks.CheckSpec, str]]":
    """Each check the base declares, as (spec, the point it was read at): over the
    base's policy points, the strictest mode (the first point on a tie)."""
    out: "dict[Key, tuple[static_checks.CheckSpec, str]]" = {}
    for sha in base.policy_shas():
        for key, spec in _checks_in(read_at(root, sha, "codemap.yaml"), root).items():
            if key not in out or _stricter(spec.mode, out[key][0].mode) != out[key][0].mode:
                out[key] = (spec, sha)
    return out


def codemaps_at(root: Path, base: Base) -> "list[dict]":
    """The code map at each of the base's policy points ({} where it has none)."""
    return [_codemap_doc(read_at(root, sha, "codemap.yaml"), "at the base") for sha in base.policy_shas()]


def dec_ids(root: Path, sha: Optional[str] = None) -> "set[str]":
    """The DEC ids `root`'s register holds at `sha` (None: the working tree)."""
    import decision_register as dr
    out: "set[str]" = set()
    for rel, yaml_ssot in (("docs/decisions.yaml", True), ("docs/decisions.md", False)):
        text = read_at(root, sha, rel) if sha else (
            (Path(root) / rel).read_text(encoding="utf-8") if (Path(root) / rel).is_file() else None)
        if text is not None:
            out |= set(dr.ids_in_text(text, yaml_ssot=yaml_ssot))
    return out


def _stricter(a: str, b: str) -> str:
    return a if static_checks.MODES.index(a) <= static_checks.MODES.index(b) else b


def effective(root: Path, plan: Optional[Path] = None, *, phase: Optional[str] = None,
              today: Optional[datetime.date] = None) -> Policy:
    root = Path(root)
    base = resolve_base(root, plan, phase)
    head = {(lang, s.id): s for lang, s in static_checks.declared(root)}
    at_base = {k: spec for k, (spec, _) in checks_at(root, base).items()}
    today = today or datetime.date.today()
    known: "Optional[set[str]]" = None
    pol = Policy(base)
    for key in sorted(set(head) | set(at_base)):
        h, b = head.get(key), at_base.get(key)
        if h is None:
            assert b is not None  # the key came from one of the two
            pol.checks[key] = Effective(b, b.mode, "declared at the base, dropped at HEAD: still owed")
            continue
        if b is None or b.mode == h.mode:
            pol.checks[key] = Effective(h, h.mode, "HEAD" if b is None else "base = HEAD")
            continue
        strict = _stricter(b.mode, h.mode)
        if strict == h.mode:
            pol.checks[key] = Effective(h, h.mode, "HEAD is stricter than the base")
            continue
        g = h.grace
        if g:
            known = dec_ids(root) if known is None else known
            if g["dec"] not in known:
                raise PolicyBlocked("languages.%s.checks.%s grace names %s, which docs/decisions.yaml "
                                    "does not hold" % (key[0], key[1], g["dec"]))
            if datetime.date.fromisoformat(g["expires"]) >= today:
                pol.checks[key] = Effective(h, h.mode, "grace until %s (%s)" % (g["expires"], g["dec"]))
                continue
        pol.checks[key] = Effective(h, strict, "the base's %s holds (HEAD says %s%s)"
                                    % (b.mode, h.mode, ", its grace expired" if g else ""))
    return pol


@dataclass(frozen=True)
class Floor:
    line: float
    why: str


def _codemap_doc(text: Optional[str], where: str) -> dict:
    if text is None:
        return {}
    import yaml_io
    try:
        doc = yaml_io.safe_load(text) or {}
    except ValueError as e:
        raise PolicyBlocked("codemap.yaml %s is unreadable: %s" % (where, e)) from None
    return doc if isinstance(doc, dict) else {}


def coverage(root: Path, plan: Optional[Path] = None, *, phase: Optional[str] = None,
             base: Optional[Base] = None) -> "tuple[Dict[str, Floor], List[dict]]":
    """Each group's line floor -- the higher of the base's and HEAD's; a floor the base
    declares and HEAD dropped is still owed -- and the groups to judge them on: HEAD's,
    plus the base's definition of a group HEAD no longer declares."""
    import coverage_floors
    root = Path(root)
    cm = root / "codemap.yaml"
    head_doc = _codemap_doc(cm.read_text(encoding="utf-8") if cm.is_file() else None, "at HEAD")
    head_groups = [g for g in head_doc.get("groups") or [] if isinstance(g, dict)]
    try:
        head = coverage_floors.declared_floors(head_doc.get("coverage"), head_groups)
    except coverage_floors.CoverageSpecError as e:
        raise PolicyBlocked("codemap.yaml: %s" % e) from None
    base = base or resolve_base(root, plan, phase)
    # over the base's policy points the highest floor, and the group definition beside it
    at_base: "Dict[str, float]" = {}
    defined: "Dict[str, dict]" = {}
    for doc in codemaps_at(root, base):
        groups_here = [g for g in doc.get("groups") or [] if isinstance(g, dict)]
        try:
            here = coverage_floors.declared_floors(doc.get("coverage"), groups_here)
        except coverage_floors.CoverageSpecError:
            continue  # a block that never read held no one to anything
        for name, line in here.items():
            if name not in at_base or line > at_base[name]:
                at_base[name] = line
                defined.update((str(g.get("name")), g) for g in groups_here if str(g.get("name")) == name)
    floors: "Dict[str, Floor]" = {}
    for name in sorted(set(head) | set(at_base)):
        h, b = head.get(name), at_base.get(name)
        if b is None:
            assert h is not None
            floors[name] = Floor(h, "HEAD")
        elif h is None:
            floors[name] = Floor(b, "declared at the base, dropped at HEAD: still owed")
        elif h >= b:
            floors[name] = Floor(h, "base = HEAD" if h == b else "HEAD is stricter than the base")
        else:
            floors[name] = Floor(b, "the base's %s holds (HEAD says %s)" % (b, h))
    named = {str(g.get("name")) for g in head_groups}
    groups = head_groups + [g for n, g in sorted(defined.items()) if n in floors and n not in named]
    return floors, groups
