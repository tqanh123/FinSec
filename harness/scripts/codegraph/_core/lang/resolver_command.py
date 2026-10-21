"""A project's own resolver: one command a snapshot pass asks about every import at once.

THE PROTOCOL (`hs-code-resolve/1`)
    The command (argv, run with the repo as cwd) gets ONE JSON object on stdin:

        {"protocol": "hs-code-resolve/1", "repo": "<abs>", "language": "<id>",
         "ids": ["<module id>", ...],
         "files": [{"path": "<repo-relative>",
                    "imports": [{"index": 0, "import": <the reader's payload>}, ...]}, ...]}

    and answers ONE JSON object on stdout:

        {"protocol": "hs-code-resolve/1",
         "results": [{"path": "...", "imports": [{"index": 0, "status": "...", ...}]}]}

    `status` is internal | external | builtin | unresolved | ambiguous | defer:
        internal    `target` is one of `ids`; `names` the names it binds (edges
                    `<target>::<name>`, else the module), `type_only`, `in_function`
        external    `target` the package; builtin `target` the runtime's module
        unresolved  `reason` `unresolved:<code>` / `ambiguous:<code>`, the code kept on the
        ambiguous   record; `candidates`, and `spec` for what the record names it by
        defer       not this command's to answer -- only where a resolver runs after it
    `flags` (each a mapping of the package-boundary keys, `import_flags`) ride beside any
    answer. Every asked import is answered once; anything else is broken, named by the
    resolver, the file and the index.

TWO DOORS, ONE PROTOCOL
    (a) a `codemap.yaml` `adapters:` row naming `resolver:` beside its `command:` is a
        language of its own (`lang.registry.ProjectPack`, resolved by `ProjectResolver`): its files are path ids under `lang: <id>`,
        read by the reader command, every import answered by this one (no `defer`);
    (b) the TypeScript pack's `languages.typescript.resolver_command`, asked before the
        pack's own resolution for each bare specifier -- `defer` hands it back.

TRUST
    The command is the project's code: it runs only in a trusted repo whose declared
    commands are the ones trusted (`command_adapter.require_commands_trusted`), checked
    once, before the first spawn -- a refusal (exit 3), never a skip.

WHAT IT CANNOT SEE
    * A script the command runs from outside the repo: its bytes are neither in the
      trust fingerprint nor in the pack's `config_inputs`, so editing one changes no
      key. Only argv tokens naming a file inside the repo are seen.
    * A top-level path id with no `/` (`a.foo` in a root at `.`) reads as a dotted name
      to the prefix walks (`lang.ids`), which know the bin's suffixes only.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .base import ClassifiedImports, Named, SourceRef, build_symbol_entries
from .errors import AdapterError

PROTOCOL = "hs-code-resolve/1"
#: seconds one batch may take (`languages.typescript.resolver_timeout_s` sets the TS one)
TIMEOUT = 120
STATUSES = ("internal", "external", "builtin", "unresolved", "ambiguous", "defer")
#: an `import_flags` entry's keys and the JSON types each takes (`to`, `declared`,
#: `type_only` required) -- what `package_boundary`, `graph importers` and the
#: `code.importers` fact read off a record
FLAG_TYPES: "dict[str, tuple[type, ...]]" = {
    "to": (str,), "declared": (str,), "type_only": (bool,), "target_pkg": (str, type(None)),
    "runtime_resolvable": (str,), "bypasses_exports": (bool,), "inferred": (bool,), "external": (bool,)}
FLAG_KEYS = frozenset(FLAG_TYPES)
_FLAG_REQUIRED = ("to", "declared", "type_only")
_ANSWER_KEYS = frozenset({"index", "status", "target", "names", "type_only", "in_function",
                          "reason", "flags", "spec", "candidates"})
#: what a pack's own reader already knows of an import, so a command it defers to does not say
_READER_KEYS = frozenset({"names", "type_only", "in_function"})
_REASON = re.compile(r"(unresolved|ambiguous):([a-z0-9_]+)")


@dataclass(frozen=True)
class Answer:
    index: int
    status: str
    target: "str | None" = None
    names: "tuple[str, ...]" = ()
    type_only: bool = False
    in_function: bool = False
    #: the code after `unresolved:` / `ambiguous:`
    reason: str = ""
    flags: "tuple[dict[str, Any], ...]" = ()
    spec: "str | None" = None
    candidates: "tuple[str, ...]" = ()


def _strs(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


class ResolverCommand:
    """One resolver command bound to its repo. `allow_defer` when a resolver runs after
    it (door b); its answers then leave what the pack's reader knows to the reader."""

    def __init__(self, argv: "Sequence[str]", repo: Path, *, what: str, language: str = "",
                 timeout: float = TIMEOUT, allow_defer: bool = False):
        self.argv, self.repo, self.what = tuple(argv), Path(repo), what
        self.language, self.timeout, self.allow_defer = language, timeout, allow_defer
        self._trusted = False

    def require_trust(self) -> None:
        if not self._trusted:
            from ..adapter.command_adapter import require_commands_trusted
            require_commands_trusted(self.repo, self.what, self.argv)
            self._trusted = True

    def resolve_batch(self, ids: "Sequence[str]",
                      files: "Sequence[tuple[str, Sequence[tuple[int, Any]]]]") -> "dict[str, dict[int, Answer]]":
        """{path: {index: Answer}} for every import of `files` ((path, [(index, payload)])),
        in ONE run of the command; nothing to ask runs nothing."""
        if not files:
            return {}
        self.require_trust()
        req = json.dumps({"protocol": PROTOCOL, "repo": str(self.repo), "language": self.language,
                          "ids": list(ids),
                          "files": [{"path": p, "imports": [{"index": i, "import": imp} for i, imp in imps]}
                                    for p, imps in files]})
        at = "%s on %d file(s)" % (self.what, len(files))
        import process_group
        try:
            r = process_group.run(list(self.argv), timeout=self.timeout, input=req,
                                  capture_output=True, text=True, cwd=str(self.repo))
        except subprocess.TimeoutExpired as e:
            raise AdapterError("%s: no answer in %ds" % (at, self.timeout)) from e
        except OSError as e:
            raise AdapterError("%s: cannot run %s: %s" % (at, self.argv[0], e)) from e
        if r.returncode != 0:
            raise AdapterError("%s: exit %d: %s" % (at, r.returncode, (r.stderr or "").strip()[-300:]))
        try:
            doc = json.loads(r.stdout)
            if not isinstance(doc, dict) or doc.get("protocol") != PROTOCOL \
                    or not isinstance(doc.get("results"), list):
                raise ValueError("needs {protocol: %s, results: [...]}" % PROTOCOL)
            results = {}
            for res in doc["results"]:
                if not isinstance(res, dict) or not isinstance(res.get("path"), str) \
                        or not isinstance(res.get("imports"), list):
                    raise ValueError("result %r needs {path, imports}" % (res,))
                if res["path"] in results:
                    raise AdapterError("%s on %s answered twice" % (self.what, res["path"]))
                results[res["path"]] = res["imports"]
        except ValueError as e:
            raise AdapterError("%s: answer is not the %s contract (%s): %r"
                               % (at, PROTOCOL, e, (r.stdout or "")[:200])) from e
        known = set(ids)
        out: "dict[str, dict[int, Answer]]" = {}
        for path, imps in files:
            asked = {i for i, _imp in imps}
            got: "dict[int, Answer]" = {}
            for raw in results.get(path, ()):
                a = self._answer(raw, path, asked, known)
                if a.index in got:
                    raise AdapterError("%s on %s import %d: answered twice" % (self.what, path, a.index))
                got[a.index] = a
            missing = sorted(asked - set(got))
            if missing:
                raise AdapterError("%s on %s: no answer for import %d" % (self.what, path, missing[0]))
            out[path] = got
        return out

    def _answer(self, raw: Any, path: str, asked: "set[int]", known: "set[str]") -> Answer:
        idx = raw.get("index") if isinstance(raw, dict) else None
        at = "%s on %s import %s" % (self.what, path, idx)

        def bad(why: str) -> AdapterError:
            return AdapterError("%s: %s" % (at, why))

        if not isinstance(raw, dict):
            raise bad("an answer must be a mapping, got %r" % (raw,))
        unknown = sorted(set(raw) - _ANSWER_KEYS)
        if unknown:
            raise bad("unknown key(s) %s; valid: %s" % (", ".join(unknown), ", ".join(sorted(_ANSWER_KEYS))))
        if not isinstance(idx, int) or isinstance(idx, bool) or idx not in asked:
            raise bad("index is not one of the imports asked")
        status = raw.get("status")
        if status not in STATUSES:
            raise bad("status %r; valid: %s" % (status, ", ".join(STATUSES)))
        if status == "defer" and not self.allow_defer:
            raise bad("`defer` is no answer here: nothing resolves after this resolver")
        if self.allow_defer and set(raw) & _READER_KEYS:
            raise bad("%s are the pack's reader's to say" % ", ".join(sorted(set(raw) & _READER_KEYS)))
        target, reason = raw.get("target"), ""
        if status == "internal" and target not in known:
            raise bad("internal target %r is no module of the tree" % (target,))
        if status in ("external", "builtin") and not (isinstance(target, str) and target):
            raise bad("a %s answer names its `target`" % status)
        if status in ("unresolved", "ambiguous"):
            m = _REASON.fullmatch(str(raw.get("reason")))
            if m is None or m.group(1) != status:
                raise bad("reason %r is not %s:<code> (lowercase letters, digits, _)"
                          % (raw.get("reason"), status))
            reason = m.group(2)
        names, cands = raw.get("names", []), raw.get("candidates", [])
        if not _strs(names) or not _strs(cands):
            raise bad("`names` and `candidates` are lists of strings")
        if any(not n or "::" in n for n in names):
            raise bad("`names` are non-empty names without `::`, got %r" % (names,))
        if not all(isinstance(raw.get(k, False), bool) for k in ("type_only", "in_function")):
            raise bad("`type_only` and `in_function` are true or false")
        spec = raw.get("spec")
        if spec is not None and not isinstance(spec, str):
            raise bad("`spec` is a string")
        flags = raw.get("flags", [])
        if not isinstance(flags, list) or not all(isinstance(f, dict) for f in flags):
            raise bad("`flags` is a list of mappings")
        stray = sorted({k for f in flags for k in f} - FLAG_KEYS)
        if stray:
            raise bad("flags key(s) %s outside %s" % (", ".join(stray), ", ".join(sorted(FLAG_KEYS))))
        for f in flags:
            if not all(k in f for k in _FLAG_REQUIRED):
                raise bad("a flag needs `to`, `declared` and `type_only`, got %r" % (f,))
            wrong = next((k for k, v in sorted(f.items()) if not isinstance(v, FLAG_TYPES[k])), None)
            if wrong is not None:
                raise bad("flag key %r is %r, not %s" % (wrong, f[wrong],
                                                          " or ".join(t.__name__ for t in FLAG_TYPES[wrong])))
        return Answer(idx, status, target if isinstance(target, str) else None, tuple(names),
                      bool(raw.get("type_only")), bool(raw.get("in_function")), reason,
                      tuple(dict(f) for f in flags), spec, tuple(cands))


def answers_key(lang: str) -> str:
    """The import context's `tables` key holding a pass's answers for `lang`, each under
    `revision(parsed)`: one context may read two revisions of one file (`change
    structural`), and an answer is about the text it was asked about."""
    return "resolver_command:%s" % lang


def revision(parsed: Any) -> "tuple[str, str]":
    return parsed.module, parsed.file_sha


# ---- door (a): a project's language --------------------------------------------

class ProjectResolver:
    """The resolver of a project's language (`lang.registry.ProjectPack`): path ids, every
    import answered by the command."""

    def __init__(self, row: Any):
        self.row = row
        self._cmd: "ResolverCommand | None" = None

    def command(self, repo: Path) -> ResolverCommand:
        if self._cmd is None or self._cmd.repo != Path(repo):
            self._cmd = ResolverCommand(self.row.resolver, repo, what="resolver %r" % self.row.id,
                                        language=self.row.id)
        return self._cmd

    def name_modules(self, repo: Path, roots: list, descriptor: Any, payload: Any) -> Named:
        from ..layout import root_owns
        from ..walk import git_listed, walk_files
        include = tuple("*" + e for e in self.row.extensions)
        listed = git_listed(repo)
        listed_set = set(listed) if listed is not None else None
        sources: "list[SourceRef]" = []
        seen = ignored = 0
        for r in roots:
            for p in walk_files(r.dir(repo), include):
                if not root_owns(p, r, roots, repo):
                    continue
                rel = p.relative_to(repo).as_posix()
                if listed_set is not None and rel not in listed_set:
                    ignored += 1
                    continue
                seen += 1
                sources.append(SourceRef(
                    path=p, module=rel, root=r.id, unit="", unit_name="", lang=self.row.id,
                    payload=dict(payload.source_payload(repo, r, rel)) if payload else {}))
        census: "dict[str, Any]" = {"files_seen": seen, "skipped": 0}
        if ignored:
            census["ignored_skipped"] = ignored
        return Named(sources, set(), [], census)

    def config_inputs(self, repo: Path, roots: list, descriptor: Any) -> "tuple[str, ...]":
        import trust_store
        return tuple(trust_store.command_files(repo, [self.row.command, self.row.resolver]))

    def prepare(self, repo: Path, sources: "Sequence[SourceRef]", roots: Any, import_roots: Any,
                all_mods: "set[str]") -> "dict[str, Any]":
        return {}

    def ready(self, ctx: Any) -> None:
        self.command(ctx.repo).require_trust()

    def primes(self, ctx: Any) -> bool:
        return True

    def prime(self, ctx: Any, parsed: "Sequence[Any]") -> None:
        """Ask the command about every import of `parsed`, once, for this pass."""
        got = self.command(ctx.repo).resolve_batch(
            sorted(ctx.all_mods), [(p.module, list(enumerate(p.imports))) for p in parsed])
        ctx.tables.setdefault(answers_key(self.row.id), {}).update(
            {revision(p): got.get(p.module, {}) for p in parsed})

    def classify(self, parsed: Any, ctx: Any) -> ClassifiedImports:
        if revision(parsed) not in ctx.tables.get(answers_key(self.row.id), {}):
            self.prime(ctx, [parsed])
        answers = ctx.tables[answers_key(self.row.id)][revision(parsed)]
        out = ClassifiedImports(set(), set(), set(), set(), set())
        unresolved: "set[tuple[str, str]]" = set()
        detail: "dict[tuple[str, str], dict[str, Any]]" = {}
        flags: "list[dict[str, Any]]" = []
        for i, imp in enumerate(parsed.imports):
            a = answers[i]
            flags += a.flags
            if a.status == "internal":
                bucket = out.type_only if a.type_only else (out.function_local if a.in_function
                                                            else out.module_level)
                bucket.update(["%s::%s" % (a.target, n) for n in a.names] or [str(a.target)])
            elif a.status in ("external", "builtin"):
                out.external.add(str(a.target))
            else:
                key = (a.spec or str(imp.get("spec") or "#%d" % i), a.reason)
                unresolved.add(key)
                if a.candidates:
                    detail[key] = {"candidates": list(a.candidates)}
        return ClassifiedImports(out.module_level, out.function_local, out.external, out.type_only,
                                 out.ambiguous, frozenset(unresolved), detail, tuple(flags))

    def module_at(self, ctx: Any, rel_path: str) -> "str | None":
        return rel_path

    def symbol_entries(self, parsed: Any) -> "list[dict]":
        return build_symbol_entries(parsed)

    def default_unit(self, rel_path: str, root_path: str) -> str:
        """The first directory under the root; a file directly in it is its own unit."""
        rel = PurePosixPath(rel_path)
        if root_path not in ("", "."):
            rel = rel.relative_to(root_path)
        return rel.parts[0] if len(rel.parts) > 1 else rel.stem
