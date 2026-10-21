"""Guard for the binding rule "No order execution anywhere" (AGENTS.md "Engineering Rules (binding)").

These tests only read repository files: no network, no clock, no subprocess.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
THIS_FILE = Path(__file__).resolve()
MCP_CONFIG = REPO_ROOT / ".agents" / "mcp_config.json"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# Trading clients, order calls, SSI FC Trading, and the Alpaca trading REST host and endpoints (case-insensitive).
# The Alpaca market-data host data.alpaca.markets does not match.
ORDER_EXECUTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"TradingClient",
        r"submit_order",
        r"alpaca\.trading",
        r"place_\w*order",
        r"fc[\s_-]?trading",
        r"(paper-)?api\.alpaca\.markets",
        r"/v2/orders",
        r"/v2/positions",
        # Added in review (2026-10-21): the legacy Alpaca SDK, whose REST client trades without naming a host,
        # and calls that close positions or cancel orders through any client.
        r"alpaca[_-]trade[_-]api",
        r"close_\w*position",
        r"cancel_\w*order",
    )
)

# The 11 toolset names of alpaca-mcp-server 2.4.0. The server silently ignores any other name.
ALPACA_TOOLSETS = frozenset(
    {
        "account",
        "trading",
        "watchlists",
        "assets",
        "stock-data",
        "crypto-data",
        "options-data",
        "corporate-actions",
        "news",
        "fixed-income-data",
        "locates",
    }
)
# Toolsets with write operations: orders and positions (trading), watchlist edits (watchlists),
# account configuration (account) and locate requests (locates).
WRITE_TOOLSETS = frozenset({"trading", "watchlists", "account", "locates"})


def _scanned_files() -> list[Path]:
    """`*.py` under src/, `*.py` and `*.sh` under scripts/, and `*.py` under tests/ except this file."""
    files = sorted((REPO_ROOT / "src").rglob("*.py"))
    files += sorted((REPO_ROOT / "scripts").rglob("*.py")) + sorted((REPO_ROOT / "scripts").rglob("*.sh"))
    files += [path for path in sorted((REPO_ROOT / "tests").rglob("*.py")) if path.resolve() != THIS_FILE]
    return files


def _mcp_servers() -> dict[str, Any]:
    # JSON values stay untyped (Any) until each test checks the fields it needs.
    config = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
    return config["mcpServers"]


def _alpaca_entries() -> dict[str, Any]:
    """Every mcpServers entry whose command or args run alpaca-mcp-server; there must be at least one."""
    entries: dict[str, Any] = {}
    for name, entry in _mcp_servers().items():
        args = entry.get("args") or []
        parts = [entry.get("command") or "", *(args if isinstance(args, list) else [args])]
        if any("alpaca-mcp-server" in str(part) for part in parts):
            entries[name] = entry
    assert entries, (
        "no mcpServers entry in .agents/mcp_config.json runs alpaca-mcp-server; update this guard if the package was renamed"
    )
    return entries


def test_code_has_no_order_execution() -> None:
    hits = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{line_no}: {line.strip()}"
        for path in _scanned_files()
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if any(pattern.search(line) for pattern in ORDER_EXECUTION_PATTERNS)
    ]
    assert not hits, (
        "order-execution code found (AGENTS.md: no trading clients, orders or trading endpoints):\n" + "\n".join(hits)
    )


def test_alpaca_toolsets_are_read_only() -> None:
    problems: list[str] = []
    for name, entry in _alpaca_entries().items():
        env = entry.get("env") or {}
        raw = env.get("ALPACA_TOOLSETS") if isinstance(env, dict) else None
        toolsets = [item.strip() for item in raw.split(",") if item.strip()] if isinstance(raw, str) else []
        if not toolsets:
            problems.append(
                f"{name}: ALPACA_TOOLSETS is missing or empty ({raw!r}), which enables every toolset, including trading"
            )
            continue
        write = [item for item in toolsets if item in WRITE_TOOLSETS]
        if write:
            problems.append(f"{name}: ALPACA_TOOLSETS enables toolsets with write operations: {', '.join(write)}")
        unknown = [item for item in toolsets if item not in ALPACA_TOOLSETS]
        if unknown:
            problems.append(
                f"{name}: ALPACA_TOOLSETS has names that alpaca-mcp-server 2.4.0 silently ignores: {', '.join(unknown)}"
            )
    assert not problems, "\n".join(problems)


def _remote_urls(entry: Any) -> list[str]:
    """URLs an entry reaches over the network: `serverUrl`, `url`, `httpUrl`, and http(s) args (e.g. an mcp-remote proxy)."""
    urls = [str(entry.get(key) or "") for key in ("serverUrl", "url", "httpUrl")]
    args = entry.get("args") or []
    urls += [
        str(arg)
        for arg in (args if isinstance(args, list) else [args])
        if str(arg).lower().startswith(("http://", "https://"))
    ]
    return [url for url in urls if url]


def test_no_remote_alpaca_mcp_server() -> None:
    remote = sorted(
        name for name, entry in _mcp_servers().items() if any("alpaca" in url.lower() for url in _remote_urls(entry))
    )
    assert not remote, (
        f"remote Alpaca MCP servers cannot be pinned and their toolsets cannot be checked: {', '.join(remote)}"
    )


def test_alpaca_paper_trade_flag_is_true() -> None:
    flags = {name: (entry.get("env") or {}).get("ALPACA_PAPER_TRADE") for name, entry in _alpaca_entries().items()}
    wrong = {name: flag for name, flag in flags.items() if flag != "true"}
    assert not wrong, f'ALPACA_PAPER_TRADE must be "true" for every alpaca-mcp-server entry: {wrong}'


def test_ssi_mcp_server_is_disabled() -> None:
    entry = _mcp_servers()["ssi-fastconnect-vn"]
    assert entry.get("disabled") is True, (
        f"ssi-fastconnect-vn must stay disabled (no SSI client before Phase 6): disabled={entry.get('disabled')!r}"
    )


def test_env_example_keeps_paper_trade() -> None:
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    assert "ALPACA_PAPER_TRADE=true" in lines, ".env.example must keep the line ALPACA_PAPER_TRADE=true"
