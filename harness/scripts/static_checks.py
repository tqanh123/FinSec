"""Static checks as data on a language pack.

The bin's `harness/data/hs-code-languages.yaml` carries `checks:` on a pack row: how to
run a tool (`bin`, `argv`) and read it (`parser`, `broken_codes`, `suppressions_file`).
A repo turns one on, and sets its policy, under `languages.<id>.checks.<check id>` in
its `codemap.yaml`; only declared checks run. The repo's keys are a closed set:

    mode      new (default) | all | advisory -- what fails the check: a finding the
              baseline does not hold, any finding, nothing
    baseline  the tracked file of known findings (repo-relative, inside the repo, never
              under `.harness/`); not needed where the tool keeps its own
              (`suppressions_file`)
    argv      replaces the bin row's argv (the tool's own flags)
    grace     {reason, expires: YYYY-MM-DD, dec}: lets HEAD's policy be laxer than the
              base's until `expires` (`policy_floor`)

Stdlib + `yaml_io` only, like `test_runners`: the trust store and `policy_floor` read it
without the code-graph engine.
"""
from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import test_runners

MODES = ("all", "new", "advisory")  # strictest first
_KEYS = frozenset({"id", "bin", "argv", "parser", "type", "broken_codes", "suppressions_file",
                   "write_flags", "emit", "config_inputs", "reads"})
_PROJECT_KEYS = frozenset({"mode", "baseline", "argv", "grace"})
_GRACE_KEYS = frozenset({"reason", "expires", "dec"})
_PARSERS = ("tsc_lines", "eslint_json")
_TYPES = ("typecheck", "lint")
_EMITTERS = ("tsc",)
UNITS = "{units}"


class CheckSpecError(ValueError):
    """A check row, bin's or repo's, that does not satisfy this module's contract."""


@dataclass(frozen=True)
class CheckSpec:
    id: str
    bin: str
    argv: "tuple[str, ...]"
    parser: str
    type: str
    broken_codes: "tuple[str, ...]" = ()
    suppressions_file: str = ""
    write_flags: "tuple[str, ...]" = ()
    emit: str = ""
    config_inputs: "tuple[str, ...]" = ()
    #: suffixes the tool reads beyond its pack's files (tsc: a JSON module): in its digest
    reads: "tuple[str, ...]" = ()
    mode: str = "new"
    baseline: str = ""
    grace: Optional[Mapping[str, str]] = None

    @property
    def name(self) -> str:
        return "%s/%s" % (self.type, self.id)

    def fingerprint(self) -> str:
        """What a run of it ran under, less its mode (a run under a stricter one proves a
        laxer hold): an argv loosened for a run and put back is another spec."""
        import dataclasses
        import hashlib
        import json
        fields = {k: v for k, v in dataclasses.asdict(self).items() if k not in ("mode", "grace")}
        return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _strs(row: Mapping, key: str, at: str) -> "tuple[str, ...]":
    v = row.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise CheckSpecError("%s: `%s:` must be a list of strings" % (at, key))
    return tuple(v)


def _closed(row: Mapping, keys: frozenset, at: str) -> None:
    unknown = sorted(set(row) - keys)
    if unknown:
        raise CheckSpecError("%s: unknown key(s) %s; valid: %s"
                             % (at, ", ".join(map(str, unknown)), ", ".join(sorted(keys))))


def parse_row(row: Any, at: str) -> CheckSpec:
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
        raise CheckSpecError("%s: a check row is a mapping with a non-empty `id:`" % at)
    at = "%s (%s)" % (at, row["id"])
    _closed(row, _KEYS, at)
    for key, allowed in (("parser", _PARSERS), ("type", _TYPES)):
        if row.get(key) not in allowed:
            raise CheckSpecError("%s: `%s:` must be one of %s" % (at, key, ", ".join(allowed)))
    if row.get("emit", "") not in ("",) + _EMITTERS:
        raise CheckSpecError("%s: `emit:` must be one of %s" % (at, ", ".join(_EMITTERS)))
    if not isinstance(row.get("bin"), str) or not row["bin"]:
        raise CheckSpecError("%s: `bin:` names the executable under node_modules/.bin" % at)
    return CheckSpec(id=row["id"], bin=row["bin"], argv=_strs(row, "argv", at), parser=row["parser"],
                     type=row["type"], broken_codes=_strs(row, "broken_codes", at),
                     suppressions_file=str(row.get("suppressions_file") or ""),
                     write_flags=_strs(row, "write_flags", at), emit=str(row.get("emit") or ""),
                     config_inputs=_strs(row, "config_inputs", at), reads=_suffixes(row, at))


def _suffixes(row: Mapping, at: str) -> "tuple[str, ...]":
    out = _strs(row, "reads", at)
    bad = [s for s in out if not s.startswith(".") or "/" in s]
    if bad:
        raise CheckSpecError("%s: `reads:` lists file suffixes such as .json, not %s" % (at, ", ".join(bad)))
    return out


def book(language: str) -> "tuple[CheckSpec, ...]":
    for row in test_runners._bin_book():
        if row.get("id") == language:
            return tuple(parse_row(r, "languages.%s checks[%d]" % (language, i))
                         for i, r in enumerate(row.get("checks") or [], 1))
    return ()


def _baseline(v: Any, root: Optional[Path], at: str) -> str:
    if not isinstance(v, str) or not v or os.path.isabs(v):
        raise CheckSpecError("%s: `baseline:` is a repo-relative path" % at)
    norm = os.path.normpath(v).replace(os.sep, "/")
    if norm == ".." or norm.startswith("../"):
        raise CheckSpecError("%s: `baseline:` %s must sit inside the repo" % (at, v))
    if norm == ".harness" or norm.startswith(".harness/"):
        raise CheckSpecError("%s: `baseline:` %s sits under .harness/, which is not tracked" % (at, v))
    if root is not None:
        real, top = os.path.realpath(Path(root) / norm), os.path.realpath(root)
        if not (real == top or real.startswith(top + os.sep)):
            raise CheckSpecError("%s: `baseline:` %s resolves outside the repo (a symlink)" % (at, v))
    return norm


def _grace(v: Any, at: str) -> "dict[str, str]":
    if not isinstance(v, dict) or set(v) != _GRACE_KEYS:
        raise CheckSpecError("%s: `grace:` needs exactly %s" % (at, ", ".join(sorted(_GRACE_KEYS))))
    try:
        datetime.date.fromisoformat(str(v["expires"]))
    except ValueError:
        raise CheckSpecError("%s: `grace.expires:` %r is no ISO date (YYYY-MM-DD)" % (at, v["expires"])) from None
    return {k: str(v[k]) for k in sorted(_GRACE_KEYS)}


def merge(language: str, block: Any, root: Optional[Path] = None) -> "tuple[CheckSpec, ...]":
    """The checks a repo's `languages.<language>.checks` block declares, each over the
    bin's row of the same id; `CheckSpecError` names what it cannot read."""
    if block is None:
        return ()
    at = "codemap.yaml languages.%s.checks" % language
    if not isinstance(block, dict):
        raise CheckSpecError("%s: must be a mapping {<check id>: {<policy>}}" % at)
    rows = {s.id: s for s in book(language)}
    out = []
    for cid, fields in block.items():
        here = "%s.%s" % (at, cid)
        if cid not in rows:
            raise CheckSpecError("%s: the %s pack has no check `%s` (it has: %s)"
                                 % (here, language, cid, ", ".join(sorted(rows)) or "none"))
        fields = {} if fields is None else fields
        if not isinstance(fields, dict):
            raise CheckSpecError("%s: must be a mapping of %s" % (here, ", ".join(sorted(_PROJECT_KEYS))))
        _closed(fields, _PROJECT_KEYS, here)
        mode = fields.get("mode", "new")
        if mode not in MODES:
            raise CheckSpecError("%s: `mode:` %r must be one of %s" % (here, mode, ", ".join(MODES)))
        spec = rows[cid]
        out.append(CheckSpec(**{**spec.__dict__, "mode": mode,
                                "baseline": _baseline(fields["baseline"], root, here) if "baseline" in fields else "",
                                "argv": _strs(fields, "argv", here) if "argv" in fields else spec.argv,
                                "grace": _grace(fields["grace"], here) if "grace" in fields else None}))
    return tuple(out)


def load(root: Optional[Path], language: str) -> "tuple[CheckSpec, ...]":
    return merge(language, (test_runners._repo_languages(root).get(language) or {}).get("checks"), root)


def declared(root: Optional[Path]) -> "list[tuple[str, CheckSpec]]":
    """(language, check) for every check `root`'s codemap.yaml declares."""
    return [(str(lang), s) for lang, v in test_runners._repo_languages(root).items()
            if isinstance(v, dict) for s in load(root, str(lang))]
