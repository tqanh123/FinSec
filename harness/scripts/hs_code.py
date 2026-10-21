#!/usr/bin/env python3
"""hs-code — the shell for "every operation on a codebase's own MAP": measure,
restructure, test, port. Third member of the hs-* family, sibling to hs-run
(the step engine) and hs-cli (operator config) — see those two modules' own
docstrings for what they own; this one owns neither.

THE SHELL ITSELF DECLARES NO DOMAINS. `harness/data/hs-code-registry.d/`
carries a `_shell.yaml` fragment declaring only the shell's own identity —
every real domain (`graph`/`change`/...) lands as its own sibling fragment, a
data-file row, never an if/else branch here (the same promise hs-run's
registry already keeps). Dispatching into a domain name the LOADED registry
does not declare reports "0 domains registered" when the loaded fragment set
is genuinely empty (e.g. `HARNESS_HS_CODE_REGISTRY` pointed at an empty file —
see `test_hs_code_shell.py`'s own control), or names the domain(s) it DOES
know otherwise (the core's unknown-domain refusal) — the correct, honest answer either
way, not a bug to route around.

Output is the family envelope: ONE JSON line on stdout for every exit path, the verb's
own JSON payload under `data`; `--text` prints for a human instead (the family core,
`hs_family.cli`). A verb that read a snapshot or measured the tree also carries a
top-level `measured: {repo, head, dirty, mode: fresh|snapshot, snapshot,
snapshot_commit}` — the first tree the answer is about — and, when it read more than
one distinct tree, `measured_trees`: every row in read order. Recorded at the two
doors every tree passes (`codegraph_snapshot_reader.read_index` for a stored
snapshot, `take_snapshot` for a fresh measure), never by each verb. Top level, not
inside `data`: it describes the call, as `warnings` does, so it can never collide
with a verb's own payload keys or change what `--write` publishes; a verb that read
no tree carries none. Flags of the shell itself, taken from anywhere before routing:
  --repo PATH              the target codebase this invocation operates on —
                           NEVER hs-code's own installation location (that is
                           always this script's own `_ROOT`, exactly like
                           hs-run's registry and hs-cli's catalog). Defaults
                           to `harness_root.resolve_project_root()`.
  --write FILE             also publish the verb's payload (`data`) to FILE,
                           atomically; a verb that answered with no JSON data
                           is refused, nothing written.
  --apply                  write for real; a domain defaults to a DRY RUN
                           unless it reads this flag itself.
  --fresh / --allow-stale  freshness posture for a domain reading a cached
                           snapshot (mutually exclusive).
  --step BATCH/STEP[/DIST] record this call against the port domain's own
                           run ledger (`<repo>/.port/batches/<batch>/
                           runs.jsonl`, one line per call: {actor, ts,
                           batch, step, dist, argv, exit_code} — `argv` is
                           the command words as run, `--step` itself
                           excluded). Refused BEFORE the verb runs when the
                           shape is wrong or the named batch does not
                           exist. Without it, nothing is written — see
                           `port_runs.py`.
hs-run and hs-cli deliberately do NOT accept these: hs-run's own contract is
"reports only" (an `--apply` there would contradict it), and both resolve their
root through `harness_root`/`harness_paths`, never a `--repo` flag — a bare
`--repo`/`--apply` on either is an unknown flag/domain, same as any other typo
(see test_hs_run_common_flags.py / test_hs_cli_common_flags.py, the guard pair
this decision is pinned by).

Exit table: the plain family ladder (`hs_family.codes`) — 0 done · 1 findings
· 2 broken/usage · 3 deliberate refusal. No 10/11: those are hs-run's OWN
"needs a model turn"/"needs a deterministic step" extension for its two-beat
plan/cook steps; hs-code answers a read/measure/act question, never "what
step comes next", so it never needs that pair.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parents[1]  # harness/scripts -> harness -> repo root (mirrors hs_run.py)
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import attrs  # noqa: E402  (REQUIRED — see harness/requirements.txt)
import harness_root  # noqa: E402
import hs_family  # noqa: E402
import hs_family.registry as hs_registry  # noqa: E402
import hs_family.verb_help as hs_verb_help  # noqa: E402
import hs_family.cli as _family_cli  # noqa: E402
import pack_config  # noqa: E402

_REGISTRY_NAME = "hs-code-registry.yaml"
_REGISTRY_DIR_NAME = "hs-code-registry.d"
_CODEMAP_NAME = "codemap.yaml"

# No on-disk domain-module convention exists yet (no real domain has shipped
# to measure one from) — `family_env`'s own default `None` is registry.py's
# documented no-op shape, never a false "nothing is undeclared". A domain
# module lands together with the candidate scanner for its shape, the way
# hs_run.py's `_domain_module_candidates` describes hs-run's — until then,
# every field this family derives from the "hs-code" name by naming
# convention already matches, so nothing here needs to override it.
_REGISTRY_ENV = hs_registry.family_env("hs-code")


# ------------------------------------------------------------- shell's own flags

@attrs.define(frozen=True, kw_only=True)
class ShellOptions:
    """hs-code's own flags, handed to every domain verb as a single typed object —
    never loose dict keys a verb could mistype or half-populate. `json` is the
    output posture: True unless the caller asked for `--text`."""
    repo: str | None = None
    json: bool = True
    apply: bool = False
    fresh: bool = False
    allow_stale: bool = False
    write: str | None = None
    step: str | None = None

    def as_dict(self) -> dict:
        return attrs.asdict(self)


def _options(flags, *, text: bool = False) -> ShellOptions:
    return ShellOptions(repo=flags.get("repo"), json=not text, apply=bool(flags.get("apply")),
                        fresh=bool(flags.get("fresh")),
                        allow_stale=bool(flags.get("allow_stale")), write=flags.get("write"),
                        step=flags.get("step"))


def _step_argv(raw_argv: "list[str]", shell_flags) -> "list[str]":
    """`raw_argv` (the WHOLE call, before any shell-flag stripping) minus
    `--step` and every shell flag that is not part of the call's identity
    (`ShellFlag.identity` False: `--repo`, `--write`), each with its value.
    Everything else (domain, verb, args, `--apply`/`--fresh`) stays exactly as
    typed. This is the `argv` a run record carries: `hs_run_port.py` compares
    it, word for word, against the command it freshly renders from
    `port-steps.yaml`, which never carries where the call ran from."""
    drop = {f.name: f.takes_value for f in shell_flags if f.name == "--step" or not f.identity}
    out: "list[str]" = []
    i = 0
    while i < len(raw_argv):
        tok = raw_argv[i]
        if tok == "--":
            out.extend(raw_argv[i:])
            break
        if tok in drop:
            i += 2 if drop[tok] else 1
            continue
        out.append(tok)
        i += 1
    return out


def _target_root(opts: "ShellOptions") -> Path:
    """The codebase this invocation OPERATES ON — `--repo` when given,
    else the ordinary project-root resolution every hs-* CLI shares
    (`harness_root.resolve_project_root`). Never hs-code's OWN installation
    root (`_ROOT`), which is a completely different question — see the
    module docstring's `--repo` entry."""
    if opts.repo:
        return Path(opts.repo).resolve()
    root, _source = harness_root.resolve_project_root()
    return root


# ------------------------------------------------------------------------ help

def _zero_domains_message(sources: list) -> str:
    return ("registry loaded 0 domains from sources=%s — check harness/data/%s "
            "and harness/data/%s/*.yaml are present" % (sources, _REGISTRY_NAME, _REGISTRY_DIR_NAME))


def _codemap_declares_schema_version(codemap_path: Path) -> bool:
    """A plain YAML peek (never the strict descriptor loader) at whether
    `codemap_path` even carries a `schema_version:` key. An unparseable or
    non-mapping document reads as `False` here, on purpose: that failure
    belongs to whichever reader actually needs the document next (the
    strict v2 loader for a real v2 document, `pack_config.read_enabled_
    preset` for everything else) — this peek exists only to route AROUND
    the strict loader for a document that structurally cannot carry one,
    never to report an error of its own."""
    import yaml_io
    try:
        doc = yaml_io.yaml_load(codemap_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a parse failure is someone else's to report
        return False
    return isinstance(doc, dict) and "schema_version" in doc


def _pack_frag_dirs(opts: "ShellOptions") -> tuple:
    """Every enabled pack's own `hs-code-registry.d/` — resolved from the
    TARGET PROJECT's own `codemap.yaml` (`_target_root(opts)`, `--repo` or
    the resolved project root), never a harness-global file: which packs a
    project turns on is a per-project decision (a global install serves many
    projects off ONE shared binary, so a bin-global config would enable a
    pack for all of them at once — see `preset`'s own field docstring in
    `codegraph/_core/descriptor/model.py`).

    A project with no `codemap.yaml` at all has no packs enabled (packs are
    opt-in; most invocations never touch this file) — not an error. A
    `codemap.yaml` that exists but fails the descriptor's own validation IS
    an error (the same "one door, no partial trust" the `check`/`graph`
    domains already apply to this file) — validated HERE, via the real
    descriptor loader, rather than inside `pack_config.read_enabled_preset`
    (a plain-YAML reader on purpose: `harness/scripts/hs_family/
    gate_registry.py` calls the same function and must never reach
    `codegraph` — see that reader's own docstring). Preset paths resolve
    against `_ROOT` (where `packs/` physically lives — this install's own
    root, the shared bin under a global install), never against the project
    root: a project OPTS IN to a shared pack, it does not carry a private
    copy."""
    from codegraph._core.descriptor import DescriptorError, load as _load_descriptor
    codemap_path = _target_root(opts) / _CODEMAP_NAME
    if codemap_path.is_file() and _codemap_declares_schema_version(codemap_path):
        # `preset:` is a v2-only key (`descriptor/model.py`) -- a document
        # with no `schema_version` at all can never declare one, so it has
        # nothing here to validate strictly. Reaching for the full v2
        # loader anyway would wrongly route a LEGACY document (one meant
        # for a different domain's own codec, e.g. `hs-code port snapshot`
        # + `port_legacy_snapshot.register_all()`, which only registers
        # once THAT verb runs -- after this dispatch-time call) into the
        # descriptor's "no registered legacy codec claims it" refusal, on
        # every single invocation against the project, regardless of
        # domain (measured: `port snapshot` against its own legacy
        # fixture -- `test_a_legacy_schema_version_less_codemap_does_not_
        # block_dispatch` pins the fix).
        try:
            _load_descriptor(codemap_path, codemap_path.parent)
        except DescriptorError as e:
            raise hs_family.UsageError(str(e)) from e
    preset = pack_config.read_enabled_preset(_target_root(opts))
    frag_dirs, problems = pack_config.resolve_frag_dirs(_ROOT, preset)
    if problems:
        raise hs_family.UsageError(
            "%s: %s" % (_target_root(opts) / _CODEMAP_NAME, "; ".join(problems)))
    return frag_dirs


# --------------------------------------------------------------------- dispatch

class HsCodeCli(_family_cli.PackMixin, _family_cli.CliBase):
    prog, depth = "hs-code", 2
    description = ("hs-code: measure, restructure, test and port a codebase's own map, "
                   "behind a data-driven <domain> <verb> dispatch table "
                   "(harness/data/%s)." % _REGISTRY_NAME)
    shell_flags = (
        _family_cli.ShellFlag("--repo", "repo", takes_value=True, identity=False,
                              help="the codebase to operate on (default: the project root)"),
        _family_cli.ShellFlag("--write", "write", takes_value=True, identity=False,
                              help="also publish the verb's payload to FILE"),
        _family_cli.ShellFlag("--apply", "apply", help="write for real (default: dry run)"),
        _family_cli.ShellFlag("--fresh", "fresh",
                              help="measure the tree as it is on disk now (uncommitted files "
                                   "included); a graph query files that measurement in this "
                                   "worktree's own base and reuses it for identical content"),
        _family_cli.ShellFlag("--allow-stale", "allow_stale",
                              help="read a snapshot older than HEAD anyway"),
        _family_cli.ShellFlag("--step", "step", takes_value=True,
                              help="record this call in the port domain's own "
                                   "BATCH/runs.jsonl (BATCH/STEP[/DIST])"),
    )

    def registry_env(self):
        return _REGISTRY_ENV

    def pack_frag_dirs(self, flags) -> tuple:
        return _pack_frag_dirs(_options(flags))

    def check_flags(self, flags) -> None:
        if flags.get("fresh") and flags.get("allow_stale"):
            raise hs_family.UsageError("--fresh and --allow-stale are mutually exclusive")

    def project_root(self, flags) -> Path:
        return _target_root(_options(flags))

    def empty_message(self, table) -> str:
        return _zero_domains_message(_family_cli.RegistrySource(
            self.registry_env(), self.install_root, depth=self.depth).load().sources)

    def wants_help(self, cmd, args) -> bool:
        return hs_verb_help.wants_help(args)

    def bind(self, ns, ctx) -> None:
        ns.opts = _options(ctx.flags, text=bool(self._call.get("text")))
        if ns.opts.write is not None and not Path(ns.opts.write).parent.is_dir():
            raise hs_family.UsageError("--write: directory %s does not exist"
                                       % Path(ns.opts.write).parent)
        if ns.opts.step is not None:
            # Refuse BEFORE the verb runs — a bad shape or an unknown batch must
            # never let the command underneath execute at all (see port_runs.py's
            # own module docstring for why this is a pre-run refusal, not a
            # post-hoc one).
            import port_runs
            port_runs.resolve_step(_target_root(ns.opts), ns.opts.step)

    def run_command(self, cmd, ns):
        """Every snapshot read and every fresh measure the verb makes is recorded at
        its own door (`codegraph_snapshot_reader.recording`); keep the events for
        `finish()` to put on the envelope."""
        import codegraph_snapshot_reader
        with codegraph_snapshot_reader.recording() as events:
            try:
                return cmd.run(ns)
            finally:
                if events:
                    self._call["measured_events"] = (list(events), _target_root(ns.opts))

    def _measured_keys(self) -> dict:
        """`measured` (+ `measured_trees` past one distinct tree) for this call's
        envelope; `{}` when the verb read no tree."""
        recorded = self._call.get("measured_events")
        if not recorded:
            return {}
        from codegraph._core.snapshot.freshness import provenance_rows
        rows = provenance_rows(*recorded)
        if not rows:
            return {}
        return {"measured": rows[0], **({"measured_trees": rows} if len(rows) > 1 else {})}

    def envelope_for(self, command, **kw) -> dict:
        """A verb that read a tree and then refused or broke still says which tree."""
        return {**super().envelope_for(command, **kw), **self._measured_keys()}

    def main(self, argv: "list | None" = None) -> int:
        """Wraps the shared dispatcher SOLELY to append the `--step` run
        record AFTER the call is fully decided — success, a deliberate
        refusal (`hs_family.RefusedError`/`UsageError`, which unwinds past
        `finish()` straight to `main()`'s own `except HsFamilyError`), or a
        crash. One write door regardless of path, using the FINAL `rc` —
        never `outcome.exit_code`, which a refusal never reaches. `bind()`
        already refused BEFORE the verb ran when `--step` itself was
        malformed or named an unknown batch; re-resolving that same
        problem here means there is nothing valid to write into, so this
        never lets a bookkeeping failure change what the caller already
        saw (the same fail-open posture `_log_call` already carries for
        its own telemetry line)."""
        rc = super().main(argv)
        try:
            self._record_step_run(rc)
        except Exception:  # noqa: BLE001 — bookkeeping never overrides the real rc
            pass
        return rc

    def _record_step_run(self, rc: int) -> None:
        raw_argv = list(self._call.get("argv", ()))
        step = None
        i = 0
        while i < len(raw_argv):
            if raw_argv[i] == "--step" and i + 1 < len(raw_argv):
                step = raw_argv[i + 1]
                break
            i += 1
        if step is None:
            return
        import hs_family
        import port_runs
        _clean, flags = self.strip_shell_flags(list(raw_argv))
        if "-h" in _clean or "--help" in _clean or "--version" in _clean or "--list" in _clean:
            return  # a help/meta call never ran a step — nothing to record
        opts = _options(flags)
        try:
            port_root, batch, step_id, dist = port_runs.resolve_step(
                _target_root(opts), step)
        except hs_family.HsFamilyError:
            return
        argv_recorded = _step_argv(raw_argv, self.shell_flags)
        port_runs.append_run(port_root, batch.name, step_id, dist, argv_recorded, rc)
        self._write_step_handoff(port_root, batch, step_id, dist, argv_recorded)

    def _write_step_handoff(self, port_root: Path, batch, step_id: str, dist, argv_recorded) -> None:
        """`.port/batches/<b>/handoff.json` bookkeeping —
        fires ONLY when the verb's OWN envelope `data` carried a `handoff`
        key (`self._call["handoff_items"]`, captured in `finish()` below);
        most verbs (anything that is not `build`/`tests-land`) never set it,
        so most `--step` calls never touch this file. Never lets a write
        failure change the caller's `rc` — the same fail-open posture
        `main()`'s own wrapper already documents — but a failure DOES print
        a warning line to stderr rather than vanishing silently (stdout
        stays exactly the one envelope line `_dispatch` already emitted;
        this call always runs strictly AFTER that line is on the wire, so
        there is no envelope left to add a `warnings` entry to)."""
        items = self._call.get("handoff_items")
        if items is None:
            return  # the verb reported no `handoff` key at all -- nothing to do
        import port_handoff
        try:
            source = _handoff_source(batch)
            target_head = _handoff_target_head(port_root)
            mechanical_done = _handoff_mechanical_done(port_root, batch)
            done_when_cmd = "hs-code --step %s %s" % (
                "%s/%s%s" % (batch.name, step_id, ("/%s" % dist if dist else "")),
                " ".join(argv_recorded))
            port_handoff.write_step_items(
                port_root, batch.name, source=source, target_head=target_head,
                mechanical_done=mechanical_done, step=step_id, dist=dist,
                done_when_cmd=done_when_cmd, items=items)
        except Exception as exc:  # noqa: BLE001 — bookkeeping never overrides the real rc
            print("hs-code: --step handoff write failed: %s" % exc, file=sys.stderr)

    def finish(self, row, ctx, outcome, output) -> dict:
        measured = self._measured_keys()
        if measured:
            outcome = attrs.evolve(outcome, extra={**outcome.extra, **measured})
        env = super().finish(row, ctx, outcome, output)
        data = env.get("data")
        if isinstance(data, dict) and "handoff" in data:
            # Captured NOW, while `env["data"]` exists — `_record_step_run`
            # runs strictly AFTER this envelope is already on stdout (see
            # `main()`'s own docstring), so this is the only place the
            # verb's own reported items are ever visible.
            self._call["handoff_items"] = data["handoff"]
        write = ctx.flags.get("write")
        if write is not None:
            if "data" not in env:
                raise hs_family.UsageError("--write: `hs-code %s` answered with no JSON data, so "
                                           "there is nothing to write" % " ".join(ctx.command))
            import json
            from artifact_io import atomic_write_text
            atomic_write_text(Path(write), json.dumps(env["data"], ensure_ascii=False, indent=2)
                              + "\n", newline="")
        # The walk-up disclosure, hs-code's half of the seam hs-run already
        # carries: when the TARGET was resolved by `_target_root()`'s shared
        # resolver walking up from cwd to a different directory (no `--repo`
        # pin), say so on the envelope — the same one shared definition
        # (`harness_root.walkup_disclosure`). Re-resolving here is safe: the
        # resolution is pure over the same env the verbs just ran under, and
        # the disclosure itself changes no answer and no exit code. A
        # `--repo`-pinned target is the caller's own choice and stays quiet.
        # Deliberately no `root`/`root_source` envelope fields here — that
        # would be an envelope-contract change, out of this card's scope.
        if not ctx.flags.get("repo"):
            root, source = harness_root.resolve_project_root()
            disclosure = harness_root.walkup_disclosure(root, source)
            if disclosure is not None:
                env.setdefault("warnings", []).append(disclosure)
        return env


def _handoff_source(batch) -> dict:
    """`{id, sha}` for `handoff.json`'s own `source` field — the batch's
    FIRST declared source (`batch.yaml sources[0]`; `pinned_sha` is already
    validated 40-hex by `port_batch_reader`). A batch declaring more than
    one source still has exactly one line here: which source produced any
    ONE step's finding is not tracked per-item today (no producer verb
    currently reports it), so the batch's own primary pin is the honest
    answer rather than a guess."""
    src = batch.sources[0]
    return {"id": src.get("id"), "sha": src.get("pinned_sha")}


def _handoff_target_head(port_root: Path) -> "str | None":
    """The target repo's current git HEAD, or `None` when it cannot be
    read (no git repo yet, a shallow clone with no HEAD, ...) — never a
    reason to fail the write. Read-only (`rev-parse`), never a commit/push:
    unlike every `port_*.py`/`codegraph_port_*.py` verb module, `hs_code.py`
    itself is outside `test_port_verb_modules_never_write_git.py`'s glob,
    but this call still only ever reads."""
    try:
        from codegraph._core.git import git_text
        head = git_text(port_root, "rev-parse", "HEAD", check=False)
    except Exception:  # noqa: BLE001 — bookkeeping never overrides the real rc
        return None
    return head or None


def _handoff_mechanical_done(port_root: Path, batch) -> "list[str]":
    """The batch's own lane step ids whose LAST run is green right now —
    see `port_runs.green_step_ids`'s own docstring for the exact criterion.
    Fails open to `[]`: an unmeasurable lane is not a reason to refuse the
    write, and `mechanical_done` is a courtesy summary for a human reader,
    never a decision `hs-run port next` re-derives from this file."""
    import port_runs
    try:
        return port_runs.green_step_ids(port_root, batch)
    except Exception:  # noqa: BLE001 — bookkeeping never overrides the real rc
        return []


def call_verb(domain: str, verb: str, argv, root, opts: "ShellOptions | None" = None):
    """Run one registered verb in-process the way `hs-code <domain> <verb>` does, minus the
    envelope: its row off the same table, its parser, `ns.ctx`, `ns.opts`, `run(ns)` — for
    a test, or a verb that runs another. What the verb prints goes to stdout as it would
    under `--text` when `opts.json` is False; its return value comes back unchanged."""
    opts = opts or ShellOptions()
    cli = HsCodeCli(install_root=_ROOT)
    row = cli.table(opts.as_dict()).find((domain, verb))
    if row is None or row.moved_to is not None:
        raise hs_family.UsageError("no hs-code verb %s %s" % (domain, verb))
    ctx = _family_cli.Context(cli="hs-code", command=(domain, verb), root=Path(root),
                              flags=opts.as_dict())
    cmd = row.load()
    parser = _family_cli.CommandParser(prog="hs-code %s %s" % (domain, verb))
    cmd.add_args(parser)
    ns = _family_cli.parse(cmd, parser, list(argv or []))
    ns.ctx, ns.opts = ctx, opts
    return cmd.run(ns)


def main(argv=None) -> int:
    return HsCodeCli(install_root=_ROOT).main(argv)


if __name__ == "__main__":
    sys.exit(main())
