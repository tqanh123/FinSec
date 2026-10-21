#!/usr/bin/env python3
"""gate_stage.py — PreToolUse(Bash) compliance hook: the stage gate.

Detects whether the incoming Bash command advances an SDLC stage
(stage_detector, boundary-strict). Personal-first: a missing/failed receipt on a
hard stage is NO LONGER a local block — it is surfaced as an advisory + trace, and
presence enforcement lives in remote CI (receipts-gate). The ONE hard local block
is the artifact-forgery arm (a shell write to a receipt path — the agent cage);
an internal crash / missing dependency still fails closed (exit 2) via the wrapper.

HONESTY: this is a PRESENCE gate — it proves the gated step RAN (the
artifact exists and satisfies the verdict policy), NOT who ran it. An agent
writing its own PASS artifact passes it. Actor on trace events is
attribution, never authorization. Role checks live in plan_approval, not here.

Every decision is traced with actor: gate_advisory (hard-stage receipt gap, no
block) / gate_pass / gate_skip (always with a reason) / stage_guess (advisory
sampling of free-floating stage words — evasion like `sh -c 'git push'` shows up
here and is otherwise caught at the transport level by the git pre-push hook).
Config consumption is traced as gate_config_loaded with the policy file hash
(tamper-visible).
"""

import hashlib
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.append(_HERE)
sys.path.append(os.path.join(os.path.dirname(_HERE), "scripts"))

import hook_runtime   # noqa: E402
import shell_unwrap   # noqa: E402
import trace_log      # noqa: E402

HOOK_CLASS = "compliance"
_HOOK = "gate_stage"

# Plan artifacts are produced ONLY by their skill controller via the Write tool
# (traced). A shell-spelled write into one is forgery — an agent fabricating a
# verdict to clear this gate. Blocked PRE here, the last point a Bash write can
# be stopped (bash_write_guard is PostToolUse, too late).
_ARTIFACT_GLOBS = ("plans/*/artifacts/*.json", "plans/*/artifacts/*.yaml")
# A regex over arbitrary bash cannot reconstruct every write-target spelling
# (subshell/pushd cwd, tee/sed/dd, python joins, concatenated quotes). So the
# forgery gate ALSO does a coarse region check: the artifacts DIR appearing in a
# write-capable command is forgery, whatever the exact target. Over-blocks a
# python/redirect READ of an artifact — acceptable (use the Read tool).
_ARTIFACT_DIR = r"plans/[^/\s'\";|&]*/artifacts"
# KNOWN GAP: the forgery regex does not detect subshell `(cd plans/.../artifacts ...)`
# or paths with encoded whitespace; the CD/redirect matchers below catch most variants
# and the post-push transport gate is the authoritative floor.
_ARTIFACT_REGION_RE = re.compile(_ARTIFACT_DIR + r"\b")
# Floor vectors the precise shell_write_targets parser misses. EACH requires the
# artifact path in a WRITE-TARGET position (redirect target, cd-into-artifacts + a
# write, or a python -c writing to it) — so reading/copying an artifact OUT
# (`cat art > /tmp/x`, `cp art /tmp`) or merely naming it in written content
# (`echo "...art..." >> doc`) is NOT over-blocked; direct tee/dd/sed/redirect to an
# artifact stay covered by shell_write_targets below.
_CONCAT_REDIR_RE = re.compile(r"(?:^|[^&\d])>>?\|?\s*[^\s|;&<>()]*" + _ARTIFACT_DIR)
_CD_INTO_ART_RE = re.compile(r"\b(?:cd|pushd)\s+[^\s|;&]*" + _ARTIFACT_DIR)
_TEE_TARGET_RE = re.compile(r"\btee\b(?:\s+-\S+)*\s+[^\s|;&]*" + _ARTIFACT_DIR)
_DD_TARGET_RE = re.compile(r"\bdd\b[^|;&]*\bof=[^\s|;&]*" + _ARTIFACT_DIR)
_ANY_WRITE_RE = re.compile(
    r"(?:^|[^&\d])>>?\|?\s*[^&\s>]|\btee\b|\bdd\b|\bsed\b|\btruncate\b")
_PY_C_RE = re.compile(r"\bpython[0-9.]*\b[^|;&]*?\s-c\b")
# An interpreter reading its PROGRAM from a heredoc on stdin: `python3 - <<PY`.
# A separate arm because neither existing layer sees it. Measured:
# shell_write_targets returns [] for every heredoc spelling AND for the bare body
# text — it only knows the `python -c` form — and the coarse battery never sees
# the body at all, since the heredoc strip below removes it before matching. So
# this arm reads the ORIGINAL command, deliberately: the strip is correct for the
# shell matchers (a body is data to them) and wrong for this one (here the body
# is the program). Do not "unify" the two by feeding this the stripped text.
_INTERP_STDIN_RE = re.compile(
    r"\b(?:python[0-9.]*|perl|ruby|node|php)\b[^|;&\n]*?<<-?\s*['\"]?\w")
# `open(...)`'s write-detection needs an actual MODE argument, not a
# substring hunt: the old form `open\s*\([^)]*['"][^'"]*[wax]` matched any
# quoted content containing a w/a/x character anywhere — and "artifacts"
# itself starts with 'a', so a pure `open('.../artifacts/x.json')` READ
# (default mode 'r') matched and was refused as forgery. The fixed clause
# requires a comma-separated second argument (positional or `mode=`) whose
# QUOTED VALUE is, in full, a realistic write-capable Python file mode
# (w/a/x, optionally +/b/t) — never a substring of the path or an unrelated
# kwarg value (`errors='ascii'` must not read as mode='a...').
_MODE_LITERAL = r"""['"](?:[wax][bt]?\+?|[wax]\+[bt]?)['"]"""
_PY_WRITE_METHOD_RE = re.compile(
    r"\.write_(?:text|bytes)\b"
    r"|open\s*\([^()]*,\s*(?:\w+\s*=\s*)?" + _MODE_LITERAL)

def _load_artifact_producers():
    """Declared CLI producers (harness/data/artifact-producers.yaml), each
    the one case where the caller IS the producing CLI and needs its own
    output flag rather than the Write tool. ADVICE ONLY: this never decides
    block vs allow (that stays the coarse battery below) — a malformed or
    missing config just falls back to the generic "use the Write tool"
    wording, never to a crash or a changed verdict. `match` is tested at a
    genuine command HEAD via stage_detector's own wrapper reach (`_HEAD`,
    which already peels env VAR=/sudo/uv run [--project <dir>]/etc.), so a
    producer's `match` regex never needs to spell a wrapper itself.

    No process-local cache: this hook runs as a fresh subprocess per Bash
    call, so a module cache would only help across segments of ONE command
    against a two-entry file — not worth the test-isolation cost (config_
    layers already caches the LAYER resolution; only the small yaml parse
    repeats)."""
    import config_layers
    import stage_detector as _sd
    doc, _diag = config_layers.read_soft("artifact-producers.yaml")
    raw = doc.get("producers") if isinstance(doc, dict) else None
    out = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            match = entry.get("match")
            out_flag = entry.get("out_flag")
            if not isinstance(match, str) or not match.strip():
                continue
            if not isinstance(out_flag, str) or not out_flag.strip():
                continue
            try:
                head_re = re.compile(_sd._HEAD + match)
            except re.error:
                continue
            out.append({"head_re": head_re, "out_flag": out_flag})
    return out


def _declared_producer(stripped):
    """The first declared producer whose command head matches `stripped`,
    or None."""
    for producer in _load_artifact_producers():
        if producer["head_re"].search(stripped):
            return producer
    return None


_ABS_ARTIFACT_RE = re.compile(r"(/[^\s'\";|&>]*?/)?" + _ARTIFACT_DIR + r"\b")


def _only_names_artifacts_outside_the_repo(stripped):
    """True when every artifact path in the command is absolute and lands elsewhere.

    The region check matches the SHAPE `plans/<x>/artifacts` wherever it appears, with
    no idea where the write ends up. That refused a probe building a fixture tree under
    /tmp, and refused it three times in a row — while the same write went through the
    moment it moved into a .py file the gate does not read. It cost work and stopped
    nobody who meant to get around it.

    A path this gate can prove is outside the CURRENT working tree cannot forge a
    receipt the gate reads. RELATIVE paths stay blocked: they resolve against a cwd
    the hook cannot see, and that ambiguity is exactly a forger's cover. So this only
    fires when the command names artifact regions and EVERY one of them is an
    absolute path outside the root — one in-repo mention and the normal matchers run.

    Uses protected_trees.protected_working_trees(), NOT project_root(): each Bash
    call may run with a DIFFERENT cwd (a linked worktree living OUTSIDE its
    host, the default `hs:worktree` shape), and project_root() answers "which
    ONE tree do reads resolve to for the CURRENT cwd" — the wrong shape for a
    forgery cage, which must protect every tree a forged artifact could ever be
    read FROM, not just the one cwd happens to read right now (a write let
    through while standing in the worktree still sits on disk for a later
    invocation whose cwd is back at the host). protected_working_trees()
    returns exactly that union: EVERY member `git worktree list` reports for
    the anchor's own repo (host + every linked worktree, including one
    NESTED under the host — the default `.claude/worktrees/` layout — not
    just a sibling one lives outside it), plus, when the anchor itself is a
    subdirectory of its own worktree's toplevel (a project inside a
    monorepo), that same relative subdirectory under every OTHER member too.
    A worktree of some OTHER, unrelated project (measured: a parallel-agent
    isolation worktree under /tmp) never joins the union — its own host does
    not match the anchor.
    """
    hits = list(_ABS_ARTIFACT_RE.finditer(stripped))
    if not hits:
        return False
    try:
        import protected_trees
        roots = protected_trees.protected_working_trees()
        if not roots:
            return False  # no tree resolved: judge nothing, block as before
        root_strs = [str(r) for r in roots]
    except Exception:  # noqa: BLE001 — no root resolved: judge nothing, block as before
        return False
    for m in hits:
        text = m.group(0)
        if not text.startswith("/"):
            return False          # relative — cwd unknown, keep blocking
        if any(text == r or text.startswith(r.rstrip("/") + "/") for r in root_strs):
            return False          # inside one of the protected trees
    return True


def _forgery_reason_in_segment(segment):
    """The coarse-battery forgery check, scoped to ONE top-level segment.
    Extracted so `_artifact_forgery_reason` can run it independently per
    segment — a write pattern in one segment must never be paired with a
    bare artifacts-region mention that only lives in an unrelated one."""
    if not segment or not segment.strip():
        return None
    # Heredoc bodies are data to the shell matchers below, so they come out first.
    # This used to be an inline regex requiring the closing delimiter immediately
    # after a newline, which three LEGAL spellings defeat: `<<-DELIM` (tab-indented
    # closer), an unterminated body, and a mismatched closer. When the strip failed
    # the body leaked into the redirect matcher, and three commands whose only write
    # went to /tmp were refused. Shared locator now, not a private regex — a caller
    # with its own heredoc parser gets to be wrong in its own private way.
    nohere = shell_unwrap.without_heredocs(segment)
    # A commit message or echo/printf argument is DATA — it is stored or
    # printed, never executed or used as a write target — so text it
    # happens to contain must not read as the shell's own syntax to the
    # coarse matchers below. Same false-positive class `without_heredocs`
    # above already exists to close for heredoc bodies; this closes it for
    # -m/--message and echo/printf specifically. Quote-preserving (masking
    # only blanks the argument text, in place) so the mode-literal check
    # just below still has the quotes it needs.
    masked = shell_unwrap.mask_literal_data_arguments(nohere)
    stripped = re.sub(r"\$?['\"]", "", masked)
    # The raw segment, not `stripped`: heredoc removal takes the interpreter body with
    # it, so a /tmp path living in that body is invisible here — and the arm below that
    # judges heredocs reads the raw segment anyway. Carving out only the stripped text
    # left that arm refusing the same write the moment it moved into a heredoc body:
    # fixed in one spelling, live in the other.
    if _only_names_artifacts_outside_the_repo(re.sub(r"\$?['\"]", "", segment)):
        return None
    forged = (
        _CONCAT_REDIR_RE.search(stripped)
        or _TEE_TARGET_RE.search(stripped)
        or _DD_TARGET_RE.search(stripped)
        or (_CD_INTO_ART_RE.search(stripped) and _ANY_WRITE_RE.search(stripped))
        # `masked`, NOT `stripped`, for the write-method check: it needs an
        # actual quoted mode literal ('w', 'a', ...), and `stripped` has
        # already removed every quote character — against `stripped` this
        # conjunct could never match ANY write, quoted or not, real python-c
        # writes included. `masked` still carries quotes (masking only
        # blanks argument TEXT, in place); `_PY_C_RE`/`_ARTIFACT_REGION_RE`
        # don't care about quotes either way, so they stay on `stripped`.
        or (_PY_C_RE.search(stripped) and _ARTIFACT_REGION_RE.search(stripped)
            and _PY_WRITE_METHOD_RE.search(masked))
        # Interpreter-on-stdin. Reads `segment`, NOT `stripped`: the program lives
        # in the body that `nohere` just removed, and the write method needs the
        # quotes that `stripped` removes. Three conjuncts, same shape as the
        # `python -c` arm above — an interpreter heredoc that writes nowhere, or
        # writes while never naming an artifact region, stays allowed. One that
        # writes AND names the region anywhere is refused even when the write goes
        # elsewhere: a variable holding the path reads the same, so the target
        # cannot be told apart and the cage over-blocks on purpose.
        or (_INTERP_STDIN_RE.search(segment)
            and _ARTIFACT_REGION_RE.search(segment)
            and _PY_WRITE_METHOD_RE.search(segment))
    )
    if not forged:
        return None
    # A declared producer CLI redirected into an artifact path looks EXACTLY
    # like forgery from here and stays blocked — but "use the Write tool" is
    # not a route a CLI has, so that advice alone turned a legitimate
    # `grid_engine.py review ... > .../grid-verdict.json` into a dead
    # end. Name the flag that does work when a producing CLI is what is
    # being run; keep the Write-tool route for a bare shell write, which
    # has no CLI to point at. _declared_producer only ever changes this
    # WORDING — the `forged` verdict above is already final.
    producer = _declared_producer(stripped)
    route = (
        " That command runs a producing CLI: pass its own output flag "
        "(`%s <path>`) instead of a shell redirect." % producer["out_flag"]
        if producer else
        " If you are the producing skill, use the Write tool."
    )
    return (
        "a shell command writes a file and names a plans/<plan>/artifacts/ path, "
        "and its target cannot be told apart from that artifact — gate "
        "artifacts are written ONLY by their producing "
        "skill (Write tool / its CLI), never the shell. This is forgery and is "
        "blocked." + route
    )


def _artifact_forgery_reason(command, cwd=None):
    """Block reason if ``command`` shell-writes a plan artifact, else None.
    Reuses the shared shell-write-target parser with copy/move INCLUDED.
    `cwd` is the hook payload's own `cwd` field when the caller has one — the
    anchor a relative shell token resolves against when the command itself
    never `cd`s, so `cp f lnk/verification.json` resolves against the REAL
    working directory the Bash tool actually ran in, not a protected-tree
    root. A parser error never fabricates a block (returns None)."""
    if not command:
        return None
    # Evaluated per TOP-LEVEL SEGMENT (`;`/`&`/`|`/newline-separated, a
    # heredoc body kept glued to its own opener — shell_unwrap.top_level_
    # segments), never the whole raw command: a write pattern in one
    # segment merged with a bare artifacts-region mention in an unrelated
    # one refused two measured commands that were not forgery — a heredoc
    # writing plan.md alongside a LATER `git add plans/<plan>/artifacts/...`
    # in the same shell line, and a producing CLI's `--out` flag alongside
    # an unrelated write elsewhere. Both pass run alone.
    for segment in shell_unwrap.top_level_segments(command):
        reason = _forgery_reason_in_segment(segment)
        if reason:
            return reason
    nohere = shell_unwrap.without_heredocs(command)
    try:
        import fnmatch
        import bash_write_guard
        import protected_trees
        import write_guard
        # Every cp/mv/redirect destination resolved to an ABSOLUTE path — a
        # relative token anchored at the command's own tracked `cd`, else at
        # `cwd` (the hook payload's own cwd, never a protected-tree root),
        # symlinks followed — the union (one `git worktree list`) is resolved
        # only when one of them could land on an artifact under SOME tree.
        # Text-matching the command for `plans/<plan>/artifacts` instead
        # misses `cd plans/p && cp f artifacts/x.json` and a symlinked
        # destination; anchoring at a bare root (rather than the real cwd)
        # missed a RELATIVE symlinked destination (`cp f lnk/x.json`) — the
        # resolved path never followed `lnk` because it was never joined to
        # the directory the symlink actually lives in.
        candidates = bash_write_guard.shell_write_absolute_targets(
            nohere, include_copy_move=True, cwd=cwd)
    except Exception:  # noqa: BLE001 — a parser error must not invent a block
        return None
    if not any(write_guard.could_match_tail(str(c), _ARTIFACT_GLOBS) for c in candidates):
        return None
    try:
        # roots=protected_working_trees(), NEVER the bare default (bin_root):
        # a cp/mv destination is a project ARTIFACT, so it must resolve
        # against the same host+worktree union the `>`/tee arm above already
        # uses (_only_names_artifacts_outside_the_repo) — an anchor of
        # bin_root let a cp/mv into a worktree's (or, under global, the
        # project's OWN) plans/*/artifacts/*.json pass while the identical
        # write via `>`/tee was already denied.
        roots = protected_trees.protected_working_trees()
    except protected_trees.WorktreeListError as exc:
        # The command names an artifacts-region shape and the repo's
        # worktree family could not be resolved (git itself failed — never
        # the legitimate "not a git repo" case, which protected_trees already
        # folds into (anchor,) without raising). Fail CLOSED: silently
        # falling through to `return None` here is exactly the
        # drop-forgery-protection bug this guard exists to prevent.
        return (
            "cannot resolve this repo's worktrees (git worktree list failed: "
            "%s) — refusing a shell write that names a plans/<plan>/artifacts/ "
            "region, which could be a protected artifact path. Retry once git "
            "is working again, or resolve the underlying git failure." % exc
        )
    except Exception:  # noqa: BLE001 — a parser error must not invent a block
        return None
    if not roots:
        # Global layout with no project named: judging against the bin (the
        # parser's default root) would pass a project artifact.
        return (
            "project root unresolved under a global install — refusing a shell "
            "write whose destination could be a plans/<plan>/artifacts/ file. "
            "Set CLAUDE_PROJECT_DIR (or HARNESS_DATA_ROOT) to the project."
        )
    try:
        targets = bash_write_guard.shell_write_targets(
            nohere, include_copy_move=True, roots=roots, cwd=cwd)
    except Exception:  # noqa: BLE001 — a parser error must not invent a block
        return None
    for rel in targets:
        if any(fnmatch.fnmatch(rel, pat) for pat in _ARTIFACT_GLOBS):
            return (
                "%s is a gate artifact — written ONLY by its producing skill "
                "via the Write tool (which the harness traces), never through "
                "the shell. A shell write here is forgery and is blocked. If "
                "you are the producing skill, use the Write tool." % rel
            )
    return None


def _policy_hash() -> str:
    """sha256 (12-hex) of the policy file bytes — ties the trace line to the
    exact config the decision used; any tampering shifts the hash."""
    import artifact_check
    try:
        return hashlib.sha256(
            artifact_check._policy_path().read_bytes()).hexdigest()[:12]
    except OSError:
        return "unreadable"


# Posture-env knobs that redirect the in-session gate's config. They are
# legitimate dev/test flexibility (the pre-push hook prefix-scrubs them and
# re-judges a push against tracked config), so the gate does NOT refuse them
# here — but it makes an active override tamper-EVIDENT: a stage gated under a
# redirected policy emits gate_env_override so the redirection is auditable,
# never silent. Prevention lives at the transport layer (pre-push) and the
# remote tier; in-session, evidence is the honest ceiling.
_POSTURE_ENV = ("HARNESS_STAGE_POLICY", "HARNESS_PROTECTED_BRANCHES",
                "HARNESS_GUARD_POLICY")


def _active_posture_overrides():
    return [k for k in _POSTURE_ENV if (os.environ.get(k) or "").strip()]


def _evaluate_dod(stage, root, session, tool_input, allow_completed=False):
    """DoD-by-change-class, folded into this ONE compliance hook. Runs
    only for a hard stage AFTER the presence check cleared. Returns a block
    reason (already routed through the test_policy_dod guard, so it respects the
    preset: block strict/balanced, warn lenient) or None to continue.

    The change-class is DERIVED from the git diff (honest — the gate does not
    trust a declared class); HARNESS_CHANGE_CLASS still short-circuits as an
    explicit, traced override. A graced class was already traced policy_grace at
    policy load. Every run emits one test-execution telemetry line.

    Fail-open boundary (honest): a DERIVATION or TELEMETRY hiccup
    must not fabricate a block — those stay fail-OPEN (traced). But a CRASH of
    our own evaluator is a defect that silently disables the gate, so it fails
    CLOSED (the common config-typo vector — an unquoted grace.expires — is turned
    into an actionable FAIL upstream, so only a genuinely unexpected crash lands
    here). The raw-result FAIL path returns a reason (routed through the
    test_policy_dod guard); the evaluator-crash path re-raises so the wrapper
    (run_compliance_hook) fails closed with exit 2."""
    import artifact_check
    import change_class_derivation as ccd

    plan_dir = artifact_check.resolve_active_plan(root, allow_completed=allow_completed)
    if plan_dir is None:
        # No plan to evaluate against. require_plan in check_stage (Layer A)
        # already blocks a team (require_plan:true) no-plan push; solo
        # (require_plan:false, the single-person carve-out) intentionally
        # reaches here and passes. A
        # "block here when require_plan" branch would be dead code behind Layer A.
        return None

    try:
        changed = ccd.repo_changed_paths(root)
        derivation = ccd.derive_from_repo(root)
    except Exception as e:  # noqa: BLE001 — a derivation bug never invents a block
        # derive_from_repo already degrades to [] on every git failure, so this
        # only catches an internal derivation bug. Stay fail-OPEN (no block) but
        # trace it loud so the audit shows the DoD was not evaluated.
        trace_log.append_event(hook=_HOOK, event="dod_derivation_failed",
                               session=session, tool="Bash", target=stage,
                               note="change-class derivation failed: %s" % e)
        sys.stderr.write(
            "[advisory] %s: change-class derivation failed (%s) — DoD not "
            "evaluated; set HARNESS_CHANGE_CLASS to force a class\n" % (_HOOK, e))
        return None
    try:
        verdict = artifact_check.evaluate_test_policy(
            plan_dir, derivation.cls, changed, root=root,
            ambiguous=derivation.ambiguous)
    except Exception as e:  # noqa: BLE001 — an evaluator crash is a DEFECT, fails CLOSED
        # A crash inside our OWN evaluator would silently disable the gate. Personal-
        # first relaxes the POLICY gates (a missing receipt → advisory, swallowed by
        # the hard_stage_advisory downgrade in core), but a harness self-defect is
        # NOT a policy matter: it re-raises so run_compliance_hook fails CLOSED
        # (exit 2) — the same wrapper arm that catches a missing-PyYAML import. The
        # owner fixes the defect now; it is never downgraded to a note remote CI may
        # re-run. (The common config-typo vector — an unquoted grace.expires — is
        # turned into an actionable FAIL upstream, so only a genuine crash lands here.)
        trace_log.append_event(hook=_HOOK, event="dod_eval_crash", session=session,
                               tool="Bash", target=stage,
                               note="DoD evaluation crashed: %s" % e)
        raise

    try:
        import test_result_readers
        test_result_readers.emit_test_execution(
            change_class=derivation.cls, signals=derivation.signals,
            test_types=[], verdict=verdict.status)
    except Exception:  # noqa: BLE001 — telemetry is fail-open
        pass

    if verdict.status != "FAIL":
        return None
    if verdict.enforcement != "hard":
        # soft / ambiguous / graced class: advisory only, never a block. Default
        # sink systemMessage (H2-resolved, INV-3 "gate_stage soft" — a stderr-on-
        # exit-0 advisory is spec-invisible); reroutable via nudge-channels.yaml.
        # emit_nudge's systemMessage path QUEUES (core() never writes stdout;
        # run_compliance_hook drains the queue into its single terminal write); a
        # stderr/relay/off reroute writes stderr/observation/nothing and the
        # wrapper then emits a plain continue.
        hook_runtime.emit_nudge(
            _HOOK,
            "[advisory] %s: %s (soft enforcement — not blocking)"
            % (_HOOK, verdict.reason),
            session=session, default_channel="systemMessage")
        trace_log.append_event(hook=_HOOK, event="dod_advisory", session=session,
                               tool="Bash", target=stage, note=verdict.reason)
        return None
    # hard FAIL → route through the independently-tunable test_policy_dod guard.
    import guard_policy
    return guard_policy.gate("test_policy_dod", verdict.reason, hook=_HOOK,
                             session=session)


def _emit_coverage_grid_advisory(stage, plan_dir, session, hard_advisory=True):
    """Advisory-only reader for the OPTIONAL coverage-grid gate artifact.

    Read-if-present, never a presence gate: no plan / no artifact / an
    unparseable artifact / a missing `verdict` field all resolve to silence
    (no stderr, no trace) — coverage-grid is not in any stage's `requires:`,
    so its absence is never a nag. Only a present `verdict` of `reject` or
    `needs-detail` surfaces (an `[advisory]` line gated the same as the
    existing hard-stage advisory + a `gate_advisory` trace, target
    `coverage-grid`); `pass` is silent.

    Fail-open ABSOLUTELY: this is a read of an artifact this hook does not
    own the schema for (a different owner module registers the kind), so any exception here
    (parse error, missing module, a future field-shape change) is swallowed
    — it must never turn an advisory read into a block. Side-effect only:
    always returns None, never a block reason."""
    if plan_dir is None:
        return None
    try:
        import artifact_check
        rec, _problem = artifact_check._load_artifact(plan_dir, "coverage-grid")
        if not isinstance(rec, dict):
            return None
        verdict = rec.get("verdict")
        if verdict not in ("reject", "needs-detail"):
            return None
        trace_log.append_event(hook=_HOOK, event="gate_advisory", session=session,
                               tool="Bash", target="coverage-grid",
                               status="ADVISORY",
                               note="coverage-grid verdict=%s" % verdict)
        if hard_advisory:
            sys.stderr.write(
                "[advisory] %s: coverage-grid verdict=%s — presence/verdict "
                "enforcement lives in remote CI (receipts-gate)\n"
                % (_HOOK, verdict))
    except Exception:  # noqa: BLE001 — an advisory read must never block the gate
        return None
    return None


def core(data: dict):
    """None ⇒ pass; string ⇒ block reason (run_compliance_hook contract)."""
    # Imports that need PyYAML stay INSIDE core so a machine that skipped
    # preflight fails through the wrapper's ImportError arm (exit 2 + install
    # command), never an unguarded import-time crash.
    import artifact_check
    import harness_paths
    import stage_detector

    # Shape guard: an unexpected payload shape means "no command to gate",
    # not an internal crash — letting an AttributeError bubble here would
    # block with a misleading "gate crashed" message.
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    command = hook_runtime.bash_command(data)
    session = data.get("session_id")

    forge = _artifact_forgery_reason(command, cwd=data.get("cwd"))
    if forge:
        trace_log.append_event(hook=_HOOK, event="gate_block", session=session,
                               tool="Bash", target="artifact-forgery",
                               status="BLOCKED", note=forge, tool_input=tool_input)
        return forge

    stage = stage_detector.detect_stage(command)
    if stage is None:
        guess = stage_detector.guess_stage(command)
        if guess:
            trace_log.append_event(hook=_HOOK, event="stage_guess",
                                   session=session, tool="Bash", target=guess,
                                   tool_input=tool_input)
        return None

    root = harness_paths.project_root()
    policy = artifact_check.load_policy()["stages"].get(stage) or {}
    trace_log.append_event(hook=_HOOK, event="gate_config_loaded",
                           session=session, target=stage,
                           note="stage-policy sha256:%s" % _policy_hash())

    overrides = _active_posture_overrides()
    if overrides:
        trace_log.append_event(
            hook=_HOOK, event="gate_env_override", session=session,
            target=stage,
            note="posture env override(s) active: %s — the in-session gate "
                 "honored a redirected policy; the pre-push transport re-judges "
                 "with tracked config. Audit the source." % ",".join(overrides))
        sys.stderr.write(
            "[advisory] %s: posture env override active (%s) — in-session policy "
            "may be redirected; tracked config still governs the transport push.\n"
            % (_HOOK, ",".join(overrides)))

    # Personal-first: local NEVER blocks the human on a missing/failed receipt.
    # Presence + DoD enforcement lives in remote CI (receipts-gate); locally we
    # surface it as an advisory + trace so the signal is not lost. The forgery-arm
    # above is the one hard local block (the agent cage). The HARD-stage advisory
    # line is suppressible via `hard_stage_advisory:false` (a SEPARATE knob from
    # `soft_stage_advisory`); the trace is emitted either way.
    hard_advisory = artifact_check.load_policy().get("hard_stage_advisory", True)

    # coverage-grid is read-if-present, advisory-only (never a `requires:`
    # presence gate) — runs independently of check_stage's own verdict below,
    # never affects core()'s return value. Gated on the per-stage `advises:`
    # key so stages that never opted in (e.g. commit) never read it.
    if "coverage-grid" in (policy.get("advises") or []):
        grid_plan_dir = artifact_check.resolve_active_plan(
            root, allow_completed=bool(policy.get("allow_completed_plan", False)))
        _emit_coverage_grid_advisory(stage, grid_plan_dir, session,
                                     hard_advisory=hard_advisory)

    reason = artifact_check.check_stage(stage, root)
    if reason:
        trace_log.append_event(hook=_HOOK, event="gate_advisory", session=session,
                               tool="Bash", target=stage, status="ADVISORY",
                               note=reason, tool_input=tool_input)
        if hard_advisory:
            sys.stderr.write(
                "[advisory] %s: %s — presence enforcement lives in remote CI "
                "(receipts-gate)\n" % (_HOOK, reason))
        return None

    if policy.get("hard"):
        dod_reason = _evaluate_dod(stage, root, session, tool_input,
                                   allow_completed=bool(policy.get("allow_completed_plan", False)))
        if dod_reason:
            trace_log.append_event(hook=_HOOK, event="gate_advisory", session=session,
                                   tool="Bash", target=stage, status="ADVISORY",
                                   note=dod_reason, tool_input=tool_input)
            if hard_advisory:
                sys.stderr.write(
                    "[advisory] %s: %s — enforced in remote CI (receipts-gate)\n"
                    % (_HOOK, dod_reason))
            return None
        trace_log.append_event(hook=_HOOK, event="gate_pass", session=session,
                               tool="Bash", target=stage, status="PASS",
                               tool_input=tool_input)
    elif artifact_check.load_policy().get("soft_stage_advisory", True):
        # Soft stage: never blocks; advisory note only (check_stage already
        # returned None — soft stages carry no enforced requirement). The
        # reminder is suppressible (soft_stage_advisory:false in stage-policy);
        # the decision is still loaded/traced above.
        sys.stderr.write("[advisory] %s: soft stage %r proceeding\n"
                         % (_HOOK, stage))
    return None


def main() -> None:
    # A skipped gate decision must stay VISIBLE in the audit trace (the shared
    # entry traces the skip with the config file that actually decided).
    hook_runtime.compliance_skip_or_run(
        _HOOK, core, skip_event="gate_skip",
        skip_note="disabled (enabled: false) via %s" % hook_runtime._config_path())


if __name__ == "__main__":
    main()
