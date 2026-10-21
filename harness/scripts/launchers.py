#!/usr/bin/env python3
"""launchers.py — the declared list of on-PATH CLIs (harness/data/launchers.yaml)."""
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import yaml_io  # noqa: E402

LIST_REL = "harness/data/launchers.yaml"
_KEYS = {"name", "script", "summary"}
_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


class LauncherListError(ValueError):
    pass


def _read(root: Path) -> dict:
    path = Path(root) / LIST_REL
    if not path.is_file():
        raise LauncherListError("%s is missing" % LIST_REL)
    try:
        data = yaml_io.yaml_load(path.read_text(encoding="utf-8")) or {}
    except yaml_io.YAMLError as e:
        raise LauncherListError("%s is not valid YAML: %s" % (LIST_REL, e))
    if not isinstance(data, dict):
        raise LauncherListError("%s must be a mapping" % LIST_REL)
    return data


def load(root: Path) -> list:
    rows = _read(root).get("launchers") or []
    if not isinstance(rows, list) or not rows:
        raise LauncherListError("%s declares no launcher" % LIST_REL)
    seen = set()
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise LauncherListError("%s: each launcher must be a mapping, got %r" % (LIST_REL, row))
        missing, extra = _KEYS - set(row), set(row) - _KEYS
        if missing or extra:
            raise LauncherListError("%s: launcher %r has missing %s / unknown %s keys"
                                    % (LIST_REL, row.get("name"), sorted(missing), sorted(extra)))
        name = str(row["name"])
        if not _NAME.match(name):
            raise LauncherListError("%s: %r is not a valid launcher name" % (LIST_REL, name))
        if name in seen:
            raise LauncherListError("%s: %s is declared twice" % (LIST_REL, name))
        seen.add(name)
        out.append({k: str(row[k]) for k in _KEYS})
    return out


def exempt(root: Path) -> dict:
    return {str(k): str(v) for k, v in (_read(root).get("not_launchers") or {}).items()}


TEMPLATES_REL = "harness/data/launcher-templates"
_KINDS = {"sh": ("launcher.sh.tmpl", ""), "cmd": ("launcher.cmd.tmpl", ".cmd")}


def launcher_path(root: Path, name: str, kind: str) -> Path:
    return Path(root) / "harness" / "bin" / (name + _KINDS[kind][1])


def render(root: Path, entry: dict, kind: str) -> bytes:
    text = (Path(root) / TEMPLATES_REL / _KINDS[kind][0]).read_text(encoding="utf-8")
    for key in ("name", "script", "summary"):
        text = text.replace("@%s@" % key.upper(), entry[key])
    return text.encode("utf-8")


def drift(root: Path) -> list:
    out = []
    for entry in load(root):
        for kind in _KINDS:
            path = launcher_path(root, entry["name"], kind)
            if not path.is_file():
                out.append((path, "missing"))
            elif path.read_bytes() != render(root, entry, kind):
                out.append((path, "differs from its template"))
    return out


def write(root: Path) -> list:
    written = []
    for entry in load(root):
        for kind in _KINDS:
            path = launcher_path(root, entry["name"], kind)
            body = render(root, entry, kind)
            if not path.is_file() or path.read_bytes() != body:
                path.write_bytes(body)
                written.append(path)
            if kind == "sh":
                path.chmod(0o755)
    return written


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Generate harness/bin launchers from their templates.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 if a launcher drifts")
    mode.add_argument("--write", action="store_true", help="regenerate drifted launchers")
    ap.add_argument("--root", default=str(_HERE.parents[1]), help="repo root")
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        if args.write:
            for path in write(root):
                print("wrote %s" % path)
            return 0
        problems = drift(root)
    except (LauncherListError, OSError) as e:
        print("launchers: %s" % e, file=sys.stderr)
        return 2
    for path, why in problems:
        print("%s: %s — run `python3 harness/scripts/launchers.py --write`" % (path, why))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
