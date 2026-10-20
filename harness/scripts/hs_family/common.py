"""hs_family.common — the two flags shared by every hs-* CLI.

`--version` and `--list` are the ONLY flags every hs-* CLI in this family
understands the same way; nothing else is common. `--repo`/`--write`/
`--apply`/`--fresh`/`--allow-stale` belong to hs-code alone (hs-run's own
contract is "reports only", so an `--apply` there would contradict it; hs-run
and hs-cli resolve their root through `harness_root`/`harness_paths`, never a
`--repo` flag). `COMMON_FLAGS` is a plain module-level tuple, not a class,
specifically so a caller checks membership (`argv[0] in COMMON_FLAGS`)
instead of re-declaring the two strings per CLI — and so a test can assert
this tuple never grows to include one of the CLI-specific flags above.

`--version` prints the harness's own version identity
(`harness_release.read_release`) — there is exactly one harness build on a
given tree, so every CLI in the family reports the identical line. `--list`
prints the CLI's own verb surface, but the ENUMERATION is supplied by the
caller: a registry-backed CLI (hs-run, hs-code) has `domain verb` pairs from
its loaded registry; hs-cli is argparse-subparsers-only (no registry) and
lists its subcommand names instead. The flag word is shared; what counts as
"a verb" is not, so this module only owns the TEXT shape (one string, one
name per line sorted), never the enumeration itself.

Both are PURE (`version_line`/`list_lines` return the text, print nothing):
every CLI in the family answers `--version`/`--list` through the CLI core
(`hs_family.cli.CliBase`), which puts the text into its envelope — stdout
stays exactly one JSON line. `print_version` is the one printing half left,
for `hs-cli version`, the subcommand that predates the flag.
"""
from __future__ import annotations

from pathlib import Path

COMMON_FLAGS = ("--version", "--list")


def version_line(root: Path) -> str:
    """The harness's version identity as one line — no I/O beyond the read.

    Reuses `harness_release.read_release` — the same source `hs-cli version`
    (the pre-existing subcommand) already reads, so every spelling of
    "what version is this" (the subcommand, this flag, hs-run's own
    envelope) can never report two different versions off one tree.
    """
    import harness_release
    rel = harness_release.read_release(root)
    return "hs {ver} ({ch})  kit_digest={dig}".format(
        ver=rel.get("harness_version", "?"),
        ch=rel.get("channel", "?"),
        dig=rel.get("kit_digest", "?"),
    )


def print_version(root: Path) -> int:
    """Print `version_line(root)` and return 0 — `hs-cli version`'s own answer."""
    print(version_line(root))
    return 0


def list_lines(names) -> list:
    """`names`, sorted, as a list of strings — no I/O."""
    return sorted(names)
