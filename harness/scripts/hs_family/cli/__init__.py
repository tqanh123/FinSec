"""hs_family.cli — the shared core of the hs-* CLIs (hs-run, hs-code, hs-cli, hs-rops,
hs-secrets).

`CliBase` routes argv through one `CommandTable` built from ranked sources (registry,
pack, repo files, aliases; the mixins only append a source), hands the command a parser
it declared with `add_args`, runs `run(ns)`, and answers with the family envelope."""
from .base import CliBase, ShellFlag
from .contract import (Command, CommandParser, Context, Outcome, as_command, call, normalize,
                       parse, state_of)
from .envelope import SCHEMA, build as build_envelope, dumps, render_text
from .mixins import CommandAliasMixin, CustomCommandMixin, PackMixin
from .sources import AliasSource, PackSource, RegistrySource, RepoVerbSource
from .table import CommandConflict, CommandRow, CommandTable, Origin

__all__ = [
    "AliasSource", "CliBase", "Command", "CommandAliasMixin", "CommandConflict", "CommandParser",
    "CommandRow", "CommandTable", "Context", "CustomCommandMixin", "Origin", "Outcome",
    "PackMixin", "PackSource", "RegistrySource", "RepoVerbSource", "SCHEMA", "ShellFlag",
    "as_command", "build_envelope", "call", "parse", "dumps", "normalize", "render_text", "state_of",
]
