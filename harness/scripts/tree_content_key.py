"""The content identity of a git working tree AS IT IS NOW, clean or dirty.

One algorithm for every reader that asks "was this measured on the tree I am
standing in?" -- a snapshot's reuse key and a suite run's junit alike. Flat and
stdlib-only (plus the `git` binary), so a reader outside the `codegraph`
package can call it.

    clean within scope  -> `HEAD^{tree}` (the known answer: `git rev-parse HEAD^{tree}`)
    dirty within scope  -> sha256 over HEAD^{tree}, `git diff --binary HEAD -- <scope>`,
                           and each untracked file in scope with the sha256 of its bytes

Two different uncommitted edits at one commit give two keys; returning to an
earlier edit gives the earlier key back. `scope` is a tuple of repo-relative
pathspecs (empty = the whole tree); nothing outside it moves the key.

Untracked means `git ls-files --others --exclude-standard`: a file the repo
IGNORES never moves the key, so run state written under an ignored directory
cannot make a measurement stale.

Git runs under `secret_scan_before_ship.hardened_git_env()` (no system or
global config, no inherited GIT_DIR/GIT_WORK_TREE) with `core.fsmonitor=false`,
`core.quotepath=off` and no external diff or textconv: the key depends on the
tree and on the repo's own config, not on the caller's environment.

Global ignores, as measured: git's DEFAULT excludes path
(`$XDG_CONFIG_HOME/git/ignore`, else `~/.config/git/ignore`) still applies under
the hardened env, since git reads it without any config. What the hardened env
loses is a `core.excludesFile` DECLARED in the global config, so that one path
(and no other global setting) is read separately and passed as `-c`.

WHAT IT CANNOT SEE
    * File mode changes count only as far as `git diff` reports them; a change
      git itself does not see (an ignored file, a submodule's inner edits) is
      invisible.
    * `None` when the directory is not a git work tree, HEAD does not resolve,
      or a git call fails: no key is not a key that matches.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

from secret_scan_before_ship import GIT_SAFE_CONFIG, hardened_git_env

_CONFIG = (*GIT_SAFE_CONFIG, "-c", "core.quotepath=off")


def _global_excludes() -> "tuple[str, ...]":
    """`-c core.excludesFile=<path>` when the user's global config names one: the
    PATH only, read with the caller's global config and nothing else run, so the
    hardened env (which reads no global config) still honours the user's global
    ignores. Absent -> git's own default (`$XDG_CONFIG_HOME/git/ignore`, else
    `~/.config/git/ignore`) already applies under the hardened env."""
    env = hardened_git_env()
    if "GIT_CONFIG_GLOBAL" in os.environ:
        env["GIT_CONFIG_GLOBAL"] = os.environ["GIT_CONFIG_GLOBAL"]
    else:
        env.pop("GIT_CONFIG_GLOBAL", None)
    try:
        proc = subprocess.run(["git", "config", "--global", "--type=path", "--get",
                               "core.excludesFile"], capture_output=True, env=env, cwd="/",
                              check=False)
    except OSError:
        return ()
    path = proc.stdout.decode("utf-8", "surrogateescape").strip()
    return ("-c", "core.excludesFile=" + path) if proc.returncode == 0 and path else ()


def _git(repo: Path, *argv: str, config: "tuple[str, ...]" = ()) -> Optional[bytes]:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *_CONFIG, *config, *argv],
                              capture_output=True, env=hardened_git_env(), check=False)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def content_key(repo: Path, scope: Sequence[str] = ()) -> Optional[str]:
    """See the module docstring."""
    repo = Path(repo)
    head = _git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{tree}")
    if not head:
        return None
    tree = head.strip()
    spec = ("--", *scope) if scope else ()
    diff = _git(repo, "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", *spec)
    listed = _git(repo, "ls-files", "--others", "--exclude-standard", "-z", *spec,
                  config=_global_excludes())
    if diff is None or listed is None:
        return None
    untracked = sorted(p for p in listed.decode("utf-8", "surrogateescape").split("\0") if p)
    if not diff and not untracked:
        return tree.decode("ascii")
    parts = [tree, diff]
    for rel in untracked:
        try:
            body = (repo / rel).read_bytes()
        except OSError:
            body = b""
        parts.append(rel.encode("utf-8", "surrogateescape") + b":"
                     + hashlib.sha256(body).hexdigest().encode("ascii"))
    return hashlib.sha256(b"\x00".join(parts)).hexdigest()


def main(argv: "Sequence[str] | None" = None) -> int:
    """`--write-meta PATH --command STR` — the ONE thing a caller needs this
    module for from a shell chain (hs-run's `suite` domain, `_run_chain`): stamp
    the tree's fingerprint BEFORE a remote run starts, so a long run whose local
    tree changes mid-flight is paired with the tree it was ACTUALLY measured
    against.

    Writes one JSON object `{content_key, command, actor, ts}` to `--write-meta`:
    `command` is recorded VERBATIM, never executed — this process only
    fingerprints, it does not run anything. `actor` via the repo's own
    `resolve_actor()` (hook_runtime.py; attribution, not authentication — see
    that function's own docstring). `ts` is ISO-8601 UTC.

    Exit 2 when the fingerprint itself is `None` (`--repo` is not a git work
    tree with a resolvable HEAD) — nothing was written in that case; a caller
    chaining this with `&&` stops there rather than pairing a junit result with
    no fingerprint at all."""
    import argparse
    import json as _json
    from datetime import datetime, timezone

    p = argparse.ArgumentParser(
        prog="tree_content_key.py",
        description="Write {content_key, command, actor, ts} for the tree at --repo.")
    p.add_argument("--repo", default=".", help="git work tree to fingerprint (default: cwd)")
    p.add_argument("--write-meta", required=True, metavar="PATH",
                   help="write the fingerprint JSON to this path")
    p.add_argument("--command", required=True,
                   help="the command this fingerprint is about to be measured "
                        "against — recorded verbatim, never executed here")
    args = p.parse_args(argv)

    repo = Path(args.repo).resolve()
    key = content_key(repo)
    if key is None:
        sys.stderr.write(
            "tree_content_key: %s is not a git work tree with a resolvable "
            "HEAD -- nothing to fingerprint\n" % repo)
        return 2

    hooks_dir = Path(__file__).resolve().parent.parent / "hooks"
    if str(hooks_dir) not in sys.path:
        sys.path.insert(0, str(hooks_dir))
    import hook_runtime  # local import: keeps this module stdlib+git-only for

    # every OTHER caller (worktree_key.py, hs_run_suite.py) that only ever needs
    # `content_key` itself.
    payload = {"content_key": key, "command": args.command,
              "actor": hook_runtime.resolve_actor(),
              "ts": datetime.now(timezone.utc).isoformat()}
    dest = Path(args.write_meta)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
