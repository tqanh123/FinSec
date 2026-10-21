#!/usr/bin/env python3
"""agent_behavior_linter_detectors.py — anti-pattern detectors over the
linter's folded session state.

Every detector is a pure function of `state` and returns either None or a
finding carrying the evidence it saw, the alternative available, and a
pointer to the rule that makes it a problem. Findings link to
`harness/rules/agent-operational-discipline.md` (this repo's own home rule
for tool-cost discipline) and `harness/plugins/hs/skills/test/SKILL.md` (the
"Focused first pass" scoping guidance) rather than to AK's
`ak:context-engineering`, which this harness does not carry.

Thresholds are deliberately generous (mirrors AK): a linter that cries wolf
gets disabled, and a missed finding costs far less than a false one.
"""

_OP_DISCIPLINE_RULE = "harness/rules/agent-operational-discipline.md"
_TEST_SKILL = "harness/plugins/hs/skills/test/SKILL.md"

PRINCIPLES = {
    "reuse_verified_facts": (
        "%s — MINIMIZE TOOL COST #3: don't re-run an expensive read/command "
        "for a fact already verified and unchanged" % _OP_DISCIPLINE_RULE),
    "bounded_retry": (
        "%s — PROBE BEFORE YOU BUILD ON A GUESS: read the error literally and "
        "change something before rerunning; nothing changed means a repeat "
        "cannot answer differently" % _OP_DISCIPLINE_RULE),
    "search_before_read": (
        "%s — MINIMIZE TOOL COST #1-2: read only the slice you need; grep for "
        "the line, don't sweep the whole tree" % _OP_DISCIPLINE_RULE),
    "bound_output": (
        "%s — MINIMIZE TOOL COST #5: bound tool output (limit/offset/"
        "head_limit/--stat) instead of dumping everything" % _OP_DISCIPLINE_RULE),
    "focused_checks_first": (
        "%s, \"Focused first pass\": run only the tests a change can break, "
        "then the full suite before the gate" % _TEST_SKILL),
}

SEVERITY = {"low": "low", "medium": "medium"}

DEFAULT_THRESHOLDS = {
    "unchanged_reads": 3,
    "retry_attempts": 2,
    "broad_searches": 4,
    "shared_root_searches": 2,
    "output_bytes": 200 * 1024,
}


def detect_repeated_unchanged_read(state, thresholds=DEFAULT_THRESHOLDS):
    """D1 — the same unchanged file read over and over. The counter resets
    whenever the file's mtime/size change or a mutation lands on it, so a
    surviving count means the content genuinely did not move between reads."""
    offenders = [
        (path, entry) for path, entry in (state.get("files") or {}).items()
        if entry.get("reads", 0) >= thresholds["unchanged_reads"]
        and entry.get("mutated_at") is None
        # More than one range means different regions were read, not a repeat
        # of the same question.
        and len(entry.get("ranges") or []) == 1
    ]
    if not offenders:
        return None
    offenders.sort(key=lambda kv: kv[1]["reads"], reverse=True)
    offenders = offenders[:5]
    return {
        "id": "repeated-unchanged-read", "severity": SEVERITY["medium"],
        "evidence": {"files": [
            {"path": p, "reads": e["reads"], "unchanged_size": e.get("last_size"),
             "region": (e.get("ranges") or [None])[0]}
            for p, e in offenders]},
        "alternative": ("Tệp không đổi từ lần đọc đầu. Dùng lại kết quả đó, hoặc "
                         "đọc vùng khác nếu thật sự cần vùng khác."),
        "principle": PRINCIPLES["reuse_verified_facts"],
    }


def detect_deterministic_retry(state, thresholds=DEFAULT_THRESHOLDS):
    """D2 — the same command failing the same way, twice+, with nothing changed.
    Nondeterministic classes and any command with a mutation since its last
    attempt are excluded — the rerun really could differ in both cases."""
    offenders = [
        (h, e) for h, e in (state.get("commands") or {}).items()
        if e.get("attempts", 0) >= thresholds["retry_attempts"]
        and e.get("nondeterministic") is not True
        and e.get("mutated_since") is not True
    ]
    if not offenders:
        return None
    offenders.sort(key=lambda kv: kv[1]["attempts"], reverse=True)
    offenders = offenders[:5]
    return {
        "id": "deterministic-retry", "severity": SEVERITY["medium"],
        "evidence": {"commands": [
            {"command_hash": h, "command_class": e.get("class"), "attempts": e["attempts"],
             "same_error_signature": e.get("error_sig_hash")}
            for h, e in offenders]},
        "alternative": ("Không có gì đổi giữa các lần chạy và chữ ký lỗi giống hệt "
                         "nhau — chạy lại không thể ra kết quả khác. Đọc lỗi, đổi "
                         "một đầu vào, rồi chạy lại."),
        "principle": PRINCIPLES["bounded_retry"],
    }


def detect_broad_search_without_narrowing(state, thresholds=DEFAULT_THRESHOLDS):
    """D3 — repeated tree-wide searching instead of narrowing. "Without
    narrowing" is meant literally: one search anywhere in the session that
    named a real subdirectory is proof the agent DID narrow, and ends the
    finding — a repository-root search to START is ordinary."""
    searches = state.get("searches") or []
    broad = [s for s in searches if s.get("broad") is True]
    if len(broad) < thresholds["broad_searches"]:
        return None
    if any(s.get("broad") is False for s in searches):
        return None
    root_counts = {}
    for s in broad:
        root = s.get("root") or "."
        root_counts[root] = root_counts.get(root, 0) + 1
    repeated = next(
        ((r, c) for r, c in root_counts.items() if c >= thresholds["shared_root_searches"]), None)
    if not repeated:
        return None
    return {
        "id": "broad-search-without-narrowing", "severity": SEVERITY["low"],
        "evidence": {"broad_searches": len(broad), "repeated_root": repeated[0],
                     "repeated_root_searches": repeated[1]},
        "alternative": ("Thu hẹp tìm kiếm: nêu một thư mục con dưới \"%s\" hoặc "
                         "thêm bộ lọc loại tệp thay vì quét lại cùng gốc." % repeated[0]),
        "principle": PRINCIPLES["search_before_read"],
    }


def detect_low_yield_tool_output(state, thresholds=DEFAULT_THRESHOLDS):
    """D4 — a large amount of tool output that produced no change. A mutation
    anywhere counts as yield (including a report/plan write), so a genuine
    read-only investigation that writes up its result does not fire."""
    counters = state.get("counters") or {}
    output_bytes = counters.get("output_bytes") or 0
    mutations = counters.get("mutations") or 0
    if output_bytes <= thresholds["output_bytes"] or mutations > 0:
        return None
    return {
        "id": "low-yield-tool-output", "severity": SEVERITY["low"],
        "evidence": {
            "output_kilobytes": round(output_bytes / 1024),
            "budget_kilobytes": round(thresholds["output_bytes"] / 1024),
            "reads": counters.get("reads", 0), "searches": counters.get("searches", 0),
            "commands": counters.get("commands", 0), "files_mutated": 0,
        },
        "alternative": ("Bấy nhiêu output mà không đổi gì. Dùng giới hạn có sẵn "
                         "của công cụ hoặc bộ lọc có cấu trúc, chỉ đọc đúng vùng cần."),
        "principle": PRINCIPLES["bound_output"],
    }


def detect_oversized_verification_for_docs_only_change(state, thresholds=DEFAULT_THRESHOLDS):
    """D5 — a repository-wide suite run for a documentation-only change. Not
    generalized to "small code change": for code, this repo's own rules
    require broadening when a shared contract moves, so there is no honest
    signal there — only a docs-only diff is unambiguous."""
    full_suite = state.get("full_suite_command")
    if not full_suite or state.get("opaque_mutation"):
        # A change the shell made without naming its files may be code.
        return None
    mutated = [(p, e) for p, e in (state.get("files") or {}).items() if e.get("mutated_at")]
    if not mutated:
        return None
    if not all(e.get("documentation") is True for _p, e in mutated):
        return None
    return {
        "id": "oversized-verification-for-docs-only-change", "severity": SEVERITY["low"],
        "evidence": {"mutated_paths": [p for p, _e in mutated][:10],
                     "verification_class": full_suite.get("class")},
        "alternative": ("Chỉ có tài liệu thay đổi. Chạy kiểm tra tài liệu/liên kết "
                         "trước, chỉ mở rộng khi cổng của kho yêu cầu."),
        "principle": PRINCIPLES["focused_checks_first"],
    }


DETECTORS = (
    detect_repeated_unchanged_read,
    detect_deterministic_retry,
    detect_broad_search_without_narrowing,
    detect_low_yield_tool_output,
    detect_oversized_verification_for_docs_only_change,
)


def run_detectors(state, thresholds=DEFAULT_THRESHOLDS):
    """Every finding this state can prove, most severe first. A detector that
    raises is skipped rather than allowed to cost the session its summary."""
    if not isinstance(state, dict):
        return []
    findings = []
    for detector in DETECTORS:
        try:
            finding = detector(state, thresholds)
        except Exception:  # noqa: BLE001 — one broken detector must not cost the rest
            finding = None
        if finding:
            findings.append(finding)
    order = {SEVERITY["medium"]: 0, SEVERITY["low"]: 1}
    findings.sort(key=lambda f: order.get(f["severity"], 9))
    return findings
