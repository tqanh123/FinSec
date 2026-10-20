#!/usr/bin/env python3
"""trust_store.py — Trust-On-First-Use (TOFU) gate for shell-detector auto-fire.

A rule's shell detector runs an arbitrary command. Auto-firing it on a review of
foreign code is an RCE vector, so a shell detector auto-fires ONLY when the repo
root is TRUSTED — the operator ran `hs-cli trust <repo>`, an explicit per-machine
opt-in recorded in ~/.harness/trust.json (never in git). Trust is the SOLE
authorizer: an untrusted hostile clone never auto-fires a shell detector (the
operator would not trust it).

is_base_verified is an INTEGRITY signal only, NOT an auto-fire authorizer. A rule
file is base-verified when its bytes match `<root>/harness/manifest.json` —
the same digest check verify_install uses, catching ACCIDENTAL tampering of an OWN
install. It is deliberately NOT a trust substitute: the in-tree manifest has no
out-of-tree anchor, so a HOSTILE CLONE controls both the rule bytes AND the
manifest they are checked against and can self-attest (ship a manifest whose
digest matches its tampered rule). Wiring base-verify into the auto-fire decision
would therefore reopen the foreign-repo RCE — so the runner gates on is_trusted
ALONE. is_base_verified stays a standalone integrity primitive (verify_install runs
the equivalent digest check independently; the function itself is exercised by its
own tests today). To make base-verify defend a foreign root it would first need an
out-of-tree manifest anchor (see BACKLOG) — that deferred work is its real consumer.

Path-safety: realpath both the entry and the queried root before comparing
(so `..` traversal can't dodge a match), and REFUSE to trust a symlinked root (a
symlink could be re-pointed at hostile content after the trust was granted).

This module makes NO decision to execute — it only answers is_trusted /
is_base_verified. The auto-fire caller (mechanical_runner, standards_compliance_run)
gates on is_trusted. Fail-closed: any error/ambiguity yields "not trusted" /
"not verified".
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Set

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import hs_family  # noqa: E402
import secret_scan_before_ship as _secret_scan  # noqa: E402 (reuse its git-safe-config/env, not a copy)


class TrustError(Exception):
    """Raised when a trust operation cannot proceed. `exit_code` tells the
    caller which family code applies — bad input / a write failure is
    EXIT_BROKEN (2), a symlinked root is a deliberate EXIT_REFUSED (3). Kept
    as its own exception type (not raising the family errors directly) so a
    caller that only wants "did trust fail" still catches one type."""

    def __init__(self, message, *, exit_code=None):
        super().__init__(message)
        self.exit_code = exit_code if exit_code is not None else hs_family.EXIT_BROKEN


def _store_path() -> Path:
    raw = os.environ.get("HARNESS_TRUST_STORE")
    if raw:
        return Path(raw)
    return Path.home() / ".harness" / "trust.json"


def load_trust() -> Set[str]:
    """The set of trusted repo roots (realpath strings). Missing/corrupt store →
    empty set (fail-closed: nothing is trusted)."""
    p = _store_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return set()
    if not isinstance(data, dict):
        return set()
    entries = data.get("trusted")
    if not isinstance(entries, list):
        return set()
    return {str(e) for e in entries if isinstance(e, str)}


def _write_trust(trusted: Set[str]) -> None:
    p = _store_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"trusted": sorted(trusted)}, indent=2) + "\n",
                     encoding="utf-8")
    except OSError as exc:
        # a misconfigured store path (e.g. HARNESS_TRUST_STORE points at a dir)
        # surfaces as a TrustError the CLI reports, not a raw traceback
        raise TrustError("cannot write trust store at %s: %s" % (p, exc)) from exc


def add_trust(repo_root) -> str:
    """Trust a repo root (idempotent). Refuses a symlinked root. Returns the
    canonical (realpath) form recorded."""
    p = Path(repo_root)
    if p.is_symlink():
        raise TrustError("refusing to trust a symlinked repo root: %s" % p,
                          exit_code=hs_family.EXIT_REFUSED)
    if not p.is_dir():
        raise TrustError("repo root is not a directory: %s" % p)
    rp = os.path.realpath(p)
    trusted = load_trust()
    trusted.add(rp)
    _write_trust(trusted)
    return rp


_GIT_TIMEOUT_S = 10
_WT_MARKER = "/.git/worktrees/"


def _worktree_hint_root(p: Path) -> Optional[str]:
    """Read `p`'s own `.git` FILE (never a directory) and, if its `gitdir:`
    line has the `<root>/.git/worktrees/<name>` shape `git worktree add`
    itself writes, return realpath(<root>).

    This is a HINT ONLY: is_trusted never trusts `p` because of this file — a
    stranger directory can hold a forged `.git` file naming a REAL trusted
    root's REAL admin dir. The hint only selects which already-trusted root's
    OWN `git worktree list` gets asked; that root's own list (never this
    file) is where the actual admit/refuse decision comes from, so a forged
    gitdir buys nothing — the forger's path was never registered there.

    Not a `.git` FILE at all (a normal repo has a `.git` DIRECTORY here),
    unreadable, or shaped differently -> None. No hint means no fallback scan
    of the whole trust store."""
    git_path = p / ".git"
    try:
        if not git_path.is_file() or git_path.is_symlink():
            return None
        content = git_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not content.startswith("gitdir:"):
        return None
    gitdir = content[len("gitdir:"):].strip()
    if not gitdir:
        return None
    gitdir_path = Path(gitdir)
    if not gitdir_path.is_absolute():
        gitdir_path = p / gitdir_path
    gitdir_str = str(gitdir_path)
    idx = gitdir_str.find(_WT_MARKER)
    if idx <= 0:
        return None
    try:
        return os.path.realpath(gitdir_str[:idx])
    except OSError:
        return None


def _list_worktrees(trusted_root: str) -> Optional[Set[str]]:
    """realpath set of the worktrees `trusted_root` itself registers, per its
    own `git worktree list --porcelain` — run FROM `trusted_root`, never from
    the queried path. Fail-closed: no git binary, a non-zero exit, a timeout,
    or unparsable output all yield None (never an empty-but-valid set, which
    a caller could otherwise misread as "confirmed no worktrees")."""
    try:
        r = subprocess.run(
            ["git", *_secret_scan.GIT_SAFE_CONFIG, "-C", trusted_root,
             "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
            env=_secret_scan.hardened_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out: Set[str] = set()
    for line in r.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        wt_path = line[len("worktree "):].strip()
        if not wt_path:
            continue
        try:
            out.add(os.path.realpath(wt_path))
        except OSError:
            continue
    return out


def is_trusted(repo_root) -> bool:
    """True when `repo_root` (canonicalized) is trusted — either directly (its
    realpath is in the trust store) or because it is a git worktree of an
    already-trusted repo (that repo's OWN `git worktree list` names it by
    exactly its realpath). A symlinked root is never trusted; `..` traversal
    is normalized via realpath."""
    p = Path(repo_root)
    # A symlinked final component is refused — the recorded realpath of a real
    # dir can never equal the live symlink's resolved target by this rule, so we
    # reject the symlink outright rather than silently resolve it.
    try:
        if p.is_symlink():
            return False
    except OSError:
        return False
    rp = os.path.realpath(p)
    trusted = load_trust()
    if rp in trusted:
        return True
    hint_root = _worktree_hint_root(p)
    if hint_root is None or hint_root not in trusted:
        return False
    worktrees = _list_worktrees(hint_root)
    if worktrees is None:
        return False
    return rp in worktrees


def _sha256(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _load_manifest(manifest_path: Path) -> dict:
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data.get("files", {}) if isinstance(data, dict) else {}


def is_base_verified(file_path, root, *, manifest_path=None) -> bool:
    """True when `file_path`'s bytes match its entry in the shipped manifest.

    Identity is the digest, not the location: a file under harness/ whose bytes
    were tampered (a hostile clone) fails. A file absent from the manifest, or
    outside `root`, or unreadable, is not base-verified (fail-closed)."""
    # A symlinked rule file is refused outright (mirror the F6 root rule): a
    # symlink could point a base-named path at an intact manifest-listed target,
    # inheriting its verification while the symlink itself is attacker-placed.
    try:
        if Path(file_path).is_symlink():
            return False
    except OSError:
        return False
    root_rp = Path(os.path.realpath(Path(root)))
    fp = Path(os.path.realpath(Path(file_path)))
    try:
        rel = fp.relative_to(root_rp).as_posix()
    except ValueError:
        return False
    man = _load_manifest(Path(manifest_path) if manifest_path
                         else root_rp / "harness" / "manifest.json")
    want = man.get(rel)
    if not isinstance(want, str) or not want:
        return False
    got = _sha256(fp)
    return got is not None and got == want
