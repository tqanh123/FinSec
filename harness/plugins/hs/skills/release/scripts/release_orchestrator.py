"""release_orchestrator — the deterministic 3-step harness release engine.

Single source of the release sequence; both the auto path (`--yes --version`)
and the interactive `/hs:release` skill call into it. The three steps, in hard
order:

  1. cut          — release.py writes CHANGELOG/manifest/release.json + local tag
  2. push tag     — git push HEAD + tag to origin (outward, confirm-gated) — this
                    is what fires .github/workflows/release.yml
  3. pack+verify  — pack dist/ + extract release notes LOCALLY, for verification
                    only (confirm-gated). It does NOT run `gh release create`:
                    .github/workflows/release.yml (softprops/action-gh-release) is
                    the sole publisher of the GitHub Release, triggered by the tag
                    step 2 already pushed. This step used to pre-create the release
                    itself, which made the workflow's own publish step fail on
                    conflict (reproduced on 5.3.0).

Runs only in the harness source repo: it drives release/ (release.py, pack.py,
release.yaml), which never ships.

Showcase publishing moved to CI (.github/workflows/sync-showcase.yml auto-syncs
the committed public/ to the release repo on push to main), so it is no longer a
release step.

Every step is idempotent: a re-run probes "already done for this version?" and
skips. Critically, the cut lock is LOCAL (CHANGELOG section + local tag +
release.json) and the push lock is REMOTE (git ls-remote) — so a push that died
after the cut does NOT re-cut (which would raise "already exists"); it just
re-pushes (F1).

stdlib only; all shell-outs go through an injectable `runner` so the flow is
testable without running CI / git / gh.
"""
import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent.parent.parent.parent.parent.parent / "release"
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent.parent.parent.parent / "release"))

_SCRIPTS = Path(__file__).resolve().parent.parent.parent.parent.parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from release import (  # noqa: E402  (sibling toolkit module; constants reused)
    REPO, TAG_PREFIX, CHANGELOG_REL, RELEASE_REL, RELEASE_FILES, plugin_readme_rels,
)
import yaml_io  # noqa: E402

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent.parent.parent.parent.parent / "release" / "release.yaml"

# Exit code returned (not raised) when an outward-facing step needs human OK.
NEEDS_CONFIRM = 10

_DEFAULTS = {
    "harness": {"branch": "main"},
    # Where preflight runs the CI: `remote` -- every job scripts/ci_local.sh dispatches
    # runs on the host `.rops.env` names, one `hs-rops <alias> --lane <lane> -- <job>`
    # each, and only the light diff-vs-main steps run here; `local` -- the whole of
    # scripts/ci_local.sh runs here. A host not configured or not answering stops the
    # cut rather than falling back.
    "ci": {"where": "remote", "lane": "release", "alias": "releaseci", "timeout_s": 3600},
}
_CI_WHERE = ("remote", "local")
_DISPATCH_RE = re.compile(r"^\s*bash scripts/ci\.sh ([a-z0-9-]+)\s*$")


def dispatched_jobs(root):
    """The scripts/ci.sh jobs scripts/ci_local.sh dispatches, in its order: the one
    list the remote cells run, read where a local run reads it."""
    p = Path(root) / "scripts" / "ci_local.sh"
    return [m.group(1) for m in map(_DISPATCH_RE.match, p.read_text(encoding="utf-8").splitlines())
            if m]


def _merge(base, over):
    """Recursive dict merge: over wins, nested dicts merge key-by-key (in-place,
    mutates and returns `base`).

    Delegates to `mergedeep.merge` (default REPLACE strategy) rather than a
    hand-rolled walk: verified byte-identical on this config's shape (scalar +
    nested-dict values, no lists — release.yaml carries none today; a future
    list-valued key needs re-verifying since `mergedeep`'s default strategy
    replaces lists wholesale, same as this function always did for non-dict
    values). See the mergedeep-settings-hooks-merge parity probe for the
    details."""
    import mergedeep
    return mergedeep.merge(base, over)


def _deepcopy(d):
    return {k: (_deepcopy(v) if isinstance(v, dict) else
                list(v) if isinstance(v, list) else v)
            for k, v in d.items()}


def load_config(path=None, overrides=None):
    """Built-in defaults < release.yaml < CLI overrides (recursive merge)."""
    cfg = _deepcopy(_DEFAULTS)
    p = Path(path) if path else DEFAULT_CONFIG
    if p.is_file():
        try:
            loaded = yaml_io.yaml_load(p.read_text(encoding="utf-8")) or {}
        except yaml_io.YAMLError as e:
            raise SystemExit("release config %s is not valid YAML: %s" % (p, e))
        if not isinstance(loaded, dict):
            raise SystemExit("release config %s is not a YAML mapping" % p)
        _merge(cfg, loaded)
    if overrides:
        _merge(cfg, overrides)
    # A nested key set to a scalar/null in release.yaml (`harness: solo`) clobbers
    # its default sub-dict — later `self.config["harness"].get(...)` would raise a
    # raw AttributeError, even during a read-only dry-run. Reject it here with an
    # actionable message instead (the top-level shape guard above misses the nested
    # case). The consumer indexes this key as a mapping.
    for key in ("harness", "ci"):
        if not isinstance(cfg.get(key), dict):
            raise SystemExit("release config key %r must be a mapping, got %s"
                             % (key, type(cfg.get(key)).__name__))
    if cfg["ci"].get("where") not in _CI_WHERE:
        raise SystemExit("release config ci.where must be one of %s, got %r"
                         % ("|".join(_CI_WHERE), cfg["ci"].get("where")))
    # `courier` is OPT-IN (no default in _DEFAULTS) — absent/null is fine (consumers
    # read `_courier_cfg() = self.config.get("courier") or {}`), but a present, truthy,
    # wrong-typed value must be rejected here, not crash later at `.get(...)`.
    if cfg.get("courier") is not None and not isinstance(cfg.get("courier"), dict):
        raise SystemExit("release config key 'courier' must be a mapping (or absent), "
                         "got %s" % type(cfg.get("courier")).__name__)
    return cfg


def _default_runner(cmd, cwd=None, check=False, capture_output=True, text=True):
    return subprocess.run(cmd, cwd=cwd, check=check,
                          capture_output=capture_output, text=text)


class Orchestrator:
    def __init__(self, root, version=None, *, apply=False, yes=False,
                 config=None, runner=None, printer=print):
        self.root = Path(root).resolve()
        self.version = version
        self.apply = apply
        self.dry_run = not apply
        self.yes = yes
        self._run = runner or _default_runner
        self._print = printer
        self.config = config or load_config()
        # a hand-built config without a `ci` block reads the defaults, never a KeyError
        self.config["ci"] = {**_DEFAULTS["ci"], **(self.config.get("ci") or {})}
        # Personal-first: releases are owner-only — the posture is always solo
        # (the owner fires their own outward work; no team print-only path).
        self.posture = "solo"
        self.tag = "%s%s" % (TAG_PREFIX, version) if version else None

    # ---- idempotency probes --------------------------------------------------

    def _cut_files_written(self):
        """release.py --apply has run: CHANGELOG section + release.json version match."""
        cl = self.root / CHANGELOG_REL
        if not cl.is_file() or ("## [%s]" % self.version) not in cl.read_text(encoding="utf-8"):
            return False
        rel = self.root / RELEASE_REL
        if not rel.is_file():
            return False
        import json
        try:
            _rj = json.loads(rel.read_text(encoding="utf-8"))
        except ValueError:
            return False
        return isinstance(_rj, dict) and _rj.get("harness_version") == self.version

    def _cut_committed(self):
        res = self._run(["git", "log", "-1", "--format=%s"], cwd=str(self.root),
                        capture_output=True)
        return (res.stdout or "").strip() == self._commit_subject()

    def _commit_subject(self):
        return "release: harness v%s" % self.version

    def cut_done_local(self):
        """The cut wrote everything locally: CHANGELOG section + release.json
        version + local tag all match the target."""
        if not self._cut_files_written():
            return False
        res = self._run(["git", "tag", "-l", self.tag], cwd=str(self.root),
                        capture_output=True)
        return bool((res.stdout or "").strip())

    def tag_on_remote(self):
        # a probe that cannot reach the remote reports 'not done', never raises —
        # the checklist stays usable offline / without the tool installed.
        try:
            res = self._run(["git", "ls-remote", "--tags", "origin", self.tag],
                            cwd=str(self.root), capture_output=True)
            return bool((res.stdout or "").strip())
        except Exception:  # noqa: BLE001
            return False

    def release_published(self):
        try:
            res = self._run(["gh", "release", "view", self.tag], cwd=str(self.root),
                            capture_output=True)
            return res.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    # ---- gate helpers --------------------------------------------------------

    def _confirm(self, action):
        """Auto mode: --yes is the confirmation. Otherwise stop and tell the
        operator how to proceed (the interactive skill supplies its own OK)."""
        if self.yes:
            return True
        self._print("[confirm needed] %s — re-run with --yes (auto) or use /hs:release"
                    % action)
        return False

    # ---- steps ---------------------------------------------------------------

    def preflight(self):
        """Hard gate before any cut: reconciled decisions + green CI + skill
        structure + clean tree + gh auth. Any failure aborts (SystemExit)."""
        # Decisions reconcile gate (cheap, first): refuse a cut while the register
        # has drifted since the last reconcile marker. Owner-only, release-local
        # — it never touches a normal ship/deploy, only the rare cut.
        recon = self._run(
            [sys.executable, "harness/scripts/decision_reconcile.py",
             "--root", str(self.root), "--status"],
            cwd=str(self.root), capture_output=True)
        try:
            drift = json.loads(recon.stdout or "{}")
        except ValueError:
            drift = {}
        if drift.get("never_reconciled") is True:
            raise SystemExit(
                "preflight failed (the decision register has NEVER been reconciled here "
                "— no marker in this project's state home) — run the decision-reconciler "
                "agent, then `decision_reconcile.py --mark`, before cutting. An absent "
                "marker is not a clean register; it used to read as one.")
        if drift.get("new_decs", 0) > 0 or drift.get("flips", 0) > 0:
            raise SystemExit(
                "preflight failed (decisions un-reconciled: %d new / %d flips since "
                "the last marker) — run the decision-reconciler agent, then "
                "`decision_reconcile.py --mark`, before cutting."
                % (drift.get("new_decs", 0), drift.get("flips", 0)))
        # A clean tree before any CI: the CI must run on the commit about to be cut (a
        # lane is brought to the tree; uncommitted work would be measured, not cut).
        status = self._run(["git", "status", "--porcelain"], cwd=str(self.root),
                           capture_output=True)
        if (status.stdout or "").strip():
            raise SystemExit("preflight failed (dirty tree) — commit/stash first.")
        checks = [
            # Structural docs gate (cheap, before heavy CI): refuse a cut while
            # docs/ fails frontmatter/graph validation. Release VERIFIES structure
            # — it never regenerates content. --fresh re-runs the analyzer so the
            # artifact reflects the working tree, not a stale run.
            ([sys.executable,
              "harness/plugins/hs/skills/docs-standardize/scripts/docs_gate.py",
              "--fresh"], "docs structure"),
        ]
        self._checks(checks)
        self._ci()
        checks = [
            ([sys.executable, "harness/scripts/check_skill_structure.py",
              "harness/plugins/hs/skills", "--strict"], "skill structure"),
            ([sys.executable, "harness/plugins/hs/skills/release/scripts/validate_skill_crossrefs.py",
              "harness/plugins"], "skill cross-refs"),
        ]
        self._checks(checks)
        auth = self._run(["gh", "auth", "status"], cwd=str(self.root), capture_output=True)
        if auth.returncode != 0:
            raise SystemExit("preflight failed (gh not authenticated) — run gh auth login.")

    def _checks(self, checks):
        for cmd, label in checks:
            res = self._run(cmd, cwd=str(self.root), capture_output=True)
            if res.returncode != 0:
                raise SystemExit("preflight failed (%s) — aborting before cut:\n%s"
                                 % (label, (res.stdout or res.stderr or "")[-2000:]))

    def _ci_target(self):
        """(host, None) when `.rops.env` names a host and the cell alias, and the host
        answers over ssh; else (host or None, why not)."""
        ci = self.config["ci"]
        key = "ROPS_CMD_" + str(ci["alias"]).upper()
        try:
            from rops.ropslib import config as rops_config
            cfg = rops_config.load(self.root, detect=False)
        except Exception as exc:  # a broken .rops.env is a reason, not a traceback
            return None, "the hs-rops config does not load (%s)" % exc
        host = (cfg.get("ROPS_HOST") or "").strip()
        if not host:
            return None, "ROPS_HOST is not set in .rops.env"
        if not (cfg.get(key) or "").strip():
            return host, ("%s is not set in .rops.env — the command each job is appended "
                          "to, e.g. %s='export PATH=$PWD/.venv-ci/bin:$PATH && bash "
                          "scripts/ci.sh'" % (key, key))
        try:
            probe = self._run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                               *shlex.split(cfg.get("ROPS_SSH_OPTS") or ""), host, "true"],
                              cwd=str(self.root), capture_output=True)
        except OSError as exc:
            return host, "ssh cannot run here (%s)" % exc
        if probe.returncode != 0:
            return host, "%s does not answer over ssh (rc %d)" % (host, probe.returncode)
        return host, None

    def ci_plan(self):
        """One line: where preflight's CI will run, or why the cut would stop."""
        ci = self.config["ci"]
        if ci["where"] == "local":
            return "here: scripts/ci_local.sh (ci.where: local)"
        host, why = self._ci_target()
        if why:
            return "STOP — %s; fix it, or run the whole CI here: --ci local" % why
        return ("on %s, lane %s: %d job(s) one hs-rops cell each, then ci_local.sh "
                "--local-only here" % (host, ci["lane"], len(dispatched_jobs(self.root))))

    def _ci(self):
        ci = self.config["ci"]
        if ci["where"] == "local":
            self._print("  CI: here (ci.where: local)")
            self._checks([(["bash", "scripts/ci_local.sh"], "local CI")])
            return
        host, why = self._ci_target()
        if why:
            raise SystemExit("preflight stopped (CI runs on the homelab, ci.where: remote): "
                             "%s — fix it, or run the whole CI here: --ci local" % why)
        for job in dispatched_jobs(self.root):
            res = self._run([sys.executable, "harness/scripts/hs_rops.py", str(ci["alias"]),
                             "--lane", str(ci["lane"]), "--timeout", str(int(ci["timeout_s"])),
                             "--", job], cwd=str(self.root), capture_output=True)
            self._print("  CI cell %s on %s (lane %s): rc %d" % (job, host, ci["lane"],
                                                                res.returncode))
            if res.returncode != 0:
                raise SystemExit("preflight failed (CI cell %s on %s, lane %s, rc %d) — "
                                 "aborting before cut:\n%s"
                                 % (job, host, ci["lane"], res.returncode,
                                    ((res.stdout or "") + (res.stderr or ""))[-2000:]))
        self._checks([(["bash", "scripts/ci_local.sh", "--local-only"],
                       "local-only CI steps")])

    def step1_cut(self):
        self._print("step 1/3 — cut release v%s (local)" % self.version)
        # Resumable: a failure after any sub-step leaves cut_done_local() false and
        # run() calls this again. Re-cutting would consume [Unreleased] twice and a
        # second commit fails on a clean tree, so each sub-step skips once done.
        if not self._cut_files_written():
            self._run([sys.executable, "release/release.py", "--release", self.version,
                       "--apply"], cwd=str(self.root), check=True)
        if self._cut_committed():
            self._run(["git", "tag", self.tag], cwd=str(self.root), check=True)
            return
        # release.py --apply only WRITES the tracked files (its --push owns the git
        # commit+tag+push). The orchestrator makes the LOCAL commit + tag here so
        # the cut is self-contained and cut_done_local() (git tag -l) sees it; the
        # outward push stays a separate, confirm-gated step (step2). No push here.
        self._run(["git", "add", *RELEASE_FILES, *plugin_readme_rels(self.root)],
                  cwd=str(self.root), check=True)
        self._run(["git", "commit", "-m", self._commit_subject()],
                  cwd=str(self.root), check=True)
        self._run(["git", "tag", self.tag], cwd=str(self.root), check=True)

    def step2_push_tag(self):
        self._print("step 2/3 — push harness HEAD + tag %s (--no-verify)" % self.tag)
        branch = self.config["harness"].get("branch", "main")
        head = ["git", "push", "--no-verify", "origin", branch]
        tag = ["git", "push", "--no-verify", "origin", self.tag]
        if self.posture != "solo":
            # team posture: print the commands for a rostered human, never fire them.
            self._print("  %s" % " ".join(head))
            self._print("  %s" % " ".join(tag))
            return
        # solo: owner self-bypasses the pre-push gate; idempotent (re-pushing an
        # existing tag is a no-op).
        self._run(head, cwd=str(self.root), check=True)
        self._run(tag, cwd=str(self.root), check=True)

    def step3_pack_and_release(self):
        """Pack dist/ + extract the release notes LOCALLY, for verification only.
        `.github/workflows/release.yml` (softprops/action-gh-release, triggered by
        the tag step2 already pushed) is the SOLE publisher of the GitHub Release —
        this step must never call `gh release create` itself. It used to, which
        pre-created the release ahead of the workflow's own publish step; on a real
        cut that step then failed on conflict (BL-273, reproduced on 5.3.0) and the
        assets had to be attached by hand. Posture makes no difference here: neither
        solo nor team creates the release from this step."""
        self._print("step 3/3 — pack dist/ for verification %s "
                    "(release.yml publishes from the pushed tag)" % self.tag)
        self._run([sys.executable, "release/pack.py", "--version", self.version],
                  cwd=str(self.root), check=True)
        # Generate SBOM as a release asset (CycloneDX 1.5, zero new deps)
        sbom_out = self.root / "dist" / "sbom.json"
        self._run([sys.executable, "release/sbom.py", "--output", str(sbom_out)],
                  cwd=str(self.root), check=False)  # fail-open: sbom failure must not block release
        notes = self.root / "dist" / ("release-notes-%s.md" % self.version)
        extract = self._run([sys.executable, "release/release.py", "--extract",
                             self.version, "--reflow"], cwd=str(self.root),
                            capture_output=True)
        if extract.returncode != 0 or not (extract.stdout or "").strip():
            raise SystemExit(
                "release notes extraction failed for v%s — the tag is already "
                "pushed and release.yml will build its own notes independently, "
                "but the local verification pack cannot be trusted:\n%s"
                % (self.version, (extract.stderr or extract.stdout or "")[-2000:]))
        notes.write_text(extract.stdout, encoding="utf-8")
        self._print("  packed dist/ for v%s (local verification only) — "
                    "release.yml publishes the GitHub release from the pushed "
                    "tag; this step does not run gh release create" % self.version)

    def _courier_cfg(self):
        return self.config.get("courier") or {}

    def step_courier(self):
        """Publish the harness courier plugin to the public dist repo (OPT-IN).
        Builds a scrubbed tarball in-process, dresses the dist tree, and pushes —
        leak-scan + confirm are enforced inside courier_sync (F4/F9), so this step
        cannot publish private content or push unattended."""
        cfg = self._courier_cfg()
        url = cfg.get("remote_url")
        if not url:
            self._print("  (courier) no dist remote configured — skipping publish")
            return
        self._print("step courier — publish the harness courier plugin to %s" % url)
        # The push is a FORCE-PUSH that fully replaces the remote branch (the dist
        # repo is generated). Surface that on the PRODUCTION path too — not only in
        # the courier_sync CLI — so an operator on --apply/--yes is not force-pushing
        # a (possibly mis-typed) public URL without a warning.
        self._print(
            "  WARNING: this FORCE-PUSHES a generated tree to %s, REPLACING the "
            "entire '%s' branch — any out-of-band content on that remote is "
            "overwritten. Verify the URL is the intended public dist repo."
            % (url, cfg.get("branch", "main")))
        if self.posture != "solo":
            self._print("  (team) would build + leak-scan + publish the courier to %s"
                        % url)
            return
        import tempfile
        import pack
        import courier_sync
        # mkdtemp + best-effort cleanup, not `with TemporaryDirectory(...)`: that
        # context manager's exit-time rmtree raises out of the block, and the push
        # below is OUTWARD — a scratch dir the OS declines to remove after the remote
        # already accepted the publish would be reported as a courier failure, sending
        # the operator to re-run work that is done. This dir holds one flat tarball so
        # the risk is small, but it sits on the release path and the fix is free.
        td = tempfile.mkdtemp(prefix="courier-pack-")
        try:
            tarball = Path(td) / "courier.tar.gz"
            pack.build_tarball(self.root, pack.manifest_files(self.root), tarball)
            dist = self.root.parent / cfg.get("local_dir", "sdlc-harness-courier")
            courier_sync.build_dist_tree(
                tarball, dist, version=self.version, repo_root=self.root,
                marketplace_name=cfg.get("marketplace_name", "harness"))
            res = courier_sync.push_dist(
                dist, remote_url=url, branch=cfg.get("branch", "main"),
                version=self.version,
                confirm=lambda files: self._confirm(
                    "FORCE-PUSH %d courier files to %s (REPLACES the remote branch)"
                    % (len(files), url)),
                runner=self._run)  # honor the injectable-runner contract (testable;
                # a FakeRunner must NOT fall through to a real `git push -f`)
            self._print("  courier: %s" % res)
        finally:
            try:
                shutil.rmtree(td)
            except OSError as exc:
                self._print("  warning: could not remove the courier scratch dir %s "
                            "(%s) — left for the OS to reap" % (td, exc))

    # ---- orchestration -------------------------------------------------------

    def print_checklist(self):
        self._print("release v%s — dry-run checklist (re-run with --apply to write):"
                    % self.version)
        self._print("  posture: %s" % self.posture)
        self._print("  CI (preflight): %s" % self.ci_plan())
        self._print("  step 1/3  cut        — %s" %
                    ("DONE (local)" if self.cut_done_local() else "pending"))
        self._print("  step 2/3  push tag   — %s" %
                    ("DONE (remote)" if self.version and self.tag_on_remote() else "pending"))
        self._print("  step 3/3  release    — %s" %
                    ("DONE" if self.version and self.release_published() else "pending"))
        if self._courier_cfg().get("remote_url"):
            self._print("  step +    courier    — pending (FORCE-PUSH to %s — replaces "
                        "the remote branch)" % self._courier_cfg().get("remote_url"))

    def run(self):
        if self.dry_run:
            self.print_checklist()
            return 0
        self.preflight()
        if not self.cut_done_local():
            self.step1_cut()
        if not self.tag_on_remote():
            if not self._confirm("push harness commit + tag to origin"):
                return NEEDS_CONFIRM
            self.step2_push_tag()
        if not self.release_published():
            if not self._confirm("pack dist/ for verification (release.yml "
                                 "publishes the GitHub release)"):
                return NEEDS_CONFIRM
            self.step3_pack_and_release()
        # Courier publish is OPT-IN: only runs when a dist remote is configured.
        # Its own leak-scan + confirm gate the outward push (courier_sync F4/F9).
        if self._courier_cfg().get("remote_url"):
            import courier_sync
            try:
                self.step_courier()
            except courier_sync.ConfirmError:
                # a declined confirm → the SAME graceful NEEDS_CONFIRM the other
                # outward steps use, not an uncaught traceback + exit 1.
                self._print("  (courier) confirm declined — re-run with --yes to publish")
                return NEEDS_CONFIRM
            except Exception as e:
                # The REAL release (steps 1-4) already shipped; the opt-in courier MIRROR
                # is best-effort. ANY failure past this point (LeakError, a CourierError
                # manifest drift, a subprocess.CalledProcessError from a git op, an OSError
                # cleaning .git, …) must become a clean, actionable message — never a raw
                # traceback that hides that the release itself succeeded. ConfirmError is
                # handled above; a deliberate SystemExit still propagates (BaseException).
                raise SystemExit(
                    "release v%s SHIPPED; the opt-in courier publish to %s FAILED: %s\n"
                    "  Fix the dist remote and re-run — only the courier mirror is "
                    "outstanding." % (self.version, self._courier_cfg().get("remote_url"), e))
        self._print("release v%s complete" % self.version)
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Orchestrate a harness release (3 hard-ordered, idempotent steps).")
    ap.add_argument("--version", help="release version X.Y.Z (required for --yes/auto)")
    ap.add_argument("--apply", action="store_true",
                    help="write/run (default: dry-run checklist)")
    ap.add_argument("--yes", action="store_true",
                    help="auto/non-interactive: confirm outward-facing steps")
    ap.add_argument("--root", default=str(REPO), help="repo root")
    ap.add_argument("--ci", choices=_CI_WHERE,
                    help="where preflight runs the CI, over release.yaml ci.where: remote "
                         "(the .rops.env host, one hs-rops cell per job) or local")
    args = ap.parse_args(argv)

    if (args.apply or args.yes) and not args.version:
        ap.error("--apply/--yes require --version (refusing to guess semver)")

    config = load_config(overrides={"ci": {"where": args.ci}} if args.ci else None)
    orch = Orchestrator(args.root, args.version, apply=args.apply, yes=args.yes, config=config)
    return orch.run()


if __name__ == "__main__":
    raise SystemExit(main())
