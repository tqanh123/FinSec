"""gitlab — the heavy lane: a real GitLab and a registered runner, on the remote.

    hs-rops gitlab up          stand the server up and wait for it to answer
    hs-rops gitlab runner      register a docker-executor runner against it
    hs-rops gitlab project     create (or find) the project a push will target
    hs-rops gitlab push        push this tree to that project
    hs-rops gitlab trigger     start a pipeline and wait for it to reach a terminal state
    hs-rops gitlab pipeline    the latest pipeline's state
    hs-rops gitlab jobs        one pipeline's job records, retried attempts INCLUDED
    hs-rops gitlab log         one job's trace
    hs-rops gitlab down        stop the server and the runner

One verb with subcommands rather than nine files: they all address the same thing --
one server, one runner -- and nine files would hold nine copies of "where is the
server". A new SUBJECT (a different remote system) is still a new file.

This lane is what `hs-rops ci` escalates to. The light lane cannot execute `retry:`,
cannot enforce `timeout:`, has no queue and no credentials, and hands the job an
rsynced directory rather than a git checkout. Measured 2026-09-10 on CE 19.3.1: a
`retry: 2` job produced three job records here and one over there.

The price is a push. Nothing on this lane runs on your uncommitted tree.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time

from hs_family import UsageError
from rops.ropslib import hosts
from rops.ropslib.gitlab import config as gc
from rops.ropslib.gitlab import reader
from rops.ropslib.gitlab import server as srv
from rops.ropslib.prog import PROG
from rops.ropslib.transport import sq


#: `retry` is deliberately absent. It names a CAPABILITY the light lane cannot
#: honour, and a verb of the same name would read as "make retry work" -- the one
#: claim this gate exists to prevent. The action that re-runs a pipeline is `rerun`.
#: `monitor` is absent too: watching is a switch on `pipeline`, because two doors for
#: one question is where two answers start to disagree.
ACTIONS = ("status", "up", "runner", "project", "push", "trigger",
           "pipeline", "jobs", "log", "rerun", "graph", "lint", "report",
           "artifacts", "var", "config", "down")

RC_OK, RC_FAILED, RC_BROKEN = 0, 1, 2

DEFAULT_DIR = "~/rops/gitlab"
DEFAULT_PORT = 8929
DEFAULT_HOSTNAME = "gitlab.local"

#: ONE spelling. Measured: `status` inspected a name typed separately from the one
#: `up` creates, and reported a running server as `not created`.
SERVER = "rops-gitlab"

#: Measured 2026-09-10 on a 20-core / 62 GiB host: healthy 73 s after `compose up`.
#: The budget is several times that because a first run also pulls ~3 GiB of image.
BOOT_TIMEOUT_S = 1200
POLL_S = 10

#: `grafana[...]` was in the first draft of this file and CE 19.3.1 rejects it --
#: `Mixlib::Config::UnknownConfigOptionError: Reading unsupported config value
#: grafana.` The container exited 6 seconds in, which reads exactly like a slow boot
#: to anything that only watches for `healthy`. Keep this list to keys CE still knows.
COMPOSE = """services:
  gitlab:
    image: gitlab/gitlab-ce:latest
    container_name: {name}
    hostname: {hostname}
    shm_size: 1g
    environment:
      GITLAB_OMNIBUS_CONFIG: |
        external_url "http://{hostname}:{port}"
        gitlab_rails["initial_root_password"] = "{password}"
        prometheus_monitoring["enable"] = false
        alertmanager["enable"] = false
        puma["worker_processes"] = 2
        sidekiq["max_concurrency"] = 5
    ports:
      - "{port}:{port}"
"""


def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=ACTIONS, nargs="?", default="status")
    p.add_argument("--dir", default="", help=f"remote working dir (default {DEFAULT_DIR})")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--project", default="rops", help="project path under root/")
    p.add_argument("--ref", default="HEAD", help="local ref to push")
    p.add_argument("--branch", default="main", help="branch to push it as")
    p.add_argument("--job", default="", help="job name or id, for `log`")
    # NEVER a default: a credential with one reaches a machine nobody meant to name.
    p.add_argument("--token", default="", help="API token (or ROPS_GITLAB_TOKEN)")
    p.add_argument("--timeout", type=int, default=BOOT_TIMEOUT_S)
    p.add_argument("--yes", action="store_true", help="skip the confirmation on `down`")
    # Never the default. Measured: the server refused a force push to a protected
    # branch and was right to; onto an unprotected one the same command discards
    # whatever was there without asking.
    p.add_argument("--force", action="store_true",
                   help="overwrite the remote branch (discards what is there)")
    p.add_argument("--watch", action="store_true",
                   help="`pipeline`: stay until it reaches a terminal state")
    p.add_argument("--pipeline", type=int, default=0,
                   help="act on this pipeline id instead of the latest")
    p.add_argument("--file", default=".gitlab-ci.yml", help="`lint`: which CI file")
    p.add_argument("--dest", default="", help="`artifacts`: where to put them")
    p.add_argument("--list", action="store_true",
                   help="`runner`: show the queue instead of registering one")
    # A CI lane allocates on demand and leaves nothing behind. Warm boot measured at
    # 66s against 73s cold, so the cost of a clean server is ~7s: the time goes into
    # GitLab starting, not into pulling the image. Reuse stays available for
    # iterating, as the explicit choice.
    p.add_argument("--reuse", action="store_true",
                   help="`up`: keep an existing server and its data instead of a fresh one")
    p.add_argument("--purge", action="store_true",
                   help="`down`: drop the volume too (every project and pipeline)")


def verdict(state: str, attempts: dict) -> tuple[int, str]:
    """A pipeline's state PLUS what it actually ran.

    A green state with zero job records is the undefined-anchor shape: the pipeline
    exited without executing anything. The attempt counts are reported as numbers
    rather than as a `retry worked` boolean, because the number is the evidence.
    """
    if not attempts:
        return RC_BROKEN, f"{state}, but no job ran -- nothing was executed"
    ran = ", ".join(f"{name} x{n}" for name, n in sorted(attempts.items()))
    if srv.passed(state):
        return RC_OK, f"success ({ran})" if any(n > 1 for n in attempts.values()) else "success"
    return RC_FAILED, f"{state} ({ran})"


# --------------------------------------------------------------------------- helpers

def _dir(args) -> str:
    return args.dir or args.cfg.get("ROPS_GITLAB_DIR") or DEFAULT_DIR


def _port(args) -> int:
    return args.port or int(args.cfg.get("ROPS_GITLAB_PORT") or DEFAULT_PORT)


def _base(args) -> str:
    return f"http://localhost:{_port(args)}"


def _token(args) -> str:
    tok = args.token or args.cfg.get("ROPS_GITLAB_TOKEN")
    if not tok:
        raise UsageError(
            f"{PROG} gitlab: no API token. Pass --token, or declare ROPS_GITLAB_TOKEN.\n"
            f"      `{PROG} gitlab up` prints the one it creates.")
    return tok


def _curl_url(args, host, url: str, *, method: str = "GET", data: str = "") -> str:
    """One API call to an ALREADY-BUILT url, run ON the remote.

    The door for a call site that has one of the `server.py` URL shapes
    (`srv.jobs_url`, `srv.pipelines_url`) in hand rather than a bare path -- those
    functions already prepend `srv.api(base)`, so routing them through `_curl`
    instead would double it.
    """
    bits = ["curl", "-s", "-H", sq(f"PRIVATE-TOKEN: {_token(args)}")]
    if method != "GET":
        bits += ["-X", method]
    if data:
        bits += ["-d", sq(data)]
    bits.append(sq(url))
    return host.out(" ".join(bits), timeout=120)


def _curl(args, host, path: str, *, method: str = "GET", data: str = "") -> str:
    """One API call, run ON the remote: the server listens on its loopback."""
    return _curl_url(args, host, f"{srv.api(_base(args))}{path}",
                     method=method, data=data)


def _json(raw: str, what: str):
    """Parse an API answer, refusing an ERROR payload here rather than at its use.

    Measured 2026-09-10: an absent project made the URL `/projects/None/pipelines`,
    the server answered `{"message":"404 Project Not Found"}`, and because a dict is
    truthy the caller's `if pipes` passed and `pipes[0]` raised `KeyError: 0` -- a
    traceback naming neither the project nor the 404. The server's own words are the
    useful message, so they travel.
    """
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError(f"{PROG} gitlab: {what} did not parse: {exc}\n{raw[:300]}")
    if isinstance(doc, dict) and ("message" in doc or "error" in doc) \
            and "id" not in doc:
        said = doc.get("message") or doc.get("error")
        raise UsageError(f"{PROG} gitlab: {what}: the server said {said!r}")
    return doc


def run(args: argparse.Namespace) -> int:
    host = hosts.gate(args, need_dir=False)[0]
    action = args.action
    fn = globals().get(f"_do_{action}")
    if fn is None:                      # pragma: no cover - argparse already refuses
        raise UsageError(f"{PROG} gitlab: no such action {action!r}")
    return fn(args, host)


# ------------------------------------------------------------------------- actions

def _do_status(args, host) -> int:
    state = host.out(f"docker inspect -f '{{{{.State.Status}}}} {{{{.State.Health.Status}}}}' {SERVER} 2>/dev/null")
    # The fleet is found by the LABEL every rops runner carries, not by a name. Names
    # are per runner and per executor (`rops-docker-1`, ...); measured 2026-09-10, a
    # single hard-coded name reported `not registered` while two runners were up and
    # online, which reads to the next step as "register again".
    fleet = [n for n in host.out(
        "docker ps --filter label=rops.fleet=gitlab "
        "--format '{{.Names}}' 2>/dev/null").split() if n]
    print(f"server  {state or 'not created'}   ({_base(args)} on {host.name})")
    print(f"runner  {f'{len(fleet)} up: ' + ', '.join(sorted(fleet)) if fleet else 'not registered'}")
    return RC_OK if state.startswith("running") else RC_BROKEN


def up_summary(*, reused: bool, seconds: int) -> str:
    """Say WHICH server the caller got. "ready" alone leaves them unable to tell a
    clean one from one carrying an earlier run's projects."""
    if reused:
        return f"reused the running server ({seconds}s) — it still holds earlier runs"
    return f"fresh server ready in {seconds}s"


def api_ready_from(raw: str) -> bool:
    """Is this body an answer from the API, or the sound of a door still shut?

    Measured shapes on a booting CE 19.3.1: '' (nothing listening yet), an HTML 502
    from the bundled nginx, and -- once Rails serves -- JSON. An HTML page is the
    misleading one: it is a non-empty 200-ish response that would satisfy any check
    asking only "did something come back".
    """
    body = (raw or "").strip()
    if not body or body[0] not in "[{":
        return False
    try:
        json.loads(body)
    except json.JSONDecodeError:
        return False
    return True


def _api_ready(args, host) -> bool:
    """Knock on the door the NEXT step will use.

    `/version` needs a token; `/api/v4/projects` on a fresh server answers `[]`
    without one, so this stays usable in the window before the token is minted.
    """
    raw = host.out(
        f"curl -s --max-time 10 {sq(srv.api(_base(args)) + '/projects?per_page=1')} "
        f"2>/dev/null", timeout=60)
    return api_ready_from(raw)


def _wait_for_api(args, host, *, deadline: float, poll: float = POLL_S) -> bool:
    """Block until the API answers, or the deadline passes. Says which.

    `poll` is a seam: a test asserting that the wait RE-PROBES must not buy that
    assertion with wall-clock seconds, or the suite pays POLL_S for every case.
    """
    while time.time() < deadline:
        if _api_ready(args, host):
            return True
        time.sleep(poll)
    print("!! the container is healthy but the API is not answering", file=sys.stderr)
    return False


def _do_up(args, host) -> int:
    """Stand the server up and WAIT for it to answer.

    The wait watches every terminal state, not just `healthy`. Measured: a bad
    omnibus key made the container exit six seconds in, and a loop that only looked
    for `healthy` read that as a slow boot for the whole of its budget.
    """
    d, port = _dir(args), _port(args)
    password = args.cfg.get("ROPS_GITLAB_PASSWORD") or "ropsropsrops123"
    started_at = time.time()

    running = host.out(
        f"docker inspect -f '{{{{.State.Status}}}} {{{{.State.Health.Status}}}}' "
        f"{SERVER} 2>/dev/null")
    if running.startswith("running") and "healthy" in running:
        if args.reuse:
            # Reuse KEEPS it. An API that is not answering yet is a reason to wait,
            # never a reason to destroy the server the caller asked to keep.
            _wait_for_api(args, host, deadline=time.time() + args.timeout,
                          poll=getattr(args, "poll", POLL_S))
            print(up_summary(reused=True, seconds=int(time.time() - started_at)))
            return _mint_token(args, host)
        # Allocate on demand, clean each run: a server carrying an earlier run's
        # projects makes every count ambiguous, and the measured price of starting
        # over is ~7s.
        print(f"a server is already running — replacing it for a clean run "
              f"(pass --reuse to keep it and its data)")
        _teardown(args, host, purge=True)
    compose = COMPOSE.format(name=SERVER, hostname=DEFAULT_HOSTNAME,
                             port=port, password=password)
    host.run(f"mkdir -p {d}")
    host.run(f"cat > {d}/compose.yml <<'ROPSEOF'\n{compose}ROPSEOF")
    rc = host.run(f"cd {d} && docker compose up -d", timeout=600)
    if rc.returncode:
        print((rc.stderr or rc.stdout).strip()[:500], file=sys.stderr)
        return RC_BROKEN

    started = time.time()
    while time.time() - started < args.timeout:
        status = host.out(f"docker inspect -f '{{{{.State.Status}}}}' {SERVER} 2>/dev/null")
        health = host.out(f"docker inspect -f '{{{{.State.Health.Status}}}}' {SERVER} 2>/dev/null")
        # `healthy` is the IMAGE's own opinion, and `gitlab-rails runner` is a
        # console inside the container -- neither touches HTTP. Measured: both said
        # yes, `up` printed `ready in 61s`, and one second later `POST /user/runners`
        # returned an EMPTY body. The same command minutes later returned HTTP 201.
        # So the wait continues until the API itself answers.
        if health == "healthy" and _api_ready(args, host):
            print(up_summary(reused=False, seconds=int(time.time() - started))
                  + f" on {host.name} ({_base(args)})")
            return _mint_token(args, host)
        if status in ("exited", "dead"):
            log = host.out(f"docker logs {SERVER} 2>&1 | grep -iE 'FATAL|ERROR' | tail -3")
            print(f"!! the server exited after {int(time.time() - started)}s\n{log}",
                  file=sys.stderr)
            return RC_BROKEN
        time.sleep(POLL_S)
    print(f"!! still not healthy after {args.timeout}s", file=sys.stderr)
    return RC_BROKEN


#: Rails writes deprecation notices and warnings around the output, so the marker is
#: NOT reliably the last line -- reading only the tail is how a successful mint gets
#: reported as a failure.
MINT_MARK = "TOKEN_OK"


def mint_script(token: str) -> str:
    """Create the API token, replacing any token of the same name.

    `create!` on an existing name raises `Name has already been taken`. Measured
    2026-09-10: a cold-started server minted fine by hand and then failed from the
    verb, because the by-hand run had already taken the name. A purged volume leaves
    the name free and a reused one does not -- and reuse is a supported path, so this
    has to be idempotent rather than lucky.
    """
    return (
        "u = User.find_by(username: %q{root}); "
        "u.personal_access_tokens.where(name: %q{rops}).destroy_all; "
        "t = u.personal_access_tokens.create!(scopes: [:api], name: %q{rops}, "
        "expires_at: 30.days.from_now); "
        f"t.set_token(%q{{{token}}}); t.save!; puts %q{{{MINT_MARK}}}")


def mint_ok(output: str) -> bool:
    return MINT_MARK in output


def mint_failure(output: str) -> str:
    """The line that says what broke, out of a Ruby stack trace.

    `gitlab-rails runner` prints the message first and the trace after it. Measured
    2026-09-10: the verb took `tail -1` and reported
    `could not mint an API token: from bin/rails:4:in <main>` -- a file name and
    nothing about the cause, while the real error sat two lines above it.
    """
    for line in output.splitlines():
        s = line.strip()
        if not s or s == "rails aborted!":
            continue
        if s.startswith(("from ", "/opt/", "bin/rails")):
            continue
        return s
    return output.strip()[:200] or "no output"


def _mint_token(args, host) -> int:
    """A token the caller can actually use. Printed, never stored by rops."""
    tok = args.token or args.cfg.get("ROPS_GITLAB_TOKEN") or "ropstoken1234567890ab"
    # The WHOLE output, not a tail: the marker is not reliably the last line, and the
    # error never is.
    out = host.out(
        f"docker exec {SERVER} gitlab-rails runner {sq(mint_script(tok))} 2>&1",
        timeout=300)
    if not mint_ok(out):
        print(f"!! could not mint an API token: {mint_failure(out)}", file=sys.stderr)
        return RC_BROKEN
    print(f"token   {tok}   (declare it as ROPS_GITLAB_TOKEN)")
    return RC_OK


#: How many runners the fleet has by default.
#:
#: ONE runner serialises everything: a `parallel: 6` test job runs its six shards one
#: after another, and a pipeline whose stages could overlap does not. Ten is the
#: default because the measured host has 20 cores, and it is CONFIGURATION because the
#: right number is a property of the machine rather than of this file.
DEFAULT_RUNNERS = 10

#: Executors gitlab-runner accepts here. An unknown name fails at `register` with a
#: message about the executor, several containers into a fleet build -- cheaper to
#: refuse before the first one.
KNOWN_EXECUTORS = ("docker", "shell", "docker+machine", "ssh", "parallels",
                   "virtualbox", "kubernetes")
DEFAULT_EXECUTORS = ("docker",)


def runner_count(cfg) -> int:
    raw = str(cfg.get("ROPS_GITLAB_RUNNERS") or DEFAULT_RUNNERS).strip()
    if not raw.lstrip("-").isdigit():
        raise UsageError(f"{PROG} gitlab: ROPS_GITLAB_RUNNERS={raw!r} is not a number "
                         f"— falling back to a default would give you a fleet you "
                         f"did not ask for and say nothing")
    n = int(raw)
    if n < 1:
        raise UsageError(f"{PROG} gitlab: ROPS_GITLAB_RUNNERS={n} — with no runner every "
                         f"job queues forever. Quietly making it 1 would override a "
                         f"decision you made.")
    return n


def executors(cfg) -> list[str]:
    named = cfg.csv("ROPS_GITLAB_EXECUTORS") or list(DEFAULT_EXECUTORS)
    unknown = [e for e in named if e not in KNOWN_EXECUTORS]
    if unknown:
        raise UsageError(
            f"{PROG} gitlab: unknown executor(s): {', '.join(unknown)}\n"
            f"      known: {', '.join(KNOWN_EXECUTORS)}")
    return named


def runner_tags(executor: str) -> list[str]:
    """What a job writes in `tags:` to reach this runner.

    A fleet of untagged runners cannot honour `tags:` at all, which is the difference
    between running a job and running it on the wrong kind of executor.
    """
    return [executor, "rops"]


def runner_plan(count: int, execs: list[str]) -> list[tuple[str, str]]:
    """(name, executor) per runner, spread round-robin.

    Round-robin rather than "count of the first": four runners all on docker gives
    zero parallelism for a shell job. The remainder lands on the first executor,
    which is the one the caller named first.

    Every name is distinct because gitlab-runner keys its config by name -- two
    registrations under one name leave ONE runner, the pipeline serialises, and the
    tool reports the number it asked for.
    """
    return [(f"rops-{execs[i % len(execs)]}-{i + 1}", execs[i % len(execs)])
            for i in range(count)]


def runner_rows(records: list) -> list[str]:
    """One line per runner, OFFLINE ONES INCLUDED.

    An offline runner is why a job sits queued forever; listing only the online ones
    answers "who could take this" with a blank exactly where the cause was. Tags are
    spelled out because they decide who is even allowed to take a job -- and a runner
    with none says `untagged`, which is a different thing from having no tag shown.
    """
    rows = []
    for r in records:
        state = r.get("status", "?")
        # Only when the status does not already say it: GitLab reports
        # `status: paused` for a paused runner, and appending the marker printed
        # `paused (paused)`. An older GitLab reporting `online` + `paused: true`
        # still needs the marker, so the flag is not simply ignored.
        if r.get("paused") and "paused" not in state:
            state += " (paused)"
        # An ABSENT `tag_list` is not an empty one. Measured 2026-09-10:
        # `/runners/all` omits the field entirely while `/runners/:id` returns
        # `['docker', 'rops']` for the same runner, so printing `untagged` for the
        # whole fleet said "no tagged job can reach any of these" -- the opposite of
        # what was true.
        if "tag_list" not in r:
            tags = "tags not listed (ask /runners/:id)"
        else:
            tags = ", ".join(r.get("tag_list") or []) or "untagged"
        rows.append(f"  {r.get('id','?'):<4} {str(r.get('description') or '?'):<22} "
                    f"{state:<18} {r.get('job_execution_status','?'):<10} {tags}")
    return rows


def runner_verdict(records: list) -> tuple[int, str]:
    """Could ANY runner take a job right now?

    Zero usable runners means every job queues forever, and reporting that as fine
    is the same silence as a pipeline that ran nothing. Paused counts as unusable:
    the runner is there and will not pick anything up.
    """
    if not records:
        return RC_BROKEN, "no runner is registered — every job would queue forever"
    usable = [r for r in records if r.get("online") and not r.get("paused")]
    if not usable:
        return RC_BROKEN, (f"{len(records)} runner(s) registered but none online and "
                           f"unpaused — every job would queue forever")
    return RC_OK, f"{len(usable)} of {len(records)} runner(s) can take a job"


def _do_runner(args, host) -> int:
    """Register a docker-executor runner, or with --list, show the queue.

    The queue is the surface the light lane cannot represent at all: its
    `--concurrency` is a parallel ceiling inside one process, not runners competing
    for tagged jobs.
    """
    if args.list:
        records = _json(_curl(args, host, "/runners/all?per_page=100"), "the runners")
        for row in runner_rows(records):
            print(row)
        rc, reason = runner_verdict(records)
        print(f"\n{reason}")
        return rc
    return _register_runner(args, host)


def _register_runner(args, host) -> int:
    """Register the FLEET: N runners across the configured executors.

    One runner serialises everything -- a `parallel: 6` job runs its six shards one
    after another, and stages that could overlap do not. The count and the executor
    mix are configuration (ROPS_GITLAB_RUNNERS, default 10; ROPS_GITLAB_EXECUTORS,
    default docker) because the right numbers are properties of the machine.

    Each runner is its own container with its own config dir. gitlab-runner keys its
    config by name, so sharing one dir would leave the LAST registration standing
    while the tool reported N -- the pipeline would serialise and nothing would say so.

    Every runner is registered on the SERVER's network with the server's IP as a host
    alias: the runner talks to the external_url, which resolves to nothing otherwise.
    """
    net = host.out("docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' " + SERVER)
    ip = host.out("docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' " + SERVER)
    if not net or not ip:
        print(f"!! the server is not running — `{PROG} gitlab up` first", file=sys.stderr)
        return RC_BROKEN

    knobs = gc.resolve(args.cfg)
    declared = {k: v.value for k, v in knobs.items()
                if v.source == gc.SRC_ENV and k in gc.REGISTER_FLAGS}
    for warn in gc.enforcement_warnings(declared):
        print(f"!! {warn}", file=sys.stderr)
    plan = runner_plan(runner_count(args.cfg), executors(args.cfg))
    d = _dir(args)
    print(f"fleet: {len(plan)} runner(s) across {', '.join(sorted({e for _, e in plan}))}")
    # Remove the whole fleet first, by the label every rops runner carries: matching
    # on a NAME PREFIX would leave a runner from a previous, larger fleet running and
    # taking jobs nobody is accounting for.
    host.run(f"docker ps -aq --filter label=rops.fleet=gitlab | xargs -r docker rm -f "
             f">/dev/null 2>&1; true", timeout=300)
    # And the config dirs with them. `register` APPENDS a `[[runners]]` block, and
    # these dirs are host mounts that outlive the container -- measured, three builds
    # left three blocks in one file and the runner kept serving the first, which
    # predated every limit set afterwards. The containers run as root, so the removal
    # goes through a container too.
    host.run(f"docker run --rm -v {d}/runners:/r alpine:3.20 "
             f"sh -c 'rm -rf /r/* /r/.[!.]* 2>/dev/null; true' >/dev/null 2>&1; true",
             timeout=300)

    made = 0
    for name, executor in plan:
        raw = _curl(args, host, "/user/runners", method="POST",
                    data=f"runner_type=instance_type&description={name}"
                         f"&run_untagged=true&tag_list={','.join(runner_tags(executor))}")
        tok = _json(raw, "the runner registration").get("token")
        if not tok:
            print(f"!! {name}: no runner token came back: {raw[:200]}", file=sys.stderr)
            continue
        cdir = f"{d}/runners/{name}"
        host.run(f"mkdir -p {cdir}")
        host.run(
            f"docker run -d --name {sq(name)} --label rops.fleet=gitlab "
            f"--network {sq(net)} "
            f"-v /var/run/docker.sock:/var/run/docker.sock "
            f"-v {cdir}:/etc/gitlab-runner "
            f"--add-host {DEFAULT_HOSTNAME}:{ip} gitlab/gitlab-runner:latest",
            timeout=300)
        extra = (f"--docker-image alpine:3.20 --docker-network-mode {sq(net)} "
                 f"--docker-volumes /var/run/docker.sock:/var/run/docker.sock"
                 if executor == "docker" else "")
        reg = host.run(
            f"docker exec {sq(name)} gitlab-runner register --non-interactive "
            f"--url http://{DEFAULT_HOSTNAME}:{_port(args)} --token {sq(tok)} "
            f"--name {sq(name)} --executor {sq(executor)} {extra} "
            + " ".join(sq(a) for a in gc.register_args(declared, executor)),
            timeout=300)
        blob = (reg.stdout or "") + (reg.stderr or "")
        if "registered successfully" in blob:
            # `concurrent` is unreachable through register (measured: 0 of 353 flags),
            # so the value has to be written into the global section of config.toml
            # and the runner restarted to read it. Skipping this leaves a fleet of N
            # runners each taking ONE job -- which is what the first fleet did.
            want = knobs["concurrent"].value
            if want and int(want) != 1:
                host.run(
                    f"docker exec {sq(name)} sed -i "
                    f"{sq(f's/^concurrent = .*/concurrent = {int(want)}/')} "
                    f"/etc/gitlab-runner/config.toml && docker restart {sq(name)} "
                    f">/dev/null", timeout=180)
            made += 1
        else:
            print(f"!! {name} ({executor}): {blob.strip()[:300]}", file=sys.stderr)

    print(f"{made} of {len(plan)} runner(s) registered")
    # A partial fleet is not the fleet that was asked for: the pipeline runs with
    # less parallelism than the caller sized it for, and reporting OK hides that.
    return RC_OK if made == len(plan) else RC_BROKEN


def _do_project(args, host) -> int:
    raw = _curl(args, host, f"/projects?search={args.project}")
    for p in _json(raw, "the project search"):
        if p.get("path") == args.project:
            print(f"project {p['id']}  {p['path_with_namespace']}")
            return RC_OK
    # Created EMPTY on purpose. `initialize_with_readme=true` puts an `Initial
    # commit` on the default branch, and the next step of rops' own cycle -- `push
    # --branch main` -- is then a non-fast-forward that the honest default refuses.
    # Measured 2026-09-10 on a cold start: the two steps disagreed with each other.
    raw = _curl(args, host, f"/projects?name={args.project}&path={args.project}"
                            f"&visibility=private",
                method="POST")
    p = _json(raw, "the project creation")
    if not p.get("id"):
        print(f"!! could not create the project: {raw[:300]}", file=sys.stderr)
        return RC_BROKEN
    print(f"project {p['id']}  {p['path_with_namespace']} (created)")
    return RC_OK


def _project_id(args, host) -> int | None:
    raw = _curl(args, host, f"/projects?search={args.project}")
    for p in _json(raw, "the project search"):
        if p.get("path") == args.project:
            return p["id"]
    return None


#: A refusal that came from a pre-push hook on the machine rather than from GitLab.
#: Measured 2026-09-10 by reading the hook on homelab -- these are ITS words, not a
#: guess at them. The first version of this pattern was invented (`prohibited
#: patterns`, `[ERROR] Matched`) and matched 0 lines of the real 2.6 MB body.
_HOOK_MARK = re.compile(
    r"GIT OPERATION BLOCKED|Policy version:|secrets\.allowed|git-secrets|"
    r"pre-push hook|hook declined", re.IGNORECASE)

#: Lines worth keeping wherever they sit. A scan report is thousands of match lines
#: with a verdict buried among them, so a fixed head+tail window walks straight past
#: the one line a person acts on -- measured, the banner sits ~14 lines from the end
#: and a 6-line tail missed it.
#: git writes its own verdict at the START of a line, or right after `remote: `.
#: Measured 2026-09-10 on the live refusal: a bare `error:` alternative matched
#: INSIDE the word `UnicodeDecodeError:` in the CONTENT of a scanned JSONL line, and
#: `[:200]` then trimmed it into something that read like an ordinary path line. Seven
#: scan hits were relayed as verdicts. Scanned content is data, never a verdict.
_PUSH_SIGNAL = re.compile(
    r"^(?:remote:\s*)?(?:error|fatal):|"
    r"GIT OPERATION BLOCKED|Policy version:|remote rejected|"
    r"\[remote rejected\]|protected branch|non-fast-forward|secrets\.allowed|"
    r"contact Security|hook declined", re.IGNORECASE)

_PUSH_HEAD_LINES, _PUSH_MAX_SIGNAL = 3, 12


def push_failure(out: str) -> str:
    """The useful parts of a push failure: a sample of the noise, then the SIGNAL.

    Keyed on meaning rather than on position. The head is kept so the caller can see
    what kind of thing matched; everything else is kept only if it says something.
    """
    lines = [l for l in out.splitlines() if l.strip()]
    head = lines[:_PUSH_HEAD_LINES]
    rest = lines[_PUSH_HEAD_LINES:]
    signal = [l for l in rest if _PUSH_SIGNAL.search(l)][-_PUSH_MAX_SIGNAL:]
    if not rest:
        return "\n".join(l[:200] for l in head)
    cut = len(rest) - len(signal)
    out_lines = [l[:200] for l in head]
    if cut > 0:
        out_lines.append(f"   ... {cut} more line(s) ...")
    out_lines += [l[:200] for l in signal]
    return "\n".join(out_lines)


def _do_push(args, host) -> int:
    pid = _project_id(args, host)
    if pid is None:
        print(f"!! no such project — `{PROG} gitlab project` first", file=sys.stderr)
        return RC_BROKEN
    url = (f"http://root:{_token(args)}@localhost:{_port(args)}"
           f"/root/{args.project}.git")
    remote_dir = args.cfg.get("ROPS_REMOTE_DIR", "~")
    force = "--force " if args.force else ""
    r = host.run(f"cd {remote_dir} && git push -q {force}{sq(url)} "
                 f"{sq(args.ref)}:refs/heads/{sq(args.branch)} 2>&1", timeout=600)
    if r.returncode:
        out = (r.stdout or r.stderr).strip()
        # A pre-push scanner reports every MATCH and then its verdict, so the head of
        # the body is the least informative part of it. Measured 2026-09-10: rops
        # printed 400 characters of a 2.6 MB git-secrets report -- 400 characters of
        # match lines, and nothing about what refused.
        print(push_failure(out), file=sys.stderr)
        if _HOOK_MARK.search(out):
            print("   a LOCAL git hook refused this push, not the server. On that "
                  "machine `git config --show-origin --get core.hooksPath` names the "
                  "file that installs it. rops does not disable it: it is a secret "
                  "scanner, and the hits are the thing to look at.", file=sys.stderr)
        elif "protected branch" in out:
            print("   the branch is protected on the server — push to another branch "
                  "(--branch), or unprotect it there. This is a permission the light "
                  "lane cannot even represent.", file=sys.stderr)
        elif "non-fast-forward" in out or "rejected" in out:
            print("   the remote has commits this push would drop — pass --force only "
                  "if discarding them is what you mean.", file=sys.stderr)
        return RC_BROKEN
    print(f"pushed {args.ref} -> {args.project}:{args.branch}")
    return RC_OK


def _do_pipeline(args, host) -> int:
    """The pipeline's state, and with `--watch`, its state when it STOPS.

    Watching is a switch here rather than a `monitor` verb: two doors onto one
    question is where two answers start to disagree. The wait ends on every terminal
    state, not just success -- a loop waiting for good news is silent through a
    failure, which is indistinguishable from still running.
    """
    pid = _project_id(args, host)
    if pid is None:
        print(f"no project {args.project!r} on the server — "
              f"`{PROG} gitlab project --project {args.project}` first")
        return RC_BROKEN
    run_id = args.pipeline or _latest_pipeline(args, host)
    if not run_id:
        print("no pipeline yet")
        return RC_BROKEN
    started = time.time()
    while True:
        p = _json(_curl(args, host, f"/projects/{pid}/pipelines/{run_id}"),
                  "the pipeline")
        state = p.get("status", "unknown")
        if not args.watch or srv.is_terminal(state):
            break
        if time.time() - started > args.timeout:
            print(f"pipeline {run_id}  {state}  (still running after {args.timeout}s)")
            return RC_BROKEN
        time.sleep(POLL_S)
    print(f"pipeline {run_id}  {state}  ref={p.get('ref')}  {p.get('web_url','')}")
    return RC_OK if srv.passed(state) else RC_FAILED


def _do_jobs(args, host) -> int:
    """The pipeline being asked about's job records, retried attempts INCLUDED.

    Scoped to ONE pipeline (`--pipeline`, or the latest): the project-wide listing
    counts a job's name across every pipeline, so a job that ran once in EACH of two
    pipelines reads as `flaky x2` though neither one ever retried it -- see
    `jobs_url`'s docstring for the measurement this is named after.
    """
    pid = _project_id(args, host)
    run_id = args.pipeline or _latest_pipeline(args, host)
    if not run_id:
        print("no pipeline yet")
        return RC_BROKEN
    raw = _curl_url(args, host, srv.jobs_url(_base(args), pid, pipeline_id=run_id))
    records = _json(raw, "the job listing")
    counts = srv.attempts(records)
    for rec in sorted(records, key=lambda r: r.get("id", 0)):
        print(f"  id={rec.get('id'):<5} {rec.get('name','?'):<20} {rec.get('status','?')}")
    retried = {n: c for n, c in counts.items() if c > 1}
    print(f"\n{len(records)} job record(s)"
          + (f" — retried: {retried}" if retried else " — no job retried"))
    return RC_OK


def _do_trigger(args, host) -> int:
    """Start a pipeline and wait for a TERMINAL state, then report what ran."""
    pid = _project_id(args, host)
    if pid is None:
        print(f"!! no such project — `{PROG} gitlab project` first", file=sys.stderr)
        return RC_BROKEN
    # The pipeline THIS call started, by id. A push has usually created one already,
    # and counting the project's jobs would add the two runs together -- measured as
    # `flaky x6` for a job whose retry:2 caps it at three attempts.
    made = _json(_curl(args, host, f"/projects/{pid}/pipeline?ref={args.branch}",
                       method="POST"), "the pipeline creation")
    run_id = made.get("id")
    if not run_id:
        print(f"!! no pipeline was created: {str(made)[:200]}", file=sys.stderr)
        return RC_BROKEN
    print(f"pipeline {run_id} started")
    started = time.time()
    state = "unknown"
    while time.time() - started < args.timeout:
        pipe = _json(_curl(args, host, f"/projects/{pid}/pipelines/{run_id}"),
                     "the pipeline")
        state = pipe.get("status", "unknown")
        if srv.is_terminal(state):
            break
        time.sleep(POLL_S)
    records = _json(_curl_url(args, host, srv.jobs_url(_base(args), pid, pipeline_id=run_id)),
                    "the job listing")
    rc, reason = verdict(state, srv.attempts(records))
    print(f"pipeline: {reason}")
    return rc


def _do_log(args, host) -> int:
    pid = _project_id(args, host)
    records = _json(_curl_url(args, host, srv.jobs_url(_base(args), pid)),
                    "the job listing")
    wanted = [r for r in records
              if str(r.get("id")) == args.job or r.get("name") == args.job]
    if not wanted:
        names = ", ".join(sorted({str(r.get("name")) for r in records}))
        print(f"!! no job {args.job!r} (have: {names})", file=sys.stderr)
        return RC_BROKEN
    for rec in wanted:
        print(f"--- job {rec['id']} {rec['name']} {rec['status']} ---")
        print(_curl(args, host, f"/projects/{pid}/jobs/{rec['id']}/trace"))
    return RC_OK


def _teardown(args, host, *, purge: bool) -> None:
    """Remove what rops created, BY NAME.

    Measured 2026-09-10: `docker compose stop` in the configured directory reported
    success while the container kept running healthy -- it belongs to whichever
    compose project created it, not to the directory the tool was pointed at. Two
    later "warm boot" measurements (0s, then 10s) were taken against a server that
    had never stopped, and were pure noise.
    """
    host.run("docker ps -aq --filter label=rops.fleet=gitlab | xargs -r docker rm -f "
             ">/dev/null 2>&1; true", timeout=300)
    host.run(f"docker rm -f {SERVER} >/dev/null 2>&1; true", timeout=300)
    if purge:
        # The volume holds every project and pipeline. Named after the container so a
        # compose-created volume from any project is reached.
        host.run(f"docker volume ls -q | grep -E 'gitlab' | xargs -r docker volume rm "
                 f">/dev/null 2>&1; true", timeout=300)
    host.run(f"docker run --rm -v {_dir(args)}/runners:/r alpine:3.20 "
             f"sh -c 'rm -rf /r/* /r/.[!.]* 2>/dev/null; true' >/dev/null 2>&1; true",
             timeout=300)


def _do_down(args, host) -> int:
    if not args.yes and sys.stdin.isatty():
        what = "the server, its runners and ALL its data" if args.purge \
            else "the server and its runners"
        if input(f"remove {what}? [y/N] ").strip().lower() not in ("y", "yes"):
            return RC_BROKEN
    elif not args.yes:
        print("!! refusing without --yes (not a terminal)", file=sys.stderr)
        return RC_BROKEN
    _teardown(args, host, purge=args.purge)
    print("server and runners removed"
          + (" — volume dropped, every project and pipeline is gone" if args.purge
             else " — the volume is kept (`--purge` drops it)"))
    return RC_OK


# ------------------------------------------------------- verdicts for the new actions

def lint_verdict(payload: dict) -> tuple[int, str]:
    """What the SERVER says about a CI file, before anything is pushed.

    `valid: true` is not the whole question. Measured in this repo at 63b3a873: an
    `extends:` naming an anchor nobody defined resolved the entire file to nothing and
    the pipeline ran no job. A validator answering only `valid` about a file that
    yields zero jobs answers a different question than the one that was asked, so the
    merged document is checked for a job as well.
    """
    if "valid" not in payload:
        return RC_BROKEN, f"not a lint response: {str(payload)[:200]}"
    if not payload["valid"]:
        errs = "; ".join(str(e) for e in payload.get("errors") or []) or "invalid"
        return RC_FAILED, errs
    merged = payload.get("merged_yaml") or ""
    if merged and not _has_job(merged):
        return RC_BROKEN, ("the server calls it valid but the merged document holds "
                           "no job — an extends: at an undefined anchor resolves to "
                           "exactly this, and the pipeline then runs nothing")
    warns = payload.get("warnings") or []
    return RC_OK, "valid" + (f" ({len(warns)} warning(s))" if warns else "")


def _has_job(merged_yaml: str) -> bool:
    import yaml_io as _yaml  # the one shared reader/writer (docs/code-standards.md)

    from rops.ropslib.gitlab import capability as cap
    try:
        doc = _yaml.safe_load(merged_yaml)
    except _yaml.YAMLError:
        return False
    return bool(isinstance(doc, dict) and cap.jobs_of(doc))


def report_verdict(payload: dict) -> tuple[int, str]:
    """The TEST results, not the job results.

    A job's exit code says one thing; `failed_count` says how many tests. Two
    numbers matter beyond the obvious: `error_count` is a test that could not even
    run (ignoring it reports green on a suite that crashed), and a `total_count` of
    zero is how a shard that silently collected nothing reads -- a suite that ran no
    test is not a suite that passed.
    """
    total = int(payload.get("total_count", 0) or 0)
    failed = int(payload.get("failed_count", 0) or 0)
    errors = int(payload.get("error_count", 0) or 0)
    skipped = int(payload.get("skipped_count", 0) or 0)
    if total == 0:
        return RC_BROKEN, ("no test ran — a report of zero tests is how a shard that "
                           "collected nothing reads, not a pass")
    if failed or errors:
        return RC_FAILED, (f"{failed} failed, {errors} error(s) of {total} test(s)"
                           + (f", {skipped} skipped" if skipped else ""))
    return RC_OK, (f"{total} test(s) passed"
                   + (f", {skipped} skipped" if skipped else ""))


# --------------------------------------------------------------------- new actions

def _do_lint(args, host) -> int:
    """Ask the server whether the file is valid, WITHOUT running anything.

    The cheapest gate there is: no job, no runner, no push. And it catches the class
    of error that cost this repo months of a silently empty pipeline.
    """
    pid = _project_id(args, host)
    if pid is None:
        print(f"!! no such project — `{PROG} gitlab project` first", file=sys.stderr)
        return RC_BROKEN
    remote_dir = args.cfg.get("ROPS_REMOTE_DIR", "~")
    # The file's content reaches the API as JSON, built ON the remote: quoting a
    # 30 KB YAML document through two shells is how a lint of the wrong bytes happens.
    payload = host.out(
        f"cd {remote_dir} && python3 -c "
        f"'import json,sys; print(json.dumps({{\"content\": open(sys.argv[1]).read()}}))' "
        f"{sq(args.file)}", timeout=120)
    if not payload.strip():
        print(f"!! could not read {args.file} on {host.name}", file=sys.stderr)
        return RC_BROKEN
    raw = host.out(
        f"printf %s {sq(payload)} | curl -s -X POST -H {sq(f'PRIVATE-TOKEN: {_token(args)}')} "
        f"-H 'Content-Type: application/json' --data-binary @- "
        f"{sq(f'{srv.api(_base(args))}/projects/{pid}/ci/lint')}", timeout=180)
    rc, reason = lint_verdict(_json(raw, "the lint response"))
    print(f"lint: {reason}")
    return rc


def _do_report(args, host) -> int:
    pid = _project_id(args, host)
    run_id = args.pipeline or _latest_pipeline(args, host)
    if not run_id:
        print("no pipeline yet")
        return RC_BROKEN
    raw = _curl(args, host, f"/projects/{pid}/pipelines/{run_id}/test_report")
    rc, reason = report_verdict(_json(raw, "the test report"))
    print(f"pipeline {run_id} tests: {reason}")
    return rc


def _do_rerun(args, host) -> int:
    """Re-run the failed jobs of a pipeline. NOT the same thing as `retry:`.

    `retry:` is the server retrying a job by itself, which the light lane cannot do.
    This is a person asking for another attempt, and the attempt counts afterwards
    show both.
    """
    pid = _project_id(args, host)
    run_id = args.pipeline or _latest_pipeline(args, host)
    if not run_id:
        print("no pipeline to re-run")
        return RC_BROKEN
    made = _json(_curl(args, host, f"/projects/{pid}/pipelines/{run_id}/retry",
                       method="POST"), "the rerun")
    rc, text = rerun_verdict(made)
    print(text if rc == RC_OK else f"!! {text}", file=sys.stdout if rc == RC_OK else sys.stderr)
    return rc


def _do_graph(args, host) -> int:
    """What waits on what: `needs:` ordering, and the child pipelines a `trigger:`
    job spawns (`bridges`) -- two separate surfaces that "dependencies" blurs."""
    pid = _project_id(args, host)
    run_id = args.pipeline or _latest_pipeline(args, host)
    if not run_id:
        print("no pipeline yet")
        return RC_BROKEN
    records = _json(_curl(args, host, f"/projects/{pid}/pipelines/{run_id}/jobs"
                                      f"?include_retried=false&per_page=100"),
                    "the job listing")
    by_stage: dict[str, list] = {}
    for rec in records:
        by_stage.setdefault(rec.get("stage", "?"), []).append(rec)
    for stage, jobs in by_stage.items():
        print(f"{stage}")
        for j in sorted(jobs, key=lambda r: r.get("name", "")):
            print(f"    {j.get('name','?'):<28} {j.get('status','?')}")
    bridges = _json(_curl(args, host, f"/projects/{pid}/pipelines/{run_id}/bridges"),
                    "the bridges")
    if isinstance(bridges, list) and bridges:
        print("\nchild pipelines (trigger:)")
        for b in bridges:
            child = (b.get("downstream_pipeline") or {}).get("id")
            print(f"    {b.get('name','?'):<28} -> pipeline {child or '(none)'}")
    else:
        print("\nno child pipeline in this run")
    return RC_OK


def artifact_state(record: dict) -> str:
    """`declared` | `pending` | `none` -- three findings, never collapsed into two.

    Measured 2026-09-10: the verb printed `no artifact` for a job whose API record
    held `artifacts_file: artifacts.zip` and whose download returned HTTP 200. A
    false "there is nothing there" is worse than an error, because it closes the
    question instead of raising it.
    """
    # NOT `artifacts` wholesale: every job carries `{file_type: trace,
    # filename: job.log}` there -- the console log the server keeps for all of them.
    # Measured 2026-09-10: reading the array made EVERY job look like it had an
    # artifact, the download answered 404, and a job with nothing to fetch was
    # reported as "declared an artifact but the download failed". A false-positive
    # detector next to a real 404 reads like a broken server.
    real = [a for a in (record.get("artifacts") or [])
            if isinstance(a, dict) and a.get("file_type") != "trace"]
    if record.get("artifacts_file") or real:
        return "declared"
    if record.get("status") in ("running", "pending", "created"):
        return "pending"
    return "none"


def rerun_verdict(payload: dict) -> tuple[int, str]:
    """What the rerun actually DID.

    Measured: the verb printed `pipeline 7 re-running (success)` -- a sentence that
    contradicts itself, built from the status field of a response about a pipeline
    that had already finished. A pipeline that comes back terminal had nothing to
    retry, and saying otherwise is a claim the API never made.
    """
    run_id, state = payload.get("id"), payload.get("status", "unknown")
    if not run_id:
        return RC_BROKEN, f"the rerun was refused: {str(payload)[:200]}"
    if srv.is_terminal(state):
        return RC_BROKEN, (f"pipeline {run_id} came back {state} — nothing to re-run "
                           f"(a rerun only restarts jobs that failed or were canceled)")
    return RC_OK, f"pipeline {run_id} restarted ({state})"


def _do_artifacts(args, host) -> int:
    pid = _project_id(args, host)
    records = _json(_curl_url(args, host, srv.jobs_url(_base(args), pid)),
                    "the job listing")
    wanted = [r for r in records
              if str(r.get("id")) == args.job or r.get("name") == args.job]
    if not wanted:
        print(f"!! no job {args.job!r}", file=sys.stderr)
        return RC_BROKEN
    dest = args.dest or f"{_dir(args)}/artifacts"
    # `~` is expanded by the SHELL, not by stat(1): `stat -c %s ~/x/y.zip` answers
    # "No such file" for a file that exists, and the size printed as `?`. Resolve the
    # directory once, on the far side, and use the absolute form from here on.
    dest = host.out(f"mkdir -p {dest} && cd {dest} && pwd") or dest
    got = failed = 0
    for rec in wanted:
        state = artifact_state(rec)
        if state == "pending":
            print(f"  {rec['name']} (job {rec['id']}): still running — no artifact yet")
            continue
        if state == "none":
            print(f"  {rec['name']} (job {rec['id']}): declares no artifact")
            continue
        path = f"{dest}/{rec['id']}-{rec['name'].replace('/', '_')}.zip"
        url = f"{srv.api(_base(args))}/projects/{pid}/jobs/{rec['id']}/artifacts"
        code = host.out(
            f"curl -s -w '%{{http_code}}' -H {sq(f'PRIVATE-TOKEN: {_token(args)}')} "
            f"-o {sq(path)} {sq(url)}", timeout=600)
        if code.strip() == "200":
            size = host.out(f"stat -c %s {sq(path)} 2>/dev/null") or "?"
            print(f"  {rec['name']} (job {rec['id']}): {path} ({size} bytes)")
            got += 1
        else:
            # An artifact the job DECLARED and we could not fetch is a fetch failure,
            # never "there is nothing there".
            failed += 1
            print(f"  {rec['name']} (job {rec['id']}): declared an artifact but the "
                  f"download answered HTTP {code.strip() or '?'}", file=sys.stderr)
    if failed:
        return RC_BROKEN
    if not got:
        print("no job in this selection declares an artifact")
        return RC_BROKEN
    print(f"\n{got} artifact(s) under {host.name}:{dest}")
    return RC_OK


def _do_var(args, host) -> int:
    """The project's CI variables -- including the protected ones, which the light
    lane cannot even represent. VALUES ARE NOT PRINTED: they are credentials."""
    pid = _project_id(args, host)
    records = _json(_curl(args, host, f"/projects/{pid}/variables"), "the variables")
    if not records:
        print("no CI variable declared on this project")
        return RC_OK
    for v in sorted(records, key=lambda r: r.get("key", "")):
        flags = [f for f, on in (("protected", v.get("protected")),
                                 ("masked", v.get("masked")),
                                 ("raw", v.get("raw"))) if on]
        print(f"  {v.get('key','?'):<28} {v.get('variable_type','env_var'):<12} "
              f"{' '.join(flags)}")
    print(f"\n{len(records)} variable(s) — values withheld (they are credentials)")
    return RC_OK


def _latest_pipeline(args, host) -> int | None:
    pid = _project_id(args, host)
    pipes = _json(_curl_url(args, host, srv.pipelines_url(_base(args), pid)),
                  "the pipelines")
    return pipes[0]["id"] if pipes else None


def _do_config(args, host) -> int:
    """What was asked for, what the runners actually carry, and where they differ.

    The point is the third column. A knob can be declared, accepted by `register`,
    and still not be in force -- `concurrent` is the measured case: it is not a
    register flag at all, so a fleet that only registers keeps `concurrent = 1` per
    runner while the caller believes the number they set.
    """
    knobs = gc.resolve(args.cfg)
    # Door 2 read from the file rather than described: `(per job)` was true and
    # useless. The document comes from the remote, resolved, so `include:` and
    # `extends:` are already applied.
    declared_jobs: dict = {}
    remote_dir = args.cfg.get("ROPS_REMOTE_DIR", "~")
    raw = host.out(f"cd {remote_dir} && cat {sq(args.file)} 2>/dev/null", timeout=120)
    if raw.strip():
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
            fh.write(raw)
            tmp = pathlib.Path(fh.name)
        try:
            declared_jobs = gc.job_knobs(reader.load(tmp))
        except reader.ReadError as exc:
            print(f"!! {args.file}: {exc}", file=sys.stderr)
        finally:
            tmp.unlink(missing_ok=True)

    print(f"{'knob':<22} {'door':<16} {'value':<26} source")
    for name in sorted(gc.KNOBS):
        door = gc.owner(name)
        if door == gc.DOOR_YAML:
            per_job = declared_jobs.get(name)
            if not per_job:
                shown, src = "—", f"not declared in {args.file}"
            else:
                shown = ", ".join(f"{j}={v}" for j, v in sorted(per_job.items()))[:26]
                src = f"{len(per_job)} job(s) in {args.file}"
            print(f"{name:<22} {door:<16} {shown:<26} {src}")
            continue
        v = knobs[name]
        print(f"{name:<22} {door:<16} {str(v.value):<26} {v.source}")

    names = [n for n in host.out(
        "docker ps --filter label=rops.fleet=gitlab --format '{{.Names}}'").split()]
    if not names:
        print(f"\nno fleet running — `{PROG} gitlab runner` builds one")
        return RC_BROKEN

    observed: dict[str, int | None] = {}
    blocks: dict[str, int] = {}
    for name in names:
        toml = host.out(f"docker exec {sq(name)} cat /etc/gitlab-runner/config.toml "
                        f"2>/dev/null", timeout=120)
        observed[name] = gc.effective_concurrent(toml) if toml.strip() else None
        blocks[name] = gc.runner_blocks(toml)

    print(f"\nlive fleet: {len(names)} runner(s)")
    for name in sorted(observed):
        got = observed[name]
        print(f"  {name:<22} concurrent={got if got is not None else 'unreadable'}")

    declared_now = {k: v.value for k, v in knobs.items() if v.source == gc.SRC_ENV}
    for warn in gc.enforcement_warnings(declared_now):
        print(f"\nDECLARED BUT NOT ENFORCING: {warn}")

    rows = gc.drift({k: v.value for k, v in knobs.items()}, observed)
    if rows:
        print("\nDECLARED BUT NOT IN FORCE:")
        for row in rows:
            print(row)

    # A registration that ran twice against the same mounted config dir leaves the
    # runner serving the FIRST block, which predates every setting above —
    # everything just printed can read as correct and still not be what runs.
    stacked_rows = gc.stacked(blocks)
    if stacked_rows:
        print("\nSTACKED CONFIG:")
        for row in stacked_rows:
            print(row)

    if rows or stacked_rows:
        return RC_FAILED
    total = sum(g for g in observed.values() if g)
    print(f"\nevery runner carries what was asked — the fleet can run {total} job(s) "
          f"at once")
    return RC_OK
