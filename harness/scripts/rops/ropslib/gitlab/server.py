"""The heavy lane: a real GitLab and a registered runner.

This lane exists because of one measurement, made 2026-09-10 on GitLab CE 19.3.1 with
a docker-executor runner registered against it:

    retry: 2   ->   THREE job records (ids 1, 3, 4)

The same file on gitlab-ci-local 4.75.1 produced one run and no warning. Everything
the light lane cannot honour -- retry, timeout, a real queue with tag matching,
protected branches, environments, server credentials, and a git CHECKOUT rather than
an rsync of files -- is real here. The price is a push: the code has to reach the
server before it can run, so this is the lane you escalate to, not the one you live in.

Two counting rules are load-bearing:

  * `include_retried=true` or the retry count is unmeasurable. Without it the API
    returns the last attempt only, and a job that retried twice reads exactly like a
    job that ran once.
  * an unknown pipeline state is NOT terminal. Guessing `finished` on a state nobody
    anticipated ends the wait early and reports whatever the incomplete listing held.
"""
from __future__ import annotations

from collections import Counter

#: States a pipeline stops in. A poll loop waiting for `success` alone hangs forever
#: on a failed run, and a monitor that reports only the happy path is silent through
#: a crash -- so every one of these ends the wait, and only `success` passes.
TERMINAL = frozenset({"success", "failed", "canceled", "skipped", "manual"})


def api(base: str) -> str:
    return f"{base.rstrip('/')}/api/v4"


def pipelines_url(base: str, project_id: int) -> str:
    return f"{api(base)}/projects/{project_id}/pipelines"


def jobs_url(base: str, project_id: int, pipeline_id: int | None = None) -> str:
    """`include_retried=true` is not optional: see the module docstring.

    Scope it to ONE pipeline whenever the question is about a run. Measured
    2026-09-10: a push created a pipeline and `trigger` created a second at the same
    SHA, and the project-wide count reported `flaky x6` for a job whose `retry: 2`
    caps it at three attempts. A count above the configured maximum is not a
    measurement, it is two runs added together.
    """
    where = (f"/projects/{project_id}/pipelines/{pipeline_id}/jobs" if pipeline_id
             else f"/projects/{project_id}/jobs")
    return f"{api(base)}{where}?include_retried=true&per_page=100"


def is_terminal(state: str) -> bool:
    return state in TERMINAL


def passed(state: str) -> bool:
    return state == "success"


def attempts(records: list) -> dict[str, int]:
    """job name -> how many times it RAN.

    A parallel cell counts under its own cell name (`test: [1/2]`): two cells are two
    jobs, not two attempts at one.
    """
    return dict(Counter(r["name"] for r in records
                        if isinstance(r, dict) and r.get("name")))
