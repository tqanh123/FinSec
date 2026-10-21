"""What `tsc -b <roots>` will write, read off the compiler before it runs.

Every project the roots reach through `references` (transitively) is asked twice:
`--showConfig` for its options, `--listFilesOnly` for its inputs (including a file
imported from outside its `include`). Layout, measured on tsc 6.0.3 and 7.0.2:

    rootDir       declared, else the config's own directory
    js            outDir/<path from rootDir> when the input is under rootDir and an outDir
                  is set, else beside the input; .ts->.js, .tsx->.jsx (`jsx: preserve`)
                  or .js, .mts->.mjs, .cts->.cjs; none under emitDeclarationOnly/noEmit
    declarations  (declaration/composite) declarationDir ?? outDir ?? beside, same rule
    maps          sourceMap: <js>.map; declarationMap: <d.ts>.map
    tsbuildinfo   composite/incremental: tsBuildInfoFile from the config's directory, else
                  <outDir ?? config dir>/<config name>.tsbuildinfo; neither: `-b` still
                  writes <config dir>/<config name>.tsbuildinfo

A config the compiler cannot read, or a reference that does not resolve, is an
`EmitGuessError`: the run is refused before it starts. The guess only refuses early;
`dirty_tree_guard` still wraps the run and puts back what a wrong guess missed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

import process_group

_DECL_IN = (".d.ts", ".d.mts", ".d.cts")
_JS = {".ts": ".js", ".mts": ".mjs", ".cts": ".cjs", ".js": ".js", ".mjs": ".mjs", ".cjs": ".cjs"}
_DTS = {".ts": ".d.ts", ".tsx": ".d.ts", ".mts": ".d.mts", ".cts": ".d.cts", ".js": ".d.ts", ".jsx": ".d.ts",
        ".mjs": ".d.mts", ".cjs": ".d.cts"}
TIMEOUT_S = 300.0
_DIAGNOSTIC = re.compile(r"(?:^|\): )error TS\d+: ")


class EmitGuessError(Exception):
    """A project whose outputs cannot be read off the compiler. `output` is what the
    compiler printed when it was the one that failed ("" otherwise): a config error it
    names (TS5101) is the check's own verdict, not a reason to refuse."""

    def __init__(self, msg: str, output: str = "") -> None:
        super().__init__(msg)
        self.output = output


@dataclass
class Guess:
    targets: Set[str] = field(default_factory=set)
    projects: List[str] = field(default_factory=list)
    outside: Set[str] = field(default_factory=set)
    #: repo-relative outDir/declarationDir of every project: the `.gitignore` line to offer
    out_dirs: Set[str] = field(default_factory=set)


def _config(path: Path) -> Path:
    return path / "tsconfig.json" if path.is_dir() else path


def _diagnostics(text: str) -> str:
    """The compiler's diagnostics (each with its indented continuation lines), without
    the file list `--listFilesOnly` goes on printing after them."""
    keep: List[str] = []
    on = False
    for line in text.splitlines():
        if line[:1].isspace():
            if on:
                keep.append(line)
            continue
        on = bool(_DIAGNOSTIC.search(line))
        if on:
            keep.append(line)
    return "".join(x + "\n" for x in keep)


def _tsc(exe: str, cfg: Path, flag: str, cwd: Path, env: Optional[Dict[str, str]]) -> str:
    try:
        r = process_group.run([exe, "-p", str(cfg), flag], cwd=str(cwd), env=env, capture_output=True,
                              text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise EmitGuessError("tsc -p %s %s ran past %gs" % (cfg, flag, TIMEOUT_S)) from None
    if r.returncode != 0:
        raise EmitGuessError("tsc -p %s %s failed (rc %d): %s"
                             % (cfg, flag, r.returncode, (r.stdout + r.stderr).strip()[:300]), _diagnostics(r.stdout))
    return r.stdout


def _under(p: str, d: str) -> bool:
    return p == d or p.startswith(d.rstrip(os.sep) + os.sep)


def _place(src: str, root_dir: str, out: Optional[str]) -> str:
    """Where `src` lands: under `out` by its path from `root_dir`, else beside itself."""
    if out and _under(src, root_dir):
        return os.path.join(out, os.path.relpath(src, root_dir))
    return src


def _swap(path: str, ext: str, new: str) -> str:
    return path[: -len(ext)] + new


def _project(cfg: Path, opts: dict, inputs: Iterable[str]) -> Set[str]:
    d = str(cfg.parent)
    o = {k: (os.path.normpath(os.path.join(d, v)) if k in ("outDir", "rootDir", "declarationDir", "tsBuildInfoFile")
             else v) for k, v in opts.items()}
    root_dir = o.get("rootDir", d)
    out_dir = o.get("outDir")
    decl = bool(o.get("declaration") or o.get("composite"))
    js = not (o.get("emitDeclarationOnly") or o.get("noEmit"))
    out: Set[str] = set()
    for src in inputs:
        if src.endswith(_DECL_IN) or src.endswith(".json") or "%snode_modules%s" % (os.sep, os.sep) in src:
            continue
        ext = os.path.splitext(src)[1]
        if ext not in _DTS:
            continue
        if js and (ext in (".ts", ".tsx", ".mts", ".cts") or out_dir):
            new = (".jsx" if o.get("jsx") == "preserve" else ".js") if ext in (".tsx", ".jsx") else _JS[ext]
            target = _swap(_place(src, root_dir, out_dir), ext, new)
            out.add(target)
            if o.get("sourceMap"):
                out.add(target + ".map")
        if decl and not o.get("noEmit"):
            target = _swap(_place(src, root_dir, o.get("declarationDir") or out_dir), ext, _DTS[ext])
            out.add(target)
            if o.get("declarationMap"):
                out.add(target + ".map")
    name = os.path.splitext(cfg.name)[0] + ".tsbuildinfo"
    if o.get("composite") or o.get("incremental"):
        out.add(o.get("tsBuildInfoFile") or os.path.join(out_dir or d, name))
    else:
        out.add(os.path.join(d, name))
    return out


def tsc_targets(root: Path, exe: str, roots: Iterable[str], env: Optional[Dict[str, str]] = None) -> Guess:
    """Every file `tsc -b <roots>` run from `root` writes, repo-relative; files it would
    write outside `root` come back in `outside`."""
    root = Path(root).resolve()
    queue = [_config((root / r).resolve()) for r in roots]
    seen: Set[Path] = set()
    guess = Guess()
    while queue:
        cfg = queue.pop(0)
        if cfg in seen:
            continue
        seen.add(cfg)
        if not cfg.is_file():
            raise EmitGuessError("tsc -b reaches %s, which does not exist (a reference that does not "
                                 "resolve)" % os.path.relpath(cfg, root))
        try:
            shown = json.loads(_tsc(exe, cfg, "--showConfig", root, env))
        except ValueError as e:
            raise EmitGuessError("tsc -p %s --showConfig printed no JSON: %s" % (cfg, e)) from None
        guess.projects.append(os.path.relpath(cfg, root))
        for ref in shown.get("references") or ():
            queue.append(_config((cfg.parent / str(ref.get("path", ""))).resolve()))
        if not shown.get("files") and not shown.get("include"):
            continue  # a solution config (`files: []`, which --showConfig omits): builds its references only
        inputs = [os.path.normpath(x.strip()) for x in _tsc(exe, cfg, "--listFilesOnly", root, env).splitlines()
                  if x.strip()]
        opts = shown.get("compilerOptions") or {}
        for k in ("outDir", "declarationDir"):
            if opts.get(k):
                rel = os.path.relpath(os.path.normpath(os.path.join(cfg.parent, opts[k])), root)
                if not rel.startswith(".."):
                    guess.out_dirs.add(rel.replace(os.sep, "/"))
        for t in _project(cfg, opts, inputs):
            rel = os.path.relpath(t, root)
            (guess.outside if rel.startswith("..") else guess.targets).add(rel.replace(os.sep, "/"))
    return guess
