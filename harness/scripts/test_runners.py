"""Test runners as data on a language pack.

The bin's `harness/data/hs-code-languages.yaml` carries `runners:` on a pack row; a
repo adds or overrides runners under `languages.<id>.runners` in its `codemap.yaml`
(`{<runner id>: {<fields>}}` — a known id overrides fields, a new id is a whole row).
Repo-declared runners come first, in declared order, then the bin's. Every key is
checked here, for both sources: a mistyped key would otherwise read as nothing.

`argv` holds the literal `"{targets}"` exactly once, as its own item; it expands to
one argv item per target (the `domains.test.command` contract, same wording).

Stdlib + `yaml_io` only: `detect_techstack`, the hs:test scripts and `hs_run_cook`
import this without the code-graph engine.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

PLACEHOLDER = "{targets}"
BOOK = Path(__file__).resolve().parents[1] / "data" / "hs-code-languages.yaml"
EXECUTORS = ("agent", "harness")
_KEYS = frozenset({"id", "executor", "detect", "argv", "target_mode", "select", "empty_ok",
                   "result", "coverage", "test_exts", "bin", "type", "write_flags",
                   "config_inputs", "outputs"})
_DETECT_KEYS = frozenset({"files", "deps", "package_json_key", "config_lookup"})
_LOOKUPS = ("cwd_only", "walk_up")
#: what the harness reads back from a run it executes
_FORMATS = ("jest-json", "junit")
#: test-policy test types a harness-run runner may report as (its check is `<type>/<id>`)
_TYPES = ("unit", "integration", "component", "e2e", "smoke", "regression")
RESULT = "{result}"
#: the verb that runs a `harness`-executor runner (`codegraph.test.run`)
HARNESS_VERB = ("hs-code", "test", "run")
HARNESS_EXIT_NOTE = (
    "`hs-code test run` exits 0 PASS or SKIP, 1 FAIL (a declared test file not on disk "
    "yet is a FAIL naming it -- write it), 2 BROKEN (the runner never got to run tests: "
    "fix its setup), 3 BLOCKED (a test no runner lists, or a run of no test)")
_SELECT_KEYS = frozenset({"list_argv", "exclude_flag"})
_EMPTY_KEYS = frozenset({"flag", "rc"})
_RESULT_KEYS = frozenset({"format", "out"})
_COVERAGE_KEYS = frozenset({"flags", "provider_key", "lcov_sf_base", "never_reported"})
_TARGET_MODES = ("path", "substring_ci", "regex")


def _strs(row: Mapping, key: str, at: str) -> "tuple[str, ...]":
    v = row.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise RunnerError("%s: `%s:` must be a list of strings" % (at, key))
    return tuple(v)


class RunnerError(ValueError):
    """A runner row that does not satisfy this module's contract."""


@dataclass(frozen=True)
class RunnerSpec:
    id: str
    executor: str
    argv: "tuple[str, ...]"
    target_mode: str = "path"
    detect: Mapping[str, Any] = field(default_factory=dict)
    select: Mapping[str, Any] = field(default_factory=dict)
    empty_ok: Mapping[str, Any] = field(default_factory=dict)
    result: Mapping[str, Any] = field(default_factory=dict)
    coverage: Mapping[str, Any] = field(default_factory=dict)
    test_exts: "tuple[str, ...]" = ()
    #: the executable under `node_modules/.bin` (harness runners); the argv follows it
    bin: str = ""
    #: the test-policy type its check reports as: `<type>/<id>`
    type: str = "unit"
    #: argv items that make a run write into the tree (snapshot update): refused
    write_flags: "tuple[str, ...]" = ()
    #: repo files (globs, unit-relative) whose content the result depends on
    config_inputs: "tuple[str, ...]" = ()
    #: unit-relative dirs a run writes on purpose (coverage): not inputs, not restored
    outputs: "tuple[str, ...]" = ()


def fingerprint(spec: RunnerSpec) -> str:
    """What a run of `spec` ran under: an argv loosened for a run and put back is another
    spec."""
    import dataclasses
    import hashlib
    import json
    return hashlib.sha256(json.dumps(dataclasses.asdict(spec), sort_keys=True, default=str)
                          .encode()).hexdigest()[:16]


def check_argv(argv: Any, at: str) -> "tuple[str, ...]":
    """`argv` as a tuple, or `RunnerError` naming what breaks the `{targets}` contract
    (shared with `domains.test.command`, which delegates here)."""
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise RunnerError("%s: must be a list of strings, got %r" % (at, argv))
    exact = sum(a == PLACEHOLDER for a in argv)
    embedded = [a for a in argv if a != PLACEHOLDER and PLACEHOLDER in a]
    if embedded:
        raise RunnerError("%s: \"%s\" must be its own argv item, not embedded inside %r"
                          % (at, PLACEHOLDER, embedded[0]))
    if not exact:
        raise RunnerError("%s: missing the \"%s\" placeholder item" % (at, PLACEHOLDER))
    if exact > 1:
        raise RunnerError("%s: \"%s\" must appear exactly once, found %d"
                          % (at, PLACEHOLDER, exact))
    return tuple(argv)


def _sub(row: Mapping, key: str, keys: frozenset, at: str) -> dict:
    v = row.get(key, {})
    if not isinstance(v, dict):
        raise RunnerError("%s: `%s:` must be a mapping" % (at, key))
    unknown = sorted(set(v) - keys)
    if unknown:
        raise RunnerError("%s: `%s:` unknown key(s) %s; valid: %s"
                          % (at, key, ", ".join(unknown), ", ".join(sorted(keys))))
    return dict(v)


def parse_row(row: Any, at: str) -> RunnerSpec:
    """One whole runner row, every key checked."""
    if not isinstance(row, dict):
        raise RunnerError("%s must be a mapping" % at)
    rid = row.get("id")
    if not isinstance(rid, str) or not rid:
        raise RunnerError("%s: `id:` must be a non-empty string" % at)
    at = "%s (%s)" % (at, rid)
    unknown = sorted(set(row) - _KEYS)
    if unknown:
        raise RunnerError("%s: unknown key(s) %s; valid: %s"
                          % (at, ", ".join(unknown), ", ".join(sorted(_KEYS))))
    if row.get("executor") not in EXECUTORS:
        raise RunnerError("%s: `executor:` must be one of %s, got %r"
                          % (at, ", ".join(EXECUTORS), row.get("executor")))
    mode = row.get("target_mode", "path")
    if mode not in _TARGET_MODES:
        raise RunnerError("%s: `target_mode:` must be one of %s" % (at, ", ".join(_TARGET_MODES)))
    exts = row.get("test_exts", [])
    if not isinstance(exts, list) or not all(isinstance(e, str) and e.startswith(".") for e in exts):
        raise RunnerError("%s: `test_exts:` must be a list of suffixes like '.py'" % at)
    empty_ok = _sub(row, "empty_ok", _EMPTY_KEYS, at)
    if "rc" in empty_ok and not isinstance(empty_ok["rc"], int):
        raise RunnerError("%s: `empty_ok.rc:` must be an integer exit code" % at)
    argv = check_argv(row.get("argv"), "%s `argv:`" % at)
    detect = _sub(row, "detect", _DETECT_KEYS, at)
    if detect.get("config_lookup", "cwd_only") not in _LOOKUPS:
        raise RunnerError("%s: `detect.config_lookup:` must be one of %s" % (at, ", ".join(_LOOKUPS)))
    result = _sub(row, "result", _RESULT_KEYS, at)
    if "format" in result and result["format"] not in _FORMATS:
        raise RunnerError("%s: `result.format:` must be one of %s" % (at, ", ".join(_FORMATS)))
    rtype = row.get("type", "unit")
    if rtype not in _TYPES:
        raise RunnerError("%s: `type:` must be one of %s" % (at, ", ".join(_TYPES)))
    if row["executor"] == "harness" and not (result.get("format") and any(RESULT in a for a in argv)):
        raise RunnerError("%s: a harness-run runner names `result.format:` and writes it where "
                          "`argv:` says \"%s\" — the verdict is read from that file" % (at, RESULT))
    return RunnerSpec(id=rid, executor=row["executor"], argv=argv, target_mode=mode,
                      detect=detect, select=_sub(row, "select", _SELECT_KEYS, at), empty_ok=empty_ok,
                      result=result, coverage=_sub(row, "coverage", _COVERAGE_KEYS, at),
                      test_exts=tuple(exts), bin=str(row.get("bin") or ""), type=rtype,
                      write_flags=_strs(row, "write_flags", at),
                      config_inputs=_strs(row, "config_inputs", at), outputs=_strs(row, "outputs", at))


def parse_rows(raw: Any, at: str) -> "tuple[RunnerSpec, ...]":
    """A pack row's `runners:` list; ids unique."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RunnerError("%s: `runners:` must be a list of runner rows" % at)
    out = [parse_row(r, "%s runners[%d]" % (at, i)) for i, r in enumerate(raw, 1)]
    ids = [s.id for s in out]
    if len(set(ids)) != len(ids):
        raise RunnerError("%s: `runners:` declares a runner id twice" % at)
    return tuple(out)


def _yaml(path: Path) -> Any:
    import yaml_io
    return yaml_io.safe_load(path.read_text(encoding="utf-8"))


def _bin_book() -> "list[dict]":
    return [r for r in (_yaml(BOOK) or {}).get("languages") or [] if isinstance(r, dict)]


def _bin_rows(language: str) -> "list[dict]":
    for row in _bin_book():
        if row.get("id") == language:
            return list(row.get("runners") or [])
    return []


def _repo_languages(root: Optional[Path]) -> dict:
    if root is None or not (Path(root) / "codemap.yaml").is_file():
        return {}
    return (_yaml(Path(root) / "codemap.yaml") or {}).get("languages") or {}


def _repo_rows(block: Any, language: str) -> "dict[str, dict]":
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise RunnerError("codemap.yaml languages.%s.runners: must be a mapping "
                          "{<runner id>: {<fields>}}" % language)
    for rid, fields in block.items():
        if not isinstance(fields, dict) or "id" in fields:
            raise RunnerError("codemap.yaml languages.%s.runners.%s: must be a mapping of "
                              "fields (the key is the id)" % (language, rid))
    return {str(k): dict(v) for k, v in block.items()}


def merge(language: str, block: Any) -> "tuple[RunnerSpec, ...]":
    """`language`'s runners with a repo's `languages.<language>.runners` block (None:
    the bin's alone), repo-declared first; every merged row checked (`RunnerError`).
    A repo field set over a bin row of the same id replaces that field."""
    bin_rows = {r.get("id"): r for r in _bin_rows(language) if isinstance(r, dict)}
    repo = _repo_rows(block, language)
    rows = [dict(bin_rows.get(rid, {}), **fields, id=rid) for rid, fields in repo.items()]
    rows += [r for rid, r in bin_rows.items() if rid not in repo]
    at = "languages.%s" % language
    return tuple(parse_row(r, "%s runners.%s" % (at, r["id"])) for r in rows)


def load(root: Optional[Path], language: str) -> "tuple[RunnerSpec, ...]":
    """`language`'s runners for the repo at `root` (None: the bin's alone)."""
    return merge(language, (_repo_languages(root).get(language) or {}).get("runners"))


def pick(language: str, root: Optional[Path] = None) -> Optional[RunnerSpec]:
    """The runner `language` uses in the repo at `root`: the first one, or None."""
    specs = load(root, language)
    return specs[0] if specs else None


def _languages(root: Optional[Path]) -> "list[str]":
    """Every language a runner is declared for: the bin's, then the repo's own."""
    langs = [str(r.get("id")) for r in _bin_book() if r.get("runners")]
    return langs + [str(k) for k, v in _repo_languages(root).items()
                    if k not in langs and isinstance(v, dict) and v.get("runners")]


def enabled_languages(root: Optional[Path]) -> "set[str]":
    """The language packs `root`'s codemap.yaml enables (`languages:` keys)."""
    return {str(k) for k in _repo_languages(root)}


def harness_specs_of(languages: Any) -> "list[tuple[str, RunnerSpec]]":
    """The `harness`-executor runners a code map's `languages:` block (read at any commit)
    enables -- each language's bin rows with the block's own over them."""
    if not isinstance(languages, dict):
        return []
    out: "list[tuple[str, RunnerSpec]]" = []
    for lang, v in sorted(languages.items()):
        block = v.get("runners") if isinstance(v, dict) else None
        out += [(str(lang), s) for s in merge(str(lang), block) if s.executor == "harness"]
    return out


def all_specs(root: Optional[Path]) -> "list[tuple[str, RunnerSpec]]":
    """(language, runner) for every runner of every language, in `load` order."""
    return [(lang, s) for lang in _languages(root) for s in load(root, lang)]


def _package_json(root: Path, d: str) -> dict:
    import json
    try:
        doc = json.loads((Path(root) / d / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def detected(root: Path, d: str, spec: RunnerSpec) -> bool:
    """`spec` is set up in the directory `d` of `root`: one of its `detect.files`, its
    `package_json_key`, or one of its `deps` in that directory's package.json."""
    det = spec.detect
    if any((Path(root) / d / f).is_file() for f in det.get("files") or ()):
        return True
    pj = _package_json(root, d)
    if det.get("package_json_key") and det["package_json_key"] in pj:
        return True
    deps = {**(pj.get("dependencies") or {}), **(pj.get("devDependencies") or {})}
    return any(x in deps for x in det.get("deps") or ())


def harness_inputs(root: Path, lang: str) -> "tuple[tuple[str, ...], tuple[str, ...]]":
    """(config_inputs, outputs) over every runner of `lang` the harness runs: what a
    coverage line -- one over all of them -- depends on and writes."""
    specs = [s for lang_id, s in harness_specs(root) if lang_id == lang]
    return (tuple(sorted({g for s in specs for g in s.config_inputs})),
            tuple(sorted({o for s in specs for o in s.outputs})))


def owner(root: Path, rel: str, specs: "Sequence[tuple[str, RunnerSpec]]"
          ) -> "Optional[tuple[str, str, RunnerSpec]]":
    """(unit dir, language, runner): the nearest directory above `rel` (repo-relative)
    where one of `specs` is `detected`, the first of `specs` winning in a directory."""
    import posixpath
    d = posixpath.dirname(str(rel)) or "."
    while True:
        for lang, spec in specs:
            if detected(root, d, spec):
                return d, lang, spec
        if d == ".":
            return None
        d = posixpath.dirname(d) or "."


def harness_specs(root: Optional[Path], suffix: Optional[str] = None
                  ) -> "list[tuple[str, RunnerSpec]]":
    """The `harness`-executor runners, those claiming `suffix` when one is given."""
    return [(lang, s) for lang, s in all_specs(root) if s.executor == "harness"
            and (suffix is None or suffix in s.test_exts)]


def harness_command(targets: "Optional[Sequence[str]]" = None, *, plan: Any = None,
                    phase: Optional[str] = None) -> str:
    """What an agent types to have the harness run `targets` (None: every test)."""
    argv = list(HARNESS_VERB) + (["--targets", *map(str, targets)] if targets else ["--all"])
    if plan:
        argv += ["--plan", str(plan), "--phase", str(phase)]
    return render(argv)


def harness_runs(root: Optional[Path], target: str) -> bool:
    """A harness runner claims `target`'s suffix and is set up above it in `root`."""
    specs = harness_specs(root, Path(str(target)).suffix)
    return bool(specs) and root is not None and owner(Path(root), str(target), specs) is not None


def commands(root: Optional[Path], targets: Sequence[str], *, plan: Any = None,
             phase: Optional[str] = None) -> "tuple[list[str], list[str]]":
    """One rendered command per language whose runner claims a target's suffix
    (`test_exts`), in first-target order, and every target no runner claims. A target
    a harness runner owns joins one `hs-code test run` (`--plan --phase` when given):
    the harness picks each one's runner itself."""
    specs = [s for s in (pick(lang, root) for lang in _languages(root)) if s is not None]
    groups: "dict[str, tuple[Optional[RunnerSpec], list[str]]]" = {}
    unclaimed: "list[str]" = []
    for t in targets:
        if harness_runs(root, str(t)):
            groups.setdefault("", (None, []))[1].append(str(t))
            continue
        spec = next((s for s in specs if s.executor == "agent"
                     and Path(str(t)).suffix in s.test_exts), None)
        if spec is None:
            unclaimed.append(str(t))
        else:
            groups.setdefault(spec.id, (spec, []))[1].append(str(t))
    return [harness_command(ts, plan=plan, phase=phase) if s is None
            else render(scoped_argv(s, ts)) for s, ts in groups.values()], unclaimed


def require(root: Optional[Path], language: str, runner_id: str, *, tool: str) -> RunnerSpec:
    """`tool` only drives `runner_id`; a repo whose runner is another one is refused by
    name rather than handed a run of the wrong tool."""
    spec = pick(language, root)
    if spec is None or spec.id != runner_id:
        import hs_family
        raise hs_family.RefusedError(
            "%s drives %s only; this repo's %s runner is %s (languages.%s.runners in "
            "codemap.yaml) — nothing run" % (tool, runner_id, language,
                                             spec.id if spec else "not declared", language))
    return spec


def scoped_argv(spec: RunnerSpec, targets: Sequence[str]) -> "list[str]":
    """`spec.argv` with its `{targets}` item replaced by one item per target."""
    if not targets:
        raise ValueError("scoped_argv: at least one target is required")
    out: "list[str]" = []
    for a in spec.argv:
        out.extend(str(t) for t in targets) if a == PLACEHOLDER else out.append(a)
    return out


def render(argv: Sequence[str]) -> str:
    return shlex.join(list(argv))
