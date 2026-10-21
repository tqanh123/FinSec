#!/usr/bin/env python3
"""agent_behavior_linter_report.py — renders the linter's end-of-session
summary. Advisory text only: counters, then one block per finding (observed /
instead / principle). Never carries file content, command text, or raw output
— the state it reads is already redacted (agent_behavior_linter_events.py)."""

_HEADING = "Agent behavior linter"


def _format_kb(n) -> str:
    if not isinstance(n, (int, float)) or n <= 0:
        return "0 KB"
    return "%d KB" % round(n / 1024)


def format_counters(counters) -> str:
    counters = counters or {}
    parts = [
        "%d lần đọc" % (counters.get("reads") or 0),
        "%d lần tìm" % (counters.get("searches") or 0),
        "%d lệnh" % (counters.get("commands") or 0),
        "%d lần sửa" % (counters.get("mutations") or 0),
        "%s output công cụ" % _format_kb(counters.get("output_bytes") or 0),
    ]
    if counters.get("compactions"):
        parts.append("%d lần nén ngữ cảnh" % counters["compactions"])
    return ", ".join(parts)


def format_evidence(finding: dict) -> str:
    ev = finding.get("evidence") or {}
    if isinstance(ev.get("files"), list):
        return "; ".join(
            "%s đọc %dx không đổi (%s byte, vùng %s)"
            % (f["path"], f["reads"], f.get("unchanged_size"), f.get("region"))
            for f in ev["files"])
    if isinstance(ev.get("commands"), list):
        return "; ".join(
            "%s thất bại %dx với cùng một lỗi" % (c.get("command_class"), c["attempts"])
            for c in ev["commands"])
    if isinstance(ev.get("broad_searches"), (int, float)):
        return ("%d lần tìm toàn cây, %d trong đó cùng gốc \"%s\""
                % (ev["broad_searches"], ev.get("repeated_root_searches", 0),
                   ev.get("repeated_root")))
    if isinstance(ev.get("output_kilobytes"), (int, float)):
        return ("%s KB output qua %s lần đọc, %s lần tìm, %s lệnh — không đổi tệp nào"
                % (ev["output_kilobytes"], ev.get("reads"), ev.get("searches"), ev.get("commands")))
    if isinstance(ev.get("mutated_paths"), list):
        return ("%s chạy cả bộ trong khi chỉ %s đổi"
                % (ev.get("verification_class"), ", ".join(ev["mutated_paths"])))
    return ""


def render_summary(state: dict, findings) -> str:
    """The session summary, or "" when there is nothing worth saying."""
    if not isinstance(state, dict):
        return ""
    findings = list(findings or [])
    if not findings:
        return ""

    lines = [
        "%s: %d quan sát trong phiên này." % (_HEADING, len(findings)),
        "Tổng phiên: %s." % format_counters(state.get("counters")),
        "",
    ]
    for finding in findings:
        lines.append("- %s (%s)" % (finding["id"], finding["severity"]))
        evidence = format_evidence(finding)
        if evidence:
            lines.append("  Quan sát: %s" % evidence)
        lines.append("  Thay vào đó: %s" % finding["alternative"])
        lines.append("  Nguyên tắc: %s" % finding["principle"])
        lines.append("")

    lines.append(
        "Chỉ quan sát: không có gì bị chặn. Tắt bằng khoá "
        "`agent_behavior_linter` trong harness/data/harness-hooks.yaml.")
    return "\n".join(lines)
