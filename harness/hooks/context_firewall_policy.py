#!/usr/bin/env python3
"""context_firewall_policy.py — turn blast-radius signals into a firewall
verdict: allow -> warn -> narrow -> block.

Deliberate departure from AK's `context-firewall/policy.cjs`: AK ships its
`SAFE_REWRITES` table EMPTY on principle — narrowing a read/search necessarily
returns fewer results than the caller asked for, so AK treats that as a
correctness bug rather than a context saving, and every risky call there takes
the warn/block path. This port's task instead asks for `narrow` to be reachable
for real: `NARROW_REWRITES` below has two live entries. "Keeps the semantics"
is read here as *keeps the SAME operation on the SAME target* (a still-a-Read-
of-this-file, still-a-Grep-of-this-pattern call), not as *returns byte-
identical results* — the caller gets a bounded window of the same call instead
of the whole thing, with the reason it was bounded named in the same message.
A signal outside this table (glob breadth, shell shape) has no honest rewrite
— cutting a `find`'s output loses files with no way to say which — so it still
takes AK's warn/block path.
"""


def _human_bytes(n) -> str:
    if not isinstance(n, (int, float)):
        return "unknown size"
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / (1024 * 1024))
    return "%d KB" % round(n / 1024)


def dominant_signal(signals):
    """The signal that drives the verdict: the most severe, and among equals the
    first computed, so ordering is stable."""
    worst = None
    for s in signals:
        if worst is None:
            worst = s
        elif worst["severity"] != "block" and s["severity"] == "block":
            worst = s
    return worst


def strategy_for(signal: dict, facts: dict):
    """(reason, strategy) — the concrete bounded alternative for one signal. Every
    warn/block names one; a block with no better path is just an obstacle."""
    sid = signal["id"]
    if sid == "read.unbounded":
        return (
            "Đọc toàn bộ %s (%s) sẽ tràn ngữ cảnh."
            % (facts.get("file_path"), _human_bytes(signal.get("bytes"))),
            "Tìm đoạn cần trong tệp trước (Grep output_mode=content -n), rồi Read "
            "đúng vùng đó bằng offset/limit.",
        )
    if sid == "glob.breadth":
        return (
            "Mẫu \"%s\" khớp %s tệp — không có biên." % (facts.get("pattern"), signal.get("count")),
            "Thu hẹp path hoặc mẫu (thêm thư mục con / phần mở rộng cụ thể).",
        )
    if sid == "search.unbounded_content":
        return (
            "Tìm kiếm nội dung không có head_limit — có thể trả về không giới hạn.",
            "Giới hạn bằng head_limit, hoặc dùng output_mode \"files_with_matches\" "
            "trước để biết cần đọc tệp nào.",
        )
    if sid == "shell.file_dump":
        return (
            "`%s %s` in toàn bộ tệp (%s)." % (signal.get("verb"), signal.get("target"),
                                              _human_bytes(signal.get("bytes"))),
            "Trích đúng phần cần: `grep -n <mẫu> %s`, `tail -n 200 %s`, hoặc "
            "`sed -n '<đầu>,<cuối>p' %s`." % ((signal.get("target"),) * 3),
        )
    if sid == "shell.recursive_walk":
        return (
            "`%s` duyệt toàn bộ cây từ điểm bắt đầu, không giới hạn độ sâu." % signal.get("verb"),
            "Thêm -maxdepth, thu hẹp thư mục bắt đầu, hoặc lọc qua head.",
        )
    if sid == "shell.recursive_search":
        return (
            "`%s` tìm đệ quy không có cờ thu hẹp." % signal.get("verb"),
            "Thu hẹp bằng --include/--exclude-dir, giới hạn số kết quả bằng -m, "
            "hoặc dùng -l để liệt kê tệp trước.",
        )
    if sid == "shell.verbose_history":
        return (
            "`%s` in toàn bộ lịch sử/diff, không có biên." % signal.get("verb"),
            "Giới hạn bằng -n, --oneline, --stat, hoặc một đường dẫn cụ thể.",
        )
    return ("Lệnh này có blast radius lớn.", "Thu hẹp phạm vi trước khi thử lại.")


def _narrow_read(facts: dict, thresholds: dict) -> dict:
    return {"limit": thresholds["narrow_read_limit_lines"]}


def _narrow_search(facts: dict, thresholds: dict) -> dict:
    return {"head_limit": thresholds["narrow_search_head_limit"]}


# Enumerated semantics-preserving-by-the-module's-own-definition rewrites,
# keyed by signal id. Each entry returns the `updatedInput` fields to MERGE
# onto the original tool_input (never a replacement — the caller still owns
# file_path/pattern/etc., only the bounding fields are added).
NARROW_REWRITES = {
    "read.unbounded": _narrow_read,
    "search.unbounded_content": _narrow_search,
}


def decide(signals, facts: dict, thresholds: dict, repeat_count: int = 0) -> dict:
    """The firewall verdict for one call's signals.

    {"action": "allow"} | {"action": "warn"|"block", "reason", "strategy",
    "signal_id"} | {"action": "narrow", "reason", "strategy", "signal_id",
    "updated_input"}.
    """
    if not signals:
        return {"action": "allow"}
    signal = dominant_signal(signals)
    if signal is None:
        return {"action": "allow"}

    reason, strategy = strategy_for(signal, facts or {})
    rewrite = NARROW_REWRITES.get(signal["id"])
    if rewrite is not None:
        updated = rewrite(facts or {}, thresholds)
        if updated:
            return {"action": "narrow", "reason": reason, "strategy": strategy,
                     "signal_id": signal["id"], "updated_input": updated}

    escalated = signal["severity"] == "block" or (repeat_count + 1) >= thresholds["repeat_block_count"]
    if escalated:
        if signal["severity"] != "block":
            reason = "%s Đây là lần thử thứ %d cho cùng một thao tác không giới hạn." % (
                reason, repeat_count + 1)
        return {"action": "block", "reason": reason, "strategy": strategy,
                 "signal_id": signal["id"]}
    return {"action": "warn", "reason": reason, "strategy": strategy, "signal_id": signal["id"]}
