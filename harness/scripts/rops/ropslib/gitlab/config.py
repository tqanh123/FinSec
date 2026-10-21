"""Resource and load configuration, through TWO doors that do not overlap.

  * `.gitlab-ci.yml` says what a JOB needs -- `timeout:`, `resource_group:`,
    `interruptible:`, `parallel:`. It travels with the code, and every GitLab that
    runs the file reads the same answer.
  * `ROPS_GITLAB_*` says what the FLEET provides -- how many runners, how many jobs
    each takes at once, memory and CPU per job. That is a property of the machine, so
    it must not live in a file that gets pushed to every machine.

The measurement that forced the split: after ten runners registered successfully,
every config.toml read `concurrent = 1`. Ten runners, one job each. And `concurrent`
is NOT a `register` flag -- 0 of the 353 flags accept it, and `run` has none either.
It exists only as a global key in config.toml. So a fleet builder that only calls
`register` cannot set it, and a tool reporting the fleet's parallelism from its runner
COUNT is reporting a number it never checked.

Every flag name below was read off `gitlab-runner register --help` on the image in
use. A name that is not a real flag makes register fail on an unknown flag, several
containers into building a fleet.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from hs_family import UsageError
from rops.ropslib.prog import PROG

DOOR_ENV = "env var"
DOOR_YAML = ".gitlab-ci.yml"

SRC_DEFAULT, SRC_ENV = "default", "env"

#: knob -> (door, default). The door is not a preference: a fleet knob in the CI file
#: ships one machine's answer everywhere, and a job knob in the environment is invisible
#: to every other GitLab that runs the same pipeline.
KNOBS: dict[str, tuple[str, object]] = {
    # fleet — how much machine there is
    "runners": (DOOR_ENV, 10),
    "concurrent": (DOOR_ENV, 1),
    "request_concurrency": (DOOR_ENV, 0),
    "limit": (DOOR_ENV, 0),
    "memory": (DOOR_ENV, ""),
    "memory_swap": (DOOR_ENV, ""),
    "cpus": (DOOR_ENV, ""),
    "cpuset_cpus": (DOOR_ENV, ""),
    "image": (DOOR_ENV, "alpine:3.20"),
    "shm_size": (DOOR_ENV, 0),
    "maximum_timeout": (DOOR_ENV, 0),
    "output_limit": (DOOR_ENV, 0),
    # job — what the work needs, wherever it runs
    "timeout": (DOOR_YAML, None),
    "resource_group": (DOOR_YAML, None),
    "interruptible": (DOOR_YAML, None),
    "parallel": (DOOR_YAML, None),
    "tags": (DOOR_YAML, None),
}

#: The knobs `register` can carry, with the flag it advertises. Measured off
#: `--help`: 14 of them, and `concurrent` is not among them.
REGISTER_FLAGS = {
    "request_concurrency": "--request-concurrency",
    "limit": "--limit",
    "maximum_timeout": "--maximum-timeout",
    "output_limit": "--output-limit",
    "memory": "--docker-memory",
    "memory_swap": "--docker-memory-swap",
    "cpus": "--docker-cpus",
    "cpuset_cpus": "--docker-cpuset-cpus",
    "image": "--docker-image",
    "shm_size": "--docker-shm-size",
}

#: Flags only the docker executor understands. Passing `--docker-memory` to a shell
#: runner is rejected or inert, and either way the caller believes the limit applies.
_DOCKER_ONLY = frozenset({"memory", "memory_swap", "cpus", "cpuset_cpus", "image",
                          "shm_size"})

#: Keys that exist ONLY as globals in config.toml -- unreachable through register.
TOML_GLOBALS = frozenset({"concurrent", "check_interval", "shutdown_timeout"})

#: The runner's own floor, from its help text: "Minimum is 4M."
_MEM_MIN_BYTES = 4 * 1024 * 1024
_MEM = re.compile(r"^(\d+)([bkmg])$", re.IGNORECASE)
_UNIT = {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}


def owner(knob: str) -> str:
    return KNOBS[knob][0]


@dataclass(frozen=True)
class Value:
    value: object
    source: str


def _int(raw: str, knob: str) -> int:
    if not raw.lstrip("-").isdigit():
        raise UsageError(f"{PROG} gitlab: ROPS_GITLAB_{knob.upper()}={raw!r} is not a "
                         f"number — defaulting silently would give you a limit you "
                         f"did not ask for")
    return int(raw)


def _memory(raw: str, knob: str) -> str:
    """`<number><unit>`, unit-preserving.

    Stripping the unit turns `512m` into 512 BYTES, which is under the runner's own
    4M floor and fails at container start -- a failure that reads like a broken image
    rather than a bad limit.
    """
    m = _MEM.match(raw)
    if not m:
        raise UsageError(f"{PROG} gitlab: ROPS_GITLAB_{knob.upper()}={raw!r} needs a "
                         f"unit (b, k, m or g) — a bare number means BYTES to the "
                         f"runner, so 512 is 512 bytes, not 512 MiB")
    if int(m.group(1)) * _UNIT[m.group(2).lower()] < _MEM_MIN_BYTES:
        raise UsageError(f"{PROG} gitlab: ROPS_GITLAB_{knob.upper()}={raw!r} is below "
                         f"the runner's own minimum of 4M — it registers fine and "
                         f"then every job dies at container start")
    return raw


def resolve(cfg) -> dict[str, Value]:
    """Every fleet knob, with where its value came from.

    Job knobs are not resolved here: they live in the CI file, and reading them out
    of the environment is exactly the confusion the two doors exist to prevent.
    """
    out: dict[str, Value] = {}
    for knob, (door, default) in KNOBS.items():
        if door != DOOR_ENV:
            continue
        raw = str(cfg.get(f"ROPS_GITLAB_{knob.upper()}") or "").strip()
        if not raw:
            out[knob] = Value(default, SRC_DEFAULT)
            continue
        if knob in ("memory", "memory_swap"):
            out[knob] = Value(_memory(raw, knob), SRC_ENV)
        elif isinstance(default, int):
            out[knob] = Value(_int(raw, knob), SRC_ENV)
        else:
            out[knob] = Value(raw, SRC_ENV)
    return out


def _bytes_of(raw: str) -> int:
    m = _MEM.match(str(raw))
    return int(m.group(1)) * _UNIT[m.group(2).lower()] if m else 0


def enforcement_warnings(declared: dict) -> list[str]:
    """Knobs that are declared and will not actually take effect.

    One measured case, and it is the important kind. `--docker-memory=512m` WITHOUT
    `--docker-memory-swap` does not cap memory on a host with swap: measured
    2026-09-10 on a box with 2.0 GiB of swap, a job under that limit allocated and
    TOUCHED 900 MB and exited 0, while its cgroup correctly read
    `memory.max = 536870912`. Adding `--docker-memory-swap=512m` killed the same job
    and left a 64 MB control passing.

    So the limit was in the config, visible in the cgroup, and not in force. Nothing
    reported that, which is why this function exists rather than a comment.
    """
    out: list[str] = []
    mem, swap = declared.get("memory"), declared.get("memory_swap")
    if mem and not swap:
        out.append(
            "memory is capped but swap is not — on a host with swap the limit does "
            "NOT bite (measured: 900 MB touched under a 512m limit, exit 0). "
            "Declare ROPS_GITLAB_MEMORY_SWAP equal to ROPS_GITLAB_MEMORY.")
    if mem and swap and _bytes_of(swap) < _bytes_of(mem):
        raise UsageError(
            f"{PROG} gitlab: ROPS_GITLAB_MEMORY_SWAP={swap} is below "
            f"ROPS_GITLAB_MEMORY={mem} — docker requires swap >= memory and rejects "
            f"the container at start, which reads as a broken image")
    return out


def register_args(declared: dict, executor: str) -> list[str]:
    """The flags for ONE runner. Only what was declared, only what this executor has.

    A knob left at its default is omitted rather than passed as zero: `--docker-cpus 0`
    is a different statement than not limiting CPUs.
    """
    args: list[str] = []
    for knob, flag in REGISTER_FLAGS.items():
        if knob not in declared:
            continue
        if knob in _DOCKER_ONLY and executor != "docker":
            continue
        args += [flag, str(declared[knob])]
    return args


#: A GLOBAL `concurrent = N`: at the start of a line, outside any `[section]`.
_CONCURRENT = re.compile(r"^concurrent\s*=\s*(\d+)\s*$")


def effective_concurrent(toml: str) -> int | None:
    """What the runner's own config says, or None when it does not say.

    None means UNKNOWN, never 1. Reporting the documented default for a file that
    never mentions the key is how a fleet ends up reporting a parallelism nobody
    verified. A value under `[[runners]]` is a different (inert) setting and does not
    count -- reading it as the global would report a limit that is not in force.
    """
    in_section = False
    for line in toml.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") :
            in_section = True
            continue
        if in_section:
            continue
        m = _CONCURRENT.match(stripped)
        if m:
            return int(m.group(1))
    return None


#: Job knobs worth reading out of the CI document. `tags` is excluded: it is already
#: reported per runner, and repeating it here would give two numbers for one thing.
_JOB_KNOBS = ("timeout", "resource_group", "interruptible", "parallel")


def job_knobs(doc: dict) -> dict[str, dict[str, object]]:
    """knob -> {job: value}, `default:` already applied.

    `config` printed `(per job)` for every YAML knob, which is true and useless -- the
    file is right there. A knob no job declares is ABSENT rather than reported as an
    empty mapping: sixteen rows of nothing would bury the two that matter.

    `default:` reaches every job, the same asymmetry that made the capability gate
    read the expanded document rather than `--preview`.
    """
    from rops.ropslib.gitlab import capability as _cap

    defaults = doc.get("default") if isinstance(doc.get("default"), dict) else {}
    out: dict[str, dict[str, object]] = {}
    for name, body in _cap.jobs_of(doc).items():
        for knob in _JOB_KNOBS:
            val = body[knob] if knob in body else (defaults or {}).get(knob)
            if val is not None:
                out.setdefault(knob, {})[name] = val
    return out


def runner_blocks(toml: str) -> int:
    """How many `[[runners]]` blocks one config holds.

    `gitlab-runner register` APPENDS a block, and the config dir is a host mount that
    outlives the container. Measured 2026-09-10: three fleet builds against the same
    dir left three blocks in one file, and the runner served jobs from the FIRST --
    registered before any memory limit existed. Every later measurement of "does the
    limit bite" was reading a config the runner was not using, and the answer looked
    like "the limit does not work".
    """
    return sum(1 for line in toml.splitlines()
               if line.strip() == "[[runners]]")


def stacked(observed: dict[str, int]) -> list[str]:
    """Runners whose config holds more than one registration."""
    return [f"  {name}: {n} [[runners]] blocks — the runner serves the FIRST one, "
            f"which predates the current settings"
            for name, n in sorted(observed.items()) if n > 1]


def drift(asked: dict, observed: dict[str, int | None]) -> list[str]:
    """Runners whose live config does not carry what was asked for.

    A config the tool could not read counts as drift: that is the one case where
    saying nothing is indistinguishable from saying it matched.
    """
    want = asked.get("concurrent")
    rows = []
    for name, got in sorted(observed.items()):
        if got is None:
            rows.append(f"  {name}: could not read its config — unverified, "
                        f"not assumed to match")
        elif want is not None and got != want:
            rows.append(f"  {name}: concurrent={got}, asked for {want}")
    return rows
