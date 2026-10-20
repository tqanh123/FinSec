#!/usr/bin/env python3
"""courier_sync.py — publish the harness courier plugin to a public dist repo.

Pipeline (extends the release toolkit; ships inside the release skill but runs only
in the harness source repo, where release/pack.py lives):

    pack tarball (scrubbed)  ->  courier_tree  ->  <dist>/harness/ (plugin)
                                                +  <dist>/.claude-plugin/marketplace.json
                                                +  <dist>/README.md (EN)

Two hard safety rails, ENFORCED INSIDE push_dist so no caller flag can bypass:
  * leak-scan (F4, fail-closed): the payload's REAL surface must be clean — the
    protective protected-branches default, a preamble-only LESSONS, no dev abs
    paths / .harness-dev / plans/ / secrets — BEFORE any commit.
  * confirm gate (F9): an interactive confirm(files) -> True is required; there is
    no push=True escape hatch. Nothing — solo or team — ever auto-pushes.

Idempotent: a VERSION marker on the remote skips a re-push of the same version.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[6]
for _p in (str(_HERE.parent), str(_REPO_ROOT / "harness" / "install"),
           str(_REPO_ROOT / "harness" / "scripts"), str(_REPO_ROOT / "release")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import courier_tree  # noqa: E402

# LESSONS seed headings the shipped file is allowed to keep (mirrors pack).
try:
    from pack import LESSONS_SEED_HEADINGS
except Exception:  # noqa: BLE001
    LESSONS_SEED_HEADINGS = {"How to use this file", "Entry template"}


class LeakError(RuntimeError):
    """Leak-scan found private content — refuse to publish."""


class ConfirmError(RuntimeError):
    """The interactive confirm gate was not satisfied — refuse to publish."""


# --- dist tree -----------------------------------------------------------


def build_dist_tree(tarball, dist_root, *, version, repo_root,
                    marketplace_name="harness") -> dict:
    dist_root = Path(dist_root)
    # dist_root is a PERSISTENT sibling dir reused across releases. `git add` on a
    # dir is recursive, so a stray file INSIDE harness/ or .claude-plugin/ (an editor
    # .bak/.swp, .DS_Store, an old-layout leftover) would be published even though the
    # publish stages "only" those dirs. Fully remove the generated set FIRST so what
    # gets staged is exactly what this build produced — nothing that lingered.
    for slot in ("harness", ".claude-plugin", "README.md", "VERSION"):
        p = dist_root / slot
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists() or p.is_symlink():
            try:
                p.unlink()
            except OSError:
                pass
    # the courier plugin lives at <dist>/harness (marketplace source ./harness)
    courier_tree.build_courier_tree(Path(tarball), dist_root / "harness",
                                    repo_root=Path(repo_root))
    _write_marketplace(dist_root, marketplace_name, version)
    _write_readme(dist_root, marketplace_name)
    return {"dist": str(dist_root), "version": version}


def _write_marketplace(dist_root: Path, name: str, version: str) -> None:
    mk = {
        "name": name,
        "owner": {"name": "SDLC Harness"},
        "description": "Public marketplace for the harness courier — installs the "
                       "SDLC harness engine via a cache-copy plugin.",
        "plugins": [{
            "name": "harness",
            "source": "./harness",
            "description": "Harness courier — carries the SDLC harness engine to "
                           "your machine; run `harness setup` after install.",
            "version": version,
        }],
    }
    d = dist_root / ".claude-plugin"
    d.mkdir(parents=True, exist_ok=True)
    (d / "marketplace.json").write_text(json.dumps(mk, indent=2) + "\n",
                                        encoding="utf-8")


def _write_readme(dist_root: Path, marketplace_name: str) -> None:
    text = """# Harness courier

Install the SDLC harness engine as a Claude Code plugin. The plugin is a *courier*:
installing it drops a small payload into the plugin cache; `harness setup` then copies
the engine to a stable home and wires the current repo.

Requires `python3` >= 3.12 and the `claude` CLI.

## Install

```
claude plugin marketplace add <org>/<dist-repo>
claude plugin install harness@%s
harness setup            # run inside the repo you want the harness wired into
```

Restart the Claude Code session after `harness setup` so the wired env takes effect.

## Upgrade

```
claude plugin update harness@%s
harness upgrade          # copy + verify + repoint the engine home to the new version
```

Note: `harness upgrade` repoints the engine home's `current` pointer. A Claude Code
session already running against `current` picks up the new engine on its next hook
call (per-session version-pin is a planned enhancement).

## Uninstall

```
harness setup --uninstall    # removes this repo's harness wiring; prints the engine home to remove manually
```

## Health

```
harness doctor           # integrity check of the engine home + advisory version skew
harness version          # engine-home + cache versions and the running source
```
""" % (marketplace_name, marketplace_name)
    (dist_root / "README.md").write_text(text, encoding="utf-8")


# --- leak scan (fail-closed) --------------------------------------------


# Real-secret patterns. The engine is all git-tracked source (the repo's own
# secret-scan gates it at commit), so this is a secondary net whose main risk is
# false-positiving on a security DOC's teaching examples — hence the PRIVATE KEY
# pattern requires an actual base64 body (not a bare header) and any match that
# contains "EXAMPLE"/"XXXX" placeholder text is skipped in leak_scan.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[A-Za-z0-9+/=\s]{40,}-----END"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"sk-ant-[A-Za-z0-9-]{30,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
]
_SECRET_PLACEHOLDERS = ("EXAMPLE", "XXXX", "REDACTED", "YOUR_", "<")
_FORBIDDEN_DIRS = (".harness-dev", "plans")
# The exact top-level entries build_dist_tree generates + push_dist stages/publishes.
# leak_scan scans ONLY these so scan == publish (a non-published leftover in the
# persistent dist dir neither leaks nor false-blocks).
_GENERATED = ("harness", ".claude-plugin", "README.md", "VERSION")
# Homes that ACTUALLY ship as a generic template in the engine tree — nulling the
# dev-home check for these avoids a false-block on a legitimate placeholder. Kept to
# the SHIPPED set only (`/home/user`, `/root`): the other common container/CI homes
# (/home/ubuntu, /home/runner, /home/vscode, /home/node, /home/dev) never appear in a
# shipped file, so nulling them gave ZERO false-positive benefit but DISABLED the
# dev-path net for a publisher whose real HOME is one of them — a leak from
# `/home/ubuntu/private/...` published from an Ubuntu cloud box would slip. Keep this
# tight to whatever `git grep` shows actually ships as a template.
_GENERIC_HOMES = {"/home/user", "/root", "/root/"}
# Deny known BINARY types; everything else is content-scanned (an allowlist by
# suffix silently skipped .js/.ts/.env.example/.svg/... — 160 shipped files — so a
# secret or dev path in any of them would publish unscanned).
_BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp", ".pdf",
    ".zip", ".tar", ".tgz", ".bz2", ".xz", ".7z",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".mp4", ".mp3", ".mov", ".wav", ".webm", ".pyc", ".so", ".dylib", ".dll",
}
# `.gz` is deliberately NOT in the skip set: a single-stream gzip is reversible
# TEXT (a shipped `.json.gz` — e.g. drawio's shape-index — could carry a dev path
# or secret), so it is gunzipped and content-scanned like any text file. Archive
# containers (`.tgz`/`.tar.gz`/`.zip`) stay opaque above.


def _try_gunzip(raw: bytes):
    """Decompress a single-stream gzip to text if it is plausibly text, else None (a
    genuine binary payload, archive, or corrupt blob). Closes the `.gz` content-scan
    blind spot (a shipped `.json.gz` could carry a dev path/secret). Never raises —
    scanning untrusted content must not crash the publish gate."""
    import gzip
    try:
        data = gzip.decompress(raw)
    except Exception:  # noqa: BLE001 — not valid gzip / truncated -> treat as opaque
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    printable = sum(1 for c in text if c.isprintable() or c in "\r\n\t")
    if text and printable / len(text) > 0.9:
        return text
    return None


def _try_utf16(raw: bytes):
    """Return UTF-16 (LE/BE) decoded text if `raw` is plausibly UTF-16 text, else
    None (a genuine binary). Closes the null-byte-skip blind spot for a secret in a
    UTF-16-encoded text file."""
    if len(raw) < 2:
        return None
    for enc in ("utf-16-le", "utf-16-be", "utf-16"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
        # plausible text = overwhelmingly printable/whitespace
        printable = sum(1 for c in text if c.isprintable() or c in "\r\n\t")
        if text and printable / len(text) > 0.9:
            return text
    return None


def leak_scan(dist_root, dev_home=None) -> list:
    """Return a list of leak findings ([] = clean). Fail-closed: the caller must
    abort the publish on any finding.

    `dev_home` (default: this machine's HOME) is the SPECIFIC absolute dev path
    whose leak into a shipped file is the real risk — a generic `/home/user/`
    template or a documented `.harness-dev` config reference is NOT a leak, so
    those are deliberately not matched (only the `.harness-dev` DIR being PRESENT
    in the dist is)."""
    dist_root = Path(dist_root)
    dev_home = str(dev_home) if dev_home is not None else str(Path.home())
    # A generic container/CI home (/home/user, /root, …) is NOT a PII dev path — and
    # shipped engine files legitimately carry a `/home/user` template — so scanning
    # for it would permanently false-block a publish. Only a SPECIFIC dev home leaks.
    if dev_home in _GENERIC_HOMES:
        dev_home = None
    hits = []

    # Scan EXACTLY the set that will be PUBLISHED (build_dist_tree's generated
    # output), not the whole persistent dist dir — a non-published leftover
    # (top-level scratch file / stale plans dir) must not block a legit publish, and
    # scanning only what ships keeps scan==publish (no false negatives on the ship set).
    scan_files: list[Path] = []
    scan_dirs: list[Path] = []
    for name in _GENERATED:
        p = dist_root / name
        if ".git" in Path(name).parts:
            continue
        if p.is_dir() and not p.is_symlink():
            scan_dirs.append(p)
            scan_files.extend(x for x in p.rglob("*") if x.is_file())
        elif p.is_file():
            scan_files.append(p)

    # 1. forbidden dirs anywhere WITHIN the published set (a nested plans/ under a
    #    shipped skill fixture would evade a top-level-only check)
    for d in scan_dirs:
        for sub in d.rglob("*"):
            if sub.is_dir() and sub.name in _FORBIDDEN_DIRS:
                hits.append("forbidden dir present in dist: %s/"
                            % sub.relative_to(dist_root))

    # 2. protected-branches must carry the protective default, not an empty floor
    prot = (dist_root / "harness" / "engine" / "harness" / "data"
            / "protected-branches.yaml")
    if prot.is_file():
        text = prot.read_text(encoding="utf-8", errors="replace")
        empty = False
        try:
            import yaml_io
            data = yaml_io.safe_load(text)
            plist = data.get("protected") if isinstance(data, dict) else None
            empty = not plist  # None, [], missing, or otherwise falsy
        except yaml_io.YAMLError:  # unparseable: fall back to a regex net that
            # matches ONLY the inline-empty forms; a bare `protected:` line (the
            # block-list default with items on following lines) must NOT match.
            empty = bool(re.search(r"(?m)^protected:\s*(\[\s*\]|~|null)\s*$", text))
        if empty:
            hits.append("protected-branches.yaml ships an EMPTY/missing protected floor")

    # 3. LESSONS.md preamble-only
    lessons = dist_root / "harness" / "engine" / "harness" / "LESSONS.md"
    if lessons.is_file():
        seed = re.sub(r"(?ms)^```.*?^```", "",
                      lessons.read_text(encoding="utf-8", errors="replace"))
        for h in re.findall(r"(?m)^## (.+)$", seed):
            if h.strip() not in LESSONS_SEED_HEADINGS:
                hits.append("LESSONS.md carries a dev entry heading: %r" % h.strip())

    # 4. content scan across the PUBLISHED text files (deny known binaries + null-byte
    #    probe). Only scan_files (the generated set) — scan == publish.
    for p in scan_files:
        suf = p.suffix.lower()
        if suf in _BINARY_SUFFIXES:
            continue
        rel = p.relative_to(dist_root)
        try:
            raw = p.read_bytes()
        except Exception:  # noqa: BLE001
            continue
        if suf == ".gz":
            # reversible-text container: gunzip before scanning; a non-gzip/corrupt
            # blob (or a non-text payload) is treated as opaque and skipped.
            text = _try_gunzip(raw)
            if text is None:
                continue
        elif b"\x00" in raw[:8192]:
            # Null bytes: either a real binary (skip) or UTF-16 text (a secret in a
            # UTF-16 file must NOT slip through the null-byte skip). Try UTF-16;
            # scan it only if it decodes to plausible text, else treat as binary.
            text = _try_utf16(raw)
            if text is None:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
        if dev_home and dev_home in text:
            hits.append("absolute dev home path (%s) in %s" % (dev_home, rel))
        for pat in _SECRET_PATTERNS:
            m = pat.search(text)
            if m and not any(ph in m.group(0).upper() for ph in _SECRET_PLACEHOLDERS):
                hits.append("possible secret in %s" % rel)
                break
    # de-dup, stable order
    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


# --- push (leak-scan + confirm gated) -----------------------------------


def _default_runner(cmd, cwd=None, check=False):
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def _git(run, cwd, *args, check=True):
    # commit.gpgsign=false: the dist repo is a generated, unsigned publish tree; a
    # global `commit.gpgsign=true` with no key would otherwise fail the commit (and
    # that failure used to be swallowed → a confusing downstream "src refspec" error).
    return run(["git", "-C", str(cwd), "-c", "user.email=harness@local",
                "-c", "user.name=harness", "-c", "commit.gpgsign=false", *args],
               check=check)


def _dist_files(dist_root: Path) -> list:
    """The files that will actually be PUBLISHED (the generated set only), for the
    confirm list — NOT a whole-dir walk that would count non-published leftovers."""
    out: list[str] = []
    for name in _GENERATED:
        p = dist_root / name
        if p.is_dir() and not p.is_symlink():
            out.extend(str(x.relative_to(dist_root)) for x in p.rglob("*") if x.is_file())
        elif p.is_file():
            out.append(name)
    # VERSION is written by push_dist AFTER this confirm list is built, but it is always
    # part of the published set — include it unconditionally so the operator's confirm
    # list equals exactly what force-pushes (else they approve N files while N+1 ship).
    if "VERSION" not in out:
        out.append("VERSION")
    return sorted(out)


def push_dist(dist_root, *, remote_url, branch, version, confirm, runner=None) -> dict:
    """Publish the dist tree. leak-scan + confirm are enforced HERE, unconditionally.
    `confirm(files)` receives the file list and must return True. Idempotent on the
    remote VERSION marker."""
    run = runner or _default_runner
    dist_root = Path(dist_root)

    # confirm + idempotency FIRST — both gate before any git op (F9), and doing them
    # before the scan keeps the scan→stage step tight (no blocking input()/network
    # clone between the leak_scan and the `git add`, which would be a TOCTOU window
    # for an unscanned write into the persistent dist to be published).
    files = _dist_files(dist_root)
    if not confirm(files):
        raise ConfirmError("publish not confirmed by the operator")

    # mkdtemp + best-effort cleanup, NOT `with TemporaryDirectory(...)`: that context
    # manager's exit-time rmtree raises straight out of push_dist, so a scratch dir the
    # OS declines to remove turns a completed publish into an exception on the outward
    # step of a release. A git clone in a temp dir is exactly the shape that produced an
    # Errno 39 on a CI runner (see probe_install_modes.py's own note). The scratch clone
    # exists only to read a VERSION marker; failing to delete it afterwards says nothing
    # about whether the publish worked, so it must not be able to report otherwise.
    td = tempfile.mkdtemp(prefix="courier-probe-")
    try:
        clone = Path(td) / "clone"
        cr = run(["git", "clone", "--depth", "1", "-b", branch, remote_url,
                  str(clone)], check=False)
        if cr.returncode == 0:
            marker = clone / "VERSION"
            if marker.is_file() and marker.read_text(encoding="utf-8").strip() == version:
                return {"skipped": True, "pushed": False, "version": version}
    finally:
        try:
            shutil.rmtree(td)
        except OSError as exc:
            sys.stderr.write(
                "warning: could not remove the version-probe scratch dir %s (%s) — "
                "left for the OS to reap; the publish result stands\n" % (td, exc))

    # Fresh single-commit history every publish: drop any prior .git so the pushed
    # HEAD contains EXACTLY the leak-scanned tree — no accreted history that could
    # re-surface a past leak-scan miss. `git init` does NOT reset an existing repo, so
    # if .git cannot be fully removed we must NOT proceed (a stacked history / origin
    # collision would defeat the containment). Fail loud instead of ignore_errors.
    git_dir = dist_root / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)  # no ignore_errors — a survivor is a hard stop
        if git_dir.exists():
            raise RuntimeError(
                "could not remove the prior %s — refusing to publish onto stale git "
                "state (would stack history / collide origin)" % git_dir)
    run(["git", "init", "-q", "-b", branch, str(dist_root)], check=True)

    (dist_root / "VERSION").write_text(version + "\n", encoding="utf-8")
    # Scan the generated set, then stage it IMMEDIATELY — nothing (no input, no
    # network) between the scan and the `git add`, so what is committed == what was
    # scanned. Stage EXACTLY the generated set, never `git add -A` (a stray in the
    # persistent dist must not be swept into the public force-push).
    hits = leak_scan(dist_root)
    if hits:
        raise LeakError("leak-scan found %d issue(s), refusing to publish: %s"
                        % (len(hits), "; ".join(hits[:8])))
    _staged = [p for p in _GENERATED if (dist_root / p).exists()]
    _git(run, dist_root, "add", "--", *_staged)
    cr = _git(run, dist_root, "commit", "-q", "-m",
              "publish harness courier %s" % version, check=False)
    # Surface a real commit failure (gpgsign, a pre-commit hook, an identity error)
    # instead of swallowing it — a swallowed failure leaves no HEAD and the operator
    # then sees a misleading "push failed: src refspec does not match any" downstream.
    # A benign empty commit cannot occur (the tree is reinit-fresh with staged files).
    if cr.returncode != 0:
        blob = ((cr.stderr or "") + (cr.stdout or "")).strip()
        if "nothing to commit" not in blob.lower():
            raise RuntimeError("dist commit failed: %s"
                               % (blob or "git commit exited %d" % cr.returncode))
    _git(run, dist_root, "remote", "add", "origin", remote_url)
    pr = _git(run, dist_root, "push", "-f", "origin", branch, check=False)
    if pr.returncode != 0:
        raise RuntimeError("push failed: %s" % (pr.stderr or pr.stdout))
    return {"skipped": False, "pushed": True, "version": version}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tarball", required=True)
    ap.add_argument("--dist", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--marketplace-name", default="harness")
    ap.add_argument("--remote", default=None, help="dist remote URL (push step)")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--push", action="store_true",
                    help="attempt the push (still leak-scan + confirm gated)")
    args = ap.parse_args(argv)

    try:
        build_dist_tree(Path(args.tarball), Path(args.dist), version=args.version,
                        repo_root=Path(args.repo_root),
                        marketplace_name=args.marketplace_name)
    except courier_tree.CourierError as e:
        # symmetric with courier_tree.main: a bad tarball (directory / not-a-tar /
        # malformed release.json) reports the actionable message + exit 2, not a raw
        # traceback.
        sys.stderr.write("courier-sync: %s\n" % e)
        return 2
    hits = leak_scan(Path(args.dist))
    if hits:
        sys.stderr.write("leak-scan FAILED:\n  " + "\n  ".join(hits) + "\n")
        return 2
    print("dist tree built + leak-scan clean at %s" % args.dist)
    if args.push:
        if not args.remote:
            sys.stderr.write("--push needs --remote\n")
            return 2

        def _confirm(files):
            sys.stderr.write(
                "About to FORCE-PUSH %d files to %s, REPLACING the entire remote "
                "branch (the courier dist repo is generated — any out-of-band files "
                "on the remote are overwritten). Review the exact set to publish:\n"
                % (len(files), args.remote))
            for f in files[:80]:
                sys.stderr.write("  " + f + "\n")
            if len(files) > 80:
                sys.stderr.write("  ... (%d more)\n" % (len(files) - 80))
            return input("Publish (full replace)? [y/N] ").strip().lower() == "y"

        res = push_dist(Path(args.dist), remote_url=args.remote, branch=args.branch,
                        version=args.version, confirm=_confirm)
        print(json.dumps(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
