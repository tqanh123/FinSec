"""The `typescript` engine of the TypeScript/JavaScript pack: the harness-pinned
TypeScript compiler's own parser, run in Node through `ts_bridge.cjs`.

One bridge process serves one reading pass (`AdapterSet.reading` opens a `session`
over the files it will read): the reader sends the pass's files in batches -- the one
asked for and those after it -- and answers each `parse` from that batch. A file whose
text changed after its batch was sent is read again on its own. A reader used outside
a pass starts a process for its one file.

The compiler is the pinned one under the user's lang-tools home (`lang.toolchain`),
never a project's `node_modules`, so no project code runs and no trust is asked. A
missing `node`, a missing or different compiler refuses (exit 3) naming
`hs-code init run --apply --only lang-tools`; a bridge that dies or stops answering is
broken (exit 2), naming the batch and the bridge's stderr. Neither ever falls back to
the tree-sitter engine.

The payload is the tree-sitter reader's (`TsImport`, the same symbol kinds), so the two
engines compare record for record; the body hashes differ by design
(`typescript:<version>+a1:`, see `ts_bridge.cjs`).

The same compiler resolves the pack's specifiers (`ResolveBackend`, a process of its
own) over a host loaded once with the tree's files and the workspace packages'
directories: a real `node_modules` is never seen, `<dir>/node_modules/<workspace
name>` is that package's directory. It also reads bundler configs for the alias
evaluator (`alias_ir`), so this engine needs no tree-sitter wheel.

WHAT IT CANNOT SEE
    * Syntax only: no type checker runs, so a re-export chain or a declaration merged
      across files is not followed here.
    * `/// <reference path|types>` directives: not import edges.
    * The implicit `react/jsx-runtime` import a JSX file compiles to.
    * A specifier that is not a string literal (`import(x)`): kept as an expression,
      never resolved.
    * Names bound by destructuring (`const {a} = require("x")`): the import is read,
      the names are not symbols; an ambient (`declare`) declaration that is not
      exported is no symbol, as for the tree-sitter engine.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import queue
import subprocess
import tempfile
import threading
import weakref
from pathlib import Path
from typing import Any, ClassVar, Iterator, Sequence

from ....adapter.base import ModuleSymbols, SymbolInfo
from ...errors import AdapterError, AdapterRefused
from ..facts import TsImport

BRIDGE = Path(__file__).resolve().parent / "ts_bridge.cjs"
_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
BATCH = 200
TIMEOUT = 120.0


def bridge_argv(node: "Path | str", ts_dir: "Path | str") -> "list[str]":
    return [str(node), str(BRIDGE), "--typescript", str(Path(ts_dir).resolve())]


def _text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


class Bridge:
    """One `ts_bridge.cjs` process: `hello` on entry (a compiler other than
    `expect_version` refuses), `read` in batches, the process gone on exit."""

    def __init__(self, argv: "Sequence[str]", expect_version: str, batch: int = BATCH,
                 timeout: float = TIMEOUT, order: "Sequence[Path]" = (), repo: "Path | None" = None):
        self.argv, self.expect, self.batch, self.timeout = list(argv), expect_version, batch, timeout
        self.repo = repo
        self._order = list(order)
        self._at = {p: i for i, p in enumerate(self._order)}
        self._cache: "dict[Path, tuple[str, dict]]" = {}
        self._sent = 0
        self.process: "subprocess.Popen[bytes] | None" = None
        self._stderr: Any = None

    def __enter__(self) -> "Bridge":
        from ...toolchain import child_env
        self._stderr = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=self._stderr, env=child_env(self.repo),
                                            cwd=str(Path(self.argv[-1]).parent))
        except OSError as e:
            self._stderr.close()
            raise AdapterRefused("the typescript engine cannot start %s: %s" % (self.argv[0], e)) from e
        try:
            hello = self._call({"op": "hello"}, "hello")
            if hello.get("ts_version") != self.expect:
                raise AdapterRefused(
                    "the typescript engine needs TypeScript %s; %s holds %s -- install: "
                    "hs-code init run --apply --only lang-tools"
                    % (self.expect, self.argv[-1], hello.get("ts_version")))
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        p = self.process
        if p is not None and p.poll() is None:
            with contextlib.suppress(OSError):
                p.stdin.close()   # type: ignore[union-attr]
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        if p is not None:
            with contextlib.suppress(OSError):
                p.stdout.close()  # type: ignore[union-attr]
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None

    def _stderr_tail(self) -> str:
        if self._stderr is None:
            return ""
        self._stderr.seek(0)
        return self._stderr.read().decode("utf-8", "replace")[-2000:].strip()

    def _call(self, req: dict, what: str) -> dict:
        p = self.process
        assert p is not None and p.stdin is not None and p.stdout is not None
        # ASCII only: Node's readline also breaks a line at U+2028/U+2029
        line = (json.dumps(req) + "\n").encode("ascii")
        box: "queue.Queue[tuple[str, Any]]" = queue.Queue()

        def talk() -> None:
            try:
                p.stdin.write(line)   # type: ignore[union-attr]
                p.stdin.flush()       # type: ignore[union-attr]
                box.put(("ok", p.stdout.readline()))  # type: ignore[union-attr]
            except (OSError, ValueError) as e:
                box.put(("err", e))

        t = threading.Thread(target=talk, daemon=True)
        t.start()
        try:
            kind, got = box.get(timeout=self.timeout)
        except queue.Empty:
            p.kill()
            p.wait()
            t.join(5)
            raise AdapterError("the typescript bridge gave no answer in %gs to %s; stderr: %s"
                               % (self.timeout, what, self._stderr_tail() or "(empty)"))
        if kind == "err" or not got:
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(timeout=5)
            raise AdapterError("the typescript bridge died during %s (exit %s); stderr: %s"
                               % (what, p.returncode, self._stderr_tail() or "(empty)"))
        try:
            resp = json.loads(got.decode("utf-8"))
        except ValueError as e:
            raise AdapterError("the typescript bridge answered %s with no JSON: %r"
                               % (what, got[:200])) from e
        if "error" in resp:
            raise AdapterError("the typescript bridge refused %s: %s" % (what, resp["error"]))
        return resp

    def read(self, items: "Sequence[tuple[Path, str]]") -> "list[dict]":
        """One payload per `(path, text)`, in order, `batch` files per request."""
        out: "list[dict]" = []
        for i in range(0, len(items), self.batch):
            chunk = items[i:i + self.batch]
            self._sent += 1
            what = "batch %d (%d file(s): %s .. %s)" % (self._sent, len(chunk), chunk[0][0], chunk[-1][0])
            resp = self._call({"op": "read", "files": [{"path": str(p), "text": t} for p, t in chunk]},
                              what)
            files = resp.get("files")
            if not isinstance(files, list) or len(files) != len(chunk):
                raise AdapterError("the typescript bridge answered %s with %s payload(s)"
                                   % (what, len(files) if isinstance(files, list) else "no"))
            out += files
        return out

    def configs(self, paths: "Sequence[Path]") -> "list[dict]":
        """The compiler's reading of each tsconfig (`ts_bridge.cjs` op `configs`)."""
        if not paths:
            return []
        resp = self._call({"op": "configs", "paths": [str(p) for p in paths]},
                          "configs (%d file(s): %s ..)" % (len(paths), paths[0]))
        out = resp.get("configs")
        if not isinstance(out, list) or len(out) != len(paths):
            raise AdapterError("the typescript bridge answered configs with %s config(s) for %d"
                               % (len(out) if isinstance(out, list) else "no", len(paths)))
        return out

    def tree(self, root: Path, files: "Sequence[str]", workspaces: "dict[str, str]") -> int:
        """Load the tree `resolve` answers from: its repo-relative files, workspace dirs."""
        resp = self._call({"op": "tree", "root": str(root), "files": list(files), "workspaces": workspaces},
                          "tree (%d file(s))" % len(files))
        return int(resp.get("files", 0))

    def resolve(self, requests: "Sequence[dict]") -> "list[dict | None]":
        """`ts.resolveModuleName` per `{from, spec, mode, config, mr}` (`ts_bridge.cjs`)."""
        if not requests:
            return []
        resp = self._call({"op": "resolve", "requests": list(requests)},
                          "resolve (%d specifier(s): %s ..)" % (len(requests), requests[0].get("spec")))
        out = resp.get("resolved")
        if not isinstance(out, list) or len(out) != len(requests):
            raise AdapterError("the typescript bridge answered resolve with %s result(s) for %d"
                               % (len(out) if isinstance(out, list) else "no", len(requests)))
        return out

    def alias_ir(self, items: "Sequence[tuple[str, str]]") -> "list[dict]":
        """Each bundler config's syntax tree in the tree-sitter grammar's names (`alias_ir`)."""
        resp = self._call({"op": "alias_ir", "files": [{"path": p, "text": t} for p, t in items]},
                          "alias_ir (%d file(s))" % len(items))
        out = resp.get("trees")
        if not isinstance(out, list) or len(out) != len(items):
            raise AdapterError("the typescript bridge answered alias_ir with %s tree(s) for %d"
                               % (len(out) if isinstance(out, list) else "no", len(items)))
        return out

    def refs(self, items: "Sequence[tuple[str, str]]") -> "list[dict]":
        """Each file's references, `facts.Refs` as lists (`refs`)."""
        resp = self._call({"op": "refs", "files": [{"path": p, "text": t} for p, t in items]},
                          "refs (%d file(s))" % len(items))
        out = resp.get("refs")
        if not isinstance(out, list) or len(out) != len(items):
            raise AdapterError("the typescript bridge answered refs with %s result(s) for %d"
                               % (len(out) if isinstance(out, list) else "no", len(items)))
        return out

    def take(self, path: Path, text: str) -> dict:
        """`path`'s payload for `text`: from the batch already read when its text is
        the one sent, else from a new batch starting at `path`."""
        hit = self._cache.pop(path, None)
        if hit is not None and hit[0] == _text_key(text):
            return hit[1]
        start = self._at.get(path)
        ahead: "list[tuple[Path, str]]" = [(path, text)]
        if start is not None:
            for p in self._order[start + 1:]:
                if len(ahead) >= self.batch:
                    break
                if p in self._cache:
                    continue
                try:
                    ahead.append((p, p.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    continue
        payloads = self.read(ahead)
        for (p, t), pl in zip(ahead[1:], payloads[1:]):
            self._cache[p] = (_text_key(t), pl)
        return payloads[0]


def _engine_row() -> Any:
    from ... import registry
    for pack in registry.packs():
        for eng in pack.engines:
            if eng.reader_ref == (__name__, NodeTsReader.__name__):
                return eng
    raise AdapterError("no language book engine names %s.NodeTsReader" % __name__)


@contextlib.contextmanager
def open_bridge(repo: "Path | None" = None, order: "Sequence[Path]" = (), batch: int = BATCH,
                timeout: float = TIMEOUT) -> Iterator[Bridge]:
    """The bridge for this machine's pinned compiler, refusing what it lacks."""
    from ... import registry, toolchain
    eng = _engine_row()
    missing = registry.missing_requirements(eng, repo)
    if missing:
        raise AdapterRefused("the typescript engine needs %s -- install: %s"
                             % ("; ".join(missing), eng.install))
    (pkg,) = eng.requires_npm
    node = toolchain.find_tool("node", repo)
    assert node is not None   # missing_requirements found it
    with Bridge(bridge_argv(node, toolchain.module_dir(pkg)), pkg.version, batch, timeout, order,
                repo=repo) as b:
        yield b


def read_configs(repo: "Path | None", paths: "Sequence[Path]") -> "list[dict]":
    """Each tsconfig as the pinned compiler reads it, through one bridge process."""
    if not paths:
        return []
    with open_bridge(repo) as b:
        return b.configs(paths)


class ResolveBackend:
    """The compiler's own module resolution for `resolve_static` (engine `typescript`):
    one bridge process of its own, started on the first question and gone with this
    object, loaded once with the tree's files and the workspace packages' directories."""

    def __init__(self, repo: Path, files: "Sequence[str]", workspaces: "dict[str, str]"):
        self.repo, self.files, self.workspaces = Path(repo), list(files), dict(workspaces)
        self._bridge: "Bridge | None" = None
        self._stack = contextlib.ExitStack()
        self._seen: "dict[tuple, dict | None]" = {}
        weakref.finalize(self, self._stack.close)

    def _open(self) -> Bridge:
        if self._bridge is None:
            self._bridge = self._stack.enter_context(open_bridge(self.repo))
            self._bridge.tree(self.repo.resolve(), self.files, self.workspaces)
        return self._bridge

    def resolve(self, importer: str, spec: str, mode: str, config: "str | None", mr: str) -> "dict | None":
        key = (importer, spec, mode, config, mr)
        if key not in self._seen:
            (self._seen[key],) = self._open().resolve([{"from": importer, "spec": spec, "mode": mode,
                                                        "config": config, "mr": mr}])
        return self._seen[key]

    def alias_root(self, rel: str, text: str) -> Any:
        """A bundler config's syntax tree, read by the compiler (`aliases.read_table`'s `parse`)."""
        from ..aliases import IRNode
        (tree,) = self._open().alias_ir([(rel, text)])
        return IRNode(tree)

    def close(self) -> None:
        self._stack.close()
        self._bridge = None


def _symbol(d: dict, parent: "str | None" = None) -> SymbolInfo:
    return SymbolInfo(name=d["name"], kind=d["kind"], lines=(int(d["lines"][0]), int(d["lines"][1])),
                      sha=d["sha"], parent=d.get("parent", parent),
                      methods=tuple(_symbol(m) for m in d.get("methods", ())),
                      bases=tuple(d.get("bases", ())))


def _import(d: dict) -> TsImport:
    return TsImport(d["kind"], d.get("spec"), tuple((str(a), str(b), bool(c)) for a, b, c in d["bindings"]),
                    bool(d["type_only"]), int(d["line"]), bool(d["in_function"]), d.get("spec_expr"))


def module_symbols(path: Path, text: str, container: "tuple[str, ...]", module: str,
                   payload: dict) -> ModuleSymbols:
    data = text.encode("utf-8", "surrogatepass")
    return ModuleSymbols(path=path, module=module, container=container, loc=text.count("\n"),
                         nbytes=len(data), file_sha=hashlib.sha256(data).hexdigest()[:16], text=text,
                         imports=tuple(_import(i) for i in payload["imports"]),
                         symbols=tuple(_symbol(s) for s in payload["symbols"]),
                         dup_module_assign=tuple(payload.get("dups", ())),
                         problems=tuple(dict(p) for p in payload.get("problems", ())))


class NodeTsReader:
    """`typescript` pack, engine `typescript`."""

    ext: ClassVar[frozenset[str]] = frozenset(_SUFFIXES)

    def __init__(self, bridge: "Bridge | None" = None, repo: "Path | None" = None):
        self.bridge, self.repo = bridge, repo

    @classmethod
    def for_repo(cls, repo: "Path | None") -> "NodeTsReader":
        """A reader outside a session for `repo`'s files: its bridge looks for `node`
        the way the session's does, never inside `repo`."""
        return cls(repo=repo)

    @classmethod
    @contextlib.contextmanager
    def session(cls, repo: "Path | None", paths: "Sequence[Path]", batch: int = BATCH,
                timeout: float = TIMEOUT) -> Iterator["NodeTsReader"]:
        """A reader bound to one bridge process for a pass over `paths`."""
        with open_bridge(repo, paths, batch, timeout) as b:
            yield cls(b, repo)

    def parse(self, path: Path, text: str, container: "tuple[str, ...]", module: str) -> ModuleSymbols:
        if self.bridge is not None:
            payload = self.bridge.take(path, text)
        else:
            with open_bridge(self.repo) as b:
                (payload,) = b.read([(path, text)])
        return module_symbols(path, text, container, module, payload)
