"""hs-code check smoke — does every module under `namespace_root` import, and is any
public one imported by nobody.

The modules come from the snapshot (`codegraph.check.tree`), `sys.path` from the
descriptor's `import_roots`, and the imports run in a CHILD interpreter — the target's
own venv with `--python`, else this one — whose path holds the target's roots only, so
no module of this tool shadows one of the target's.

EXIT
    0  every module imports (orphans only warn, unless --strict-orphan)
    1  a module failed to import, or an orphan under --strict-orphan
    2  cannot run honestly: no `namespace_root`, no module under it, the child died

WHAT IT CANNOT SEE
    * Import success is not correct behaviour — only no import-time error.
    * Lazy imports inside functions never run here.
    * The orphan check reads snapshot edges: `getattr` / `importlib` users are invisible.
      A module named only by a string (entry point, plugin table, subprocess) reads
      as an orphan here — a lead to check, not a verdict.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import process_group
from codegraph.check._child import interpreter as _interpreter
from codegraph._core.testfiles import build_test_layout
from codegraph.check.tree import CheckError, add_pin_args, measure, pin_of
from hs_family.cli import Command

NOT_PKG_CODE = frozenset({"tests", "test", "docs", "scripts", "migrations"})
_MARK = "\n@@hs-code-smoke@@"
_CHILD = (
    "import importlib, json, sys\n"
    "a = json.loads(sys.argv[1]); sys.path[:0] = a['path']; errs = {}\n"
    "for m in a['modules']:\n"
    "    try: importlib.import_module(m)\n"
    "    except BaseException as e: errs[m] = '%s: %s' % (type(e).__name__, e)\n"
    "sys.stdout.write(" + repr(_MARK) + " + json.dumps(errs))\n")


def import_errors(modules: List[str], paths: List[Path], python: str, cwd: Path,
                  timeout: int) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = os.pathsep.join(str(p) for p in paths)
    try:
        r = process_group.run([python, "-c", _CHILD, json.dumps({"path": [str(p) for p in paths],
                                                                 "modules": modules})],
                              cwd=str(cwd), env=env, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CheckError("the import walk did not finish in %ds (--timeout)" % timeout)
    except OSError as e:
        raise CheckError("cannot run %s: %s" % (python, e))
    head, sep, tail = r.stdout.rpartition(_MARK)
    if not sep:
        raise CheckError("the import walk died before reporting (rc %d): %s"
                         % (r.returncode, (r.stderr or r.stdout).strip()[-300:]))
    return json.loads(tail)


def orphans(records: List[Dict[str, Any]], walked: List[str],
            is_test: Callable[[Dict[str, Any]], bool] = lambda m: m.get("role") == "test"
            ) -> Tuple[List[str], List[str]]:
    """-> (orphans, test_only) among the public modules of `walked`.

    A module is imported when an edge names it exactly, or names one of its
    SUBMODULES (importing `a.b.c` loads `a.b` first). Importing a parent
    package vouches for nothing below it — that direction exempted whole
    subtrees once (0 orphans where an exact ruler found 8). `test_only`: no
    production importer, but a test (`is_test`) imports it — alive only
    under pytest, a separate class rather than a pass."""
    def targets(recs: List[Dict[str, Any]]) -> "set[str]":
        return {t.split("::", 1)[0] for m in recs
                for t in list(m.get("imports_module_level") or []) + list(m.get("imports_function_local") or [])}

    def reached(m: str, imported: "set[str]") -> bool:
        return m in imported or any(i.startswith(m + ".") for i in imported)

    prod = targets([r for r in records if not is_test(r)])
    every = prod | targets([r for r in records if is_test(r)])
    public = [m for m in walked if not any(p.startswith("_") or p in NOT_PKG_CODE for p in m.split("."))]
    lost = [m for m in public if not reached(m, every)]
    test_only = [m for m in public if reached(m, every) and not reached(m, prod)]
    return lost, test_only


def _smoke_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--strict-orphan", action="store_true", help="a public module nobody imports is red")
    ap.add_argument("--python", help="interpreter of the target's venv; the imports run THERE")
    ap.add_argument("--timeout", type=int, default=600, help="seconds for the whole walk (default 600)")
    add_pin_args(ap)


def _smoke(args: argparse.Namespace) -> int:
    root, opts = args.ctx.root, args.opts
    try:
        tree = measure(Path(root), pin_of(args, opts))
        ns = tree.descriptor.namespace_root
        if not ns:
            raise CheckError("codemap.yaml declares no `namespace_root`: nothing names the "
                             "package to walk")
        records = tree.records()
        walked = sorted({m["module"] for m in records if m["module"] == ns or m["module"].startswith(ns + ".")}
                        - {""})
        walked = [m for m in walked if m.rsplit(".", 1)[-1] != "__main__"]
        if not walked:
            raise CheckError("no module under `%s` in the snapshot" % ns)
        errs = import_errors(walked, tree.import_paths(), _interpreter(args.python), tree.repo, args.timeout)
    except CheckError as e:
        print("hs-code check smoke: %s" % e)
        return 2
    # Computed whether or not an import failed: the edges come from the
    # snapshot, not from the import walk, and an empty list beside a red walk
    # reads as "no orphans". Tests (in a root's test tree, or the snapshot's
    # `test_modules`) import too — but only as `test_only`.
    layout = build_test_layout(tree.repo, tree.descriptor)
    lost, test_only = orphans(records + list(tree.idx.get("test_modules") or ()), walked,
                              lambda m: m.get("role") == "test" or layout.in_test_tree(m["path"]))
    red = bool(errs) or bool(args.strict_orphan and lost)
    if getattr(opts, "json", False):
        print(json.dumps({"schema": "hs-code.check.smoke/1", "namespace": ns, "modules": walked,
                          "import_errors": errs, "orphans": lost, "test_only": test_only},
                         sort_keys=True, ensure_ascii=False))
        return 1 if red else 0
    print("smoke walk: %d module(s) under %s   import errors: %d" % (len(walked), ns, len(errs)))
    for mod, err in sorted(errs.items()):
        print("  RED  %s\n      %s" % (mod, err))
    if lost:
        print("WARNING: %d public module(s) nobody imports:" % len(lost))
        for m in lost:
            print("  ~  %s" % m)
    if test_only:
        print("NOTE: %d public module(s) only tests import:" % len(test_only))
        for m in test_only:
            print("  t  %s" % m)
    if errs:
        print("RED: %d module(s) failed to import." % len(errs))
        return 1
    if lost and args.strict_orphan:
        print("RED (--strict-orphan).")
        return 1
    print("GREEN: every %s module imports." % ns)
    return 0


SMOKE = Command(add_args=_smoke_args, run=_smoke)
