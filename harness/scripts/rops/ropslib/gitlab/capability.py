"""What a lane can honestly execute -- and the refusal when it cannot.

Measured 2026-09-10 against gitlab-ci-local 4.75.1 (the LATEST release):

    retry: 2   ->   the job runs ONCE.   GitLab runs it three times.

Nothing warns. Both directions of that gap are wrong: a flaky job GitLab would paper
over goes red here, and a job that only ever passes BECAUSE of retry reads green here
without the retry ever happening. The same holds for `timeout:`, for the eight
server-bound variables (`CI_JOB_TOKEN` is a permission, not a shape -- a fake value
satisfies the string and not the API call), and for anything needing a real queue.

So: a lane DECLARES what it honours, a document DECLARES what it needs, and the
mismatch is a refusal that names the keyword and the jobs. Never an execution with a
footnote -- the next reader sees a colour, not a footnote.

`default:` is why the reader takes the EXPANDED document rather than `--preview`:
measured, `--preview` merges `default.image` into every job but silently DROPS
`default.retry` and `default.timeout`, which in GitLab bind every job in the file.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rops.ropslib.prog import PROG

#: Top-level keys that configure the pipeline rather than declare a job. A reader that
#: treated `stages:` (a list) as a job would crash; one that treated `.tpl:` as a job
#: would refuse a pipeline over a keyword no job ever inherits.
NOT_JOBS = frozenset({
    "stages", "include", "variables", "workflow", "default", "image", "services",
    "cache", "before_script", "after_script", "spec",
})

#: Server-bound variables. Absent locally (measured 8/8) and un-fakeable: the value is
#: a credential the far side checks, not a string the job reads.
_SERVER_TOKEN_VARS = ("CI_JOB_TOKEN", "CI_JOB_JWT", "CI_REGISTRY_PASSWORD",
                      "CI_API_V4_URL", "CI_DEPLOY_PASSWORD")
_MR_VARS = ("CI_MERGE_REQUEST_",)

#: Every capability name `needs()` can emit. A lane may only name these.
KEYWORDS = (
    "retry", "timeout", "manual", "environment", "tags",
    "server_token", "merge_request", "protected", "git_checkout",
    "resource_group",
)

_WHY = {
    "retry": "declared retry is not executed (measured: retry:2 -> 1 run, not 3)",
    "timeout": "declared timeout is not enforced",
    "manual": "a manual job has no one to press the button",
    "environment": "environments (and their approvals) exist only on a server",
    "tags": "runner tag matching needs a queue with more than one runner",
    "server_token": "server credentials cannot be faked -- a fake token satisfies the "
                    "string and not the API call",
    "merge_request": "merge-request variables come from a merge request on a server",
    "protected": "protected branches/variables are a server-side permission",
    "resource_group": "a resource group serialises jobs ACROSS pipelines, and a "
                      "single-process lane has no cross-pipeline anything to "
                      "serialise with",
    "git_checkout": "the light lane rsyncs files into the container instead of cloning, "
                    "so `git` commands exit 128 (measured: `git ls-files` -> not a git "
                    "repository, while a real runner clones at depth 20)",
}

#: A script INVOKING git, not a word that happens to contain those letters: `digit`,
#: `legit` and `gitleaks` all do, and matching them would mark most jobs unsupported
#: for nothing.
_GIT_CALL = re.compile(r"(?:^|[\s;&|(`$])git(?:\s|$)", re.MULTILINE)


@dataclass(frozen=True)
class Lane:
    name: str
    honours: frozenset[str]
    how: str = ""


LANE_LOCAL = Lane(
    "local", frozenset({"tags", "timeout"}),
    "gitlab-ci-local: containers, services, artifacts, cache, needs, parallel",
)
# `tags` is honoured in the only sense a single-runner lane can: every job runs, and
# no job is left queued forever waiting for a tag nobody serves. `timeout` is honoured
# by the lane's own wall-clock deadline rather than by gitlab-ci-local.
LANE_SERVER = Lane(
    "server", frozenset(KEYWORDS),
    "a real GitLab CE + a registered runner: everything, at the price of a push",
)

LANES = {lane.name: lane for lane in (LANE_LOCAL, LANE_SERVER)}


def _retry_max(value) -> int:
    """`retry: 2` and `retry: {max: 2}` are the same declaration."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        return _retry_max(value.get("max", 0))
    return 0


def jobs_of(doc: dict) -> dict[str, dict]:
    """The mappings in `doc` that are jobs: not config, not a hidden `.template`."""
    return {
        name: body for name, body in doc.items()
        if isinstance(body, dict) and name not in NOT_JOBS and not name.startswith(".")
    }


#: `script` only -- NOT `before_script`. Setup prepares the environment; the script is
#: the work. Measured 2026-09-10: the expanded document merges `default.before_script`
#: into every job, so one `git --version` diagnostic line -- there precisely to prove
#: git is installed -- marked all 17 jobs as needing a checkout, and the whole pipeline
#: was refused over its own setup step. `after_script` is cleanup, same reasoning.
_WORK_KEYS = ("script",)


def _text_of(body: dict) -> str:
    """Where a job USES a variable: its script and its own variables block.

    NOT `rules:`. A rule decides WHETHER the job runs, and by the time the gate sees
    the job that question is already answered -- the job listing carries the result.
    Measured 260910 on document-intake: all 20 live jobs share a `.di-gate-rules`
    template mentioning CI_MERGE_REQUEST_TARGET_BRANCH_NAME, so scanning rules marked
    every one of them unsupported, including nine test shards that run nothing but
    pytest against a postgres service -- exactly the work the light lane does well.
    """
    parts: list[str] = []
    for key in _WORK_KEYS:
        val = body.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, list):
            parts.extend(str(v) for v in val)
    for key, val in (body.get("variables") or {}).items():
        parts.append(f"{key}={val}")
    return "\n".join(parts)


#: How a job DECLARES what it needs, for the cases the scan cannot see.
#:
#: The scan reads what the job says, and a job that says `bash scripts/ci.sh` says
#: nothing about git -- measured in this repo, where every GitLab job dispatches into
#: one script and the scan finds zero git needs while two tests inside that script die
#: at exit 128. Following a shell script into a repo the gate does not execute is not
#: something a scan can do honestly, so the declaration is the primary surface and the
#: scan is the safety net for what nobody declared.
NEEDS_VAR = "ROPS_NEEDS"


def _declared(body: dict) -> set[str]:
    raw = str((body.get("variables") or {}).get(NEEDS_VAR, "")).strip()
    if not raw:
        return set()
    named = {p.strip() for p in raw.split(",") if p.strip()}
    unknown = sorted(named - set(KEYWORDS))
    if unknown:
        # Never dropped silently: a typo that vanishes widens the lane invisibly, and
        # the job goes on running somewhere that cannot honour what it meant to ask for.
        raise ValueError(
            f"{NEEDS_VAR} names capabilities that do not exist: {', '.join(unknown)} "
            f"(known: {', '.join(KEYWORDS)})")
    return named


def _needs_of(name: str, body: dict, defaults: dict) -> set[str]:
    """Which capabilities ONE job requires, `default:` already applied.

    A job key overrides the default even when it turns the feature OFF: `retry: 0`
    against `default: retry: 2` means this job does not retry, so it does not need a
    lane that retries.
    """
    def eff(key):
        return body[key] if key in body else defaults.get(key)

    out: set[str] = _declared(body)
    if _retry_max(eff("retry")) > 0:
        out.add("retry")
    if eff("timeout"):
        out.add("timeout")
    if body.get("when") == "manual":
        out.add("manual")
    if body.get("environment"):
        out.add("environment")
    if eff("tags"):
        out.add("tags")
    # `resource_group` is a need; `interruptible` is NOT. Ignoring a resource group
    # lets two deploys run at once, which is a wrong answer; ignoring interruptible
    # wastes CPU on a job that would have been cancelled. Refusing a pipeline over
    # the second would be the gate crying wolf.
    if body.get("resource_group"):
        out.add("resource_group")

    text = _text_of(body)
    if _GIT_CALL.search(text):
        out.add("git_checkout")
    if any(v in text for v in _SERVER_TOKEN_VARS):
        out.add("server_token")
    if any(v in text for v in _MR_VARS):
        out.add("merge_request")
    return out


def needs(doc: dict) -> dict[str, list[str]]:
    """capability -> the job names requiring it. Empty when the file needs nothing."""
    defaults = doc.get("default") or {}
    if not isinstance(defaults, dict):
        defaults = {}
    out: dict[str, list[str]] = {}
    for name, body in jobs_of(doc).items():
        for cap in _needs_of(name, body, defaults):
            out.setdefault(cap, []).append(name)
    return {k: sorted(v) for k, v in sorted(out.items())}


#: `--list-json` names a parallel cell `test: [1/2]`; the document calls the job
#: `test`. Selecting by either spelling must reach the same declaration.
_CELL = re.compile(r"^(?P<job>.+?): \[[^\]]+\]$")


def _base_name(selected: str) -> str:
    m = _CELL.match(selected)
    return m.group("job") if m else selected


@dataclass(frozen=True)
class Verdict:
    ok: bool
    lane: str
    unsupported: dict[str, list[str]] = field(default_factory=dict)
    reason: str = ""


def verdict(doc: dict, lane: Lane, jobs: list[str] | None = None) -> Verdict:
    """Can `lane` run this document (or these jobs) without lying about the result?

    `jobs` narrows the question: `--job a` must not be refused for what job `b`
    declares. A selected name that matches no job is an ERROR, not a pass -- a typo
    that reports green is a green result for zero work.
    """
    known = set(jobs_of(doc))
    if jobs:
        wanted = {_base_name(j) for j in jobs}
        missing = sorted(wanted - known)
        if missing:
            return Verdict(False, lane.name, {},
                           f"no such job: {', '.join(missing)} "
                           f"(the file declares: {', '.join(sorted(known)) or 'none'})")
    else:
        wanted = known

    bad: dict[str, list[str]] = {}
    for cap, names in needs(doc).items():
        if cap in lane.honours:
            continue
        hit = sorted(n for n in names if n in wanted)
        if hit:
            bad[cap] = hit
    if not bad:
        return Verdict(True, lane.name)

    lines = [f"lane {lane.name!r} cannot honestly run this pipeline:"]
    for cap, names in sorted(bad.items()):
        lines.append(f"  {cap:<14} {_WHY[cap]}")
        lines.append(f"  {'':14} jobs: {', '.join(names)}")
    # The command, not just the lane's name. A refusal that says "use a lane that
    # honours these" without saying HOW leaves the work stranded, and a refusal that
    # names a command the tool does not have reads as noise the second time.
    lines.append("  -> the server lane honours these:")
    lines.append(f"       {PROG} gitlab up && {PROG} gitlab runner && {PROG} gitlab project")
    lines.append(f"       {PROG} gitlab push && {PROG} gitlab trigger")
    lines.append("     or drop the keyword from the job.")
    return Verdict(False, lane.name, bad, "\n".join(lines))
