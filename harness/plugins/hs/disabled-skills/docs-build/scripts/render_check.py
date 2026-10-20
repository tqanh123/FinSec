#!/usr/bin/env python3
"""render_check.py — pre-flight + launcher for render_check.mjs (multi-viewport
layout check). Measures whether THIS host can even run a headless browser
before choosing how to report: a missing node/chrome/puppeteer-core is a loud
SKIP (exit 3), never a silent PASS a caller could misread as "layout is fine".

    python3 render_check.py [--public public] [--viewports 375x667,768x1024,1440x900] [--shots <dir>]

Exit: 0 clean · 1 layout defect found · 2 chrome failed to launch (mjs's own
signal) · 3 this host cannot run the check at all (missing node/chrome/puppeteer-core).
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[6]
HERE = Path(__file__).resolve().parent
_NODE_MODULES = REPO / "harness" / "state" / "e2e-node" / "node_modules"
_CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def _find_chrome() -> "str | None":
    import os
    env = os.environ.get("CHROME_BIN")
    if env and Path(env).is_file():
        return env
    for name in _CHROME_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def preflight() -> "str | None":
    """None when runnable; else the human reason this host cannot run the check."""
    if shutil.which("node") is None:
        return "không tìm thấy `node` trên máy này"
    if not _find_chrome():
        return "không tìm thấy trình duyệt headless (google-chrome/chromium) trên máy này"
    if not (_NODE_MODULES / "puppeteer-core").is_dir():
        return ("puppeteer-core chưa cài — chạy 1 lần: "
                "npm install --no-save --prefix harness/state/e2e-node puppeteer-core")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public", default=str(REPO / "public"))
    ap.add_argument("--viewports", default="375x667,768x1024,1440x900")
    ap.add_argument("--shots", default=None)
    a = ap.parse_args()

    reason = preflight()
    if reason:
        print("render-check: KHÔNG CHẠY ĐƯỢC — %s" % reason, file=sys.stderr)
        return 3

    chrome = _find_chrome()
    if chrome is None:  # preflight saw one; it vanished since
        print("render-check: KHÔNG CHẠY ĐƯỢC — không còn thấy trình duyệt headless", file=sys.stderr)
        return 3
    import os
    env = dict(os.environ, NODE_PATH=str(_NODE_MODULES), CHROME_BIN=chrome)
    cmd = ["node", str(HERE / "render_check.mjs"), "--public", a.public, "--viewports", a.viewports]
    if a.shots:
        cmd += ["--shots", a.shots]
    proc = subprocess.run(cmd, env=env)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
