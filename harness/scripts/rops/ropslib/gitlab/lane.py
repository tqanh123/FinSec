"""The light lane: building the gitlab-ci-local command, and judging what came back.

Every default here is a trap this project paid for on 2026-09-10, measured on real
runs rather than read from documentation:

  * Containers do NOT inherit the host's internal CA. A/B on one job: no flag ->
    `CERTIFICATE_VERIFY_FAILED` from pip while the host reached pypi.org fine;
    `--ca-file /etc/ssl/certs/ca-certificates.crt` -> `PIP_OK`.
  * A missing `origin/HEAD` makes `rules: changes:` explode at
    `git diff --name-only origin/main` -- exit 1 plus a stack trace.
  * An `extends:` naming an anchor nobody defined exits having run NO job. Its exit
    code then VANISHES through a pipe: `gitlab-ci-local | tail` returns tail's rc, so
    the run reported EXIT=0. That is why `classify()` counts jobs instead of trusting
    an exit code, and why the command never contains a pipe.
  * `include: local:` reads the file from the COMMITTED tree, not from disk: an
    uncommitted include file fails with `Local include file cannot be found`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from hs_family import UsageError
from rops.ropslib.transport import sq

DEFAULT_CLI_VERSION = "4.75.1"

#: What a finished job prints. Measured verbatim on 4.75.1:
#:     `a: [1/2] finished in 871 ms`          (passed -- NO verdict suffix)
#:     `b        finished in 957 ms`          (passed, name padded)
#:     `a finished in 651 ms  FAIL 3 `        (failed)
#: Both shapes mean the job RAN, which is what the count is for.
_FINISHED = re.compile(r"^(?P<job>\S.*?)\s+finished in\s")

#: Two framing lines share that shape and are NOT jobs. Counting them would turn the
#: undefined-anchor run -- which printed ONLY these two and ran nothing -- into a
#: two-job pass.
_NOT_A_JOB = ("parsing and downloads", "pipeline")


def jobs_finished(log: str) -> int:
    n = 0
    for line in log.splitlines():
        m = _FINISHED.match(line.strip())
        if m and m.group("job").strip() not in _NOT_A_JOB:
            n += 1
    return n


def variable_args(variables: list[str]) -> list[str]:
    """`--variable K=V` per pair, quoted.

    A pair without `=` is REFUSED: gitlab-ci-local ignores a malformed one, so the
    run proceeds under the assumption the variable was set -- and for a `rules:`
    variable that means the pipeline silently resolves to a different set of jobs.
    """
    out: list[str] = []
    for pair in variables or []:
        if "=" not in pair:
            raise UsageError(
                f"rops: --var {pair!r} has no `=` — the runner ignores a malformed "
                f"pair, so the run would proceed as if it were set")
        out += ["--variable", sq(pair)]
    return out


def local_command(cfg, *, jobs: list[str] | None = None, file: str = "",
                  privileged: bool = False, concurrency: int = 0,
                  variables_file: str = "", variables: list[str] | None = None,
                  extra: str = "") -> str:
    """One line, no pipe. The rc reaches rops through the wrapper, not through a tail."""
    version = cfg.get("ROPS_GITLAB_CLI_VERSION") or DEFAULT_CLI_VERSION
    parts = ["npx", "--yes", f"gitlab-ci-local@{version}"]
    if ca := cfg.get("ROPS_GITLAB_CA_FILE"):
        # Only when declared: a path that does not exist on the far side fails in a
        # way that reads exactly like a certificate problem.
        parts += ["--ca-file", ca]
    if file:
        parts += ["--file", sq(file)]
    if variables_file:
        parts += ["--variables-file", sq(variables_file)]
    if privileged:
        parts.append("--privileged")
    if concurrency > 0:
        parts += ["--concurrency", str(concurrency)]
    parts += variable_args(variables or [])
    if extra:
        parts.append(extra)
    # Job names carry spaces and brackets (`test:all: [1/6]`) -- one argument each.
    parts += [sq(j) for j in (jobs or [])]
    return " ".join(parts)


def preflight(cfg) -> str:
    """Run before the pipeline: repair what the local checkout is missing.

    `|| true` because a repo with no `origin` is legitimate -- the repair must not
    become the failure.
    """
    return "git remote set-head origin -a >/dev/null 2>&1 || true"


@dataclass(frozen=True)
class Outcome:
    ok: bool
    rc: int
    ran: int
    expected: int | None
    reason: str = ""


def classify(*, rc: int, log: str, expected: int | None) -> Outcome:
    """Did this run actually execute the work, or merely exit?

    `expected=None` means nobody counted the jobs first. That is UNVERIFIED, never a
    pass: reporting green on a run whose scope was never established is the same claim
    as reporting green on a pipeline that ran nothing.
    """
    ran = jobs_finished(log)
    if expected is None:
        return Outcome(False, rc, ran, None,
                       f"UNVERIFIED: {ran} job(s) ran but the expected count was never "
                       f"established -- list the jobs first, then compare")
    if ran < expected:
        return Outcome(False, rc, ran, expected,
                       f"only {ran} of {expected} job(s) ran -- an exit code alone "
                       f"cannot tell a passing pipeline from one that ran nothing")
    if rc != 0:
        return Outcome(False, rc, ran, expected, f"exit {rc} after {ran} job(s)")
    return Outcome(True, rc, ran, expected, f"{ran} of {expected} job(s) ran")


#: (signature in the output, what to actually do about it). Measured signatures only:
#: a confident wrong hint is worse than none, because it sends the reader somewhere
#: the problem is not.
_RESOLVE_HINTS = (
    ("the remote end hung up",
     "an `include:` could not be fetched — `include: project:` is pulled over git, "
     "so the machine resolving it needs BOTH credentials for that GitLab and a "
     "network route to it. Measured 2026-09-10: document-intake resolves on the work "
     "machine and not on the remote, which cannot reach the internal GitLab at all."),
    ("Local include file cannot be found",
     "an `include: local:` file is not in the COMMITTED tree — that include is read "
     "from git, not from disk, so an uncommitted include file is invisible to it. "
     "Commit it (or `hs-rops lane` after committing)."),
    ("Could not find any Kubernetes",
     "the file asks for a Kubernetes executor, which this lane does not have."),
)


def resolve_failure_hint(output: str) -> str:
    """What to DO about a pipeline that would not resolve, or "" when unrecognised."""
    for needle, hint in _RESOLVE_HINTS:
        if needle in output:
            return hint
    return ""
