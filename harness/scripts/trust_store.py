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
from typing import Dict, List, Optional, Set, Tuple

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


def load_commands() -> Dict[str, str]:
    """root (realpath) -> the command fingerprint recorded when it was trusted. A
    missing/corrupt store, or one written before fingerprints existed, records none."""
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    cmds = data.get("commands") if isinstance(data, dict) else None
    if not isinstance(cmds, dict):
        return {}
    return {str(k): str(v) for k, v in cmds.items() if isinstance(k, str) and isinstance(v, str)}


def _write_trust(trusted: Set[str], commands: Optional[Dict[str, str]] = None) -> None:
    p = _store_path()
    doc: Dict[str, object] = {"trusted": sorted(trusted)}
    cmds = load_commands() if commands is None else commands
    if cmds:
        doc["commands"] = dict(sorted(cmds.items()))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
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
    commands = load_commands()
    fp = command_fingerprint(p)
    if fp is None:
        commands.pop(rp, None)
    else:
        commands[rp] = fp
    _write_trust(trusted, commands)
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


# ── the project's own commands ───────────────────────────────────────────────
#
# Trusting a root once must not trust whatever its commands become: a pull can rewrite
# the reader script `codemap.yaml` names, an edit can swap its argv. The fingerprint
# taken at `add_trust` covers both; a command runs only while it still matches.


def _check_command(language: str, cid: str, row) -> Optional[List[str]]:
    """The tool a declared check runs: the pack's `bin` + the repo's `argv` or the pack's.
    Its policy (mode, baseline, grace) is not a command."""
    import static_checks
    spec = next((s for s in static_checks.book(language) if s.id == cid), None)
    argv = row.get("argv") if isinstance(row, dict) and isinstance(row.get("argv"), list) else None
    if spec is None:
        return [cid] + list(argv or [])
    return [spec.bin] + list(argv if argv is not None else spec.argv)


def declared_commands(repo) -> List[List[str]]:
    """Every argv `repo/codemap.yaml` declares for hs-code to run, in declaration order:
    `adapters[].command`, `adapters[].resolver`, `languages.<id>.resolver_command`, each
    `languages.<id>.runners.<rid>` `bin` / `argv` / `select.list_argv` (`hs-code test run`),
    each declared `languages.<id>.checks.<cid>` as its tool's bin + argv (`hs-code check
    run`), and `domains.test.command` (`hs-code test prove|mutation`). An unreadable or malformed
    file declares none (the loader refuses it on its own)."""
    import yaml_io
    try:
        doc = yaml_io.safe_load((Path(repo) / "codemap.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml_io.YAMLError):
        return []
    if not isinstance(doc, dict):
        return []
    out: List[List[str]] = []

    def take(v) -> None:
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            out.append(list(v))

    for row in doc.get("adapters") or ():
        if isinstance(row, dict):
            take(row.get("command"))
            take(row.get("resolver"))
    langs = doc.get("languages")
    for _lid, opts in sorted(langs.items() if isinstance(langs, dict) else ()):
        if isinstance(opts, dict):
            take(opts.get("resolver_command"))
            runners = opts.get("runners")
            for _rid, row in sorted(runners.items() if isinstance(runners, dict) else ()):
                if isinstance(row, dict):
                    if isinstance(row.get("bin"), str) and row["bin"]:
                        take([row["bin"]])
                    take(row.get("argv"))
                    sel = row.get("select")
                    take(sel.get("list_argv") if isinstance(sel, dict) else None)
            checks = opts.get("checks")
            for cid, row in sorted(checks.items() if isinstance(checks, dict) else ()):
                take(_check_command(str(_lid), str(cid), row))
    test = (doc.get("domains") or {}).get("test") if isinstance(doc.get("domains"), dict) else None
    take(test.get("command") if isinstance(test, dict) else None)
    return out


def _git(repo_rp: str, *args: str) -> "Optional[subprocess.CompletedProcess]":
    """`git <args>` in `repo_rp` under the hardened config/env; None when git cannot run."""
    import process_group
    try:
        return process_group.run(["git", *_secret_scan.GIT_SAFE_CONFIG, "-C", repo_rp, *args],
                                 capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
                                 env=_secret_scan.hardened_git_env())
    except (OSError, subprocess.TimeoutExpired):
        return None


def _checkout_root(repo_rp: str) -> str:
    """The git work tree `repo_rp` belongs to (realpath), or `repo_rp` itself off git: a
    package of a monorepo runs scripts from the whole checkout."""
    r = _git(repo_rp, "rev-parse", "--show-toplevel")
    top = r.stdout.strip() if r is not None and r.returncode == 0 else ""
    return os.path.realpath(top) if top else repo_rp


def _repo_file(repo_rp: str, token: str, top: "Optional[str]" = None) -> Optional[str]:
    """`token`, as a path, when it names a regular file inside the repo or, above it, inside
    the checkout `top` -- relative to the repo (`../tools/x.js`)."""
    cand = Path(token) if os.path.isabs(token) else Path(repo_rp) / token
    try:
        rp = os.path.realpath(cand)
        if not os.path.isfile(rp):
            return None
        bound = top or repo_rp
        Path(rp).relative_to(bound)
    except (OSError, ValueError):
        return None
    return Path(os.path.relpath(rp, repo_rp)).as_posix()


def _argv_paths(argv) -> List[str]:
    """Every path an argv may run from: each token, the value of a `--flag=value`, and
    the file a `-m module` names. A bare program name is not looked up on PATH: what PATH
    finds differs between shells, and a fingerprint must not (`program_in_repo`)."""
    toks, out = list(argv), []
    for i, tok in enumerate(toks):
        out.append(tok)
        if tok.startswith("-") and "=" in tok:
            out.append(tok.split("=", 1)[1])
        if i > 0 and toks[i - 1] == "-m":
            mod = tok.replace(".", "/")
            out += [mod + ".py", mod + "/__init__.py", mod + "/__main__.py"]
    return out


def program_in_repo(repo, argv) -> Optional[str]:
    """The repo-relative file a bare `argv[0]` runs, found on this process's PATH the
    way a child started in `repo` finds it, when that file is the repo's own source
    (tracked, or untracked and not ignored); None otherwise. A program installed where
    git ignores it (a `.venv`, `node_modules/.bin`) is an installed tool, like one
    outside the repo."""
    name = str(argv[0]) if argv else ""
    if not name or "/" in name or os.sep in name:
        return None
    repo_rp = os.path.realpath(Path(repo))
    exts = [""] + ([e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
                   if os.name == "nt" else [])
    found = None
    for entry in os.environ.get("PATH", os.defpath).split(os.pathsep):
        d = Path(entry) if os.path.isabs(entry) else Path(repo_rp) / (entry or ".")
        found = next((d / (name + e) for e in exts
                      if (d / (name + e)).is_file() and os.access(d / (name + e), os.X_OK)), None)
        if found is not None:
            break
    if found is None:
        return None
    rel = _repo_file(repo_rp, str(found), _checkout_root(repo_rp))
    if rel is None:
        return None
    r = _git(repo_rp, "check-ignore", "-q", "--", rel)
    return None if r is not None and r.returncode == 0 else rel


def command_files(repo, argvs) -> List[str]:
    """The repo-relative files inside `repo` the argvs run from (`_argv_paths`), sorted --
    the scripts a declared command runs from the tree. A module such a script imports is
    not among them."""
    repo_rp = os.path.realpath(Path(repo))
    top = _checkout_root(repo_rp)
    return sorted({rel for argv in argvs for tok in _argv_paths(argv)
                   for rel in (_repo_file(repo_rp, tok, top),) if rel is not None})


def command_fingerprint(repo) -> Optional[str]:
    """sha256 over the declared argvs and the bytes of every file inside `repo` an argv
    token names; None when `repo` declares no command. A comment or any key outside the
    commands leaves it unchanged."""
    cmds = declared_commands(repo)
    if not cmds:
        return None
    repo_rp = os.path.realpath(Path(repo))
    files = {rel: _sha256(Path(repo_rp) / rel) or "unreadable" for rel in command_files(repo, cmds)}
    blob = json.dumps({"argv": cmds, "files": files}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _trusted_root(repo) -> Optional[str]:
    """The trust-store root `repo` is trusted through: itself, or the root whose own
    worktree list names it (the `is_trusted` rule); None when it is not trusted."""
    if not is_trusted(repo):
        return None
    rp = os.path.realpath(Path(repo))
    return rp if rp in load_trust() else _worktree_hint_root(Path(repo))


def commands_trusted(repo) -> Tuple[bool, Optional[str], Optional[str]]:
    """(ok, recorded, current): `repo`'s commands may run -- it declares none, or it is
    trusted and its current fingerprint is the one recorded for its trusted root."""
    current = command_fingerprint(repo)
    if current is None:
        return True, None, None
    root = _trusted_root(repo)
    recorded = load_commands().get(root) if root else None
    return recorded is not None and recorded == current, recorded, current


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
