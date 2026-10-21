#!/usr/bin/env python3
"""workflow_weight.py — the reader for the workflow-weight policy.

The single source for "how heavy is this work" is harness/data/workflow-weight.yaml
(env override HARNESS_WORKFLOW_WEIGHT); this module is its only reader, so the
cook side never greps the YAML by hand and a number can move without code
following it. Two questions:

  * EXECUTION — `posture()`: in place by default; subagents only when the
    caller forces them (`--parallel`/`--subagents`) or the phase declares more
    files than the threshold. A missing threshold is ANSWERED ("no threshold"),
    never silently treated as zero or infinity.
  * REVIEW — `review()`: measured after the work on the real diff
    (base..head). Any one metric over its threshold makes the independent
    review mandatory; a risk tier above `when_tier_above` triggers on its own,
    derived by risk_rubric.derive_risk (never re-derived here — no second glob
    engine for the same question). Tests and data files classify toward no
    metric at all, so a large test-only change stays quiet on purpose. A range
    that cannot be measured keeps the review MANDATORY: waiving a gate you
    could not measure is the fail-open hole this module exists not to have.

Boundary (v1): like risk_rubric, this is a decision input, not an enforcement
hook. `hs-run cook` reads it at the review gate; prose and skills cite the
same file through this reader.
"""

import os
import re
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config_layers
import risk_rubric
from scope_match import glob_to_regex


Posture = namedtuple("Posture", ["mode", "reason", "threshold_declared", "declared_files"])
Metrics = namedtuple("Metrics", ["code_effective_lines", "instruction_markdown_lines",
                                 "docs_markdown_lines", "changed_files"])
Review = namedtuple("Review", ["required", "triggers", "metrics", "risk_tier",
                               "measured", "thresholds"])

#: Ordered low -> high; `when_tier_above` ranks against this, so a policy value
#: that is not on the ladder fails loud instead of quietly never matching.
_TIERS = ("tiny", "normal", "high_risk")

_METRIC_KEYS = ("code_effective_lines", "instruction_markdown_lines", "docs_markdown_lines")


class WorkflowWeightError(Exception):
    """Raised when the workflow-weight config is unreadable/malformed."""


def _policy_path():
    return config_layers.resolve_path("workflow-weight.yaml")[0]


def load_policy(path=None) -> dict:
    """Load the workflow-weight config. A missing file is an error — a size
    gate with no config must not silently default-to-pass."""
    p = Path(path) if path is not None else _policy_path()
    try:
        import yaml_io
        data = yaml_io.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise WorkflowWeightError("workflow-weight config not found at %s" % p) from e
    except (OSError, ValueError) as e:
        raise WorkflowWeightError("workflow-weight config at %s is unreadable: %s" % (p, e)) from e
    if not isinstance(data, dict):
        raise WorkflowWeightError("workflow-weight config at %s is not a mapping" % p)
    return data


def _compiled(globs):
    return [glob_to_regex(str(g)) for g in (globs or [])]


def _compile(pol: dict) -> dict:
    """The policy's glob/number tables, compiled once per question — a diff is
    classified file-by-file and re-reading the YAML per path is both slow and
    a second, drift-prone copy of the same policy."""
    rev = pol.get("review") or {}
    metrics = rev.get("metrics") or {}
    code = metrics.get("code_effective_lines") or {}
    instr = metrics.get("instruction_markdown_lines") or {}
    above = (rev.get("risk") or {}).get("when_tier_above")
    if above is not None and above not in _TIERS:
        raise WorkflowWeightError(
            "review.risk.when_tier_above=%r is not one of %s" % (above, list(_TIERS)))
    over = {}
    for key in _METRIC_KEYS:
        v = (metrics.get(key) or {}).get("over")
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0):
            raise WorkflowWeightError(
                "review.metrics.%s.over=%r must be a non-negative integer" % (key, v))
        over[key] = v
    return {
        "code_exts": {str(e) for e in (code.get("extensions") or [])},
        "code_extless": _compiled(code.get("extensionless_under")),
        "code_comment": dict(code.get("comment_prefix") or {}),
        "code_exclude": _compiled(code.get("exclude_paths")),
        "instr_paths": _compiled(instr.get("paths")),
        "over": over,
        "risk_above": above,
    }


def _ext(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[1] if "." in base else ""


def _classify(path: str, cfg: dict) -> str:
    """One path -> one counting kind. Markdown splits FIRST (instruction
    patterns are a subset of .md, so they must win over docs), extensions
    second, everything else is data and counts toward nothing."""
    if path.endswith(".md"):
        if any(rx.match(path) for rx in cfg["instr_paths"]):
            return "instruction_md"
        return "docs_md"
    ext = _ext(path)
    is_code = ext in cfg["code_exts"] or (
        not ext and any(rx.match(path) for rx in cfg["code_extless"]))
    if not is_code:
        return "data"
    if any(rx.match(path) for rx in cfg["code_exclude"]):
        return "test"
    return "code"


def classify(path, policy=None) -> str:
    """Public one-path form of `_classify` — the door tests and prose examples
    use; the diff loop uses `_classify` directly with its compiled table."""
    return _classify(str(path), _compile(policy if policy is not None else load_policy()))


# --------------------------------------------------------------- execution

def _flag_lists(policy) -> tuple:
    ex = policy.get("execution") or {}
    sub = [str(f) for f in ((ex.get("subagent_when") or {}).get("flags") or [])]
    inp = [str(f) for f in ((ex.get("in_place_when") or {}).get("flags") or [])]
    return sub, inp


def forcing_flags(policy=None) -> list:
    """Every forcing flag name the policy knows, both directions — the vocabulary
    `--flags` is validated against, so the warning can name what IS known."""
    sub, inp = _flag_lists(policy if policy is not None else load_policy())
    return sub + inp


MODES = ("in_place", "subagent")


class PostureConflict(WorkflowWeightError):
    """Two forces ask for different postures, or a declared posture is not a mode."""


def _threshold_posture(ex: dict, files: list) -> Posture:
    when = ex.get("subagent_when") or {}
    default = str(ex.get("default") or "in_place")
    threshold = when.get("phase_declares_more_than")
    if not isinstance(threshold, int) or isinstance(threshold, bool):
        return Posture(default, "no threshold declared (phase_declares_more_than absent) — "
                                "staying at the default", False, files)
    if len(files) > threshold:
        return Posture("subagent", "phase declares %d files > %d" % (len(files), threshold),
                       True, files)
    return Posture(default, "%d declared files <= %d" % (len(files), threshold), True, files)


def declared_posture(plan_dir) -> "str | None":
    """The plan's own `posture:` (plan.md frontmatter), unvalidated — `posture()`
    owns what a valid value is. None when absent or the plan is unreadable."""
    if plan_dir is None:
        return None
    import frontmatter_parser
    fm = frontmatter_parser.parse_file(Path(plan_dir) / "plan.md").get("frontmatter")
    value = fm.get("posture") if isinstance(fm, dict) else None
    return None if value is None else str(value)


def posture(declared_files, *, flags=(), declared=None, policy=None) -> Posture:
    """The threshold's answer unless forced: a flag from either list
    (`subagent_when.flags` / `in_place_when.flags`) or `declared` — the plan's own
    `posture:`. Forces that disagree raise PostureConflict; an override of the
    threshold names what the threshold alone would have answered. Flag names are
    compared normalized (leading dashes stripped, `-` -> `_`), because the same
    intent arrives as `--parallel` (CLI) and `parallel` (config/argv reshape)."""
    pol = policy if policy is not None else load_policy()
    ex = pol.get("execution") or {}
    files = [str(f) for f in (declared_files or []) if str(f)]
    given = {str(f).lstrip("-").replace("-", "_") for f in (flags or [])}
    sub, inp = _flag_lists(pol)
    forces = [("--%s" % f, "subagent") for f in sub if f in given]
    forces += [("--%s" % f, "in_place") for f in inp if f in given]
    if declared is not None:
        if declared not in MODES:
            raise PostureConflict("plan posture %r is not a mode (known: %s)"
                                  % (declared, ", ".join(MODES)))
        forces.append(("plan posture: %s" % declared, declared))
    if len({mode for _, mode in forces}) > 1:
        raise PostureConflict("contradicting posture forces: %s"
                              % ", ".join("%s (%s)" % f for f in forces))
    natural = _threshold_posture(ex, files)
    if not forces:
        return natural
    source, mode = forces[0]
    reason = "forced by %s" % source
    if mode != natural.mode:
        reason += " — the threshold alone answers %s (%s)" % (natural.mode, natural.reason)
    return Posture(mode, reason, natural.threshold_declared, files)


# ------------------------------------------------------------------ review

_DIFF_FILE = re.compile(r"^diff --git a/(.*) b/(.*)$")


def _diff_path(header_value: str) -> "str | None":
    """`b/<path>` from a +++/--- header; None for /dev/null (the other side of
    a pure creation/deletion)."""
    if header_value.strip() == "/dev/null":
        return None
    return header_value[2:] if header_value.startswith(("a/", "b/")) else header_value


def _measure(root: Path, base: str, head: str, cfg: dict) -> Metrics:
    """Count the diff's lines per metric. `-U0` because hunk context is not a
    change; rename detection stays ON so a moved file is 0 lines, not the whole
    file counted twice (the pre-fix shape a naive --no-renames parse gives)."""
    r = subprocess.run(["git", "-C", str(root), "diff", "-U0", "-M", "--no-color",
                        str(base), str(head)], capture_output=True, text=True)
    if r.returncode != 0:
        raise WorkflowWeightError("git diff %s..%s failed: %s"
                                  % (base, head, r.stderr.strip()[:200]))
    changed, seen = [], set()
    counts = {"code": 0, "instruction_md": 0, "docs_md": 0}
    kind, prefix = None, "#"
    for line in r.stdout.splitlines():
        m = _DIFF_FILE.match(line)
        if m:
            # The NEW name is the path a hunk is attributed to; a deleted file
            # (new side /dev/null) is attributed to its old name below.
            for name in (m.group(2), m.group(1)):
                if name and name not in seen:
                    seen.add(name)
                    changed.append(name)
            kind = None
            continue
        if line.startswith(("+++", "---")):
            value = _diff_path(line[4:])
            if value is not None:
                kind = _classify(value, cfg)
                ext = _ext(value)
                prefix = cfg["code_comment"].get(
                    ext, cfg["code_comment"].get("default", "#"))
            continue
        if kind is None or not line[:1] in ("+", "-"):
            continue
        body = line[1:]
        stripped = body.strip()
        if not stripped:
            continue                      # blank: never counts, any metric
        if kind == "code" and stripped.startswith(prefix):
            continue                      # comment line: never counts toward code
        if kind in counts:
            # "test" and "data" classify toward NO metric — that exclusion is
            # the whole point of the kind ladder, not an accident of the dict.
            counts[kind] += 1
    return Metrics(counts["code"], counts["instruction_md"], counts["docs_md"], changed)


def review(root, base, head, *, policy=None) -> Review:
    """Is the independent review mandatory for the work in base..head?

    Unmeasurable stays REQUIRED (a gate you could not measure is not waived);
    undeclared thresholds are ANSWERED as None in `thresholds` rather than
    silently counting as 0 or infinity."""
    pol = policy if policy is not None else load_policy()
    cfg = _compile(pol)
    thresholds = {k: cfg["over"][k] for k in _METRIC_KEYS}
    try:
        m = _measure(Path(root), base, head, cfg)
    except WorkflowWeightError as e:
        return Review(True, ["unmeasurable: %s" % e], None, None, False, thresholds)
    triggers = []
    numbers = {"code_effective_lines": m.code_effective_lines,
               "instruction_markdown_lines": m.instruction_markdown_lines,
               "docs_markdown_lines": m.docs_markdown_lines}
    for key in _METRIC_KEYS:
        over = thresholds[key]
        if over is not None and numbers[key] > over:
            triggers.append("%s %d > %d" % (key, numbers[key], over))
    risk = risk_rubric.derive_risk(root, list(m.changed_files))
    if cfg["risk_above"] is not None:
        # Rank, don't string-compare: a future tier name would otherwise sort
        # somewhere arbitrary and quietly flip the trigger.
        if _TIERS.index(risk.tier) > _TIERS.index(cfg["risk_above"]):
            triggers.append("risk_tier %s > %s" % (risk.tier, cfg["risk_above"]))
    return Review(bool(triggers), triggers, m, risk.tier, True, thresholds)


# -------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(
        prog="workflow-weight",
        description="Answer workflow-weight questions from the policy file.")
    sub = ap.add_subparsers(dest="cmd")
    p1 = sub.add_parser("posture", help="execution posture for a phase's declared files")
    p1.add_argument("--files", required=True, help="comma-separated declared file paths")
    p1.add_argument("--flags", default="", help="comma-separated command flags")
    p1.add_argument("--config", default=None)
    p2 = sub.add_parser("review", help="is the independent review mandatory for base..head?")
    p2.add_argument("--root", default=".")
    p2.add_argument("--base", required=True)
    p2.add_argument("--head", required=True)
    p2.add_argument("--config", default=None)
    args = ap.parse_args(argv)
    if args.cmd is None:
        ap.print_usage(sys.stderr)
        return 2
    try:
        pol = load_policy(args.config)
        if args.cmd == "posture":
            files = [f.strip() for f in args.files.split(",") if f.strip()]
            flags = [f.strip() for f in args.flags.split(",") if f.strip()]
            p = posture(files, flags=flags, policy=pol)
            print(json.dumps({"mode": p.mode, "reason": p.reason,
                              "threshold_declared": p.threshold_declared,
                              "declared_files": list(p.declared_files)}, ensure_ascii=False))
            return 0
        r = review(args.root, args.base, args.head, policy=pol)
        m = r.metrics
        print(json.dumps({
            "required": r.required, "triggers": list(r.triggers),
            "metrics": None if m is None else {
                "code_effective_lines": m.code_effective_lines,
                "instruction_markdown_lines": m.instruction_markdown_lines,
                "docs_markdown_lines": m.docs_markdown_lines},
            "risk_tier": r.risk_tier, "measured": r.measured,
            "thresholds": dict(r.thresholds)}, ensure_ascii=False))
        return 0
    except WorkflowWeightError as e:
        print("workflow-weight error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
