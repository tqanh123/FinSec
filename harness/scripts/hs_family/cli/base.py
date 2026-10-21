"""CliBase — the one dispatcher of the hs-* family.

    argv ─► strip --text + the CLI's shell flags ─► --version ─► table (sources by rank)
         ─► --list ─► help at any level ─► moved ─► unknown (known: …) ─► row.load()
         ─► parser from add_args ─► run(ns) ─► Outcome ─► one JSON line (or --text)

stdout carries exactly one JSON line per call, whatever happened. What a command prints
itself (a command answering with a bare exit code, a child process it runs) is captured
into the envelope's `output` and mirrored live to stderr; under `--text` nothing is
captured and the command prints as it always did. What a command writes to `sys.stderr`
still goes there, and when it answers 2 or 3 that is its envelope's `error` — the reason
a bare exit code does not carry."""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

import attrs

from .. import registry as hs_registry
from .. import main_wrapper
from ..common import list_lines
from ..errors import HsFamilyError, UsageError
from . import envelope
from .contract import CommandParser, Context, Outcome, normalize, parse
from .sources import RegistrySource
from .table import CommandRow, CommandTable

HELP = ("-h", "--help")
TEXT = "--text"
_INSTALL_ROOT = Path(__file__).resolve().parents[4]


def loosen_console_codecs() -> None:
    """Let a console that cannot encode part of an answer keep the answer.

    Measured 2026-09-28, Windows launchers job (run 36279748747): `--help`
    exited 2 on every family CLI because the console's codec (cp437/cp1252,
    inherited via `PYTHONIOENCODING` on the local twin) cannot encode
    characters the help text legitimately carries. `errors="replace"` fires
    ONLY on characters the stream's own encoding cannot write — an
    unchangeable codec costs a replacement glyph, never the exit code — so on
    a UTF-8 console, where nothing fails to encode, the bytes do not move.
    Fail-open: a stream that cannot be reconfigured (already-detached, a
    non-TextIOWrapper) changes nothing here.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


@attrs.define(frozen=True)
class ShellFlag:
    """A flag of the CLI itself, taken from anywhere in argv before routing."""
    name: str
    dest: str
    takes_value: bool = False
    help: str = ""
    #: False for a flag that says only where the call runs or where else its payload
    #: goes (`--repo`, `--write`): a record of WHAT ran leaves it out.
    identity: bool = True


class _Capture:
    """fd-level stdout capture (child processes included) teed live to stderr.

    Output whose first non-blank byte is `{` is HELD instead: it may be the
    command's answer as one JSON object, which becomes the envelope's `data`, and a
    live copy on stderr hands a reader who merges the streams a second document.
    `release(drop=)` settles it once the caller knows."""

    def __init__(self) -> None:
        self._chunks: list = []
        self._lock = threading.Lock()
        self._held: "Optional[list]" = None
        self._decided = False

    @property
    def text(self) -> str:
        with self._lock:
            return b"".join(self._chunks).decode("utf-8", "replace")

    def _add(self, data: bytes) -> None:
        with self._lock:
            self._chunks.append(data)
            if not self._decided:
                lead = b"".join(self._chunks).lstrip()
                if not lead:
                    return
                self._decided = True
                if lead.startswith(b"{"):
                    self._held = []
                data = b"".join(self._chunks)
            if self._held is not None:
                self._held.append(data)
                return
        _write_err(data)

    def release(self, *, drop: bool) -> None:
        """Drop held output (it was adopted as `data`), or write it out now."""
        with self._lock:
            held, self._held = self._held, None
            if not self._decided:
                held, self._decided = list(self._chunks), True
        if held and not drop:
            _write_err(b"".join(held))

    def _pump(self, fd: int) -> None:
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            self._add(chunk)

    def __enter__(self) -> "_Capture":
        cap = self

        class _Tee(io.TextIOBase):
            def write(self, s: str) -> int:
                cap._add(s.encode("utf-8", "replace"))
                return len(s)

        sys.stdout.flush()
        self._saved = self._r = None
        try:
            self._r, w = os.pipe()
            self._saved = os.dup(1)
            os.dup2(w, 1)
            os.close(w)
            self._thread = threading.Thread(target=self._pump, args=(self._r,), daemon=True)
            self._thread.start()
        except OSError:
            self._saved = None
        self._redirect = contextlib.redirect_stdout(_Tee())
        self._redirect.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self._redirect.__exit__(*exc)
        if self._saved is not None:
            os.dup2(self._saved, 1)
            os.close(self._saved)
            self._thread.join(timeout=5)
            if not self._thread.is_alive() and self._r is not None:
                os.close(self._r)


def _write_err(data: bytes) -> None:
    try:
        os.write(2, data)
    except OSError:
        pass


class _StderrTee(io.TextIOBase):
    """`sys.stderr` as the command sees it: every write goes through, and is kept — a
    command that fails by returning 2/3 says why there, and that is its envelope's `error`."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.parts: "list[str]" = []

    def write(self, s: str) -> int:
        self.parts.append(s)
        return self.real.write(s)

    def flush(self) -> None:
        self.real.flush()

    @property
    def text(self) -> str:
        return "".join(self.parts)


def _one_json_object(text: str) -> Optional[dict]:
    """`text` when it is exactly one JSON object — a command that printed its answer as JSON."""
    import json
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    try:
        doc = json.loads(stripped)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


class CliBase:
    prog = ""
    depth = 1
    shell_flags: tuple = ()
    description = ""
    level_names: tuple = ()
    footer: tuple = ("exit codes: 0 done  ·  1 findings  ·  2 broken/usage  ·  3 refused",)
    print_tracebacks = True

    def __init__(self, *, install_root: Optional[Path] = None) -> None:
        self.install_root = Path(install_root) if install_root else _INSTALL_ROOT
        self._call: dict = {}

    # ── extension points ────────────────────────────────────────────────────
    def registry_env(self) -> "hs_registry.RegistryEnv":
        return hs_registry.family_env(self.prog)

    def sources(self, flags: Mapping[str, Any]) -> list:
        return [RegistrySource(self.registry_env(), self.install_root, depth=self.depth)]

    def project_root(self, flags: Mapping[str, Any]) -> Path:
        import harness_root
        try:
            return harness_root.resolve_project_root()[0]
        except harness_root.ProjectRootError as e:
            raise UsageError(str(e)) from e

    def context(self, command: tuple, flags: Mapping[str, Any]) -> Context:
        return Context(cli=self.prog, command=command, root=lambda: self.project_root(flags),
                       flags=flags)

    def check_flags(self, flags: Mapping[str, Any]) -> None:
        return None

    def bind(self, ns: Any, ctx: Context) -> None:
        """Set whatever else the CLI's commands read off their namespace (after `ns.ctx`)."""
        return None

    def run_command(self, cmd: Any, ns: Any) -> Any:
        """Run the routed command — the one call every dispatch makes, so a CLI that must
        observe the run (open a scope around it) overrides this, never `_run`."""
        return cmd.run(ns)

    def usage_line(self) -> str:
        shell = " ".join("[%s%s]" % (f.name, " " + f.dest.upper() if f.takes_value else "")
                         for f in self.shell_flags)
        levels = " ".join("<%s>" % n for n in self._levels())
        return " ".join(p for p in ("usage:", self.prog, shell, levels, "[args...]") if p)

    def unknown_message(self, level: int, token: str, path: tuple, names: tuple) -> str:
        what = self._levels()[level]
        where = " for %s %r" % (self._levels()[level - 1], path[-1]) if level else ""
        return "unknown %s %r%s (known: %s)" % (what, token, where, ", ".join(names) or "(none)")

    def empty_message(self, table: CommandTable) -> str:
        return "%s: 0 commands registered" % self.prog

    def finish(self, row: CommandRow, ctx: Context, outcome: Outcome, output: str) -> dict:
        said = (self._call.get("stderr") or "").strip()
        return envelope.build(cli=self.prog, command=row.path, exit_code=outcome.exit_code,
                              state=outcome.state, warnings=outcome.warnings,
                              data=outcome.data if outcome.data else None, output=output,
                              error=said if said and outcome.exit_code in (2, 3) else None,
                              extra=outcome.extra)

    def envelope_for(self, command: Iterable[str], *, exit_code: int, state: str,
                     data: Optional[Mapping] = None, error: Optional[str] = None,
                     warnings: Iterable[str] = (), output: str = "") -> dict:
        return envelope.build(cli=self.prog, command=command, exit_code=exit_code, state=state,
                              data=data, error=error, warnings=warnings, output=output)

    def version_line(self) -> str:
        from ..common import version_line
        return version_line(self.install_root)

    # ── the table ───────────────────────────────────────────────────────────
    def table(self, flags: Optional[Mapping[str, Any]] = None) -> CommandTable:
        return CommandTable.build(self.sources(dict(flags or {})), depth=self.depth)

    def _levels(self) -> tuple:
        return self.level_names or (("command",) if self.depth == 1 else ("domain", "verb"))

    # ── argv ────────────────────────────────────────────────────────────────
    def strip_shell_flags(self, argv: list) -> "tuple[list, dict]":
        specs = {f.name: f for f in self.shell_flags}
        flags: dict = {f.dest: (None if f.takes_value else False) for f in self.shell_flags}
        out: list = []
        i = 0
        while i < len(argv):
            tok = argv[i]
            if tok == "--":
                out.extend(argv[i:])
                break
            spec = specs.get(tok)
            if spec is None:
                out.append(tok)
            elif spec.takes_value:
                if i + 1 >= len(argv):
                    raise UsageError("%s requires a value" % tok)
                flags[spec.dest] = argv[i + 1]
                i += 1
            else:
                flags[spec.dest] = True
            i += 1
        self.check_flags(flags)
        return out, flags

    # ── dispatch ────────────────────────────────────────────────────────────
    def main(self, argv: Optional[list] = None) -> int:
        loosen_console_codecs()
        argv = list(sys.argv[1:] if argv is None else argv)
        head = argv[: argv.index("--")] if "--" in argv else argv
        text = TEXT in head
        if text:
            argv = [a for i, a in enumerate(argv) if not (a == TEXT and i < len(head))]
        self._call = {"command": (), "output": "", "text": text, "argv": list(argv), "args": []}
        rc = 2
        try:
            rc = self._dispatch(argv, text)
        except HsFamilyError as e:
            rc = self._fail(main_wrapper.exit_code_for(e), str(e), text, e)
        except Exception as e:  # noqa: BLE001 — a crash still answers with an envelope
            if self.print_tracebacks:
                traceback.print_exc(file=sys.stderr)
            rc = self._fail(main_wrapper.exit_code_for(e), self.crash_message(e), text, e)
        self._log_call(rc)
        return rc

    def _log_call(self, rc: int) -> None:
        """One JSON line per call to `$HARNESS_FAMILY_CALL_LOG`, when it names a file:
        which command this call routed to, from which argv, in which process. The
        suite reads it to prove a test that claims to run a command through its
        launcher did. Fail-open: a log it cannot write never changes the answer."""
        path = os.environ.get("HARNESS_FAMILY_CALL_LOG")
        if not path:
            return
        import json
        row = {"cli": self.prog, "command": list(self._call.get("command", ())),
               "argv": list(self._call.get("argv", ())), "exit_code": rc, "pid": os.getpid()}
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            pass

    def command_parser(self, row: CommandRow, cmd: Any) -> CommandParser:
        """The parser `row`'s command is routed through — its `--help` is this parser's."""
        parser = CommandParser(prog="%s %s" % (self.prog, " ".join(row.path)), description=row.help)
        cmd.add_args(parser)
        return parser

    def wants_help(self, cmd: Any, args: list) -> bool:
        """`-h/--help` anywhere before a `--` asks for the command's help — never for a
        passthrough command, whose help is the wrapped tool's."""
        head = args[: args.index("--")] if "--" in args else args
        return not getattr(cmd, "passthrough", False) and any(a in HELP for a in head)

    def help_parser(self, parser: Any, args: list) -> Any:
        """The parser a `--help` describes: the command's own, or the sub-verb parser
        the tokens before it pick — `change test select --help` is `select`'s usage,
        not the `{select} ...` line of its parent."""
        for tok in args:
            if tok in HELP or tok == "--":
                break
            if tok.startswith("-"):
                continue
            sub = next((a for a in getattr(parser, "_actions", ())
                        if isinstance(a, argparse._SubParsersAction)), None)
            if sub is None or tok not in sub.choices:
                break
            parser = sub.choices[tok]
        return parser

    def crash_message(self, exc: BaseException) -> str:
        return "crashed: %s: %s" % (type(exc).__name__, exc)

    def normalize(self, result: Any, where: str) -> Outcome:
        return normalize(result, where)

    def _fail(self, code: int, message: str, text: bool, exc: Optional[BaseException] = None) -> int:
        env = self.envelope_for(self._call.get("command", ()), exit_code=code,
                                state="refused" if code == 3 else "error", error=message,
                                output=self._call.get("output", ""))
        self.emit(env, text)
        if not text:
            print("%s: %s" % (self.prog, message), file=sys.stderr)
        return code

    def emit(self, env: Mapping[str, Any], text: bool, outcome: Optional[Outcome] = None) -> None:
        if not text:
            sys.stdout.write(envelope.dumps(env) + "\n")
            sys.stdout.flush()
            return
        if env.get("error") is not None:
            print(envelope.render_text(env), file=sys.stderr)
            return
        if outcome is not None and outcome.text is not None:
            if callable(outcome.text):
                outcome.text()
            else:
                print(outcome.text)
        elif outcome is None or env.get("data"):
            rendered = envelope.render_text(env)
            if rendered:
                print(rendered)
        for w in (env.get("warnings") or ()) if outcome is not None else ():
            print("warning: %s" % w, file=sys.stderr)

    def _help(self, table: CommandTable, path: tuple, text: bool) -> int:
        cmds = []
        for n in table.names(path):
            row = table.find(path + (n,))
            if row is None:  # a domain: its verbs are the help
                cmds.append({"name": n, "help": ", ".join(table.names(path + (n,))),
                             "origin": None, "label": ""})
            else:
                cmds.append({"name": n, "help": row.help, "origin": attrs.asdict(row.origin),
                             "label": row.origin.label()})
        level = len(path)
        usage = self.usage_line() if not path else "usage: %s %s <%s> [args...]" % (
            self.prog, " ".join(path), self._levels()[level])
        data = {"usage": usage + ("\n\n" + self.description if self.description and not path else ""),
                "commands": cmds, "footer": list(self.footer)}
        if not path and self.shell_flags:
            data["shell_flags"] = self._shell_flag_rows()
        env = self.envelope_for(path, exit_code=0, state="help", data=data,
                                warnings=table.warnings)
        self.emit(env, text)
        return 0

    def _shell_flag_rows(self) -> list:
        return [{"name": f.name + (" " + f.dest.upper() if f.takes_value else ""), "help": f.help}
                for f in self.shell_flags]

    def _dispatch(self, argv: list, text: bool) -> int:
        clean, flags = self.strip_shell_flags(argv)
        if clean[:1] == ["--version"]:
            self.emit(self.envelope_for((), exit_code=0, state="version",
                                        data={"version": self.version_line()}), text)
            return 0
        table = self.table(flags)
        if clean[:1] == ["--list"]:
            listed = list_lines(" ".join(r.path) for r in table.live)
            self.emit(self.envelope_for((), exit_code=0, state="list", data={"commands": listed},
                                        warnings=table.warnings), text)
            return 0
        if not clean or clean[0] in HELP:
            return self._help(table, (), text)
        self._refuse_if_moved(clean, table)
        if not table.live:
            raise UsageError(self.empty_message(table))
        path: tuple = ()
        for level in range(self.depth):
            if level and (len(clean) <= level or clean[level] in HELP):
                return self._help(table, path, text)
            names = table.names(path)
            tok = clean[level]
            if tok not in names:
                raise UsageError(self.unknown_message(level, tok, path, names))
            path += (tok,)
            self._call["command"] = path
        args = clean[self.depth:]
        self._call["args"] = list(args)
        row = table.find(path)
        assert row is not None
        cmd = row.load()
        parser = self.command_parser(row, cmd)
        if self.wants_help(cmd, args):
            self.emit(self.envelope_for(path, exit_code=0, state="help",
                                        data={"usage": self.help_parser(parser, args).format_help(),
                                              "shell_flags": self._shell_flag_rows(),
                                              "footer": list(self.footer)}), text)
            return 0
        ns = parse(cmd, parser, args)
        self._call["parsed"] = True
        ctx = self.context(path, flags)
        ns.ctx = ctx
        self.bind(ns, ctx)
        outcome, output = self._run(cmd, ns, text, "%s %s" % (self.prog, " ".join(path)))
        env = self.finish(row, ctx, outcome, output)
        self.emit(env, text, outcome)
        return int(env["exit_code"])

    def _run(self, cmd: Any, ns: Any, text: bool, where: str) -> "tuple[Outcome, str]":
        if text:
            return self.normalize(self.run_command(cmd, ns), where), ""
        cap, tee = _Capture(), _StderrTee(sys.stderr)
        adopted = False
        try:
            with cap, contextlib.redirect_stderr(tee):
                result = self.run_command(cmd, ns)
            outcome, output = self.normalize(result, where), cap.text
            doc = None if isinstance(result, Outcome) else _one_json_object(output)
            adopted = doc is not None
        finally:
            self._call["output"], self._call["stderr"] = cap.text, tee.text
            cap.release(drop=adopted)
        if doc is not None:
            return attrs.evolve(outcome, data=doc), ""
        return outcome, output

    def _refuse_if_moved(self, argv: list, table: CommandTable) -> None:
        moved = table.moved
        if not moved:
            return
        domains: dict = {}
        for r in table.live:
            if self.depth == 1:
                domains[r.path[0]] = {}
            else:
                domains.setdefault(r.path[0], {})[r.path[1]] = {}
        hs_registry.refuse_if_moved(argv, hs_registry.LoadedRegistry(
            domains=domains, sources=[], moved=moved), domain_targets=self.depth == 2)


__all__ = ["CliBase", "ShellFlag"]
