"""`hs-code init`'s `lang-tools` row: install the pinned npm packages the engines a
repo declares run (`requires.npm` in the language book), once per user.

    status(root)     ready when no declared engine needs a tool outside Python, or
                     every one it needs is here (`lang.toolchain`)
    run(ctx, opts)   dry by default (prints the `npm ci` it would run); --apply installs

One package installs into a NEW staging directory `<lang-tools>/.stage-<id>` marked
`.harness-lang-tools-stage`: the shipped pin pair is copied in as `package.json` +
`package-lock.json`, then `npm ci --ignore-scripts` (npm checks the lockfile's
integrity itself: a tampered one is EINTEGRITY and installs nothing). The result is
read back -- the pinned version, npm's own `.package-lock.json` carrying the pinned
integrity -- marked `.harness-lang-tools`, and only then renamed onto
`<name>-<version>`. A directory is only ever deleted when it carries the harness's
own mark: a failed stage, or the previous install moved aside first. A directory at
the target the harness did not install is never replaced.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

import process_group

from ... import registry, toolchain
from ...base import Engine, NpmPackage
from ...errors import AdapterRefused

_SCRIPTS = Path(__file__).resolve().parents[5]
STAGE_MARK = ".harness-lang-tools-stage"
DONE_MARK = ".harness-lang-tools"
_NPM_CI = ("ci", "--ignore-scripts", "--no-audit", "--no-fund")
_NO_NODE = "install Node.js %s, or declare `engine: tree-sitter`"


def pin_files(pkg: NpmPackage) -> "tuple[Path, Path]":
    """The shipped `<pin>.pin.json` + `<pin>.pin.lock.json` (never named package.json:
    a reader of the harness's own tree would take it for a manifest)."""
    base = _SCRIPTS / pkg.pin
    return base.with_name(base.name + ".pin.json"), base.with_name(base.name + ".pin.lock.json")


def _engines(root: Path) -> "list[Engine]":
    """The engines `root`'s codemap.yaml chooses that run a tool outside Python."""
    if not (root / "codemap.yaml").is_file():
        return []
    from ....descriptor import DescriptorError, load
    try:
        descriptor = load(root / "codemap.yaml", root).descriptor
    except DescriptorError:
        return []   # the graph row reports a broken descriptor
    out = []
    for pack in registry.enabled(descriptor):
        name = registry.options_of(descriptor, pack.id).get("engine")
        eng = pack.engine(str(name)) if name is not None else None
        if eng is not None and (eng.requires_node or eng.requires_npm):
            out.append(eng)
    return out


def _problems(engines: "list[Engine]", root: Path) -> "tuple[list[str], list[str]]":
    node = sorted({p for e in engines if e.requires_node for p in toolchain.node_problems(e.requires_node, root)})
    npm = sorted({p for e in engines for pkg in e.requires_npm for p in toolchain.npm_problems(pkg)})
    return node, npm


def status(root: Path) -> "dict[str, Any]":
    engines = _engines(Path(root))
    if not engines:
        return {"ready": True, "detail": "no declared engine runs a tool outside Python"}
    node, npm = _problems(engines, Path(root))
    if node:
        floor = max(e.requires_node for e in engines if e.requires_node)
        return {"ready": False, "detail": "%s: %s" % (_NO_NODE % floor, "; ".join(node + npm))}
    if npm:
        return {"ready": False, "detail": "; ".join(npm)}
    return {"ready": True, "detail": "%s under %s" % (
        ", ".join("%s@%s" % (p.name, p.version) for e in engines for p in e.requires_npm),
        toolchain.tools_root())}


def _ours(d: Path) -> bool:
    return (d / DONE_MARK).is_file() or (d / STAGE_MARK).is_file()


def _remove_ours(d: Path) -> None:
    if _ours(d):
        shutil.rmtree(d)


def install_package(pkg: NpmPackage, root: Path, pin: "tuple[Path, Path]", npm: Path,
                    repo: "Path | None" = None) -> "dict[str, Any]":
    """Install `pkg` under `root` from the `pin` pair with `npm` -> {exit_code, detail}."""
    want = "%s@%s" % (pkg.name, pkg.version)
    target = toolchain.package_dir(pkg, root)
    if target.exists() and not _ours(target):
        return {"exit_code": 2, "detail": "%s: %s exists and was not installed by the harness (no %s) "
                "-- move it away to install here" % (want, target, DONE_MARK)}
    root.mkdir(parents=True, exist_ok=True)
    stage = root / (".stage-" + uuid.uuid4().hex)
    moved = False
    stage.mkdir()
    (stage / STAGE_MARK).write_text(want + "\n", encoding="utf-8")
    try:
        shutil.copyfile(pin[0], stage / "package.json")
        shutil.copyfile(pin[1], stage / "package-lock.json")
        try:
            # npm starts children of its own: a timeout must take the whole group
            res = process_group.run([str(npm), *_NPM_CI[:1], "--prefix", str(stage), *_NPM_CI[1:]],
                                    cwd=str(stage), env=toolchain.child_env(repo), capture_output=True,
                                    text=True, timeout=900)
        except (OSError, subprocess.SubprocessError) as e:
            return {"exit_code": 2, "detail": "%s: npm ci did not run: %s" % (want, e)}
        if res.returncode != 0:
            return {"exit_code": 2, "detail": "%s: npm ci exited %d: %s"
                    % (want, res.returncode, (res.stderr + res.stdout).strip()[-4000:])}
        problems = toolchain.installed_problems(pkg, stage)
        if problems:
            return {"exit_code": 2, "detail": "; ".join(problems)}
        (stage / DONE_MARK).write_text(json.dumps({"name": pkg.name, "version": pkg.version,
                                                   "integrity": pkg.integrity}) + "\n", encoding="utf-8")
        old = None
        if target.exists():
            if not _ours(target):
                return {"exit_code": 2, "detail": "%s: %s appeared during the install and was not "
                        "installed by the harness" % (want, target)}
            old = root / (".old-" + uuid.uuid4().hex)
            os.replace(target, old)
        os.replace(stage, target)
        moved = True
        if old is not None:
            _remove_ours(old)
        return {"exit_code": 0, "detail": "%s installed under %s" % (want, target)}
    finally:
        if not moved:
            _remove_ours(stage)


def run(ctx: Any, opts: Any) -> "dict[str, Any]":
    root = Path(ctx.root)
    engines = _engines(root)
    pkgs = list({p.name: p for e in engines for p in e.requires_npm}.values())
    if not pkgs:
        return {"exit_code": 0, "detail": "nothing to install"}
    try:
        tools, npm = toolchain.tools_root(), toolchain.find_tool("npm", root)
    except AdapterRefused as e:
        return {"exit_code": 2, "detail": str(e)}
    if npm is None:
        floor = max((e.requires_node for e in engines if e.requires_node), default=">=14.17")
        return {"exit_code": 2, "detail": "no `npm` on PATH -- " + _NO_NODE % floor}
    todo = [p for p in pkgs if toolchain.installed_problems(p, toolchain.package_dir(p, tools))]
    if not getattr(opts, "apply", False):
        return {"exit_code": 0, "npm": str(npm),
                "would": ["npm ci --prefix %s %s" % (tools / ".stage-<id>", " ".join(_NPM_CI[1:]))
                          + "  # %s@%s -> %s" % (p.name, p.version, toolchain.package_dir(p, tools))
                          for p in todo]}
    done = [install_package(p, tools, pin_files(p), npm, repo=root) for p in todo]
    return {"exit_code": max((d["exit_code"] for d in done), default=0),
            "detail": "; ".join(d["detail"] for d in done) or "already installed"}
