"""Getting the document the capability gate judges, and the count a run is measured against.

The gate is only as honest as its input. Reading the RAW `.gitlab-ci.yml` misses
everything `include:` and `extends:` bring in -- and in the measured case that is
where the pipeline lives: document-intake's 30 KB wrapper pulls a shared
`_gitflow-service.yml` out of a different project, and resolving it yields 27 jobs
that the raw file never names.

So the reader takes the EXPANDED document gitlab-ci-local writes. It resolves
`include:`, `extends:` and `!reference`, and -- unlike `--preview` -- it keeps the
`default:` block, whose `retry`/`timeout` bind every job in the file.

The expected job COUNT comes from the listing, never from the document: a
`parallel: 6` job is one entry and six runs, so counting document keys would set the
bar at 1 and call a 1-of-6 run a pass.
"""
from __future__ import annotations

import json
import pathlib

import yaml_io  # the one shared reader (docs/code-standards.md); never `import yaml`
from ruamel.yaml.constructor import SafeConstructor
from ruamel.yaml.nodes import MappingNode, SequenceNode

EXPANDED_REL = ".gitlab-ci-local/expanded-gitlab-ci.yml"


class ReadError(RuntimeError):
    """The document could not be understood. Never degraded into an empty pipeline:
    an empty document needs nothing, so it PASSES the capability gate -- a silent
    green for a file nobody managed to read."""


class _Tolerant(SafeConstructor):
    """`!reference [.tpl, script]` is valid GitLab and unknown to a plain safe loader.

    The expanded document has these resolved already, but the reader is also pointed
    at RAW files (a `preview` of someone else's repo), and dying on a tag turns a
    supported pipeline into a crash instead of a verdict. The node is kept as plain
    data: the gate reads keywords, and an unresolved reference is not one.

    A constructor SUBCLASS, not a flag on `yaml_io`'s shared loader: `yaml_io._loader()`
    is one process-wide instance every hook/script reuses, and registering a
    catch-all multi-constructor there would make every OTHER caller silently
    tolerate an unknown tag too. This reader owns its own `YAML` instance instead.

    This file is also the one reader in the tree that is NOT reading harness config —
    it is reading someone ELSE's `.gitlab-ci.yml`, and that file must parse the way
    GitLab's own parser reads it, not the way `yaml_io`'s Norway-safe YAML-1.2 reading
    reads harness YAML. `_tolerant_loader()` sets `version = (1, 1)` for exactly this:
    a bare `0022` is octal 18 there, and `yes`/`no` fold to booleans, matching what a
    real `.gitlab-ci.yml` author who tested against GitLab actually gets.

    `check_mapping_key` below is the OTHER half of the same "read it as GitLab would"
    contract: two jobs declared under the same key is valid, if surprising, GitLab —
    the second occurrence wins, silently. ruamel's own `allow_duplicate_keys` flag
    only suppresses the `DuplicateKeyError`; `check_mapping_key` still returns False
    for the repeat either way (see `BaseConstructor.check_mapping_key`), so the
    mapping keeps the FIRST value — last-wins needs this override to let the second
    assignment actually happen.
    """

    def check_mapping_key(self, node, key_node, mapping, key, value):
        return True


def _passthrough(constructor, tag_suffix, node):
    if isinstance(node, SequenceNode):
        return constructor.construct_sequence(node, deep=True)
    if isinstance(node, MappingNode):
        return constructor.construct_mapping(node, deep=True)
    return constructor.construct_scalar(node)


_Tolerant.add_multi_constructor("", _passthrough)


def _tolerant_loader() -> "yaml_io.YAML":
    y = yaml_io.YAML(typ="safe", pure=False)
    y.Constructor = _Tolerant
    # This file is someone else's, read the way GitLab's own parser reads it — not
    # harness config, read the Norway-safe YAML-1.2 way `yaml_io`'s default loader
    # reads everything else in this repo.
    y.version = (1, 1)
    return y


def load(path: pathlib.Path) -> dict:
    path = pathlib.Path(path)
    if not path.is_file():
        raise ReadError(f"no CI document at {path}")
    try:
        doc = _tolerant_loader().load(path.read_text(encoding="utf-8"))
    except yaml_io.YAMLError as exc:
        raise ReadError(f"{path}: {exc}") from exc
    if not isinstance(doc, dict) or not doc:
        raise ReadError(
            f"{path}: not a CI document (a pipeline is a mapping of jobs; this parsed "
            f"as {type(doc).__name__})")
    return doc


def parse_listing(raw: str) -> list:
    """The job listing out of a stream that also carries the tool's own chatter.

    Measured 260910: `--list-json` writes warnings to STDOUT, above the document --
    injecting merge-request variables (the whole reason to run a rules-driven
    pipeline locally) prints a multi-line `WARN Avoid overriding predefined
    variables` block first, and a plain json.loads dies at line 2.

    So the array is taken from the first `[` to the last `]`, and a stream with no
    array at all RAISES. Returning an empty list instead would surface later as
    "this pipeline resolved to no job", a true-sounding cause for what is really
    "the tool never printed a document".
    """
    # NOT the first `[` in the stream: the warning itself contains one --
    # `WARN Avoid overriding predefined variables [CI_PIPELINE_SOURCE]` -- and
    # cutting there produces a fragment that fails to parse for the wrong reason.
    # The document starts at a line whose first character is `[`.
    start = -1
    offset = 0
    for line in raw.splitlines(keepends=True):
        if line.lstrip().startswith("[") and line.strip() != "[":
            start = offset + len(line) - len(line.lstrip())
            break
        if line.strip() == "[":
            start = offset + line.index("[")
            break
        offset += len(line)
    end = raw.rfind("]")
    if start < 0 or end < start:
        raise ReadError(f"no job listing in the output "
                        f"({raw.strip().splitlines()[0][:120] if raw.strip() else 'empty'})")
    try:
        listing = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ReadError(f"the job listing did not parse: {exc}") from exc
    if not isinstance(listing, list):
        raise ReadError(f"the job listing is not a list ({type(listing).__name__})")
    return listing


def expected_count(listing: list) -> int:
    """How many jobs a run of this pipeline must execute.

    `--list-json` entries, minus the ones that will not run:

      * `when: manual` -- nobody is there to press the button;
      * `when: never`  -- the job's own `rules:` ruled it out, so it is not in this
        pipeline at all.

    Both exclusions are load-bearing, and `never` is the one that bites. Measured
    260910 on document-intake: `--list-json` is the `--list-all` variant, so all 36
    jobs came back marked `never` (their rules want merge-request variables no local
    run has) while plain `--list` returned nothing at all. Counting them set the bar
    at 36 for a pipeline whose real answer here is that it runs NOTHING -- a number
    that makes the run red for a reason that is not true.

    An empty result is an ERROR -- zero expected jobs makes every run trivially
    satisfy its count, which is exactly the shape of the undefined-anchor bug. The
    three empty causes raise DIFFERENT messages on purpose: "no jobs", "all manual"
    and "all ruled out" call for three different next actions.
    """
    if not isinstance(listing, list) or not listing:
        raise ReadError("empty job listing -- a pipeline that resolves to no job at "
                        "all is the undefined-anchor failure, not a pipeline")
    names: list[str] = []
    skipped: dict[str, list[str]] = {}
    for entry in listing:
        if not isinstance(entry, dict) or not entry.get("name"):
            raise ReadError(f"job listing entry has no name: {entry!r}")
        when = entry.get("when")
        if when in ("manual", "never"):
            skipped.setdefault(when, []).append(entry["name"])
            continue
        names.append(entry["name"])
    if not names:
        if skipped.get("never") and not skipped.get("manual"):
            raise ReadError(
                f"every job is when:never -- their rules ruled all "
                f"{len(skipped['never'])} of them out, so this pipeline runs nothing "
                f"in this context (a local run has no merge-request variables)")
        if skipped.get("manual") and not skipped.get("never"):
            raise ReadError("every job in this pipeline is manual -- nothing would run")
        raise ReadError("every job is either manual or ruled out by its rules -- "
                        "nothing would run")
    return len(names)


def flatten(doc: dict) -> dict:
    """The same pipeline, resolved, WITHOUT the include that produced it.

    This is the whole answer to "resolve where the network is, run where the machine
    is". Measured 2026-09-10: document-intake's wrapper pulls a shared engine out of
    another project over git, so it resolves on a machine inside that network and
    fails on one outside it -- `curl` to the internal GitLab from the remote is
    simply unreachable. The expanded document already HOLDS all 29 resolved jobs; the
    only thing standing between them and the other machine is the `include:` line
    still sitting in it, which makes that machine try the fetch again.

    Dropping the line and nothing else: measured 36 jobs resolving on the remote from
    the flattened file, with no access to the internal GitLab at all.

    A flatten that yields no job RAISES. Carrying an empty pipeline to another machine
    would reproduce the undefined-anchor failure there and read as a clean run.
    """
    from rops.ropslib.gitlab import capability as cap

    flat = {k: v for k, v in doc.items() if k != "include"}
    if not cap.jobs_of(flat):
        raise ReadError(
            "flattening left no job — the document's jobs came from an include that "
            "was never resolved, so this file would run nothing wherever it is taken")
    return flat
