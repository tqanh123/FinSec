"""HsSecretsCli — the family router for `hs-secrets`.

Router only: every command is a row in `harness/data/hs-secrets-registry.d/`,
never an edit here. The family core (`hs_family.cli.CliBase`) supplies the
envelope, --version/--list/--help, unknown-command suggestions and the exit
ladder; this class adds the one shell flag every verb reads (`--backend`) and
binds it onto the command's namespace.
"""
from __future__ import annotations

import sys
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hs_family.registry as hs_registry  # noqa: E402
from hs_family.cli import CliBase, ShellFlag  # noqa: E402

_INSTALL_ROOT = Path(__file__).resolve().parents[3]  # secret_store -> scripts -> harness -> repo root
_REGISTRY_ENV = hs_registry.family_env("hs-secrets")


class HsSecretsCli(CliBase):
    prog, depth = "hs-secrets", 1
    description = ("hs-secrets: the user-level secret store — values only in the "
                   "active backend (the OS keychain, else an encrypted file), "
                   "metadata in append-only ledgers under ~/.harness/secrets/ "
                   "(harness/data/%s)." % _REGISTRY_ENV.dir_name)
    shell_flags = (
        ShellFlag("--backend", "backend", takes_value=True,
                  help="storage backend for this call (memory | file; default: the "
                       "encrypted file when the machine has no OS keychain)"),
    )

    def registry_env(self):
        return _REGISTRY_ENV

    def bind(self, ns, ctx) -> None:
        """Every verb reads the backend choice off its namespace — the flag is
        a SHELL flag (stripped before routing), so this is the one bridge
        between what the caller typed and what the command sees."""
        ns.backend_choice = ctx.flags.get("backend")


def main(argv=None) -> int:
    return HsSecretsCli(install_root=_INSTALL_ROOT).main(argv)
