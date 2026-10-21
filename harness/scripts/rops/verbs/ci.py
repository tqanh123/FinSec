"""ci — run a GitLab pipeline on the remote, refusing what the lane cannot honestly do.

    hs-rops ci list                  what this pipeline resolves to (jobs, and what they need)
    hs-rops ci run [job ...]         run it -- refused up front if the lane cannot honour it
    hs-rops ci preview               the expanded document, includes and extends resolved

Nothing here decides what a lane can do; that lives in ropslib/gitlab/capability.py
next to the measurement that produced it. This file is the door: resolve the document
on the remote, hand it to the gate, and let the gate decide before anything runs.

The expansion happens REMOTELY on purpose. `include: project:` reaches an internal
GitLab, `include: remote:` reaches the network, and both answer differently depending
on which machine asks -- so the document that gets judged is the one the machine that
will run it actually resolves.
"""
from __future__ import annotations

import argparse
import sys

from rops.ropslib import hosts, tree
from rops.ropslib.exec import report, run_remote, seated
from rops.ropslib.gitlab import capability as cap
from rops.ropslib.gitlab import lane as gl
from rops.ropslib.gitlab import pipeline as pl
from rops.ropslib.gitlab import reader
from rops.ropslib.prog import PROG
from rops.ropslib.transport import sq



def add_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("action", choices=["list", "run", "preview", "flatten"],
                   nargs="?", default="list")
    p.add_argument("--file", default="", help="alternate .gitlab-ci.yml (repo-relative or absolute)")
    p.add_argument("--var", action="append", default=[], metavar="K=V",
                   help="pipeline variable (repeatable) — `rules:` read these, so a "
                        "rules-driven pipeline resolves to nothing without them")
    p.add_argument("--out", default="",
                   help="`flatten`: where to write the self-contained file")
    p.add_argument("--lane", default=cap.LANE_LOCAL.name, choices=sorted(cap.LANES),
                   help="which lane must honour the pipeline")
    p.add_argument("--privileged", action="store_true", help="needed by dind jobs")
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--tail", type=int, default=None)
    from rops.verbs.run import add_tree_args
    add_tree_args(p)
    # NOT argparse.REMAINDER: after a positional it swallows FLAGS as well. Measured
    # -- `hs-rops ci list --file other.yml` parsed as file='' and jobs=['--file',
    # 'other.yml'], so the verb resolved the DEFAULT pipeline and printed a plausible
    # answer about a file nobody asked for. `*` keeps flags reaching their own option.
    p.add_argument("jobs", nargs="*", help="job names; empty = the whole pipeline")


def _remote_expand(cfg, host, args) -> tuple[dict, list] | None:
    """Resolve the pipeline ON THE FAR SIDE and bring back the document + listing.

    Two commands rather than one: `--list-json` prints the listing to stdout, and the
    expansion lands in a FILE. Reading both from one stream would mean parsing the
    runner's own chatter out of the middle of a JSON document.
    """
    # The variables reach RESOLUTION too, not just the run: `rules:` decide which
    # jobs exist, so a listing taken without them describes a different pipeline
    # than the one that would run.
    base = f"{gl.preflight(cfg)}; " + gl.local_command(
        cfg, file=args.file, variables=getattr(args, "var", None))
    listing_raw = host.out(
        f"cd {cfg.get('ROPS_REMOTE_DIR', '~')} && {base} --list-json 2>/dev/null",
        timeout=300)
    expanded = host.out(
        f"cd {cfg.get('ROPS_REMOTE_DIR', '~')} && cat {sq(reader.EXPANDED_REL)} 2>/dev/null",
        timeout=60)
    if not listing_raw.strip() or not expanded.strip():
        # WHY, when the output says why. "could not resolve" alone reads like a bug
        # in rops, and the measured cause is usually a route or a credential the
        # resolving machine does not have.
        why = host.out(
            f"cd {cfg.get('ROPS_REMOTE_DIR', '~')} && {base} --list-json 2>&1 | tail -20",
            timeout=300)
        hint = gl.resolve_failure_hint(why)
        print("!! the remote could not resolve this pipeline", file=sys.stderr)
        if hint:
            print(f"   {hint}", file=sys.stderr)
        else:
            print(f"   run `{PROG} ci preview` to see what it said", file=sys.stderr)
        return None
    try:
        # Through the reader, not json.loads: the tool writes warnings to STDOUT
        # above the document, and `--variable` is exactly what triggers one.
        listing = reader.parse_listing(listing_raw)
    except reader.ReadError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return None
    import pathlib
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False) as fh:
        fh.write(expanded)
        path = pathlib.Path(fh.name)
    try:
        doc = reader.load(path)
    except reader.ReadError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return None
    finally:
        path.unlink(missing_ok=True)
    return doc, listing


def _flatten(cfg, args) -> int:
    """Resolve HERE, where the network and the credentials are, and write a file that
    needs neither.

    Measured 2026-09-10: document-intake's `include: project:` is fetched over git,
    so the pipeline resolves on a machine inside that network and fails on one
    outside it. Resolution runs LOCALLY for this action -- that is the point -- and
    the flattened file is then a plain input any machine can run.
    """
    import pathlib as _p
    import subprocess as _sp

    src = args.file or ".gitlab-ci.yml"
    cmd = f"{gl.preflight(cfg)}; " + gl.local_command(cfg, file=args.file) + " --preview"
    r = _sp.run(["bash", "-c", cmd], cwd=str(cfg.repo), text=True, capture_output=True)
    expanded = _p.Path(cfg.repo) / reader.EXPANDED_REL
    if not expanded.is_file():
        print(f"!! {src} did not resolve here either:\n"
              f"{(r.stderr or r.stdout).strip()[-500:]}", file=sys.stderr)
        hint = gl.resolve_failure_hint((r.stderr or "") + (r.stdout or ""))
        if hint:
            print(f"   {hint}", file=sys.stderr)
        return pl.RC_BROKEN
    try:
        flat = reader.flatten(reader.load(expanded))
    except reader.ReadError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return pl.RC_BROKEN

    import yaml_io as _yaml  # the one shared reader/writer (docs/code-standards.md)
    out = _p.Path(args.out) if args.out else _p.Path(cfg.repo) / ".gitlab-ci-flat.yml"
    out.write_text(_yaml.safe_dump(flat, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    print(f"{out}  ({out.stat().st_size} bytes, {len(cap.jobs_of(flat))} job(s), "
          f"no include)")
    print(f"   run it anywhere: {PROG} ci list --file {out.name}")
    return pl.RC_OK


def _print_list(doc: dict, listing: list, lane: cap.Lane) -> int:
    needs = cap.needs(doc)
    print(f"lane {lane.name}: {lane.how}")
    for entry in listing:
        name = entry.get("name", "?")
        marks = [c for c, jobs in needs.items() if cap._base_name(name) in jobs]
        flag = "" if all(m in lane.honours for m in marks) else "  UNSUPPORTED"
        extra = f"  [{', '.join(sorted(marks))}]" if marks else ""
        print(f"  {name:<40} {entry.get('stage', '?'):<12}{extra}{flag}")
    v = cap.verdict(doc, lane)
    print(f"\n{'runnable' if v.ok else 'NOT runnable on this lane'}: "
          f"{len(listing)} job(s) resolved")
    return pl.RC_OK if v.ok else pl.RC_UNSUPPORTED


def run(args: argparse.Namespace) -> int:
    cfg = args.cfg
    host = hosts.gate(args)[0]
    rc = tree.check(cfg, host, stale_ok=getattr(args, "stale_ok", False))
    if rc:
        return rc
    lane = cap.LANES[args.lane]

    if args.action == "flatten":
        return _flatten(cfg, args)

    if args.action == "preview":
        res = run_remote(cfg, host,
                         f"{gl.preflight(cfg)}; "
                         + gl.local_command(cfg, file=args.file) + " --preview",
                         timeout_s=300, tail=args.tail, label="ci-preview")
        return report(res)

    got = _remote_expand(cfg, host, args)
    if got is None:
        return pl.RC_BROKEN
    doc, listing = got

    if args.action == "list":
        return _print_list(doc, listing, lane)

    def runner(command: str) -> tuple[int, str]:
        res = run_remote(seated(cfg, command, why="a CI job"), host,
                         f"{gl.preflight(cfg)}; {command}",
                         timeout_s=args.timeout, tail=args.tail, label="ci-run")
        # The full log stays remote (behavior 4). `classify` counts jobs, so it needs
        # the whole log, not the tail -- fetched as one read rather than piped.
        full = host.out(f"cat {res.log}", timeout=120)
        report(res)
        return res.rc, full

    return pl.execute(cfg, doc=doc, listing=listing, lane=lane, runner=runner,
                      jobs=args.jobs or None, privileged=args.privileged,
                      concurrency=args.concurrency, file=args.file,
                      variables=args.var)
