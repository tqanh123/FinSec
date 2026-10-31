"""Structural locks on `finsec_alpharadar.capture` (plan 261022-1147 phase 2, R2.1, R2.13; red-team r1 RT14).

- No heavy package (vnstock, pandas, numpy) is imported at module level: vnstock is imported lazily inside a function.
- Only `capture/host.py` reads the clock or sleeps; every other module gets time through an injected clock and sleeper.
  `threading.Thread.join(timeout)` in the vendor worker and the watchdog's injected sleeper are not clock reads.
- Capture imports only the standard library, `finsec_alpharadar.data_root`, `capture.*` itself and the package's
  `__version__`.
- Importing the CLI module loads none of numpy, pandas or vnstock (checked in a fresh, isolated interpreter).
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
CAPTURE = SRC / "finsec_alpharadar" / "capture"
MODULES = sorted(CAPTURE.glob("*.py"))
HEAVY = {"vnstock", "pandas", "numpy"}
CLOCK_OR_SLEEP = re.compile(
    r"datetime\.now\(|datetime\.today\(|date\.today\(|utcnow\(|time\.time\(|time\.time_ns\(|time\.monotonic\("
    r"|time\.perf_counter\(|time\.sleep\(|from time import"
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_roots(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module]
    return []


def test_capture_package_has_the_expected_modules() -> None:
    names = {path.stem for path in MODULES}
    assert {"__init__", "__main__", "config", "record", "schedule", "sources", "storage", "poller", "heartbeat", "backup",
            "audit", "host"} <= names


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_capture_modules_import_no_heavy_package_at_top_level(path: Path) -> None:
    top_level = [
        name for node in _tree(path).body for name in _imported_roots(node)
    ] + [  # imports nested in module-level if/try blocks run at import time too
        name
        for block in _tree(path).body
        if isinstance(block, (ast.If, ast.Try))
        for node in ast.walk(block)
        for name in _imported_roots(node)
    ]
    heavy = sorted({name for name in top_level if name.split(".")[0] in HEAVY})
    assert not heavy, f"{path.name} imports {heavy} at module level; import it inside the function that needs it"


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_only_host_reads_the_clock_or_sleeps(path: Path) -> None:
    if path.name == "host.py":
        assert CLOCK_OR_SLEEP.search(path.read_text(encoding="utf-8")), "host.py is where the real clock lives"
        return
    hits = [
        f"{path.name}:{number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if CLOCK_OR_SLEEP.search(line)
    ]
    assert not hits, "only capture/host.py may read the clock or sleep; inject a clock/sleeper instead:\n" + "\n".join(hits)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_capture_imports_only_stdlib_data_root_and_itself(path: Path) -> None:
    bad: list[str] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom) and node.level:
            bad.append(f"relative import from {'.' * node.level}{node.module or ''}")
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "finsec_alpharadar":
            names = sorted(alias.name for alias in node.names)
            if names != ["__version__"]:
                bad.append(f"from finsec_alpharadar import {names}")
            continue
        for name in _imported_roots(node):
            if name == "finsec_alpharadar.data_root" or name == "finsec_alpharadar.capture" or name.startswith(
                "finsec_alpharadar.capture."
            ):
                continue
            if name.split(".")[0] in sys.stdlib_module_names:
                continue
            bad.append(name)
    assert not bad, f"{path.name} imports outside stdlib, data_root and capture: {bad}"


def test_importing_the_cli_loads_no_numpy_pandas_or_vnstock() -> None:
    code = (
        "import sys; sys.path.insert(0, {src!r}); import finsec_alpharadar.capture.__main__; "
        "loaded = sorted({{'numpy', 'pandas', 'vnstock'}} & set(sys.modules)); "
        "assert not loaded, loaded; print('clean')"
    ).format(src=str(SRC))
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=120, check=False)
    assert result.returncode == 0 and result.stdout.strip() == "clean", result.stderr
