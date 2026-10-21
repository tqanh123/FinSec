"""The one command contract every hs-* CLI dispatches through: `add_args(p)` declares the
command's flags on the parser the core owns, `run(ns)` does the work and answers with an
exit code or an `Outcome`. `ns.ctx` is the `Context` the CLI resolved for this call."""
from __future__ import annotations

import argparse
import shlex
from pathlib import Path
from typing import Any, Callable, Mapping, Union

import attrs

from ..errors import UsageError


@attrs.define(frozen=True, kw_only=True)
class Context:
    """`root` may be handed in as a zero-argument callable: it is resolved when a command
    reads it, so a command that never needs the project root never fails resolving one."""
    cli: str
    command: tuple
    _root: Any = attrs.field(alias="root", eq=False, repr=False)
    flags: Mapping[str, Any] = attrs.field(factory=dict)
    config: Any = None

    @property
    def root(self) -> Path:
        return Path(self._root() if callable(self._root) else self._root)


@attrs.define(frozen=True, kw_only=True)
class Outcome:
    """`data` is the command's own payload (the envelope's `data`); `text` is how `--text`
    shows it — a string, or a zero-argument callable that prints. `extra` carries top-level
    keys only a CLI that owns them may set (hs-run's step fields)."""
    exit_code: int
    state: str
    data: Mapping[str, Any] = attrs.field(factory=dict)
    warnings: tuple = ()
    text: Union[str, Callable[[], None], None] = attrs.field(default=None, eq=False)
    extra: Mapping[str, Any] = attrs.field(factory=dict)


RunResult = Union[int, Outcome]


@attrs.define(frozen=True)
class Command:
    """A named `add_args`/`run` pair — how a module serving several commands exposes each.
    `passthrough` hands the command's argv to `run` untouched as `ns.args` (a wrapper of
    another tool whose flags are that tool's): no parse, no `--help` interception.
    `lenient` parses the declared flags but refuses nothing: a token the declaration does
    not know lands in `ns.unknown`, and the argv as handed in stays at `ns.argv` — for a
    command whose argv is not a closed set (it tolerates tokens it has no opinion on)."""
    add_args: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], Any]
    passthrough: bool = False
    lenient: bool = False


def as_command(obj: Any, where: str) -> Any:
    """`obj` itself (a `Command`, a module, any object) once it carries both halves."""
    if not (callable(getattr(obj, "add_args", None)) and callable(getattr(obj, "run", None))):
        raise UsageError("%s is not a command: it needs callable add_args and run" % where)
    return obj


class CommandParser(argparse.ArgumentParser):
    """argparse with its exit doors closed: a bad flag is a `UsageError`, never a print to
    stdout plus `sys.exit`. `-h/--help` is answered by the core before parsing."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("add_help", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise UsageError("%s: %s" % (self.prog, message))

    def exit(self, status=0, message=None):
        raise UsageError("%s: %s" % (self.prog, (message or "argument error").strip()))


def parse(cmd: Any, parser: argparse.ArgumentParser, args: list) -> argparse.Namespace:
    """`args` against the parser `cmd.add_args` filled — or whole, for a passthrough
    command. A leading `--` of any list value (argparse's REMAINDER keeps it) is dropped.

    A command naming one of those values in `after_dashdash` takes it only after `--`:
    a stray word after a flag (`--lock x ly do`, meant as a reason) was otherwise the
    command, and ran. Only here is the `--` still visible to tell the two apart."""
    if getattr(cmd, "passthrough", False):
        return argparse.Namespace(args=list(args))
    if getattr(cmd, "lenient", False):
        ns, unknown = parser.parse_known_args(list(args))
        ns.unknown, ns.argv = unknown, list(args)
    else:
        ns = parser.parse_args(list(args))
    tail = getattr(cmd, "after_dashdash", None)
    stray = getattr(ns, tail, None) if tail else None
    if stray and stray[:1] != ["--"]:
        raise UsageError("%s: the command goes after `--` (%s [flags] -- <command>); %s came "
                         "before it and was not run — a flag's value is one word, quote it"
                         % (parser.prog, parser.prog, shlex.join(stray)))
    for name, val in vars(ns).items():
        if isinstance(val, list) and val[:1] == ["--"]:
            setattr(ns, name, val[1:])
    return ns


def call(cmd: Any, argv: list, ctx: "Context", *, prog: str) -> Any:
    """Run one command in-process exactly as the core does after routing: its own parser
    from `add_args`, `ns.ctx`, `run(ns)` — for a caller that already holds the command
    (a test, a command that runs another)."""
    parser = CommandParser(prog=prog)
    cmd.add_args(parser)
    ns = parse(cmd, parser, argv)
    ns.ctx = ctx
    return cmd.run(ns)


def state_of(exit_code: int) -> str:
    return {0: "done", 1: "findings", 2: "error", 3: "refused"}.get(exit_code, "exit_%d" % exit_code)


def normalize(result: Any, where: str) -> Outcome:
    if isinstance(result, Outcome):
        return result
    if result is None:
        result = 0
    if isinstance(result, bool) or not isinstance(result, int):
        raise UsageError("%s returned %r, not an exit code or an Outcome" % (where, type(result).__name__))
    return Outcome(exit_code=result, state=state_of(result))


__all__ = ["Command", "CommandParser", "Context", "Outcome", "RunResult", "as_command", "call",
           "normalize", "parse", "state_of"]

