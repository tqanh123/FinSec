"""instruction_policy — rules proposed from a project's instruction prose, adopted only by a
person, and allowed to warn or rewrite, never to block.

Three files in the project's data home (`harness_paths.data_root_for(root)/policy/`):

  candidates.json   machine-written by `compile`, replaced whole each run, never evaluated
  policy.yaml       the active document: human-editable YAML, the only one the hooks read
  adoptions.jsonl   append-only, one line per adopted rule, `actor` + `ts`

A sentence becomes a candidate only when it fits one of three forms (`FORMS`); a sentence
that reads like a rule and fits none is reported as unmatched instead of guessed at. Each
compiled rule carries the file, line and sha256 of the sentence it came from, so `stale`
can tell a rule whose instruction changed from one whose line merely moved.

A document whose rules disagree (`conflicts`) enforces nothing: `check` refuses it and both
hooks stay silent. Blocking lives in the guard policy, not here — `check_schema` refuses a
rule whose action is anything but `warn`, or `rewrite` on the one form that can rewrite.

CLI: `hs-cli policy compile|adopt|status|check|validate` (`POLICY` below).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import yaml_io

VERSION = 1
DEFAULT_SOURCES = ("CLAUDE.md", "AGENTS.md")
ACTIONS = {"forbid-edit": ("warn",), "prefer-command": ("rewrite", "warn"),
           "require-check": ("warn",)}
LANDINGS = ("commit", "push")
#: one program word: what `prefer-command` may match and put in its place
_WORD = r"[A-Za-z0-9][A-Za-z0-9._+-]*"
_WORD_RE = re.compile(r"^%s$" % _WORD)
_PATH = r"[A-Za-z0-9_.*/{}\[\]-]*[/.*][A-Za-z0-9_.*/{}\[\]-]*"

FORMS = (
    ("forbid-edit", re.compile(
        r"\b(?:never|do not|don't|must not)\s+(?:edit|modify|change|touch|write(?: to)?)\s+`(%s)`" % _PATH,
        re.I)),
    ("forbid-edit", re.compile(r"(?:không được|không|đừng|cấm)\s+(?:sửa|ghi|chỉnh|động vào)\s+`(%s)`" % _PATH,
                               re.I)),
    ("prefer-command", re.compile(r"\buse\s+`(%s)`\s+(?:instead of|rather than)\s+`(%s)`" % (_WORD, _WORD), re.I)),
    ("prefer-command", re.compile(r"\bprefer\s+`(%s)`\s+(?:over|to)\s+`(%s)`" % (_WORD, _WORD), re.I)),
    ("prefer-command", re.compile(r"\bdùng\s+`(%s)`\s+thay\s+(?:cho|vì)\s+`(%s)`" % (_WORD, _WORD), re.I)),
    ("require-check", re.compile(r"\brun\s+`([^`]+)`\s+before\s+(commit|push)(?:ting|ing)?\b", re.I)),
    ("require-check", re.compile(r"\bchạy\s+`([^`]+)`\s+trước khi\s+(commit|push)\b", re.I)),
)
#: a sentence that reads like a rule: reported when it fits no form
_RULE_LIKE = re.compile(r"\b(?:never|always|must|do not|don't|required?)\b|"
                        r"(?:không được|luôn luôn|bắt buộc|cấm)", re.I)


class PolicyError(Exception):
    """Base: every door maps a subclass to one exit code."""


class Broken(PolicyError):
    """The document cannot be read, or holds a rule the engine cannot honour."""


class Conflict(PolicyError):
    """Two rules disagree about an input both describe; nothing is enforced."""


class Refusal(PolicyError):
    """The request is well-formed but is not allowed (unknown id, overwrite, no name)."""


# ------------------------------------------------------------------ paths

def _data_home(root: Path) -> Path:
    import harness_paths
    return Path(harness_paths.data_root_for(root)) / "policy"


def candidates_path(root: Path) -> Path:
    return _data_home(root) / "candidates.json"


def policy_path(root: Path) -> Path:
    return _data_home(root) / "policy.yaml"


def adoptions_path(root: Path) -> Path:
    return _data_home(root) / "adoptions.jsonl"


# ------------------------------------------------------------------ compile

def _sha(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def _slug(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-") or "x"


def _rule_from(form: str, groups: tuple) -> dict:
    if form == "forbid-edit":
        return {"id": "forbid-edit-" + _slug(groups[0]), "form": form, "action": "warn",
                "match": {"path": groups[0]}}
    if form == "prefer-command":
        want, instead = groups
        return {"id": "prefer-%s-over-%s" % (_slug(want), _slug(instead)), "form": form,
                "action": "rewrite", "match": {"command": instead}, "replace": {"command": want}}
    ran, landing = groups[0], groups[1].lower()
    return {"id": "require-%s-before-%s" % (_slug(ran), landing), "form": form, "action": "warn",
            "match": {"landing": landing, "ran": ran}}


def compile_candidates(root: Path, sources: Optional[Iterable[str]] = None) -> dict:
    """{"candidates": [...], "unmatched": [...], "sources": [...]} — pure, writes nothing."""
    root = Path(root)
    srcs = list(sources) if sources else [s for s in DEFAULT_SOURCES if (root / s).is_file()]
    candidates, unmatched, seen = [], [], set()
    for src in srcs:
        path = root / src
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raise Broken("cannot read instruction file %s: %s" % (src, e))
        for n, line in enumerate(lines, 1):
            hits = [(form, m.groups()) for form, rx in FORMS for m in rx.finditer(line)]
            for form, groups in hits:
                rule = _rule_from(form, groups)
                base, k = rule["id"], 2
                while rule["id"] in seen:
                    rule["id"] = "%s-%d" % (base, k)
                    k += 1
                seen.add(rule["id"])
                rule["provenance"] = {"kind": "compiled", "file": src, "line": n,
                                      "text_sha256": _sha(line)}
                candidates.append(rule)
            if not hits and _RULE_LIKE.search(line):
                unmatched.append({"file": src, "line": n, "text": line.strip()})
    return {"candidates": candidates, "unmatched": unmatched, "sources": srcs}


def run_compile(root: Path, sources: Optional[Iterable[str]] = None) -> dict:
    out = compile_candidates(root, sources)
    p = candidates_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return out


# ------------------------------------------------------------------ document

def load_policy(root: Path) -> dict:
    p = policy_path(root)
    if not p.is_file():
        raise Broken("no policy document at %s; run `hs-cli policy compile`, then "
                     "`hs-cli policy adopt <id>` in a terminal" % p)
    try:
        doc = yaml_io.yaml_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 — a malformed document must name itself
        raise Broken("policy document %s is not valid YAML: %s" % (p, e))
    check_schema(doc)
    return doc


def write_policy(root: Path, doc: dict) -> None:
    check_schema(doc)
    p = policy_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(yaml_io.safe_dump(doc, sort_keys=False), encoding="utf-8")
    os.replace(tmp, p)


def check_schema(doc) -> None:
    if not isinstance(doc, dict) or not isinstance(doc.get("rules", []), list):
        raise Broken("a policy document is a mapping with a `rules:` list")
    for r in doc.get("rules") or []:
        if not isinstance(r, dict) or not r.get("id"):
            raise Broken("every rule needs an `id`: %r" % (r,))
        form, action, match = r.get("form"), r.get("action"), r.get("match") or {}
        if form not in ACTIONS:
            raise Broken("rule %s: unknown form %r (known: %s)" % (r["id"], form, ", ".join(ACTIONS)))
        if action not in ACTIONS[form]:
            raise Broken("rule %s: a %s rule may only %s, not %r — blocking belongs to the "
                         "guard policy" % (r["id"], form, " or ".join(ACTIONS[form]), action))
        if form == "forbid-edit" and not isinstance(match.get("path"), str):
            raise Broken("rule %s: forbid-edit needs match.path" % r["id"])
        if form == "require-check" and (match.get("landing") not in LANDINGS
                                        or not isinstance(match.get("ran"), str)):
            raise Broken("rule %s: require-check needs match.landing (commit|push) and match.ran"
                         % r["id"])
        if form == "prefer-command":
            want = (r.get("replace") or {}).get("command")
            for label, word in (("match.command", match.get("command")), ("replace.command", want)):
                if not isinstance(word, str) or not _WORD_RE.match(word):
                    raise Broken("rule %s: %s must be one program word (%r)" % (r["id"], label, word))


def conflicts(doc: dict) -> list:
    """Every pair of rules that disagree, plus duplicate ids and rewrite cycles."""
    out: list = []
    ids: dict = {}
    rewrites: dict = {}
    for r in doc.get("rules") or []:
        if r["id"] in ids:
            out.append("duplicate id %s" % r["id"])
        ids[r["id"]] = r
        if r.get("form") == "prefer-command" and r.get("action") == "rewrite":
            src, dst = r["match"]["command"], r["replace"]["command"]
            if src in rewrites and rewrites[src][1] != dst:
                out.append("%s and %s rewrite `%s` to different commands (`%s`, `%s`)"
                           % (rewrites[src][0], r["id"], src, rewrites[src][1], dst))
            rewrites.setdefault(src, (r["id"], dst))
    for start in rewrites:
        seen, cur = [start], rewrites[start][1]
        while cur in rewrites:
            if cur in seen:
                out.append("rewrite cycle: %s" % " -> ".join(seen + [cur]))
                break
            seen.append(cur)
            cur = rewrites[cur][1]
    return sorted(set(out))


def stale(root: Path, doc: dict) -> list:
    """Compiled rules whose source sentence is gone. A sentence that moved is not stale."""
    out = []
    for r in doc.get("rules") or []:
        prov = r.get("provenance") or {}
        if prov.get("kind") != "compiled":
            continue
        try:
            lines = (Path(root) / prov["file"]).read_text(encoding="utf-8").splitlines()
        except (OSError, KeyError):
            out.append({"id": r["id"], "reason": "source %s is unreadable" % prov.get("file")})
            continue
        n = prov.get("line") or 0
        if 0 < n <= len(lines) and _sha(lines[n - 1]) == prov.get("text_sha256"):
            continue
        if any(_sha(l) == prov.get("text_sha256") for l in lines):
            continue
        out.append({"id": r["id"], "reason": "%s:%s no longer says what the rule was compiled from"
                    % (prov["file"], n)})
    return out


# ------------------------------------------------------------------ adopt

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actor() -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    import hook_runtime
    return hook_runtime.resolve_actor()


def load_candidates(root: Path) -> dict:
    p = candidates_path(root)
    if not p.is_file():
        raise Refusal("no proposals: run `hs-cli policy compile` first")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Broken("candidates file %s unreadable: %s" % (p, e))


def adopt(root: Path, ids: list, all_: bool) -> list:
    """Copy named proposals into the active document; returns the adopted rules. Nothing
    is written when an id is unknown, already active, or the result would conflict."""
    if not ids and not all_:
        raise Refusal("name the proposals to adopt (or --all): adopting is the confirmation a "
                      "generated rule needs")
    cands = {c["id"]: c for c in load_candidates(root).get("candidates") or []}
    if not cands:
        raise Refusal("no proposals to adopt")
    chosen = list(cands) if all_ else list(ids)
    unknown = [i for i in chosen if i not in cands]
    if unknown:
        raise Refusal("unknown proposal id(s): %s" % ", ".join(unknown))
    doc: dict = load_policy(root) if policy_path(root).is_file() else {"version": VERSION, "rules": []}
    active = {r["id"] for r in doc.get("rules") or []}
    clash = [i for i in chosen if i in active]
    if clash:
        raise Refusal("already active, not overwritten: %s" % ", ".join(clash))
    new = []
    for i in chosen:
        rule = json.loads(json.dumps(cands[i]))
        rule["provenance"]["candidate"] = i
        new.append(rule)
    merged = {"version": doc.get("version", VERSION), **{k: v for k, v in doc.items() if k != "rules"},
              "rules": list(doc.get("rules") or []) + new}
    bad = conflicts(merged)
    if bad:
        raise Conflict("; ".join(bad))
    write_policy(root, merged)
    led = adoptions_path(root)
    actor, ts = _actor(), _now()
    with led.open("a", encoding="utf-8") as fh:
        for rule in new:
            fh.write(json.dumps({"id": rule["id"], "provenance": rule["provenance"],
                                 "actor": actor, "ts": ts}, ensure_ascii=False) + "\n")
    return new


# ------------------------------------------------------------------ check

def _prov(r: dict) -> dict:
    return dict(r.get("provenance") or {"kind": "hand"})


def rewrite_command(doc: dict, command: str) -> Optional[str]:
    """The command with its program word rewritten by the one rule that matches, or None."""
    m = re.match(r"^(\s*)(%s)(?=\s|$)" % _WORD, command or "")
    if not m:
        return None
    for r in doc.get("rules") or []:
        if (r.get("form") == "prefer-command" and r.get("action") == "rewrite"
                and r["match"]["command"] == m.group(2)):
            return m.group(1) + r["replace"]["command"] + command[m.end(2):]
    return None


def _landing_of(command: str) -> Optional[str]:
    try:
        words = shlex.split(command or "")
    except ValueError:
        words = (command or "").split()
    for i, w in enumerate(words[:-1]):
        if w == "git" and words[i + 1] in LANDINGS:
            return words[i + 1]
    return None


def check(doc: dict, *, path: Optional[str] = None, command: Optional[str] = None,
          landing: Optional[str] = None, ran: Iterable[str] = ()) -> dict:
    """What the document says about one operation. Refuses a conflicting document."""
    bad = conflicts(doc)
    if bad:
        raise Conflict("; ".join(bad))
    ran = set(ran or ())
    decisions = []
    rules = doc.get("rules") or []
    if path is not None:
        rel = path.replace(os.sep, "/")
        for r in rules:
            pat = (r.get("match") or {}).get("path")
            if r.get("form") == "forbid-edit" and pat and (
                    fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.rstrip("/*") + "/*")
                    or rel == pat or rel.endswith("/" + pat)):
                decisions.append({"id": r["id"], "action": "warn", "provenance": _prov(r)})
    rewritten = None
    if command is not None:
        rewritten = rewrite_command(doc, command)
        if rewritten is not None:
            m = re.match(r"^\s*(%s)" % _WORD, command)
            for r in rules:
                if m and r.get("form") == "prefer-command" and r["match"]["command"] == m.group(1):
                    decisions.append({"id": r["id"], "action": "rewrite", "provenance": _prov(r)})
        landing = landing or _landing_of(command)
    if landing is not None:
        for r in rules:
            mt = r.get("match") or {}
            if r.get("form") == "require-check" and mt.get("landing") == landing and mt["ran"] not in ran:
                decisions.append({"id": r["id"], "action": "warn", "ran": mt["ran"],
                                  "provenance": _prov(r)})
    return {"decisions": decisions, "rewritten": rewritten}


def where(prov: dict) -> str:
    if prov.get("kind") == "compiled":
        return "%s:%s" % (prov.get("file"), prov.get("line"))
    return "hand-authored"


def active_document(root: Path) -> Optional[dict]:
    """The document the hooks may act on, or None: absent, broken or conflicting ⇒ None."""
    try:
        doc = load_policy(root)
    except PolicyError:
        return None
    return None if conflicts(doc) else doc


# ------------------------------------------------------------------ CLI

def _args(p) -> None:
    sub = p.add_subparsers(dest="verb", required=True)
    c = sub.add_parser("compile", help="propose rules from the instruction files (never enforced)")
    c.add_argument("--source", action="append", default=[], help="instruction file, relative to the root")
    c.add_argument("--json", action="store_true")
    a = sub.add_parser("adopt", help="promote named proposals into the active policy (a person, at a terminal)")
    a.add_argument("ids", nargs="*")
    a.add_argument("--all", action="store_true", help="adopt every proposal")
    s = sub.add_parser("status", help="list the active rules and where each came from")
    s.add_argument("--json", action="store_true")
    k = sub.add_parser("check", help="what the policy says about one operation (read-only)")
    k.add_argument("--path")
    k.add_argument("--command", dest="op_command")
    k.add_argument("--landing", choices=LANDINGS)
    k.add_argument("--ran", action="append", default=[])
    k.add_argument("--json", action="store_true")
    v = sub.add_parser("validate", help="report conflicting rules and stale compiled rules")
    v.add_argument("--fail-on-stale", action="store_true")
    v.add_argument("--json", action="store_true")


def _root(ns) -> Path:
    ctx = getattr(ns, "ctx", None)
    if ctx is not None:
        try:
            return ctx.root
        except Exception:  # noqa: BLE001 — fall back to the working directory
            pass
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())


def _emit(ns, payload: dict, text: str) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2) if getattr(ns, "json", False) else text)


def _confirm(rules: list) -> bool:
    if not sys.stdin.isatty():
        return False
    for r in rules:
        print("  %s  %s  (%s)" % (r["id"], r["action"], where(r.get("provenance") or {})))
    print("adopt these %d rule(s)? [y/N] " % len(rules), end="", flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _run(ns) -> int:
    root = _root(ns)
    try:
        if ns.verb == "compile":
            out = run_compile(root, ns.source or None)
            lines = ["%s  %s  (%s)" % (c["id"], c["action"], where(c["provenance"])) for c in out["candidates"]]
            lines += ["unmatched  %s:%d  %s" % (u["file"], u["line"], u["text"]) for u in out["unmatched"]]
            _emit(ns, out, "\n".join(lines) or "no sentence fits a known form")
            return 0
        if ns.verb == "adopt":
            cands = {c["id"]: c for c in load_candidates(root).get("candidates") or []}
            picked = list(cands.values()) if ns.all else [cands[i] for i in ns.ids if i in cands]
            if not sys.stdin.isatty():
                print("adopt refused: it needs a person at a terminal — run it in your own shell",
                      file=sys.stderr)
                return 3
            if not _confirm(picked or [{"id": i, "action": "?", "provenance": {}} for i in ns.ids]):
                print("nothing adopted", file=sys.stderr)
                return 3
            for r in adopt(root, list(ns.ids), ns.all):
                print("adopted %s" % r["id"])
            return 0
        doc = load_policy(root)
        if ns.verb == "status":
            st = {s["id"]: s["reason"] for s in stale(root, doc)}
            rules = [dict(r, stale=st.get(r["id"])) for r in doc.get("rules") or []]
            text = "\n".join("%s  %s  %s%s" % (r["id"], r["action"], where(r.get("provenance") or {}),
                                               "  [stale]" if r["stale"] else "") for r in rules)
            _emit(ns, {"rules": rules}, text or "no active rules")
            return 0
        if ns.verb == "check":
            ans = check(doc, path=ns.path, command=ns.op_command, landing=ns.landing, ran=ns.ran)
            text = "\n".join("%s  %s  (%s)" % (d["id"], d["action"], where(d["provenance"]))
                             for d in ans["decisions"])
            if ans["rewritten"] is not None:
                text += "\nrewritten: %s" % ans["rewritten"]
            _emit(ns, ans, text.strip() or "the policy says nothing about this")
            return 0
        if ns.verb == "validate":
            bad, gone = conflicts(doc), stale(root, doc)
            _emit(ns, {"conflicts": bad, "stale": gone},
                  "\n".join(["conflict: " + b for b in bad] +
                            ["stale: %s — %s" % (s["id"], s["reason"]) for s in gone]) or "coherent")
            return 1 if bad or (gone and ns.fail_on_stale) else 0
    except Refusal as e:
        print("refused: %s" % e, file=sys.stderr)
        return 3
    except Conflict as e:
        print("conflict: %s — nothing in the document is enforced" % e, file=sys.stderr)
        return 1
    except Broken as e:
        print("broken: %s" % e, file=sys.stderr)
        return 2
    return 2


def _command():
    from hs_family.cli import Command
    return Command(add_args=_args, run=_run)


POLICY = _command()
