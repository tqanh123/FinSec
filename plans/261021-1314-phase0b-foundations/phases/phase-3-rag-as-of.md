---
phase: 3
title: "Rag As Of"
plan: 261021-1314-phase0b-foundations
created: 2026-10-21
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Phase 3 — RAG as-of

## Overview
`HybridTemporalRAGEngine.search` chỉ trả lời "tại thời điểm `as_of`": `as_of` bắt buộc, keyword-only; engine không bao giờ đọc đồng hồ; tài
liệu có `published_at > as_of` bị lọc **trước** khi xếp hạng, và thống kê corpus của BM25 chỉ tính trên các tài liệu thấy được — để một tài
liệu tương lai không thể đổi điểm hay thứ hạng của quá khứ (luật binding "No look-ahead"). API nhận `as_of` tuỳ chọn và echo lại. Phụ thuộc
Phase 2 (cùng sửa `api/server.py`, `tests/test_api_server.py`, `tests/test_end_to_end.py`; xây trên disclaimer của P2).

## Requirements
**R3.1 `src/finsec_alpharadar/rag/hybrid_temporal_rag.py`**
- Chữ ký: `def search(self, query: str, top_k: int = 5, market_filter: Optional[MarketRegion] = None, symbol_filter: Optional[str] = None,
  *, as_of: float) -> List[RetrievedPassage]` — bỏ `reference_time` (dòng 108); `as_of` = epoch giây UTC, cùng đơn vị với
  `RegulatoryFiling.published_at` (`domain/models.py:54`). Thiếu `as_of` → `TypeError` của Python; truyền theo vị trí → `TypeError`.
- Không đọc đồng hồ: xoá `now_ts = reference_time or datetime.now(timezone.utc).timestamp()` (dòng 112) và import `datetime` (dòng 5). `as_of=0.0`
  phải được tôn trọng (không có phép thử truthiness nào).
- Tập thấy được `V = [i for i, d in enumerate(self._docs) if d.published_at <= as_of]`, tính **đầu tiên**, **không phụ thuộc** bộ lọc
  market/symbol. Ứng viên = `V ∩` bộ lọc market/symbol (logic dòng 118-122). `V` rỗng hoặc ứng viên rỗng → `[]`.
- Thống kê BM25 (`N`, document frequency, `avg_dl`) tính trên `V` cho mỗi truy vấn — giữ ngữ nghĩa hiện tại (thống kê trên cả corpus, xếp hạng
  trên tập đã lọc), chỉ bỏ phần tương lai. Thống kê toàn cục lúc ingest (`_doc_freqs`, `_avg_dl`: dòng 39, 41, 68-72) bị xoá — không còn
  reader nào (OBSERVED: chỉ `_bm25_scores`, dòng 84 và 89, đọc chúng).
- Nhánh dense và ColBERT MaxSim giữ nguyên: vector từng tài liệu chỉ phụ thuộc token của chính nó (`_embed_text` dòng 54-60, `_token_vec` dòng
  47-52, `_colbert_maxsim` dòng 94-100) → không rò rỉ corpus (DERIVED từ code). Thứ hạng bm25/dense tính trên ứng viên (đã đúng, dòng 126-129).
- Tuổi: `age_days = (as_of - doc.published_at) / 86_400.0` — bỏ `max(0.0, …)` (dòng 138) vì bộ lọc bảo đảm ≥ 0.
- Thêm docstring lớp ghi rõ nhánh "dense" và MaxSim là **vector hash placeholder, không có ngữ nghĩa** (roadmap §5.2 dòng
  `rag/hybrid_temporal_rag.py`, việc của Phase 0; AGENTS.md:42 đã ghi điều này) — chỉ docstring, không đổi hành vi.

**R3.2 `src/finsec_alpharadar/api/server.py` — `search_disclosures`**
- Thêm `as_of: Optional[float] = Query(None, description="Decision time, epoch seconds UTC; default = request time")`.
- `decision_time = as_of if as_of is not None else datetime.now(timezone.utc).timestamp()` — đồng hồ **chỉ** được đọc ở biên API (thời điểm
  quyết định của một truy vấn sống = lúc nhận request); truyền `rag_engine.search(..., as_of=decision_time)`; response thêm
  `"as_of": decision_time` (cạnh `"disclaimer"` từ P2).

**R3.3 `tests/test_end_to_end.py:146`**: `reference_time=now_ts` → `as_of=now_ts` (sửa trong dòng; không dời dòng). Tài liệu FPT
(`published_at = now_ts - 600`, dòng 139) vẫn thấy được → `len(hits_vn) == 1` giữ nguyên (DERIVED).

**R3.4 `tests/test_rag_as_of.py` (mới)** — xem "Tests Before". **R3.5 `tests/test_api_server.py`**: sửa lời gọi `search_disclosures` của test
disclaimer ở P2 để truyền `as_of=` tường minh (gọi trực tiếp, tham số bỏ qua là object `Query` — OBSERVED); thêm test echo.

## Related Code Files
| Hành động | Tệp |
|---|---|
| Modify | `src/finsec_alpharadar/rag/hybrid_temporal_rag.py` |
| Modify | `src/finsec_alpharadar/api/server.py` |
| Modify | `tests/test_end_to_end.py` |
| Create | `tests/test_rag_as_of.py` |
| Modify | `tests/test_api_server.py` |
| Modify | `AGENTS.md` |
| Modify | `docs/system-architecture.md` |
| Modify | `docs/code-standards.md` |
| Modify | `docs/project-roadmap.md` |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

Challenge luật ">8 tệp": 9 tệp = 5 code/test + 4 docs (delta bắt buộc, `docs/code-standards.md:112`) → giữ.

## Implementation Steps
1. **Tests-before (RED).** Viết `tests/test_rag_as_of.py` và test echo trong `tests/test_api_server.py`. Trước khi ghi RED, thêm **shim chữ
   ký** (red-team r1 RT7) để RED là RED theo hành vi, không phải `TypeError` vì thiếu kwarg: `search(self, query, top_k=5, market_filter=None,
   symbol_filter=None, reference_time=None, *, as_of=None)` với `reference_time = as_of if as_of is not None else reference_time` ở đầu hàm;
   giữ nguyên dòng 112 (`reference_time or datetime.now(...)`) và mọi hành vi cũ. Chạy
   `.venv/Scripts/python -m pytest -q tests/test_rag_as_of.py tests/test_api_server.py` → RED theo đúng lý do ghi ở từng test dưới.
2. **Implement → xanh** (R3.1 — xoá shim: `reference_time` biến mất, `as_of` bắt buộc và keyword-only; rồi R3.2, R3.3, R3.5).
3. **Grep gate**: `grep -rn --include=*.py "datetime.now" src/finsec_alpharadar/connectors src/finsec_alpharadar/rag` (G1) → không output;
   `grep -n "reference_time\|max(0.0" src/finsec_alpharadar/rag/hybrid_temporal_rag.py` → không output.
4. **Delta docs + citation sweep** (dưới).
5. **Regression gate** → commit duy nhất, ví dụ `fix: make RAG search answer as of a required decision time` + trailer `Co-Authored-By:`.

### Delta docs (cùng commit; tiếng Anh; ghi ngày)
- **AGENTS.md**: "Known bugs" xoá dòng 47 ("RAG gives future-dated documents full weight …"); "Package Layout" dòng 59 (`rag/…`) thêm
  "`search` requires `as_of` and ignores documents published after it (<date>); this is as-of `published_at` only, not yet the point-in-time
  `information_time`" (red-team r1 RT9); dòng 61 (`api/server.py`) thêm "`GET /v1/rag/search` takes an optional `as_of` (default: request
  time) and echoes it". "Current Status": bullet **Tests** — cập nhật `<N>` và ngày OBSERVED theo số test thật sau phase này, và ghi kết quả CI của lần push trước (run id, OBSERVED — VL-1); cùng số đó ở
  `docs/code-standards.md` "Current state" (red-team r1 RT8).
- **docs/system-architecture.md**: bảng As-built dòng 28 (`rag/…`: bỏ câu "Look-ahead bug: lines 112 and 138, and no `published_at <= as_of`
  filter"; ghi as-of filter + BM25 stats over visible docs; giữ câu hash placeholder); Data flow dòng 68-70 (search nhận `as_of`); Known gaps
  xoá dòng 132; Constraints dòng 175: **viết lại, không xoá** gạch "reuse the RAG ranking until its `as_of` filter is fixed" thành "reuse the
  RAG ranking only as-of `published_at`; do not feed it into features until `published_at` is the point-in-time `information_time` /
  `first_seen_at` (roadmap Phase 1)" — red-team r1 RT9: bộ lọc chỉ dựa trên `published_at` (`domain/models.py:54`), hợp đồng PIT là OUT
  (plan.md "Out of scope"), và giờ JSON của EDGAR không khớp UTC (AGENTS.md:92).
- **docs/code-standards.md**: dòng 60 (ví dụ regression test RAG → nay có `tests/test_rag_as_of.py`); dòng 63 (Look-ahead test) thêm ví dụ
  `tests/test_rag_as_of.py`; dòng 73 xoá mục debt RAG (`reference_time or now`, age clamp); cập nhật số dòng trích ở dòng 13 (`:15`), 17
  (`:50,56`), 18 (`:151-154`) — bỏ import `datetime` làm các dòng dời lên.
- **docs/project-roadmap.md**: §7 0b mục 5 dòng 390 ("the RAG as_of fix, with a regression test") thêm "(done <date>)".

### Citation sweep (kiểm kê OBSERVED 2026-10-21)
`grep -nE "hybrid_temporal_rag\.py:[0-9]|server\.py:[0-9]|test_end_to_end\.py:[0-9]|test:[0-9]" AGENTS.md docs/system-architecture.md docs/code-standards.md docs/project-roadmap.md`
- `rag/hybrid_temporal_rag.py`: AGENTS.md:47 (xoá); code-standards:13 (`:15`), :17 (`:50,56`), :18 (`:151-154`), :60 (`:112,138`), :73
  (`:112`, `:138`); system-architecture:28 ("lines 47-52", "112 and 138", "118-122"), :80 (`:37-41`), :132.
- `api/server.py`: system-architecture:68 (`:52-66`), :80 (`:16-20`), code-standards:104 (`:19`) — kiểm lại sau khi thêm import/tham số.
- `tests/test_end_to_end.py`: không dời (sửa trong dòng 146).

### Tests Before
Fixture chung (từ spike OBSERVED 2026-10-21, `rag_spike.py` trong scratchpad): `AS_OF = 1_792_211_400.0`; P1 = `"alpha alpha alpha guidance"`
(`AS_OF − 1 ngày`), P2 = `"beta guidance"` (`AS_OF − 2 ngày`), F = `"alpha outlook"` (`AS_OF + 1 ngày`), cùng symbol/market; truy vấn
`"alpha beta"`; so sánh bộ `(filing_id, bm25_rank, dense_rank, rrf_score, colbert_maxsim_score, time_decay_factor, final_score)`.
- [x] `test_document_after_as_of_is_never_returned` — khoá luật "No look-ahead". RED (sau shim): code cũ trả F với
      `time_decay_factor == 1.0` (OBSERVED spike ca (a): `('F', 3, 2, 0.032, 1.0, 0.72)`).
- [x] `test_results_invariant_to_documents_after_as_of` (invariant prefix) — engine[P1, P2] và engine[P1, P2, F] phải cho cùng kết quả tại
      `AS_OF`. RED: code cũ trả thêm F và đổi rank. Test này **cũng bắt** một bản sửa chỉ lọc ứng viên mà vẫn giữ thống kê toàn cục: OBSERVED spike
      ca (c) — loại F khỏi ứng viên bằng bộ lọc symbol nhưng giữ IDF/`avg_dl` toàn cục thì `bm25_rank` của P1 đổi 1 → 2 và P2 đổi 2 → 1.
- [x] `test_as_of_is_required_and_keyword_only` — `search("alpha")` → `TypeError`; `search("alpha", 5, None, None, AS_OF)` → `TypeError`. RED
      (sau shim): `search("alpha")` chạy được (đọc đồng hồ), và đối số vị trí thứ 5 rơi vào `reference_time`.
- [x] `test_as_of_zero_is_honoured` — mọi tài liệu có `published_at > 0` → `search(q, as_of=0.0) == []`. RED: code cũ coi `0.0` là falsy và
      dùng đồng hồ thật (OBSERVED spike: `reference_time=0.0` → decay `0.8886` thay vì `0.9772` tại `AS_OF`).
- [x] `test_document_published_exactly_at_as_of_is_visible` — `published_at == as_of` → được trả, `time_decay_factor == 1.0` (mép `<=`). LOCK
      (sau shim code cũ cũng trả tài liệu này với decay 1.0) — khoá để bản sửa không đổi mép thành `<`.
- [x] (`tests/test_api_server.py`) `test_search_echoes_explicit_as_of` — `search_disclosures(q="alpha", market=None, symbol=None, top_k=5,
      as_of=AS_OF)["as_of"] == AS_OF`; `test_search_defaults_as_of_to_request_time` — truyền `as_of=None` **tường minh** → `isinstance(resp["as_of"],
      float)` và `> 0` (không assert giá trị đồng hồ). RED: response chưa có `as_of`.

### Implement
- Bước 2 ở trên.

### Tests After
- [x] Toàn bộ test RAG/API trên xanh; `test_end_to_end` xanh với `as_of=now_ts`; test disclaimer của P2 xanh với lời gọi đã thêm `as_of=`.
- [x] Grep gate bước 3 rỗng (G1 sạch hoàn toàn từ phase này).

### Regression Gate
- `.venv/Scripts/python -m pytest -q` (bash; PowerShell `.\.venv\Scripts\python -m pytest -q`) MUST pass, 0 skipped.
- `python -m pytest -q` (Python toàn cục) pass; skip đúng `tests/test_api_server.py`.
- `PYTHONPATH=src python tests/test_end_to_end.py` exit 0.
- `python3.13 harness/scripts/scaffold_standards.py --type system-architecture --check docs/system-architecture.md` và
  `python3.13 harness/scripts/scaffold_standards.py --type code-standards --check docs/code-standards.md` exit 0.
- Sau commit: `git push` (draft PR tự chạy CI — VL-1); run CI của commit này `success` trên 3.11 + 3.13
  (`gh run view <id> --json conclusion,jobs`) trước khi bắt đầu Phase 4.

## Success Criteria
- [x] RED (sau shim, theo hành vi — không có `TypeError` vì thiếu kwarg) ghi lại cho 6 test (4 trong `tests/test_rag_as_of.py` + 2 echo) và 1
      LOCK (mép `<=`) trước khi implement; sau implement tất cả xanh.
- [x] `test_results_invariant_to_documents_after_as_of` xanh và (kiểm tay một lần, không commit) đỏ nếu tạm thay thống kê BM25 bằng thống kê
      toàn cục — chứng minh test bắt được rò rỉ IDF.
- [x] G1 rỗng; `grep -n "reference_time" src/ tests/ -r --include=*.py` → không hit.
- [x] `grep -n "future-dated documents full weight\|reference_time or now" AGENTS.md docs/system-architecture.md docs/code-standards.md` → chỉ còn ghi chú lịch sử có ngày.
- [x] Hai standards check exit 0.

## Risk Assessment
| Rủi ro | Khả năng × Tác động | Xử lý |
|---|---|---|
| Đổi chữ ký `search` làm vỡ caller | Thấp × Trung bình | Caller đã liệt kê (codemap `70926379`): `api/server.py:61`, `tests/test_end_to_end.py:146` — cả hai sửa trong phase này; caller khác sẽ `TypeError` rõ ràng (fail-closed). |
| Tính lại thống kê BM25 mỗi truy vấn chậm hơn | Thấp × Thấp | Corpus trong bộ nhớ, rất nhỏ (API không ingest gì); hiệu năng không phải tiêu chí Phase 0; tối ưu khi có store (Phase 1 roadmap). |
| Test bất biến không đủ nhạy (rank không đổi) | Thấp × Trung bình | Fixture được chọn từ spike để rank P1/P2 lật khi rò IDF (OBSERVED); Success Criteria yêu cầu kiểm đỏ thủ công một lần. |
| Đọc đồng hồ ở API làm test không tất định | Thấp × Thấp | Test chỉ assert kiểu/dấu khi `as_of=None`; mọi test khác truyền `as_of` cố định. |
