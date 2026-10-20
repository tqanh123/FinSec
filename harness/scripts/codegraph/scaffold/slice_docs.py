"""hs-code scaffold slice-docs -- one `AGENTS.md` per dist from a batch
declaration (`dists:` + `slices:`), ported near-verbatim from ob-tools'
`portkit.doc.slice_docs` (pinned commit `60075cb89c`).

SAME DOCTRINE AS new-module/new-dist: schema-driven, `codemap.yaml
`scaffold.templates` (`kind: slice-doc`)
    The rendered SHAPE (the `## Module metadata` / `## Role` / `## Boundaries`
    / `## Doors` sections, the `**ADD-NEW:**` convention, the field ROWS) is
    core -- every project gets the same skeleton. What is NOT core: the law
    file's name, the decisions directory/glob, the unread-baseline's path,
    the root directory a dist lives under, a dist's own door path shape, and
    every prose sentence naming any of those -- all of it comes from a
    declared `--template KEY` (`engine.template_for(descriptor, key,
    kind="slice-doc")`), exactly the door `plan_module`/`plan_dist` already
    use for `kind: module`/`kind: dist`. No template given renders the
    built-in English default (`_DEFAULT_MESSAGES`, `_DEFAULT_ROOT_DIR`,
    `_DEFAULT_DOOR_TEMPLATE` below) -- a project that never opts into a
    flavor still gets a real, readable doc, never a KeyError.

    A template's prose lives in `messages: {key: {lang: text}}`, resolved
    through `pack_messages.resolve()` -- the SAME message-table door every
    port/pack/check caller shares (`harness/rules` "human-edited config is
    YAML" + `output_config.language()`), never a second ad-hoc VN/EN branch
    inside this module. `packs/opsbrain/scaffold-template.yaml` ships the
    OpsBrain flavor (Vietnamese prose, `packages/`+`src/` layout, `uv`/
    `[tool.*] tier` mentions) as reference DATA a project's own
    `codemap.yaml scaffold.templates` copies in -- this module carries no
    OpsBrain literal of its own.

    Namespace/cluster/owner/the law-doc link are still never literals: they
    are read off the BATCH declaration (a plain dict this module never
    invents a key of its own into), the same discipline as everywhere else
    in this package.

WHAT THIS PORTS, AND WHAT IT DOES NOT
    Ported: `links`, `dist_doc`, `root_doc`, `broken_links`, five of
    upstream's six validation gates -- ghost `slices:` key, empty `question`/
    `boundaries` cell, `**ADD-NEW:**` missing, `provenance:` citing a decision
    with no file, and the `CODE-UNREAD` confession-mark baseline (two-way,
    shrink-only: a new mark absent from the baseline is red, and a baseline
    entry no longer marked is ALSO red -- the baseline must shrink in the
    same commit that answers the question, never accumulate) -- and the
    Flows section, read through the flow tracer (`port_flow_trace`, the same
    door `hs-code port flow-trace` reads): a declaration carrying `flows:`
    REQUIRES `--snapshot` (a named port snapshot of this tree) and is exit 2
    without it, never a silently flow-less document -- 46 docs missing a
    section read exactly like 46 docs of a repo with no flows. Its prose is
    template messages (`flows_intro`/`flows_row`/`flows_outro`); upstream's
    "the architecture doc redraws" clause is dropped with that document.

    DELIBERATE DEVIATION from upstream on `CODE-UNREAD` detection: upstream's
    `unread_dist_ids` (`portkit/doc/slice_docs.py` near :117/:255) reads the
    RAW `slices.get(id)['question']` only. Measured on the real fixture this
    phase's own byte-match test carries (`ob-platform`,
    `260915-1419-obs-agents-refactor`): that batch's real `question:` sits
    under `dists:`, not `slices:` -- upstream's own reader would silently
    miss a `CODE-UNREAD` mark declared there, on the exact data this gate
    exists to police. A gate that cannot go red on the real data it exists
    for is a check that cannot fail. `unread_dist_ids` here instead reads the
    SAME merged `dists:`+`slices:` view `validation_errors` already builds
    via `merge_dists_and_slices` (this module, above) -- one merge, shared,
    never a second narrower one -- so a mark is caught whichever key it was
    declared under.

Exit: 0 rendered clean (and written, if --out given), or --write-baseline
shrank/held the file · 1 a validation problem (empty cell, ghost key,
missing ADD-NEW, dead provenance citation, unacknowledged/stale CODE-UNREAD
mark, or a broken relative link) -- named, nothing written · 2 the batch
file is not readable, a `--dist` this batch does not declare, or
--write-baseline would GROW the baseline (refused, file unchanged).
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

import regex
import hs_family
import pack_messages
import yaml_io

from .errors import ScaffoldError
from .engine import template_for
from hs_family.cli import Command

#: The built-in, project-agnostic prose every message key falls back to when
#: no `--template` is given -- English, no OpsBrain literal (`packages/`,
#: `src/`, `uv`, `[tool.*] tier`) anywhere. A template's own `messages:` dict
#: has the SAME keys with its own `{lang: text}` entries (see
#: `packs/opsbrain/scaffold-template.yaml` for the Vietnamese flavor); a key
#: this dict does not carry is a `pack_messages.MessageTableError`, never a
#: silent blank.
_DEFAULT_MESSAGES: "dict[str, dict[str, str]]" = {
    "root_id_note": {"en": "the repo root -- not a dist, no `tier`"},
    "root_role": {"en": (
        "This repo holds the dists under `{root_dir}/`. Each dist states its own domain\n"
        "question at its own door; this file does NOT repeat them, it states **how to\n"
        "work** here.")},
    "root_boundaries": {"en": (
        "This file speaks PROCESS, not CONTENT. It must not carry a dist list, a path\n"
        "table, or any current number about the tree -- those drift silently. A number\n"
        "belongs here only with a `yymmdd` marker (a measurement THAT HAPPENED) or with\n"
        "the command that generates it.")},
    "root_doors_dir": {"en": (
        "[`{root_dir}/`]({root_dir}/) -- every sub-directory is a dist, every dist has a\n"
        "four-section `AGENTS.md` ·")},
    "root_law_desc": {"en": (
        "the rung ladder, the three length rules, when\n"
        "to flip a dependency, and its own measurement command table. That law is\n"
        "invariant; do not copy a line of it here.")},
    "dist_law_desc": {"en": (
        "the rung ladder, file length rules, when to flip a dependency.\n"
        "That law is invariant and lives OUTSIDE this dist; do not copy a line of it here.")},
    "law_link_label": {"en": "Architecture law"},
    "id_note": {"en": "the directory name IS the id -- the filesystem (and the package "
                      "manager, if one is declared) already refuses two dists sharing one name"},
    "dist_role": {"en": (
        "This dist's domain question lives at its own door, `{door}` --\n"
        "the file's first line states it, and that is where a future reader learns "
        "where a new leaf belongs.")},
    "deps_note": {"en": "`[project.dependencies]` is the SSOT for this dist's edges{tier_clause}"},
    "tier_clause": {"en": ", `[tool.{ns}] tier` names its rung"},
    "dist_door_note": {"en": "door"},
    "flows_intro": {"en": (
        "This dist sits ON the path of a declared run. The flow graph is\n"
        "DECLARED in code and read back by AST -- do not infer it from node\n"
        "names:")},
    "flows_row": {"en": "- `{fid}` -- {n} node(s) here, declared in `{at}`"},
    "flows_outro": {"en": (
        "Add a node in that declaring file; the run above will show it. Do NOT copy\n"
        "the node list here -- it is content, and content rots on the next pass.")},
}
#: The command a Flows section points at — the one door that reads the flow.
FLOW_TRACE_COMMAND = "hs-code port flow-trace --batch <batch> --edges"
#: Neutral defaults for the two path-shaped knobs a template may override --
#: no OpsBrain literal baked in as the fallback either (`root_dir` is not
#: `"packages"`, `door_template` carries no `"src/"` prefix).
_DEFAULT_ROOT_DIR = "dists"
_DEFAULT_DOOR_TEMPLATE = "{ns_seg}{pkg}/__init__.py"


def _msg(messages: "dict[str, dict[str, str]] | None", key: str,
        language: "str | None" = None, **subs: str) -> str:
    table = messages if messages is not None else _DEFAULT_MESSAGES
    return pack_messages.resolve(table, key, language=language).format(**subs)

#: Nouns where a number in front of them is an INVENTORY, not a measurement
#: with a date marker -- see `has_date_marker`.
COUNT_NOUNS = ("dist", "file", "module", "route", "symbol", "function", "item",
               "gate", "test", "fixture", "entry point", "call site", "table",
               "tệp", "hàm", "mục", "bảng", "điểm vào", "chỗ gọi")

#: `COUNT_NOUNS` joined into the inventory pattern is read from a module-level
#: name, not a string literal at the match call site -- the config-regex-timeout
#: guard cannot prove that provenance by static analysis, so this goes through
#: `regex` (a drop-in `re` superset with a `timeout=` kwarg `re` lacks) with a
#: bounded timeout, same as `claim_verify.py`/`skill_config.py`'s config-sourced
#: patterns, instead of bare `re`.
_COUNT_NOUN_MATCH_TIMEOUT_SECONDS = 1.0
#: A `yymmdd` date marker -- no hard-coded century (a marker written from
#: 2027 on must still count); month/day validity is checked so `123456` is not one.
_NGAY = re.compile(r"\b(\d{2})(\d{2})(\d{2})\b")
#: The shape of "where does a new thing in this dist go" -- an explicit
#: declaration, never guessed from prose.
_NEW_MARK = re.compile(r"\*\*ADD-NEW:\*\*\s*\S")
#: The confession mark a writer places when they have not read the code.
UNREAD_MARK = "CODE-UNREAD"


def has_date_marker(s: str) -> bool:
    for yy, mm, dd in _NGAY.findall(s):
        if 1 <= int(mm) <= 12 and 1 <= int(dd) <= 31:
            return True
    return False


def links(from_: Path, to_: Path) -> str:
    """A relative path from the directory holding `from_` to `to_`. Never
    typed by hand: a hand-typed relative path is exactly how 46 real slice
    docs once carried a dead link to the very law file they exist to name."""
    return os.path.relpath(to_, Path(from_).parent).replace(os.sep, "/")


def broken_links(doc_file: Path, content: str) -> "list[str]":
    """Which Markdown links in `content` do NOT resolve from where the doc
    stands. A link into the TARGET tree (starting `src/`, or a bare
    `pyproject.toml`) is skipped -- that tree may not exist yet, on purpose:
    this doc is generated BEFORE the port. Only a link back into the
    plan/output tree is checked."""
    res = []
    for m in re.finditer(r"\]\(([^)]+)\)", content):
        d = m.group(1)
        if d.startswith(("http", "#")) or not d.startswith("."):
            continue
        if not (Path(doc_file).parent / d).resolve().exists():
            res.append(d)
    return res


def root_doc(r: dict, law_link: str, cluster: "str | None" = None,
            owner: "str | None" = None, *, root_dir: str = _DEFAULT_ROOT_DIR,
            messages: "dict[str, dict[str, str]] | None" = None,
            language: "str | None" = None) -> str:
    """The root `AGENTS.md` of the target repo -- five sections, the `root`
    template of the slice-doc standard. `root_dir`/`messages` come from the
    declared `--template` (see module docstring); the built-in defaults name
    no OpsBrain directory or prose."""
    def m(key: str, **subs: str) -> str:
        return _msg(messages, key, language=language, root_dir=root_dir, **subs)

    return "\n".join([
        "# %s" % (r.get("title") or root_dir), "",
        "## Module metadata", "| field | value |", "|---|---|",
        "| **id** | %s |" % m("root_id_note"),
        "| **cluster** | %s |" % ("`%s`" % cluster if cluster else "no `cluster:` declared"),
        "| **owner** | %s |" % (owner or "no `owner:` declared"),
        "| **provenance** | [Architecture law](%s) -- the invariant every dist inherits |" % law_link,
        "", "## Role", m("root_role"),
        "", "## How to work here", "",
        (r.get("how_to_work") or "_Not written yet._").strip(), "",
        "## Boundaries", m("root_boundaries"),
        "", "## Doors", m("root_doors_dir"),
        "[**%s**](%s) -- %s" % (m("law_link_label"), law_link, m("root_law_desc")),
        "",
    ])


def pkg_name(dist_id: str, ns: "str | None") -> str:
    """`ob-platform` + ns `ob` -> `platform`. The prefix is DECLARED by the
    batch (`namespace_root:`), never guessed: a batch declaring a different
    `namespace_root` strips a different prefix; without one, nothing is
    stripped and the full dist id is still a readable package name."""
    slug = dist_id[len(ns) + 1:] if ns and dist_id.startswith("%s-" % ns) else dist_id
    return slug.replace("-", "_")


def dist_doc(d: dict, law_link: str, ns: "str | None" = None,
            cluster: "str | None" = None, owner: "str | None" = None,
            flows: "list[tuple[str, str, int]] | None" = None, *,
            door_template: str = _DEFAULT_DOOR_TEMPLATE,
            messages: "dict[str, dict[str, str]] | None" = None,
            language: "str | None" = None) -> str:
    """One `AGENTS.md`. Four sections plus Flows when the dist sits on a
    declared run's path (`flows`: `port_flow_trace.flow_homes` rows). Every
    relative path is computed by `links()` via `os.path.relpath`, never
    typed -- a hand-typed one is exactly how 46 real docs once miscounted a
    directory level. `door_template`/`messages` come from the declared
    `--template` -- no namespace (`ns` falsy) means no `[tool.*] tier`
    mention at all, never a silent `"ob"` fallback."""
    def m(key: str, **subs: str) -> str:
        return _msg(messages, key, language=language, **subs)

    q = (d.get("question") or "").strip().strip('"')
    pkg = pkg_name(d["id"], ns)
    door = door_template.format(ns=ns or "", ns_seg=("%s/" % ns) if ns else "", pkg=pkg)
    tier_clause = m("tier_clause", ns=ns) if ns else ""
    rows = [
        ("id", "`%s` — %s" % (d["id"], m("id_note"))),
        ("cluster", "`%s`" % cluster if cluster else "no `cluster:` declared"),
        ("owner", owner or "no `owner:` declared"),
        ("provenance", ", ".join("[%s](%s)" % (k, v) for k, v in (d.get("_prov_link") or []))
         or "no decision touches this dist yet"),
    ]
    out = ["# `%s` — %s" % (d["id"], q or "NO DOMAIN QUESTION YET"), "",
          "## Module metadata", "| field | value |", "|---|---|"]
    out += ["| **%s** | %s |" % (k, v) for k, v in rows]
    out += ["", "## Role", m("dist_role", door=door),
           "", "## Boundaries"]
    b = (d.get("boundaries") or "").strip()
    out += [b or "_Not written yet._", "",
           "## Doors",
           "[`%s`](%s) — %s ·" % (door, door, m("dist_door_note")),
           "[`pyproject.toml`](pyproject.toml) — %s" % m("deps_note", tier_clause=tier_clause),
           "[**%s**](%s) — %s" % (m("law_link_label"), law_link, m("dist_law_desc")),
           ""]
    if flows:
        out += ["## Flows", m("flows_intro"), ""]
        out += [m("flows_row", fid=fid, n=str(n), at=at) for fid, at, n in flows]
        out += ["", "```bash", FLOW_TRACE_COMMAND, "```", m("flows_outro"), ""]
    return "\n".join(out)


# ── validation (a subset of upstream's; see module docstring for what is not ported) ──

def merge_dists_and_slices(payload: dict) -> "list[dict]":
    """`dists:` entries with their matching `slices:` prose merged in (a
    COPY, never the caller's own dict), in declaration order."""
    dists = [dict(x) for x in (payload.get("dists") or []) if isinstance(x, dict) and x.get("id")]
    slices = payload.get("slices") or {}
    for d in dists:
        d.update(slices.get(d["id"]) or {})
    return dists


def validation_errors(payload: dict, dec_files: "list[Path]") -> "list[str]":
    """Ghost `slices:` key, empty `question`/`boundaries`, missing
    `**ADD-NEW:**`, `provenance:` citing a decision with no file -- upstream's
    four cheapest, most general gates (see module docstring for the two not
    ported)."""
    dists = merge_dists_and_slices(payload)
    slices = payload.get("slices") or {}
    ids = {d["id"] for d in dists}
    errs = []
    for i in sorted(set(slices) - ids):
        errs.append("%s: has a `slices:` entry but NO dist carries that id -- a ghost "
                    "key; this prose generates no document and nobody rereads it" % i)
    for d in dists:
        i = d["id"]
        if not str(d.get("question") or "").strip():
            errs.append("%s: NO `question:` in either `dists:` or `slices:` -- every "
                        "package needs an ANSWERABLE question, not a plan code" % i)
        b = str(d.get("boundaries") or "").strip()
        if not b:
            errs.append("%s: NO `boundaries:` -- nobody can say which leaf belongs "
                        "here and which does not. An empty cell is not a clean one" % i)
            continue
        if not _NEW_MARK.search(b):
            errs.append("%s: `boundaries` does not answer ADD-NEW -- one line "
                        "`**ADD-NEW:** <which leaf kind belongs here, which does not>`" % i)
        pattern = r"(\d+)\s+(" + "|".join(re.escape(n) for n in COUNT_NOUNS) + r")\b"
        for m in regex.finditer(pattern, b, timeout=_COUNT_NOUN_MATCH_TIMEOUT_SECONDS):
            line = b[max(0, m.start() - 90):m.end() + 90]
            if not has_date_marker(line) and "`" not in line.split(m.group(0))[0][-30:]:
                errs.append("%s: `boundaries` carries a bare inventory %r -- attach a "
                            "yymmdd marker, or the command that generates it" % (i, m.group(0)))
        for p in d.get("provenance") or []:
            hit = [f for f in dec_files if f.name.startswith("%s-" % p)]
            if not hit:
                errs.append("%s: provenance cites %s -- no file in decisions/" % (i, p))
    return errs


def resolve_prov_links(d: dict, dp: Path, dec_files: "list[Path]") -> "list[tuple[str, str]]":
    """`(decision-id, relative-link)` for every `provenance:` entry this
    dist declares that resolves to a real file (a citation with no file is
    an error already raised by `validation_errors`, never silently dropped
    here)."""
    out = []
    for p in d.get("provenance") or []:
        hit = [f for f in dec_files if f.name.startswith("%s-" % p)]
        if hit:
            out.append((p, links(dp, hit[0])))
    return out


def load_unread_baseline(p: Path) -> "list[str]":
    """Which dists are ACKNOWLEDGED as code-unread. No file = empty = every
    mark is NEW."""
    if not p.is_file():
        return []
    return [ln.strip()[2:].strip() for ln in p.read_text(encoding="utf-8").splitlines()
           if ln.strip().startswith("- ")]


def unread_dist_ids(dists: "list[dict]") -> "list[str]":
    """Dist ids carrying `UNREAD_MARK` in `question:`, read from `dists`
    (the output of `merge_dists_and_slices` -- the SAME merged
    `dists:`+`slices:` view `validation_errors` uses, never a second,
    narrower reader). See the module docstring's DELIBERATE DEVIATION note
    for why this is a merged view rather than upstream's raw `slices:`
    reader."""
    return sorted(d["id"] for d in dists if UNREAD_MARK in str(d.get("question") or ""))


def unread_baseline_errors(dists: "list[dict]", baseline_path: Path) -> "list[str]":
    """Two-way: a fresh mark absent from the baseline is red, AND a baseline
    entry no longer marked is red too -- the baseline must SHRINK the moment
    a dist is answered, never just accumulate. `dists` (the merged view) is
    already scoped to real ids, so a stale baseline line for a renamed/
    removed dist reads the same as "no longer marked" with no separate
    case needed."""
    unread = set(unread_dist_ids(dists))
    prior = load_unread_baseline(baseline_path)
    errs = []
    for x in sorted(unread - set(prior)):
        errs.append("%s: `question:` carries `%s` and is NOT in the baseline (%s) -- "
                    "nobody can answer this package's domain question" % (x, UNREAD_MARK, baseline_path))
    for x in prior:
        if x not in unread:
            errs.append("%s: the baseline (%s) says it carries `%s` but it no longer "
                        "does -- delete the line in the SAME commit that answered"
                        % (x, baseline_path, UNREAD_MARK))
    return errs


def write_unread_baseline(dists: "list[dict]", baseline_path: Path) -> "tuple[bool, str]":
    """`--write-baseline`: SHRINK-ONLY. A first-ever run (no prior baseline)
    accepts every currently-marked dist; once a baseline exists, a dist
    NEWLY carrying the mark is refused -- that must be a visible hand edit
    in review, never silently folded into a bigger baseline."""
    unread = unread_dist_ids(dists)
    prior = load_unread_baseline(baseline_path)
    fresh = [x for x in unread if x not in prior]
    if fresh and prior:
        return False, ("REFUSED: --write-baseline only SHRINKS. %d dist(s) newly carry "
                       "`%s` -- that must be a hand edit, visible in review." % (len(fresh), UNREAD_MARK))
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        "# Dist carrying `%s` in `question:` -- no domain question yet.\n"
        "# A WORK LIST, not an allow-list. `--write-baseline` only SHRINKS.\n"
        "# Each line is a package nobody can answer 'what does it answer' for.\n"
        "# After answering: remove the mark AND the line, in one commit.\n"
        "version: 1\n" % UNREAD_MARK + "".join("- %s\n" % x for x in unread),
        encoding="utf-8")
    return True, "wrote %d dist(s) into %s" % (len(unread), baseline_path)


#: The template fields `SLICE_DOCS` reads by name -- everything a
#: `kind: slice-doc` template MAY declare, each with the same neutral
#: fallback its rendering function already carries when the field is
#: absent. A template is free to declare only some of these. The fallbacks
#: name no project's own convention: OpsBrain's `LUAT-KIEN-TRUC.md` and
#: `D-*.md` are data in `packs/opsbrain/scaffold-template.yaml`.
_TEMPLATE_STR_FIELDS = {
    "law_file": "ARCHITECTURE.md", "decisions_dir": "decisions",
    "decisions_glob": "*.md", "baseline_rel": "map/unread-baseline.yaml",
    "root_dir": _DEFAULT_ROOT_DIR, "door_template": _DEFAULT_DOOR_TEMPLATE,
}


def _load_template(root: Path, key: "str | None") -> dict:
    """`{}` (every field falls back to its function-level default) when no
    `--template` is given -- a project that never opts into a flavor still
    renders, in English, with no OpsBrain literal. `--template` given reads
    `codemap.yaml scaffold.templates.<key>` (`kind: slice-doc`), the SAME
    door `new-module`/`new-dist` already read their own kind through
    (`engine.template_for`) -- never a second schema reader."""
    if key is None:
        return {}
    from ._layout import load_descriptor
    descriptor = load_descriptor(root)
    return template_for(descriptor, key, kind="slice-doc")


def _slice_docs_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("batch", help="the batch declaration YAML (dists:/slices:/namespace_root/...)")
    ap.add_argument("--plan", required=True, metavar="DIR",
                    help="plans/<batch> dir -- holds the template's law file, decisions dir, "
                         "and unread-baseline (see --write-baseline)")
    ap.add_argument("--template", default=None, metavar="KEY",
                    help="a key under codemap.yaml scaffold.templates (kind: slice-doc); "
                         "absent renders the built-in English default, no OpsBrain literal")
    ap.add_argument("--dist", default=None, metavar="ID",
                    help="which dist to render (required unless --write-baseline or --root)")
    ap.add_argument("--root", action="store_true",
                    help="render the repo ROOT AGENTS.md (root_doc) instead of one dist's")
    ap.add_argument("--out", default=None, metavar="FILE", help="write here; default: print, write nothing")
    ap.add_argument("--snapshot", default=None, metavar="NAME",
                    help="a named port snapshot of this tree -- required when the declaration "
                         "carries `flows:` (builds the Flows section)")
    ap.add_argument("--write-baseline", action="store_true",
                    help="shrink-only: record every currently CODE-UNREAD dist as acknowledged; "
                         "refuses (unchanged) if that would GROW an existing baseline")


def _slice_docs(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    as_json = bool(getattr(opts, "json", False))

    try:
        tmpl = _load_template(Path(root), args.template)
    except ScaffoldError as tmpl_err:
        raise hs_family.UsageError(str(tmpl_err)) from tmpl_err
    fields = {k: tmpl.get(k, default) for k, default in _TEMPLATE_STR_FIELDS.items()}
    messages = tmpl.get("messages")

    batch_path = Path(args.batch)
    if not batch_path.is_file():
        raise hs_family.UsageError("scaffold slice-docs: no batch declaration at %s" % batch_path)
    payload = yaml_io.safe_load(batch_path.read_text(encoding="utf-8")) or {}
    plan = Path(args.plan)
    law_file = plan / fields["law_file"]
    if not law_file.is_file():
        raise hs_family.UsageError("scaffold slice-docs: %s missing -- the docs' invariant anchor" % law_file)
    decisions = plan / fields["decisions_dir"]
    dec_files = sorted(decisions.glob(fields["decisions_glob"])) if decisions.is_dir() else []
    # Never hard-coded in core (`unread_baseline_errors`/`write_unread_baseline` take a
    # path parameter): this is the ONE place that decides where under `--plan` it lives,
    # same side `--plan` already supplies the law file/decisions dir from.
    baseline_path = plan / fields["baseline_rel"]

    dists = merge_dists_and_slices(payload)
    by_id = {d["id"]: d for d in dists}

    if args.write_baseline:
        ok, msg = write_unread_baseline(dists, baseline_path)
        data = {"ok": ok, "message": msg, "baseline": str(baseline_path)}
        if as_json:
            print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        else:
            print(("scaffold slice-docs --write-baseline: %s" % msg) if ok else msg)
        return 0 if ok else 2

    if args.root:
        dest = Path(args.out).resolve() if args.out else None
        dp = dest if dest else Path(root).resolve() / "AGENTS.md"
        text = root_doc(payload, links(dp, law_file), payload.get("cluster"), payload.get("owner"),
                        root_dir=fields["root_dir"], messages=messages)
        return _emit(text, dest, dp, "root", as_json)

    if args.dist is None:
        raise hs_family.UsageError(
            "scaffold slice-docs: --dist is required (unless --write-baseline or --root)")
    if args.dist not in by_id:
        raise hs_family.UsageError(
            "scaffold slice-docs: no dist %r in %s (have: %s)"
            % (args.dist, batch_path, sorted(by_id)))

    errs = validation_errors(payload, dec_files) + unread_baseline_errors(dists, baseline_path)
    if errs:
        data = {"ok": False, "errors": errs}
        if as_json:
            print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        else:
            print("RED: %d slice-doc problem(s)" % len(errs))
            for e in errs:
                print("   %s" % e)
        return 1

    flow_rows = _flow_rows(Path(root), payload, args.snapshot)
    d = by_id[args.dist]
    dest = Path(args.out).resolve() if args.out else None
    dp = dest if dest else Path(root).resolve() / args.dist / "AGENTS.md"
    d["_prov_link"] = resolve_prov_links(d, dp, dec_files)
    text = dist_doc(d, links(dp, law_file), payload.get("namespace_root"),
                    payload.get("cluster"), payload.get("owner"),
                    flow_rows.get(args.dist), door_template=fields["door_template"],
                    messages=messages)
    return _emit(text, dest, dp, args.dist, as_json)


def _flow_rows(root: Path, payload: dict, snapshot: "str | None") -> "dict[str, list]":
    """dist -> Flows rows, when the declaration carries `flows:`; `{}` when it
    carries none. Declared flows with no `--snapshot`, or a flow the tracer
    cannot read, is exit 2 -- never a document silently missing its section."""
    if not payload.get("flows"):
        return {}
    if not snapshot:
        raise hs_family.UsageError(
            "scaffold slice-docs: the declaration carries `flows:` -- pass --snapshot NAME "
            "(a port snapshot of this tree) so the Flows section can be read, rather than "
            "generating documents that silently lack one")
    import port_batch_reader
    import port_flow_trace
    import port_workspace
    try:
        port_root = port_workspace.find_port_root(root)
    except (port_workspace.PortRootNotFoundError, port_workspace.BadRepoPathError) as e:
        raise hs_family.UsageError("scaffold slice-docs: %s" % e) from e
    _commit, idx = port_batch_reader.read_snapshot(port_root, snapshot, caller_named=True)
    try:
        traced = port_flow_trace.trace_all(
            payload["flows"], port_root, idx, port_flow_trace.load_builders(payload.get("flow_builders")))
    except port_flow_trace.Missing as e:
        raise hs_family.UsageError("scaffold slice-docs: %s" % e) from e
    return port_flow_trace.flow_homes(traced)


SLICE_DOCS = Command(add_args=_slice_docs_args, run=_slice_docs)


def _emit(text: str, dest: "Path | None", dp: Path, name: str, as_json: bool) -> int:
    dead = broken_links(dp, text)
    if dead:
        data = {"ok": False, "broken_links": dead}
        if as_json:
            print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        else:
            print("%d BROKEN LINK(S) -- a dead link reads like evidence:" % len(dead))
            for x in dead:
                print("   %s" % x)
        return 1

    if dest is None:
        data = {"ok": True, "out": None, "dist": name}
        if as_json:
            print(json.dumps(data, sort_keys=True, ensure_ascii=False))
        else:
            print(text, end="")
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    data = {"ok": True, "out": str(dest), "dist": name}
    if as_json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False))
    else:
        print("scaffold slice-docs: wrote %s" % dest)
    return 0
