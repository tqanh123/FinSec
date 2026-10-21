#!/usr/bin/env python3
"""decision_register — the Decision Register: the authoritative home for
explicit architecture/process rulings (`DEC-<n>`).

A decision is recorded when a binding call is made that later work must not
re-litigate. The register kills re-litigation: the next time the same tension
surfaces, read the register FIRST and surface the prior ruling ("DEC-n decided
X because … — keep or supersede?") instead of re-debating.

Script-vs-LLM split: this script owns the deterministic structural work —
allocate the next monotonic id, validate the `^DEC-\\d+$` grammar + record
shape, append WITHOUT overwriting prior records, parse + list. The caller owns
the human RATIONALE prose passed in as `--rationale`.

Storage (dual-mode): the SSOT is `docs/decisions.yaml` when present — a YAML
list of records (load → mutate → dump), with `docs/decisions.md` rendered as a
secondary human view. Absent decisions.yaml, the legacy markdown form IS the
store: each ruling is one `---`-fenced YAML mini-frontmatter
(id/status/date/affects/supersedes) + a `## DEC-<n> — <title>` heading +
rationale block. Both modes resolve through fs_guard zone "docs" so a register
write can never escape the docs boundary.

ID grammar: `^DEC-\\d+$`, monotonic max+1 regardless of status — a superseded
DEC still counts toward the max, so ids are never reused. BOTH append paths
(--append with an explicit id, --append-alloc) run inside the register lock:
two concurrent agents on one machine cannot overwrite each other's appends
(see register_store for the degraded-lock posture on platforms without flock).

CLI:
    decision_register.py --root <dir> --alloc-id
    decision_register.py --root <dir> --append --id DEC-unknown-2 --title "..." \\
        --rationale "..." [--affects PRD-X] [--supersedes DEC-unknown-1]
    decision_register.py --root <dir> --append-alloc --title ... --rationale ... \\
        [--meta tree=di --meta tags='[a, b]'] [--meta-json '{"scope": {...}}']
    decision_register.py --root <dir> --list   # active records as JSON
    decision_register.py --root <dir> --render-md   # re-render decisions.md from the YAML SSOT
    decision_register.py --root <dir> --amend DEC-n [--title ...] [--rationale ...]
        # reword a live ruling, locked; the replaced text -> docs/decision-revisions.jsonl

`meta` is the record's own metadata (a mapping; any YAML value): written only when
non-empty, round-tripped through every later write, shown by --list and rendered
as one `meta:` line in decisions.md. `legacy_id` is the id a ruling carried before
the id migration (`--map-legacy-ids <commit>`), written only when set and read so a
refusal can name the ruling an old id became. Any other key found in decisions.yaml is
refused by name before a write -- the dump rebuilds records from a fixed field
list, so it would otherwise vanish.
"""

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml_io

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import fs_guard  # noqa: E402
import process_group  # noqa: E402
import decision_neighbors  # noqa: E402 — blast-radius detector, DRY
import decision_confirm    # noqa: E402 — cross-scope confirm token
from register_store import (  # noqa: E402
    POST_WRITE_HELP, RECORD_RE as _RECORD_RE, UnreadableSSOT, atomic_write,
    escape_injection, load_yaml_list, register_lock, report_post_write,
    run_post_write, sanitize_field, scan_record_ids,
)

_HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.append(str(_HOOKS_DIR))
import hook_runtime  # noqa: E402

# Windows consoles may default to a legacy codepage; UTF-8 JSON output must
# not crash there. reconfigure exists on 3.7+; guard for exotic stdouts.
if hasattr(sys.stdout, "reconfigure"):
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")


# This register's own heading anchor: a rationale line that is a bare `---`
# fence would split decisions.md into a phantom record; a `## DEC-<n>` line
# could smuggle a fake heading. Both are neutralized on write.
_INJ_DEC_HEADING_RE = re.compile(r"(?m)^(##\s+DEC-)")

# ID grammar: DEC-<actor slug>-<n>. The slug is what removes the shared counter.
#
# `DEC-<n>` was one global sequence, and a global sequence is the single thing two
# clones cannot agree on: `register_lock` is a flock, which sees one process tree on
# one filesystem and nothing on another machine or branch. `docs/decisions.yaml` is a
# block list with no merge driver, so two branches that each took `DEC-v-hieubt15-299` merge
# clean — two records, one id, no conflict raised. Per-actor sequences mean an
# allocation never has to ask anyone: the only way to collide is for the SAME actor to
# allocate twice, which is exactly the case the local lock does cover.
DECISION_ID_RE = re.compile(r"^DEC-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)-(?P<n>\d+)$")

# The pre-migration spelling. Kept for READING only — prose outside the ledger
# (CHANGELOG entries, archived plans) still cites it, and a reader that raises on the
# old shape turns a historical mention into a crash.
LEGACY_DECISION_ID_RE = re.compile(r"^DEC-(?P<n>\d+)$")


# A retired id CITED in prose: `DEC-225`, `DEC-56/63`, `DEC-367..370`. Inside the
# ledger it is always wrong — every retired number up to the migration's last one
# has a current id with the same digits and a different ruling.
_BARE_REF_RE = re.compile(r"DEC-\d+(?:(?:/|\.\.)\d+)?")


def bare_refs(text) -> list:
    """Every retired-spelling citation in `text`, in order."""
    return _BARE_REF_RE.findall(str(text or ""))


# A range citation wider than this is not a list of rulings anyone means.
_MAX_CITED_RANGE = 1000


def _cited_ids(ref: str) -> Optional[List[str]]:
    """The ids one retired-spelling citation names: `DEC-56/63` -> two,
    `DEC-367..370` -> four. None for a range too wide or reversed to enumerate."""
    nums = re.findall(r"\d+", ref)
    if ".." in ref:
        lo, hi = int(nums[0]), int(nums[1])
        if hi < lo or hi - lo > _MAX_CITED_RANGE:
            return None
        return ["DEC-%d" % n for n in range(lo, hi + 1)]
    return ["DEC-%s" % n for n in nums]


def unresolved_bare_refs(text, stored) -> list:
    """The retired-spelling citations in `text` that do NOT name a record stored
    under that same spelling in `stored` (a set of ledger ids).

    In a migrated ledger a bare `DEC-<n>` names nothing — the ruling now lives at
    `DEC-<slug>-<n>` — so citing it reads as whichever current id has those digits.
    A ledger that was never migrated still stores `id: DEC-<n>`; there the legacy
    spelling IS the full id, and refusing it would leave no way to cite the ruling."""
    out = []
    for ref in bare_refs(text):
        ids = _cited_ids(ref)
        if ids is None or not set(ids) <= stored:
            out.append(ref)
    return out


def legacy_map(root) -> Dict[str, str]:
    """{old id: current id} for every record carrying a `legacy_id`."""
    return {r[_LEGACY]: r["id"] for r in _load_yaml_raw(root) if r.get(_LEGACY)}


def _renumbered_note(refs, legacy: Dict[str, str]) -> str:
    hit = [(r, legacy[r]) for r in refs if r in legacy]
    return "".join(" %s was renumbered: it is now %s." % pair for pair in hit)


def _bare_citation_message(cited, stored, legacy: Optional[Dict[str, str]] = None) -> str:
    """Why each refused citation names nothing, in the terms of THIS ledger.

    The id migration renumbered rulings — the same digits under an actor slug are a
    DIFFERENT ruling (DEC-225 became DEC-v-hieubt15-151, while DEC-v-hieubt15-225 is
    another decision), so the same-digit id is never offered. The ruling an old id
    became is named only from a stored `legacy_id` (`--map-legacy-ids`); without
    one, the refusal points at `--list`."""
    refs = ", ".join(cited)
    note = _renumbered_note(cited, legacy or {})
    if note:
        return "cite a ruling by its current id, not %s —%s" % (refs, note)
    if any(LEGACY_DECISION_ID_RE.match(i) for i in stored):
        return ("%s is not in this ledger: it stores rulings under the DEC-<n> "
                "spelling, but no record with that number — check the citation "
                "(`decision_register.py --list` shows the live ones)" % refs)
    return ("cite a ruling by its full id, not the retired spelling %s — this "
            "ledger stores no record under it, and the id migration renumbered "
            "rulings, so a current id with the same digits is a different ruling. "
            "Find the full id by title with `decision_register.py --list`" % refs)


def is_readable_id(dec_id) -> bool:
    """True for an id this register will READ — current grammar or the retired one.

    Reads are tolerant and writes are strict, deliberately. The read side skips a
    record whose id it cannot parse, so holding readers to the new grammar alone would
    make an un-migrated ledger parse as EMPTY: no error, no warning, just zero
    decisions where there are hundreds. A caller cannot tell that apart from a fresh
    install. Writes stay strict, so nothing NEW lands in the retired spelling.
    """
    s = str(dec_id or "")
    return bool(DECISION_ID_RE.match(s) or LEGACY_DECISION_ID_RE.match(s))


def id_sort_key(dec_id):
    r"""Deterministic order for a list of ids: (actor slug, sequence number).

    Ids used to be one global sequence, so `int` ordered them and every consumer
    wrote its own `^DEC-(\d+)$`. After the migration such a pattern matches nothing
    and returns its zero default for EVERY record — which does not raise, does not
    warn, and silently turns a sort into a no-op that preserves input order. It looked
    correct on the live ledger precisely because input order was already right.

    So there is one key, here, next to the grammar it reads, and the consumers import
    it. A legacy id sorts under the empty slug, ahead of every actor.
    """
    m = DECISION_ID_RE.match(str(dec_id or ""))
    if m:
        return (m.group("slug"), int(m.group("n")))
    m = LEGACY_DECISION_ID_RE.match(str(dec_id or ""))
    return ("", int(m.group("n"))) if m else ("", 0)


def normalize_actor(actor) -> str:
    """The actor string as it should have been stored.

    Two shapes are repaired rather than trusted, both measured in this repo's own
    ledger: an EMPTY actor (50 of 380 records, which yields a nameless slug and so a
    sequence nobody owns), and a DOUBLED prefix — `user:user:<email>`, written when an
    already-prefixed value reached `"user:%s" % user`. Doubling matters because it
    splits one human into two slugs, which is the opposite of what the key is for.
    """
    a = (actor or "").strip()
    while a.startswith("user:user:"):
        a = a[len("user:"):]
    return a or "unknown"


def actor_slug(actor) -> str:
    """The id-safe stem for an actor: `user:v.hieubt15@vinsmartfuture.tech` -> `v-hieubt15`.

    The email LOCAL part, not the whole address, because the whole address makes every
    id unreadable (`DEC-v-hieubt15-vinsmartfuture-tech-1`). That trade is only safe
    while distinct actors keep distinct local parts — two actors collapsing onto one
    slug would share one id sequence.
    """
    a = normalize_actor(actor)
    if a.startswith("user:"):
        a = a[len("user:"):]
    a = a.split("/agent:")[0]
    if "@" in a:
        a = a.split("@", 1)[0]
    s = re.sub(r"[^a-z0-9]+", "-", a.lower()).strip("-")
    return s[:20].rstrip("-") or "unknown"


# Record template inline (one register, one shape — no template file to drift).
# A register record is machine-written state, so it carries actor + ts like the
# other stores (actor via resolve_actor, ts a UTC isoformat instant).
_RECORD_TEMPLATE = """---
id: {id}
status: {status}
date: {date}
actor: {actor}
ts: {ts}
{affects_line}{supersedes_line}{executed_by_line}{meta_line}---

## {id} — {title}

{rationale}
"""


class DecisionError(ValueError):
    """Raised on a grammar/shape/uniqueness violation (surfaced as a JSON
    finding by the CLI; raised directly for library callers)."""


class LedgerUnreadable(UnreadableSSOT, DecisionError):
    """decisions.yaml does not read as a list of decision records, so nothing may
    be written from it. A DecisionError too, so a library caller catching that
    keeps working; the CLI maps it to its own exit code (see main)."""


class CrossScopeBlock(Exception):
    """A cross-scope flip without a matching confirm token. The ONE exit-2 path
    (DC-5): distinct from invalid_input (exit 0) so a caller can ESCALATE. The
    real enforcement is that the write is refused (raised BEFORE append_decision
    → the SSOT never changes); exit-2 is the signal, not the floor. The floor is
    write_guard blocking a direct edit of decisions.yaml (R1/R4).

    Callers branch on the stdout key `cross_scope_block`, NOT on $? (R8 — the
    caller is LLM prose that reads JSON, not a shell that inspects exit codes)."""

    def __init__(self, target, cross_scope, active_plan):
        self.target = target
        self.cross_scope = list(cross_scope)
        self.active_plan = active_plan
        super().__init__("cross-scope flip of %s needs confirmation" % target)

    def finding(self) -> Dict[str, Any]:
        cross = ",".join(self.cross_scope)
        return {
            "error": "cross_scope_block",
            "written": False,
            "target": self.target,
            "cross_scope": self.cross_scope,
            "active_plan": self.active_plan or "none",
            "confirm_cmd": ("decision_confirm.py --confirm --target %s "
                            "--neighbors %s" % (self.target, cross)),
            "hint": ("these rulings are not referenced by the active plan — if "
                     "this flip is intended, confirm with the command above; if "
                     "the active plan is wrong/ambiguous (0 or >1 in_progress), "
                     "set HARNESS_ACTIVE_PLAN to the right plan dir and retry"),
        }


# High bar for an IMPLICIT-flip warning (R11): a couple of shared domain words
# must NOT trip it — only a new ruling that clearly restates an existing one.
_IMPLICIT_MIN_SHARED = 5

#: How the ranked name must be read, carried in the payload rather than left in
#: a docstring the caller never opens.
_READ_AS = ("start here: the closest live ruling BY SUBJECT, not a ruling this "
            "contradicts — on this ledger a real flip and a same-area ruling "
            "score almost the same, so open it and decide")


def _confirm_ttl(root) -> int:
    """confirm_ttl_s from decision-governance.yaml; fail-open default 1800s when the
    knob file is absent/invalid (a different module owns the file; the gate degrades safely without
    it rather than hard-failing the write path)."""
    import config_layers
    p = config_layers.resolve_path("decision-governance.yaml", root=root)[0]
    try:
        raw = yaml_io.safe_load(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("confirm_ttl_s"), int):
            return raw["confirm_ttl_s"]
    except (FileNotFoundError, OSError, yaml_io.YAMLError, ValueError):
        pass
    return 1800


def _active_plan_text(root) -> str:
    """All text of the active plan dir (plan.md + phase-*.md + artifacts/*) for scope
    classification. R6: gather the WHOLE dir, not just plan.md — a DEC created mid-
    execution is named in the Validation Log / a verification artifact before it
    reaches plan.md, and missing that would false-block the work in flight. Fully
    fail-soft: any resolution/read failure → "" (no plan text → all neighbours
    cross_scope → the gate leans toward asking, never crashes the write path)."""
    try:
        import artifact_check
        plan_dir = artifact_check.resolve_active_plan(root)
    except Exception:  # noqa: BLE001 — resolver failure must not break a write
        return ""
    if not plan_dir:
        return ""
    parts = []
    try:
        for f in sorted(Path(plan_dir).rglob("*")):
            if f.is_file() and f.suffix in (".md", ".json", ".yaml", ".yml", ".txt"):
                try:
                    parts.append(f.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    continue
    except OSError:
        return ""
    return "\n".join(parts)


def _scope_of_flip(root, target: str) -> Dict[str, Any]:
    """Neighbours of `target` + their in_scope/cross_scope split vs the active plan.
    Pure read; used by the gate AND the --scan-flip dry run."""
    records = parse_decisions(root)
    neigh = decision_neighbors.neighbors(records, target)
    scope = decision_neighbors.classify_scope(neigh, _active_plan_text(root))
    return {"neighbors": neigh,
            "in_scope": scope["in_scope"],
            "cross_scope": scope["cross_scope"]}


def _gate_supersede(root, target: str) -> None:
    """Cross-scope confirm gate for an explicit supersede. Allows (with a one-line
    WARN when there are in-scope neighbours) unless a cross-scope neighbour exists
    without a matching confirm token — then raises CrossScopeBlock (write refused).

    Fail-OPEN on a scope-computation failure: if we cannot determine cross-scope, we
    do NOT block (treat as empty → allow). The block fires ONLY on a positively
    determined, unconfirmed cross-scope set."""
    try:
        scope = _scope_of_flip(root, target)
    except Exception:  # noqa: BLE001 — cannot compute scope → allow (degrade safe)
        return
    cross = scope["cross_scope"]
    if not cross:
        if scope["in_scope"]:
            sys.stderr.write(
                "[warn] flip %s: in-scope neighbour(s) %s — allowed\n"
                % (target, ", ".join(scope["in_scope"])))
        return
    active = None
    try:
        import artifact_check
        pd = artifact_check.resolve_active_plan(root)
        active = str(pd) if pd else None
    except Exception:  # noqa: BLE001
        active = None
    if decision_confirm.verify_and_consume(root, target, cross,
                                           ttl_s=_confirm_ttl(root)):
        return  # confirmed (and consumed) — confirm lib emits the trace event
    raise CrossScopeBlock(target, cross, active)


def closest_live_ruling(root, title: str, rationale: str,
                        affects: str = "") -> Optional[Dict[str, str]]:
    """`{"id", "title"}` of the live ruling closest in subject to a proposed new
    record, or None. The ONE door both `--scan-new` and the append warning use,
    so the dry run names what the append will name.

    An empty `affects` means the caller has nothing to offer, so the keyword
    ranking runs alone. Passing "" through would instead switch ON the required
    affects-overlap condition, which an empty value can never satisfy — the
    ranking would then name nothing for every caller that omitted the flag."""
    records = parse_decisions(root)
    rival = decision_neighbors.implicit_flip_match(
        records, title, rationale, min_shared=_IMPLICIT_MIN_SHARED,
        affects=affects or None)
    if rival is None:
        return None
    by_id = {r["id"]: r for r in records}
    return {"id": rival, "title": by_id.get(rival, {}).get("title", "")}


def _warn_implicit_flip(root, title: str, rationale: str, affects: str = "") -> None:
    """Name the closest live ruling so the author opens it → WARN only, NEVER block.

    Deliberately NOT phrased as "this contradicts X": measured on this ledger the
    matcher cannot tell a flip from a same-area ruling, so it names something on
    essentially every append. An author shown a confident-sounding false alarm
    twice stops reading the third, which is the one that matters — so the warning
    says what it actually knows. Fail-soft.

    A non-empty `affects` gates a REQUIRED second condition (shared `affects` token
    with the candidate) on top of the keyword ranking — measured against this ledger's own
    48 recorded flip pairs (positive class) and every other record (negative
    class, 354): the plain keyword bar alone fires on 94.6% of non-flip records
    [wilson95 91.8-96.5%] while catching 60.4% of real flips [46.3-73.0%]; adding
    the affects requirement cuts false alarms to 85.3% [81.2-88.6%] (a real,
    non-overlapping drop) at a catch rate of 54.2% [40.3-67.4%] (indistinguishable
    from the drop being noise — the two catch intervals overlap almost entirely).
    The false-alarm cut is larger than the catch-rate cost, so the second
    condition ships here rather than staying keyword-only."""
    try:
        closest = closest_live_ruling(root, title, rationale, affects)
    except Exception:  # noqa: BLE001
        return
    if closest:
        rival = closest["id"]
        sys.stderr.write(
            "[warn] phán quyết còn hiệu lực GẦN NHẤT với bản ghi mới: %s (%s) — mở ra "
            "đối chiếu, và thêm --supersedes %s nếu đây đúng là một lần lật.\n"
            "        Đây là độ gần CHỦ ĐỀ, không phải bằng chứng mâu thuẫn: đo trên "
            "chính sổ này, điểm của cặp-lật-thật và cặp-cùng-lĩnh-vực chồng lên nhau "
            "gần hết, nên cảnh báo này luôn nêu ra một cái tên kể cả khi không có lật.\n"
            % (rival, closest["title"], rival))


def sanitize_rationale(rationale: str) -> str:
    """Neutralize record-fence / DEC-heading injection in the multiline
    rationale, preserving the text."""
    return escape_injection(rationale, _INJ_DEC_HEADING_RE)


def _decisions_path(root) -> Path:
    """The markdown view path. In YAML mode this is the rendered secondary view;
    in legacy mode it is the source. Kept as the public name many callers/tests
    monkeypatch to redirect the register."""
    return Path(root) / "docs" / "decisions.md"


def _yaml_path(root) -> Path:
    """The pure-YAML SSOT path. When this file exists the register operates in
    YAML mode: CRUD on a YAML list, decisions.md rendered as a secondary view."""
    return Path(root) / "docs" / "decisions.yaml"


def _uses_yaml(root) -> bool:
    """True when the register is a pure-YAML SSOT (decisions.yaml present).
    Otherwise the legacy markdown path is the source (back-compat / un-migrated
    target repos)."""
    return _yaml_path(root).is_file()


# Canonical record field order (what a rendered YAML record lists, in order).
_REC_FIELDS = ("id", "status", "date", "actor", "ts", "affects", "supersedes",
               "executed_by", "title", "rationale")

# The one open field: a mapping the record owns (any YAML value, nested allowed).
# Written only when non-empty, so a record without it stays byte-identical. Every
# other key is refused by name before a write -- the dump rebuilds records from the
# field list above, so an unknown key would otherwise vanish on the next append.
_META = "meta"
# The id a ruling carried before the id migration renumbered it, when known
# (`--map-legacy-ids`). Written only when set, like `meta`; read so a refusal that
# meets the old id can name the ruling it became.
_LEGACY = "legacy_id"
_KNOWN_KEYS = frozenset(_REC_FIELDS) | {_META, _LEGACY}
_META_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_meta(meta: Any, where: str = "meta") -> Dict[str, Any]:
    if not isinstance(meta, dict):
        raise DecisionError("%s must be a mapping, got %s" % (where, type(meta).__name__))
    bad = sorted(str(k) for k in meta if not (isinstance(k, str) and _META_KEY_RE.match(k)))
    if bad:
        raise DecisionError("%s key(s) %s do not match %s"
                            % (where, ", ".join(bad), _META_KEY_RE.pattern))
    return meta


def meta_from_cli(pairs: Optional[List[str]], as_json: Optional[str]) -> Dict[str, Any]:
    """`--meta k=v` (repeatable; v read as a YAML flow value) merged with
    `--meta-json '{...}'`. A key given twice, from either flag, is refused rather
    than resolved by order."""
    meta: Dict[str, Any] = {}
    for pair in pairs or []:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise DecisionError("--meta %r is not key=value" % pair)
        if key in meta:
            raise DecisionError("--meta key %r given twice" % key)
        meta[key] = yaml_io.safe_load(raw) if raw.strip() else ""
    if as_json is not None:
        try:
            extra = json.loads(as_json)
        except ValueError as exc:
            raise DecisionError("--meta-json is not valid JSON: %s" % exc)
        validate_meta(extra, "--meta-json")
        dup = sorted(set(meta) & set(extra))
        if dup:
            raise DecisionError("meta key(s) %s given by both --meta and --meta-json"
                                % ", ".join(dup))
        meta.update(extra)
    return validate_meta(meta, "--meta")


def _refuse_foreign_keys(root) -> None:
    """Refuse a write over a file carrying keys the dump would drop, or a `meta`
    that is not a valid mapping. Names every offender; the file stays untouched."""
    try:
        data = yaml_io.safe_load(_yaml_path(root).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, yaml_io.YAMLError, ValueError):
        return
    problems = []
    for raw in data if isinstance(data, list) else []:
        if not isinstance(raw, dict):
            continue
        rid = raw.get("id", "?")
        foreign = sorted(str(k) for k in raw if k not in _KNOWN_KEYS)
        if foreign:
            problems.append("%s: unknown key(s) %s" % (rid, ", ".join(foreign)))
        if raw.get(_META) not in (None, {}):
            try:
                validate_meta(raw[_META], "%s meta" % rid)
            except DecisionError as exc:
                problems.append(str(exc))
    if problems:
        raise DecisionError(
            "refusing to write the decision SSOT: a rewrite would drop or mangle "
            "what these records carry -- %s. Move extra fields under `meta:` "
            "(a mapping) and retry." % "; ".join(problems))

# This register's own heading marker for the rendered markdown view.
_GENERATED_MARKER = (
    "<!-- generated from docs/decisions.yaml by decision_register.py — "
    "edit the YAML SSOT, not this file -->"
)

# Document-level frontmatter for the rendered view. decisions.md is a graph doc
# under docs/, so the docs-standardize contract requires id/type/status/owner/
# version at the top — distinct from each record's own `---`-fenced mini block.
# Kept static: this is the doc's schema identity, not a per-record value.
_DOC_FRONTMATTER = (
    "---\n"
    "id: harness.decisions\n"
    "type: adr\n"
    "status: stable\n"
    "owner: maintainer\n"
    "version: 1.0.0\n"
    "---"
)


def _lock_path(root) -> Path:
    return Path(root) / "docs" / ".decision_register.lock"


def _shared_dir(root) -> Optional[Path]:
    """`<git common dir>/harness` — one dir every worktree of this clone shares —
    or None outside a git work tree. The register itself is per worktree, so its
    own lock and id scan cannot see a sibling worktree allocating the same number."""
    try:
        out = process_group.run(["git", "-C", str(root), "rev-parse", "--git-common-dir"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    common = out.stdout.strip()
    if out.returncode != 0 or not common:
        return None
    path = Path(common)
    return (path if path.is_absolute() else Path(root) / path).resolve() / "harness"


def _reservations_path(root) -> Optional[Path]:
    shared = _shared_dir(root)
    return shared / "dec-reservations.jsonl" if shared else None


def _reserved_ids(root) -> List[str]:
    path = _reservations_path(root)
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(str(json.loads(line)["id"]))
        except (ValueError, KeyError, TypeError):
            continue
    return out


def _reserve(root, dec_id: str) -> None:
    """Append-only: one row per id taken, so a sibling worktree's alloc skips it."""
    path = _reservations_path(root)
    if path is None:
        return
    row = {"id": dec_id, "actor": hook_runtime.resolve_actor(),
           "ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "worktree": str(Path(root).resolve())}
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


@contextlib.contextmanager
def _register_lock(root):
    """alloc-id + append as ONE critical section over this register's lock
    file (closes the looped-alloc TOCTOU window; also serializes plain
    appends so concurrent agents cannot drop each other's records) — and,
    inside a git clone, over the clone-wide reservation lock first, so two
    worktrees allocating at once still take turns."""
    shared = _shared_dir(root)
    with contextlib.ExitStack() as stack:
        if shared is not None:
            stack.enter_context(register_lock(shared / "dec-reservations.lock"))
        stack.enter_context(register_lock(_lock_path(root)))
        yield


def _fold_duplicate_ids(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Replay-on-read for a duplicate id: a hand-merge or a degraded lock
    (`register_lock` degrades to a no-op where flock is unavailable) can land
    two physical blocks carrying the same id. Reads must never surface both —
    the LAST physical occurrence wins, the same last-wins replay idiom this
    module already uses elsewhere for a name that occurs more than once (see
    `codegraph_port_build_verbs.Ledger.replay` and `hs_run_cook`'s per-phase
    commit bracket). A dict keyed by id and filled in file order gives exactly
    that: content from the last occurrence, position from the first."""
    folded: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        folded[rec["id"]] = rec
    return list(folded.values())


def parse_decisions(root) -> List[Dict[str, Any]]:
    """Every record (active AND superseded), in order, ONE entry per id.
    Missing file → empty list. Dispatches: YAML SSOT when decisions.yaml
    exists, else the legacy markdown source. A record with a malformed id is
    skipped (fail-soft — one corrupt block never sinks --list). Records carry
    the full prose rationale. A duplicate id folds to its last occurrence
    (see `_fold_duplicate_ids`)."""
    if _uses_yaml(root):
        return _fold_duplicate_ids(_parse_yaml(root))
    return _fold_duplicate_ids(_parse_md(root))


def _rationale_from_body(body: str, dec_id: str) -> str:
    """The prose under a record's `## DEC-<n> — <title>` heading (the rationale),
    or the whole body when no heading is present. Trailing whitespace trimmed."""
    m = re.search(r"^##\s+%s\b.*?$" % re.escape(dec_id), body, re.MULTILINE)
    text = body[m.end():] if m else body
    return text.strip()


def _parse_md(root) -> List[Dict[str, Any]]:
    """Legacy markdown source: split into `---`-fenced records, parse each."""
    path = _decisions_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return []

    records: List[Dict[str, Any]] = []
    for m in _RECORD_RE.finditer(text):
        try:
            fm = yaml_io.safe_load(m.group("fm")) or {}
        except (yaml_io.YAMLError, ValueError):
            # PyYAML's timestamp/int constructors raise a bare ValueError (not a
            # YAMLError) on out-of-range values — catch both so a malformed legacy
            # frontmatter record is skipped, not crashed-on (matches the YAML-mode
            # reader + the gate's _parse_artifact_text floor).
            continue
        if not isinstance(fm, dict):
            continue
        dec_id = str(fm.get("id", "")).strip()
        if not is_readable_id(dec_id):
            continue
        rec: Dict[str, Any] = {
            "id": dec_id,
            "status": str(fm.get("status", "active")).strip() or "active",
            "date": str(fm.get("date", "")).strip(),
            "actor": str(fm.get("actor", "")).strip(),
            "ts": str(fm.get("ts", "")).strip(),
        }
        for opt in ("affects", "supersedes", "executed_by"):
            val = fm.get(opt)
            rec[opt] = str(val).strip() if val not in (None, "") else ""
        if isinstance(fm.get(_META), dict) and fm[_META]:
            rec[_META] = fm[_META]
        rec["title"] = _title_from_body(m.group("body"), dec_id)
        rec["rationale"] = _rationale_from_body(m.group("body"), dec_id)
        records.append(rec)
    return records


def _norm_record(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Coerce a raw YAML mapping to a canonical record dict, or None when the id
    is malformed (skipped fail-soft). `status` is preserved verbatim (NOT
    defaulted) so an explicit-empty status stays unflippable — the YAML analogue
    of a legacy record missing its `status:` line."""
    dec_id = str(raw.get("id", "")).strip()
    if not is_readable_id(dec_id):
        return None
    # A bare YAML key (e.g. `status:`) parses to None, NOT an absent key — so
    # dict.get's default never fires and str(None) would yield "None". Coalesce
    # null→"" so an explicit-empty/null status stays the unflippable analogue.
    def _req(key: str) -> str:
        val = raw.get(key)
        return str(val).strip() if val not in (None, "") else ""
    rec = {"id": dec_id,
           "status": _req("status"),
           "date": _req("date"),
           "actor": _req("actor"),
           "ts": _req("ts")}
    for opt in ("affects", "supersedes", "executed_by", "title", "rationale"):
        val = raw.get(opt)
        rec[opt] = str(val) if val not in (None, "") else ""
    if raw.get(_META) not in (None, {}):
        rec[_META] = raw[_META]
    if raw.get(_LEGACY):
        rec[_LEGACY] = str(raw[_LEGACY]).strip()
    return rec


def _load_yaml_raw(root) -> List[Dict[str, Any]]:
    """Raw YAML SSOT records (status preserved verbatim, empty stays empty so an
    explicit-empty status is unflippable). A whole-file parse failure yields []
    here; id allocation uses the resilient raw id-scan instead, so a corrupt
    record never collapses id reservation back to DEC-unknown-1."""
    path = _yaml_path(root)
    try:
        data = yaml_io.safe_load(path.read_text(encoding="utf-8"))
    except (yaml_io.YAMLError, OSError, UnicodeDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for raw in data:
        if isinstance(raw, dict):
            rec = _norm_record(raw)
            if rec is not None:
                out.append(rec)
    return out


def _load_yaml_strict(root) -> List[Dict[str, Any]]:
    """The YAML SSOT for a WRITE: same records as `_load_yaml_raw`, but a file
    that does not parse, a top level that is not a list, or a record the lenient
    read would skip raises LedgerUnreadable instead of reading as fewer records.
    Every writer of decisions.yaml or decisions.md reads through here, because a
    record the read cannot see is a record the write deletes."""
    path = _yaml_path(root)
    out: List[Dict[str, Any]] = []
    for n, raw in enumerate(load_yaml_list(path, LedgerUnreadable), 1):
        if not isinstance(raw, dict):
            raise LedgerUnreadable(path, "record %d is a %s, not a mapping"
                                   % (n, type(raw).__name__))
        rec = _norm_record(raw)
        if rec is None:
            raise LedgerUnreadable(
                path, "record %d has id %r, which is not a decision id — a "
                "rewrite would drop it" % (n, raw.get("id")))
        out.append(rec)
    return out


def _parse_yaml(root) -> List[Dict[str, Any]]:
    """YAML SSOT view: an empty status reads as 'active' for list purposes (the
    raw empty stays visible to _can_supersede via _load_yaml_raw)."""
    out = []
    for rec in _load_yaml_raw(root):
        view = dict(rec)
        view["status"] = rec["status"] or "active"
        out.append(view)
    return out


def _title_from_body(body: str, dec_id: str) -> str:
    m = re.search(r"^##\s+%s\s*[—-]\s*(?P<title>.+?)\s*$" % re.escape(dec_id),
                  body, re.MULTILINE)
    return m.group("title").strip() if m else ""


def list_active(root) -> List[Dict[str, Any]]:
    """Records with `status: active` only — the rulings still in force."""
    return [r for r in parse_decisions(root) if r["status"] == "active"]


def alloc_id(root, actor=None) -> str:
    """Next free id in THIS actor's sequence: `DEC-<slug>-<max for that slug + 1>`.

    Scans RAW `id:` lines so a corrupt-but-id-bearing block still reserves its number —
    a later repair can never collide with a meanwhile-allocated id.

    Only ids carrying THIS slug are counted. That is the whole point: another actor's
    numbers are none of this sequence's business, so two clones held by two actors can
    allocate at the same instant and still not collide. A legacy `DEC-<n>` reserves
    nothing here, because it belongs to the retired global sequence rather than to any
    actor's.
    """
    if actor is None:
        actor = hook_runtime.resolve_actor()
    slug = actor_slug(actor)
    used = []
    for dec_id in [*_scan_all_ids(root), *_reserved_ids(root)]:
        m = DECISION_ID_RE.match(dec_id)
        if m and m.group("slug") == slug:
            used.append(int(m.group("n")))
    return "DEC-%s-%d" % (slug, (max(used) + 1) if used else 1)


# A rendered `## DEC-<n>` heading also reserves the number: a hand-added
# heading-only record (no `id:` fence) must still count, or id-alloc would
# hand out a number that already appears in the file.
# Both grammars, deliberately. This one feeds ID RESERVATION, so a pattern that
# matched only the retired spelling would stop seeing heading-only records the moment
# ids changed shape — and the next allocation would hand out a number already printed
# in the file. It is the third place in this module where a private `DEC-\d+` pattern
# would have gone quietly blind; the other two were a sort and a tie-break.
_HEADING_ID_RE = re.compile(
    r"(?m)^##\s+(DEC-(?:[a-z0-9]+(?:-[a-z0-9]+)*-)?\d+)\b")

_YAML_KEY_LINE_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*):\s*(?P<val>.*)$")


def _scan_yaml_id_lines(text: str) -> List[str]:
    """Structure-aware `id:` scan over the YAML SSOT block-list text — the raw
    id-scan's whole purpose is reserving ids even when the file fails a
    whole-file `yaml.safe_load` (see _load_yaml_raw), so this walks lines by
    INDENTATION only, never invoking the YAML parser itself.

    Counts an `id:` line only when it sits at a record's own field indent —
    either the list-item form (`- id: DEC-unknown-1`) or the hand-written indented
    form (`-\\n  id: DEC-unknown-1`, both documented supported shapes) — established
    per record from its first key line. Any line indented DEEPER than that is
    always a continuation of a multi-line scalar VALUE (PyYAML renders a
    folded/quoted multi-line scalar's continuation lines one level deeper
    than the key that introduces it), never a sibling key — so a rationale
    that itself quotes a bare `id: DEC-999` line can never be mistaken for a
    real record id."""
    ids: List[str] = []
    field_indent: Optional[int] = None

    def _capture(key_val_text: str) -> None:
        m = _YAML_KEY_LINE_RE.match(key_val_text)
        if not m or m.group("key") != "id":
            return
        val = m.group("val").strip().strip("'\"")
        if is_readable_id(val):
            ids.append(val)

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line[indent:]
        if indent == 0 and stripped.startswith("-"):
            rest = stripped[1:]
            if rest[:1] == " ":
                rest = rest[1:]
                field_indent = indent + 2
                if rest:
                    _capture(rest)
            elif rest == "":
                field_indent = None  # resolved by the next line's indent
            else:
                field_indent = None  # not a `- key: value` list item — skip
            continue
        if field_indent is None:
            field_indent = indent  # first key line of a bare "-" record
        if indent != field_indent:
            continue  # deeper (scalar continuation) or malformed — never a key
        _capture(stripped)
    return ids


def _scan_yaml_field_lines(text: str):
    """The YAML SSOT text as a list of per-record `{key: [values]}` maps, or None
    when the text is not the flat block-list shape this scan can judge.

    Same indentation-only discipline as `_scan_yaml_id_lines` above, and for the
    same reason: a line indented DEEPER than a record's field indent is always a
    multi-line scalar continuation, never a sibling key. That is what makes a
    `rationale:` block quoting `status: superseded` in its prose harmless here.

    Values are collected per key as a LIST so a caller can tell "exactly one
    status key" from "a duplicate key it must not guess about"."""
    records: List[Dict[str, List[str]]] = []
    field_indent: Optional[int] = None
    started = False

    def _capture(key_val_text: str) -> None:
        m = _YAML_KEY_LINE_RE.match(key_val_text)
        if not m or not records:
            return
        records[-1].setdefault(m.group("key"), []).append(m.group("val").strip())

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line[indent:]
        if indent == 0:
            if not stripped.startswith("-"):
                # A column-0 line that is not a list item: a top-level mapping
                # (`decisions:`), flow style (`[]`, `{ broken`), or hand-mangled
                # text. None of those are this shape — decline rather than guess.
                return None
            started = True
            records.append({})
            rest = stripped[1:]
            if rest[:1] == " ":
                field_indent = indent + 2
                if rest[1:]:
                    _capture(rest[1:])
            elif rest == "":
                field_indent = None  # bare "-": resolved by the next line's indent
            else:
                return None  # `-foo` is not a list item at all
            continue
        if not started:
            return None  # indented content before any record marker
        if field_indent is None:
            field_indent = indent
        if indent != field_indent:
            continue  # deeper = scalar continuation; shallower = malformed
        _capture(stripped)
    return records


def scan_counts(root) -> Optional[Tuple[int, int]]:
    """`(records, superseded)` read by scanning LINES, or None when the register's
    shape is not certain enough to judge — the caller then pays `parse_decisions`.

    ── WHY THIS EXISTS ──────────────────────────────────────────────────────────
    `decision_reconcile.status()` runs on every Stop event and needs exactly two
    numbers. It got the second one from a full `parse_decisions()`, i.e. a
    `yaml.safe_load` of the whole register: measured **178.61 ms per Stop event**
    at 343 645 B, and this file only ever grows. The scan does it in 3.10 ms.

    Deliberately NOT solved with `yaml.CSafeLoader`: that takes 12.40 ms on the
    same bytes (so the scan is 4x faster even against the C parser) AND the C
    extension is not in `preflight_deps.py`, so leaning on it would silently
    restore the slow path wherever libyaml is absent.

    ── WHY IT RETURNS None INSTEAD OF A BEST GUESS ─────────────────────────────
    A count that is quietly wrong would mis-drive the reconcile nudge with no
    symptom. So the scan asserts a structural invariant — a flat block list where
    every record carries exactly ONE `status` key at its own field indent — and
    declines otherwise. Declining costs one parse on a shape that should not
    exist; guessing costs correctness on every Stop event.
    """
    if not _uses_yaml(root):
        return None
    try:
        text = _yaml_path(root).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    records = _scan_yaml_field_lines(text)
    if records is None:
        return None
    superseded = 0
    for rec in records:
        statuses = rec.get("status")
        # A missing status is normalised elsewhere and a duplicate key means the
        # file disagrees with itself. Either way this scan must not adjudicate it.
        if not statuses or len(statuses) != 1:
            return None
        if statuses[0].strip().strip("'\"") == "superseded":
            superseded += 1
    return len(records), superseded


def _scan_all_ids(root) -> List[str]:
    if _uses_yaml(root):
        try:
            text = _yaml_path(root).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return []
        return _scan_yaml_id_lines(text) + _HEADING_ID_RE.findall(text)
    path = _decisions_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return []
    return scan_record_ids(text) + _HEADING_ID_RE.findall(text)


def render_md(records: List[Dict[str, Any]]) -> str:
    """Render the YAML SSOT records to the secondary markdown view. Deterministic
    (grouped by actor, then by that actor's sequence), with rationale + title escaped
    so the rendered file can never be re-parsed into a phantom record."""
    parts = [_DOC_FRONTMATTER, "", "# Decision Register", "", _GENERATED_MARKER, ""]
    for r in sorted(records, key=lambda r: id_sort_key(r.get("id", ""))):
        parts.append(_render_record(
            r["id"],
            sanitize_field(r.get("title", ""), _INJ_DEC_HEADING_RE),
            sanitize_rationale(r.get("rationale", "")),
            r.get("date", ""),
            sanitize_field(r.get("affects", ""), _INJ_DEC_HEADING_RE),
            r.get("supersedes", ""),
            sanitize_field(r.get("executed_by", ""), _INJ_DEC_HEADING_RE),
            r.get("status") or "active",
            r.get("actor", ""),
            r.get("ts", ""),
            meta=r.get(_META),
        ))
    return "\n".join(parts).rstrip() + "\n"


def _dump_yaml_records(root, records: List[Dict[str, Any]]) -> Path:
    """Write the YAML SSOT atomically AND re-render the markdown view. Both
    writes resolve through the docs fs_guard zone. Returns the markdown view
    path (the public artifact path callers/tests read)."""
    yp = _yaml_path(root)
    _load_yaml_strict(root)  # the file being replaced must have been readable
    # Append-only guard: the resilient id-scan recovers ids even from a file
    # that failed a whole-file YAML parse (where _load_yaml_raw returns []).
    # If the records about to be written drop an id the scan can still see,
    # this dump would silently truncate history — the clobber. Refuse the
    # write and leave the SSOT byte-untouched; a human repairs the corrupt file.
    lost = set(_scan_all_ids(root)) - {r.get("id") for r in records}
    if lost:
        raise DecisionError(
            "refusing to overwrite the decision SSOT: the write would drop %d "
            "existing record(s) %s — docs/decisions.yaml likely failed to parse "
            "(corrupt). Repair it by hand instead of overwriting." % (
                len(lost), sorted(lost))
        )
    _refuse_foreign_keys(root)
    fs_guard.assert_under_root(yp, "docs", root)
    yp.parent.mkdir(parents=True, exist_ok=True)
    payload = []
    for r in records:
        row = {"id": r.get("id", "")}
        if r.get(_LEGACY):
            row[_LEGACY] = r[_LEGACY]
        row.update({k: r.get(k, "") for k in _REC_FIELDS if k != "id"})
        if r.get(_META):
            row[_META] = r[_META]
        payload.append(row)
    # block-style, keys in canonical order, unicode kept; rationale with newlines
    # becomes a literal block scalar. YAML scalar quoting makes the rationale
    # injection-inert in the SSOT itself; the md render escapes for its own view.
    text = yaml_io.safe_dump(payload, sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=4096)
    atomic_write(yp, text)
    md = _decisions_path(root)
    fs_guard.assert_under_root(md, "docs", root)
    atomic_write(md, render_md(records))
    return md


def _render_record(dec_id, title, rationale, date, affects, supersedes,
                   executed_by, status, actor, ts, meta=None) -> str:
    """Fill the record template, emitting the optional link lines ONLY when present
    so an absent affects/supersedes never leaves a bare `affects:` key — and the strip
    never touches the rationale body, which may legitimately contain such a line.

    `affects` is free text, so it is rendered as a JSON-quoted scalar (valid
    YAML): an embedded `key: value` fragment stays INSIDE the string instead
    of becoming a second frontmatter key or breaking the YAML parse.
    `supersedes` needs no quoting — it is regex-validated to `DEC-\\d+`.
    `actor`/`ts` are machine-resolved (resolve_actor + UTC isoformat) and are
    JSON-quoted so a colon in the actor (`user:x@host`) can never split the
    frontmatter into a second key."""
    # Build the optional link lines conditionally — an absent field emits NOTHING, so
    # there is never an empty `affects:`/`supersedes:` line to strip back out. This keeps
    # the strip from ever touching the rationale body, which may legitimately contain a
    # line like `affects:` (a decision discussing the field).
    affects_line = ("affects: %s\n" % json.dumps(affects, ensure_ascii=False)) if affects else ""
    supersedes_line = ("supersedes: %s\n" % supersedes) if supersedes else ""
    # JSON-quoted for the same reason as `affects`/`actor`: the pointer grammar is
    # `path:needle`, so an unquoted value splits the frontmatter at its own colon
    # and the needle becomes a second key.
    executed_by_line = ("executed_by: %s\n"
                        % json.dumps(executed_by, ensure_ascii=False)) if executed_by else ""
    out = _RECORD_TEMPLATE.format(
        id=dec_id, status=status, date=date,
        actor=json.dumps(actor, ensure_ascii=False),
        ts=json.dumps(ts, ensure_ascii=False),
        affects_line=affects_line, supersedes_line=supersedes_line,
        executed_by_line=executed_by_line,
        # JSON is a YAML flow mapping: one line, so a value can never open a
        # second frontmatter key or a phantom record.
        meta_line=("meta: %s\n" % json.dumps(meta, sort_keys=True, ensure_ascii=False,
                                               default=str)) if meta else "",
        title=title, rationale=rationale.strip(),
    )
    return out.strip() + "\n"


def append_decision(
    root,
    dec_id: str,
    title: str,
    rationale: str,
    affects: str = "",
    supersedes: str = "",
    date: Optional[str] = None,
    status: str = "active",
    meta: Optional[Dict[str, Any]] = None,
    executed_by: str = "",
) -> Path:
    """Validate + append one record. Append-only: prior records untouched.
    Raises DecisionError on malformed/duplicate id or dangling supersedes.

    NOT self-locking: this is a read-modify-write over the whole file, so
    concurrent callers can drop each other's records. The CLI paths and
    append_alloc() hold the register lock around it; a library caller must
    do the same (wrap in `with register_lock(...)`).

    Injection escape covers ALL caller-supplied text: the multiline rationale
    keeps its line breaks (anchors escaped); the single-line title/affects/
    supersedes have newlines collapsed so they cannot open a phantom record
    or smuggle extra frontmatter keys."""
    if not DECISION_ID_RE.match(dec_id):
        raise DecisionError(
            "decision id %r does not match the grammar %s" % (dec_id, DECISION_ID_RE.pattern)
        )
    if not title.strip():
        raise DecisionError("a decision needs a non-empty title")
    if not rationale.strip():
        raise DecisionError("a decision needs a non-empty rationale (the WHY)")
    if bare_refs(" ".join((title, rationale, affects))):
        stored = {r["id"] for r in parse_decisions(root)}
        cited = [ref for field in (title, rationale, affects)
                 for ref in unresolved_bare_refs(field, stored)]
        if cited:
            raise DecisionError(_bare_citation_message(cited, stored, legacy_map(root)))
    if meta:
        validate_meta(meta)
    # Tolerant on the TARGET, strict on the id being written: a new ruling may retire
    # one recorded before the migration, and refusing that would strand those records
    # as unsupersedable.
    if supersedes and not is_readable_id(supersedes):
        raise DecisionError(
            "supersedes %r is not a valid decision id (%s)" % (supersedes, DECISION_ID_RE.pattern)
        )

    existing = parse_decisions(root)
    existing_by_id = {r["id"]: r for r in existing}
    if dec_id in existing_by_id:
        raise DecisionError(
            "%s already exists in the register; the register is append-only "
            "(use a fresh --alloc-id, and `supersedes:` to retire the old one)" % dec_id
        )
    if supersedes and supersedes not in existing_by_id:
        raise DecisionError(
            "supersedes %s but that id is not in the register" % supersedes
        )
    # No false supersede chain: a DEC that was already retired is dead — a
    # second ruling claiming to supersede it would re-retire a corpse and
    # imply the wrong lineage. Refuse, naming the offender and its live heir.
    if supersedes and existing_by_id[supersedes]["status"] == "superseded":
        raise DecisionError(
            "%s is already superseded — cannot supersede it again "
            "(false supersede chain); supersede the live ruling that replaced "
            "%s instead" % (supersedes, supersedes)
        )
    # The target must carry a flippable `status:` line, else _supersede_in_place
    # can't retire it and a direct caller strands two active rulings. (append_alloc
    # also gates this; do it here so the public library entry is symmetric.)
    if supersedes and not _can_supersede(root, supersedes):
        raise DecisionError(
            "%s cannot be superseded in place (no flippable `status:` line) — "
            "superseding it would strand two active rulings" % supersedes
        )

    actor = hook_runtime.resolve_actor()
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    rec_date = date or dt.date.today().isoformat()

    if _uses_yaml(root):
        # YAML SSOT: newlines are collapsed in EVERY free-text field, rationale
        # included — mirroring backlog_register.add(), which is immune to the
        # raw id-scan false-positive for exactly this reason. A collapsed
        # rationale can never render as a multi-line PyYAML scalar, so it can
        # never contain a bare `id: DEC-<n>` line for _scan_yaml_id_lines to
        # even have to distinguish from a real key — belt-and-suspenders on
        # top of that scan being structure-aware.
        records = _load_yaml_strict(root)
        records.append({
            "id": dec_id,
            "status": status,
            "date": rec_date,
            "actor": actor,
            "ts": ts,
            "affects": re.sub(r"[\r\n]+", " ", affects).strip(),
            "supersedes": supersedes,
            "executed_by": re.sub(r"[\r\n]+", " ", executed_by).strip(),
            "title": re.sub(r"[\r\n]+", " ", title).strip(),
            "rationale": re.sub(r"[\r\n]+", " ", rationale).strip(),
            **({_META: meta} if meta else {}),
        })
        return _dump_yaml_records(root, records)

    record = _render_record(
        dec_id,
        sanitize_field(title, _INJ_DEC_HEADING_RE),
        sanitize_rationale(rationale),
        rec_date,
        sanitize_field(affects, _INJ_DEC_HEADING_RE),
        supersedes,
        # A ruling written before its code has nothing to point at yet, so the
        # field is empty unless the caller recorded one that already landed.
        sanitize_field(executed_by, _INJ_DEC_HEADING_RE),
        status,
        actor=actor, ts=ts, meta=meta,
    )

    path = _decisions_path(root)
    # Containment helper: resolve + contain BEFORE any mkdir/write so a
    # tampered path can never place the register outside the docs zone.
    fs_guard.assert_under_root(path, "docs", root)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.is_file():
        prior = path.read_text(encoding="utf-8")
        sep = "" if prior.endswith("\n\n") else ("\n" if prior.endswith("\n") else "\n\n")
        new_text = prior + sep + record
    else:
        new_text = "# Decision Register\n\n" + record

    atomic_write(path, new_text)
    return path


def _trail_path(root) -> Path:
    """Append-only amendment trail: one JSON line per `--amend`."""
    return Path(root) / "docs" / "decision-revisions.jsonl"


_AMENDABLE = ("title", "rationale")


def amend_decision(root, dec_id: str, title: Optional[str] = None,
                   rationale: Optional[str] = None) -> Dict[str, Any]:
    """Rewrite a live ruling's title and/or rationale — wording, never status,
    lineage or attribution: the record keeps the actor + ts of who ruled. Inside
    the register lock, the text replaced is appended to the trail (the reviser's
    actor + ts) BEFORE the SSOT is rewritten, so a crash between the two leaves a
    trail line the SSOT does not match yet, never a rewrite with no trail. A
    superseded ruling is history: refused, record a new one."""
    change = {k: re.sub(r"[\r\n]+", " ", v).strip()
              for k, v in (("title", title), ("rationale", rationale)) if v is not None}
    if not change:
        raise DecisionError("--amend needs --title and/or --rationale")
    empty = [k for k, v in change.items() if not v]
    if empty:
        raise DecisionError("--amend cannot empty the %s" % " or ".join(empty))
    if not _uses_yaml(root):
        raise DecisionError("--amend requires the YAML SSOT (docs/decisions.yaml) — "
                            "run --migrate first")
    with _register_lock(root):
        records = _load_yaml_strict(root)
        idx = None
        for i, r in enumerate(records):
            if r["id"] == dec_id:  # last occurrence: the one every read folds to
                idx = i
        if idx is None:
            raise DecisionError("%s is not in the register" % dec_id)
        rec = records[idx]
        if str(rec.get("status", "")).strip() == "superseded":
            raise DecisionError(
                "%s is superseded — a retired ruling is history, not amended; "
                "record a new ruling instead" % dec_id)
        before = {k: rec.get(k, "") for k in change if rec.get(k, "") != change[k]}
        if not before:
            raise DecisionError("--amend changed nothing for %s — pass a --title or "
                                "--rationale that differs" % dec_id)
        after = {k: change[k] for k in before}
        if bare_refs(" ".join(after.values())):
            stored = {r["id"] for r in parse_decisions(root)}
            cited = [ref for v in after.values() for ref in unresolved_bare_refs(v, stored)]
            if cited:
                raise DecisionError(_bare_citation_message(cited, stored, legacy_map(root)))
        trail = _trail_path(root)
        fs_guard.assert_under_root(trail, "docs", root)
        trail.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"id": dec_id, "actor": hook_runtime.resolve_actor(),
                           "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                           "before": before, "after": after},
                          ensure_ascii=False, sort_keys=True)
        with open(trail, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        rec.update(after)
        md = _dump_yaml_records(root, records)

    def rel(p: Path) -> str:
        return Path(p).resolve().relative_to(Path(root).resolve()).as_posix()
    return {"id": dec_id, "written": True, "amended": sorted(after),
            "trail": rel(trail),
            "commit_together": sorted((rel(_yaml_path(root)), rel(md), rel(trail))),
            "note": "commit these together; the trail is docs/decisions.yaml's amendment "
                    "history (append-only), not a stray file"}


def _supersede_in_place(root, dec_id: str) -> bool:
    """Flip an existing active record to `status: superseded`. Only the status
    field of the retired record — its prose changes only through `amend_decision`,
    which leaves a trail. Returns True if a record was flipped. In YAML mode a record whose
    status is explicitly empty is NOT flippable (the analogue of a legacy record
    missing its `status:` line) — flipping it would silently retire a ruling the
    caller cannot see is unanchored."""
    if _uses_yaml(root):
        records = _load_yaml_strict(root)
        # A duplicate id folds to its LAST occurrence on read (parse_decisions,
        # `_fold_duplicate_ids`) — flip that SAME occurrence here, not the
        # first one this loop happens to reach, or the write would land on a
        # record no read path ever surfaces again.
        idx = None
        for i, r in enumerate(records):
            if r["id"] == dec_id:
                idx = i
        if idx is not None and str(records[idx].get("status", "")).strip():
            records[idx]["status"] = "superseded"
            _dump_yaml_records(root, records)
            return True
        return False

    path = _decisions_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False

    # Same last-occurrence rule as the YAML branch above: find every record
    # matching dec_id and keep only the LAST match, then flip that one span —
    # a duplicate id must not flip every physical copy (nor a stale earlier
    # one), it must flip the same copy the folded read treats as current.
    target = None
    for m in _RECORD_RE.finditer(text):
        fm = m.group("fm")
        try:
            data = yaml_io.safe_load(fm) or {}
        except yaml_io.YAMLError:
            continue
        if isinstance(data, dict) and str(data.get("id", "")).strip() == dec_id:
            target = m
    if target is None:
        return False
    fm = target.group("fm")
    new_fm, n = re.subn(r"^status:\s*\S+\s*$", "status: superseded",
                        fm, count=1, flags=re.MULTILINE)
    if n == 0:
        # No status: line to substitute (hand-edited record) — reporting a
        # flip here would leave the old ruling silently active while the
        # caller believes it retired.
        return False
    # _RECORD_RE consumes the closing-fence newline AND the blank line after
    # it; reinsert the blank line so a canonical file round-trips byte-stably
    # (---\n\n## DEC-n, not ---\n## DEC-n).
    replacement = "---\n%s\n---\n\n%s" % (new_fm, target.group("body").lstrip(chr(10)))
    new_text = text[:target.start()] + replacement + text[target.end():]
    # atomic_write, not a bare truncating open() — legacy mode is the SSOT
    # itself here (append_decision's sibling write, 60 lines up, already goes
    # through atomic_write for the same file); a crash/power-loss mid-write
    # must never leave a truncated or empty register.
    atomic_write(path, new_text)
    return True


def _can_supersede(root, dec_id: str) -> bool:
    """Dry-run feasibility of _supersede_in_place: does a record with this id
    exist AND carry a flippable `status:` line? Used to gate the append BEFORE
    writing the new record, so a supersede that cannot land never leaves a
    second active ruling on disk: this check gates the append, so the later
    flip cannot fail after the new record is written."""
    if _uses_yaml(root):
        # Last occurrence wins, same as the read side (_fold_duplicate_ids) and
        # the flip itself (_supersede_in_place) — a duplicate id must answer
        # this feasibility check about the SAME record the flip would touch.
        match = None
        for r in _load_yaml_raw(root):
            if r["id"] == dec_id:
                match = r
        return match is not None and bool(str(match.get("status", "")).strip())
    path = _decisions_path(root)
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    last_fm = None
    for m in _RECORD_RE.finditer(text):
        fm = m.group("fm")
        try:
            data = yaml_io.safe_load(fm) or {}
        except yaml_io.YAMLError:
            continue
        if isinstance(data, dict) and str(data.get("id", "")).strip() == dec_id:
            last_fm = fm
    return last_fm is not None and re.search(
        r"^status:\s*\S+\s*$", last_fm, flags=re.MULTILINE) is not None


def _register_at(root, rev: str) -> list:
    got = process_group.run(["git", "-C", str(root), "show", "%s:docs/decisions.yaml" % rev],
                            capture_output=True, text=True, timeout=30)
    if got.returncode != 0:
        raise DecisionError("cannot read docs/decisions.yaml at %s: %s"
                            % (rev, got.stderr.strip() or "git show failed"))
    return [r for r in (yaml_io.safe_load(got.stdout) or []) if isinstance(r, dict)]


def map_legacy_ids(root, rev: str) -> Dict[str, Any]:
    """Store each pre-migration id as `legacy_id` on the ruling it became.

    `rev` is the commit that renumbered the register. Its parent's old-grammar
    records pair with its own by (title, date) — the migration changed ids and
    nothing else, so the pairing is exact there even if a title was edited later —
    and each old id lands on today's record with the id it was given. A key naming
    more than one record on either side, or a record already carrying a different
    `legacy_id`, refuses the whole write."""
    before = [r for r in _register_at(root, rev + "^")
              if LEGACY_DECISION_ID_RE.match(str(r.get("id", "")).strip())]
    after = _register_at(root, rev)

    def key(r):
        return (str(r.get("title") or "").strip(), str(r.get("date") or "").strip())

    def index(rows):
        out: Dict[Any, list] = {}
        for r in rows:
            out.setdefault(key(r), []).append(str(r.get("id", "")).strip())
        return out
    then, now = index(before), index(after)
    ambiguous = sorted("%s (%s)" % k for k in then if len(then[k]) > 1 or len(now.get(k, ())) > 1)
    if ambiguous:
        raise DecisionError("not mapping legacy ids: ambiguous (title, date) %s"
                            % ", ".join(ambiguous[:5]))
    pairs = {now[k][0]: then[k][0] for k in then if now.get(k)}
    unpaired = sorted(then[k][0] for k in then if not now.get(k))
    with _register_lock(root):
        records = _load_yaml_strict(root)
        mapped, already, clash, gone = 0, 0, [], []
        by_id = {r["id"]: r for r in records}
        for new_id, old_id in sorted(pairs.items()):
            rec = by_id.get(new_id)
            if rec is None:
                gone.append(new_id)
            elif rec.get(_LEGACY) == old_id:
                already += 1
            elif rec.get(_LEGACY):
                clash.append("%s carries %s, %s pairs it with %s"
                             % (new_id, rec[_LEGACY], rev, old_id))
        if clash:
            raise DecisionError("not mapping legacy ids: %s" % "; ".join(clash[:5]))
        for new_id, old_id in pairs.items():
            rec = by_id.get(new_id)
            if rec is not None and not rec.get(_LEGACY):
                rec[_LEGACY] = old_id
                mapped += 1
        if mapped:
            _dump_yaml_records(root, records)
    return {"mapped": mapped, "already": already, "unpaired_at_rev": unpaired,
            "no_longer_in_register": sorted(gone)}


def migrate_to_yaml(root) -> Dict[str, Any]:
    """One-shot: convert a legacy markdown register to the YAML SSOT. Parses the
    existing decisions.md (preserving every record + its rationale), writes
    decisions.yaml, and re-renders decisions.md as the secondary view. Idempotent
    once decisions.yaml exists (re-running just re-dumps from the SSOT). Returns a
    summary with the migrated count."""
    with _register_lock(root):
        records = _parse_yaml(root) if _uses_yaml(root) else _parse_md(root)
        # _parse_yaml returns the view (status defaulted); for a dump we want the
        # raw records when already on YAML so an empty status round-trips.
        if _uses_yaml(root):
            records = _load_yaml_strict(root)
        _dump_yaml_records(root, records)
    return {"migrated": len(records), "yaml": str(_yaml_path(root))}


def render_view(root) -> Path:
    """Re-render decisions.md from the YAML SSOT alone. Refuses — view untouched —
    when the SSOT cannot be read in full (LedgerUnreadable)."""
    if not _uses_yaml(root):
        raise DecisionError(
            "--render-md requires a YAML SSOT (docs/decisions.yaml) — "
            "nothing to render from")
    records = _fold_duplicate_ids(_load_yaml_strict(root))
    md = _decisions_path(root)
    fs_guard.assert_under_root(md, "docs", root)
    atomic_write(md, render_md(records))
    return md


def _record_status(root, dec_id: str) -> Optional[str]:
    """The `status:` of the record with this id, or None if no such record
    exists. Read directly from the file so append_alloc can gate on the
    target's state BEFORE allocating or writing anything."""
    for rec in parse_decisions(root):
        if rec["id"] == dec_id:
            return rec["status"]
    return None


def append_alloc(
    root,
    title: str,
    rationale: str,
    affects: str = "",
    supersedes: str = "",
    date: Optional[str] = None,
    status: str = "active",
    dec_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    executed_by: str = "",
) -> Dict[str, Any]:
    """Append one record in ONE locked critical section — the SINGLE home for
    the alloc/append/supersede-flip atomicity rule shared by both CLI write
    modes. `dec_id=None` allocates the next monotonic id INSIDE the lock (the
    --append-alloc path); an explicit `dec_id` is the --append path. Either way
    the alloc + append + flip run under one register lock, so two concurrent
    agents cannot overwrite each other's records, and a supersede that cannot
    land never leaves a second active ruling.

    On a dup-id race append_decision raises and the CLI surfaces a JSON finding
    — never a silently dropped ruling."""
    with _register_lock(root):
        if _uses_yaml(root):
            # Before any gate reads the ledger leniently: an unreadable SSOT
            # would otherwise surface as a misleading "cannot retire" or a
            # missing-neighbour verdict instead of the parse error it is.
            _load_yaml_strict(root)
        # Gate the append on supersede feasibility FIRST: if the target cannot
        # be retired (missing record / no status: line), refuse before writing
        # the new ruling — otherwise a failed flip would leave two active
        # rulings on disk. The feasibility check gates the write, so the later
        # flip cannot strand a second active ruling.
        if supersedes and not _can_supersede(root, supersedes):
            raise DecisionError(
                "cannot retire %s (no record with a status: line to flip); "
                "refusing to append a ruling that would leave two active "
                "rulings — resolve %s by hand.%s" % (
                    supersedes, supersedes,
                    _renumbered_note([supersedes], legacy_map(root)))
            )
        # No false supersede chain: refuse early (before alloc) if the target
        # is already retired, so the register stays byte-untouched. Names the
        # offender; the same guard lives in append_decision for direct callers.
        if supersedes and _record_status(root, supersedes) == "superseded":
            raise DecisionError(
                "%s is already superseded — cannot supersede it again "
                "(false supersede chain); supersede the live ruling that "
                "replaced %s instead" % (supersedes, supersedes)
            )
        # Cross-scope confirm gate: added LAYER after the structural guards,
        # before the write. Explicit supersede → may raise CrossScopeBlock (write
        # refused, SSOT untouched). A new active ruling with no supersedes → WARN
        # only if it strongly restates a live ruling (implicit flip), never blocks.
        if supersedes:
            _gate_supersede(root, supersedes)
        elif (status or "active") == "active":
            _warn_implicit_flip(root, title, rationale, affects)
        dec_id = dec_id or alloc_id(root)
        _reserve(root, dec_id)
        path = append_decision(
            root, dec_id=dec_id, title=title, rationale=rationale,
            affects=affects, supersedes=supersedes, date=date, status=status,
            meta=meta, executed_by=executed_by,
        )
        if supersedes and not _supersede_in_place(root, supersedes):
            raise DecisionError(
                "appended %s but could not retire %s; the register would have "
                "two active rulings — resolve %s by hand" % (
                    dec_id, supersedes, supersedes)
            )
    rel = lambda p: str(Path(p).relative_to(Path(root).resolve()))
    if _uses_yaml(root):
        # YAML mode: decisions.yaml is the SSOT, decisions.md a rendered view.
        # Report both explicitly and point `path` at the SSOT (the real write
        # target) so the output never reads as "appended to the markdown".
        return {"id": dec_id, "written": True,
                "ssot": rel(_yaml_path(root)),
                "rendered": rel(path),
                "path": rel(_yaml_path(root))}
    # Legacy mode: the markdown file IS the source of truth.
    return {"id": dec_id, "written": True, "path": rel(path)}


#: Exit rung for every refusal other than cross_scope_block: nothing was written.
EXIT_REFUSED = 3

_EPILOG = """exit codes: 0 done; 2 cross_scope_block (write refused, confirm first);
3 refused, nothing written — the JSON `error` says why: `invalid_input` (bad
input, a zone refusal) or `ssot_unreadable` (docs/decisions.yaml does not parse
or is not a list of decision records; the JSON names file, line and parse
error); 4 written, but the declared post-write command failed.

""" + POST_WRITE_HELP


def main() -> int:
    ap = argparse.ArgumentParser(epilog=_EPILOG,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--alloc-id", action="store_true",
                      help="print the next free DEC-<n>")
    mode.add_argument("--append", action="store_true",
                      help="append a decision record (explicit or alloc'd id)")
    mode.add_argument("--append-alloc", action="store_true",
                      help="atomic: alloc the next id AND append, locked")
    mode.add_argument("--list", action="store_true",
                      help="print active decisions as JSON")
    mode.add_argument("--migrate", action="store_true",
                      help="convert a legacy decisions.md to the YAML SSOT")
    mode.add_argument("--map-legacy-ids", metavar="REV",
                      help="REV = the commit that renumbered the register: pair its "
                           "parent's records with its own by title and date, and store "
                           "each old id as `legacy_id` on the ruling it became")
    mode.add_argument("--amend", metavar="DEC-n",
                      help="rewrite a live ruling's --title and/or --rationale, locked; "
                           "the replaced text goes to docs/decision-revisions.jsonl and "
                           "docs/decisions.md is re-rendered")
    mode.add_argument("--render-md", action="store_true",
                      help="re-render docs/decisions.md from decisions.yaml alone "
                           "(no append; repairs a hand-edited or drifted view)")
    mode.add_argument("--scan-flip", metavar="DEC-n",
                      help="dry-run: print neighbours + in_scope/cross_scope for a "
                           "proposed flip (exit 0 JSON; no write)")
    mode.add_argument("--scan-new", action="store_true",
                      help="dry-run: with --title/--rationale, rank the live rulings "
                           "CLOSEST IN SUBJECT and name the first — where to start "
                           "reading, never a contradiction (exit 0 JSON)")
    ap.add_argument("--id", help="decision id (with --append); default = alloc")
    ap.add_argument("--title")
    ap.add_argument("--rationale")
    ap.add_argument("--affects", default="")
    ap.add_argument("--supersedes", default="")
    ap.add_argument("--executed-by", default="",
                    help="what already executes a ruling recorded after it landed "
                         "(`path:symbol`, several joined by ` || `); empty for a "
                         "ruling whose code is still to come")
    ap.add_argument("--meta", action="append", metavar="KEY=VALUE",
                    help="record metadata (repeatable); VALUE is read as YAML, "
                         "e.g. tree=di or tags='[a, b]'")
    ap.add_argument("--meta-json", metavar="JSON",
                    help="record metadata as one JSON object, merged with --meta")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    try:
        if args.alloc_id:
            print(json.dumps({"id": alloc_id(root)}, ensure_ascii=False))
            return 0
        if args.list:
            print(json.dumps({"active": list_active(root)}, indent=2,
                             ensure_ascii=False, default=str))
            return 0
        if args.migrate:
            print(json.dumps(migrate_to_yaml(root), ensure_ascii=False))
            return 0
        if args.map_legacy_ids:
            print(json.dumps(map_legacy_ids(root, args.map_legacy_ids), ensure_ascii=False))
            return 0
        if args.amend:
            result = amend_decision(root, args.amend, title=args.title,
                                    rationale=args.rationale)
            rc = report_post_write(result, "decisions",
                                   run_post_write(root, "decisions", "amend"))
            print(json.dumps(result, ensure_ascii=False))
            return rc
        if args.render_md:
            render_view(root)
            result = {"rendered": "docs/decisions.md"}
            rc = report_post_write(result, "decisions",
                                   run_post_write(root, "decisions", "render"))
            print(json.dumps(result, ensure_ascii=False))
            return rc
        if args.scan_flip:
            print(json.dumps(_scope_of_flip(root, args.scan_flip),
                             ensure_ascii=False))
            return 0
        if args.scan_new:
            closest = closest_live_ruling(root, args.title or "",
                                          args.rationale or "", args.affects or "")
            # The caveat travels WITH the name. `implicit_flip_match` is a
            # RANKING whose own contract forbids reading it as a contradiction
            # — measured on this ledger, a real flip and a merely same-area
            # ruling score almost identically — and this print is the one place
            # a reader meets the answer. Emitted on a miss too: a caveat that
            # appears only on a hit goes missing on exactly the run where the
            # reader is most tempted to conclude "nothing conflicts".
            print(json.dumps({"implicit_flip_of": closest and closest["id"],
                              "closest": closest, "read_as": _READ_AS},
                             ensure_ascii=False))
            return 0
        meta = meta_from_cli(args.meta, args.meta_json) or None
        if args.append_alloc:
            result = append_alloc(
                root, title=args.title or "", rationale=args.rationale or "",
                affects=args.affects, supersedes=args.supersedes, meta=meta,
                executed_by=args.executed_by,
            )
            rc = report_post_write(result, "decisions",
                                   run_post_write(root, "decisions", "append"))
            print(json.dumps(result, ensure_ascii=False))
            return rc
        # --append: the explicit-id path delegates to the SAME locked critical
        # section (append_alloc) — one home for the alloc/append/supersede-flip
        # atomicity. The two write modes differ ONLY in whether the id is given:
        # an explicit --id is passed through; an absent --id allocs inside the
        # lock exactly like --append-alloc. So two concurrent appends still
        # serialize on the register lock, and a rejected supersede still leaves
        # the register byte-untouched (never zero-active + phantom-retired).
        result = append_alloc(
            root, title=args.title or "", rationale=args.rationale or "",
            affects=args.affects, supersedes=args.supersedes, dec_id=args.id,
            meta=meta, executed_by=args.executed_by,
        )
        rc = report_post_write(result, "decisions",
                               run_post_write(root, "decisions", "append"))
        print(json.dumps(result, ensure_ascii=False))
        return rc
    except CrossScopeBlock as blk:
        # The ONE exit-2 path (DC-5): a positively-determined, unconfirmed
        # cross-scope flip. The write was already refused (raised before append);
        # exit 2 is the escalation signal. Caller branches on the stdout key
        # cross_scope_block, not on $? (R8).
        print(json.dumps(blk.finding(), ensure_ascii=False))
        return 2
    except UnreadableSSOT as bad:
        # The ledger itself is broken and nothing was written; the finding names
        # the file and line rather than the generic invalid_input below.
        print(json.dumps(bad.finding(), ensure_ascii=False))
        sys.stderr.write("error: %s\n" % bad)
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 — surface as finding
        # A bad input surfaces as a JSON finding on stdout, never a bare
        # traceback — and a non-zero rung, so a caller reading only $? does not
        # take a refused write for a done one.
        print(json.dumps(
            {"error": "invalid_input", "message": str(exc), "written": False},
            ensure_ascii=False,
        ))
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
