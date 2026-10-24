---
id: 261021-1314-phase0b-foundations
title: "Phase 0b foundations: tests, correctness, hygiene"
status: completed
mode: hard
tdd: true
branch: claude/phase0b-foundations
created: 2026-10-21
author: user:tqanh123
decisions: []
phases:
  - phases/phase-1-test-infra.md
  - phases/phase-2-tick-correctness.md
  - phases/phase-3-rag-as-of.md
  - phases/phase-4-forensics-guards.md
  - phases/phase-5-hygiene.md
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Plan: Phase 0b foundations: tests, correctness, hygiene

> hs:cook đọc file này làm hợp đồng. Nhãn bằng chứng theo `harness/rules/verification-mechanism.md`:
> **OBSERVED** (đã chạy/đọc, kèm ngày), **VERIFIED** (URL nguồn, lấy từ research), **DERIVED** (suy từ OBSERVED, nêu cơ chế),
> **[PRIOR]** (kiến thức có sẵn), **[ASSUMED]** (chưa kiểm, kèm gate). Mọi `file:line` dưới đây đã đọc lại ngày 2026-10-21 trên `main` @ `7092637`
> (snapshot codemap `70926379`, khớp HEAD).

## Tổng quan
Plan này làm roadmap §7 Phase 0b mục 3 (tests), 4 (hygiene) và 5 (correctness) ở mức EXPANSION (D1), trên nhánh
`claude/phase0b-foundations`, qua 5 phase nối tiếp. Lý do: hôm nay pytest thu **0 test** (`python -m pytest -q -o pythonpath=src tests/`
→ "no tests ran", exit 5 — OBSERVED 2026-10-21), repo không có CI, và code vi phạm nhiều luật binding trong AGENTS.md: giá VN không bao giờ
được kiểm theo biên độ và sàn lạ âm thầm nhận 7% (`src/finsec_alpharadar/domain/models.py:44`), connector bịa thời gian bằng `now()`
(`connectors/us_alpaca_edgar.py:20`, `connectors/vn_ssi_vnstock.py:22`) và bịa size 100 (`us_alpaca_edgar.py:29-30`,
`vn_ssi_vnstock.py:27-28`, `api/server.py:38-39`), RAG trả tài liệu tương lai với trọng số đầy đủ (`rag/hybrid_temporal_rag.py:112,138`),
API không có câu "không phải khuyến nghị đầu tư" (`api/server.py:46-49,62-66`), forensics chia cho 0 và không có cổng loại công ty tài
chính (`forensics/xbrl_bctc_auditor.py:25-92`), và MCP Alpaca còn bật toolset `trading` (`.agents/mcp_config.json:16`). Sau plan: một bộ
pytest thật (≥ 30 test) chạy trong `.venv` và trên GitHub Actions (3.11 + 3.13), mọi lỗi binding kể trên được sửa kèm test hồi quy, và repo
sạch (gitignore neo gốc, compose/MCP được pin, chỉ bind 127.0.0.1, không mật khẩu cứng). Cắt YAGNI: không spike 0a, không poller 0b.6,
không sửa công thức forensics, không client mạng nào.

Chế độ: `--hard --tdd` (frontmatter `mode: hard`, `tdd: true`). **Không áp `--deep`, không áp `--parallel`**: chuỗi phase là tuyến tính, và
mọi phase đều sửa `AGENTS.md` + `docs/system-architecture.md` + `docs/code-standards.md` + `docs/project-roadmap.md` (luật đồng bộ debt,
`docs/code-standards.md:112`), nên không có cặp phase nào chạy song song được mà không phải khai `merger_owned` cho 4 file docs; với một người
làm, lợi ích song song gần bằng 0. Scout file-level của `--deep` đã được thay bằng bảng "Related Code Files" + danh sách "citation sweep"
chính xác trong từng phase.

## Quyết định đã khoá
Owner chốt ngày 2026-10-21 — không re-litigate:
- **D1 Scope.** Roadmap §7 Phase 0b mục 3, 4, 5, mức EXPANSION, cộng thêm: kiểm biên độ giá VN (luật binding), bỏ mọi giá trị bịa trong
  connectors và API (exchange, bid/ask size, bid/ask = giá khớp, market), và guard chia-0 + cổng tổ chức tài chính cho forensics. Guard
  forensics được **kéo sớm** từ roadmap §5.2 dòng `forensics/xbrl_bctc_auditor.py` (Phase 2, `docs/project-roadmap.md:238`); Phase 4 của
  plan này ghi một dòng Decisions có ngày vào AGENTS.md.
- **D2 CI.** Thêm workflow GitHub Actions; repo github.com/tqanh123/FinSec là PUBLIC (OBSERVED bởi main session qua `gh repo view`, 2026-10-21).
- **D3 Python floor.** `requires-python` từ `>=3.10` lên `>=3.11` (3.10 EOL 2026-10-01, VERIFIED https://devguide.python.org/versions/,
  research `ci-and-tooling.md` §2). Ma trận CI `"3.11"` + `"3.13"` (có ngoặc kép).
- **D4 Biên độ VN.** Tick VN có giá ngoài khoảng cho phép thì RAISE. `MarketTick` thêm `price_limits: Optional[Tuple[float, float]]` =
  (floor, ceiling), do feed cấp (SSI X-TRADE `Ceiling`/`Floor`, VERIFIED tên trường ở research `vn-price-bands.md` §4) hoặc caller truyền cho
  phiên đặc biệt (ngày GD đầu tiên, giao dịch lại sau ≥ 25 phiên, ngày không hưởng quyền đặc biệt, phiên chuyển sàn HNX→HOSE 2026-12-28).
  Có `price_limits`: kiểm chặt `floor <= price <= ceiling`. Không có: biên độ thường (HOSE 7% / HNX 10% / UPCOM 15%, chưa làm tròn, quanh
  `reference_price`) nới **một bước giá** mỗi phía, theo bảng bước giá VND VERIFIED (HOSE cổ phiếu: < 10.000 → 10; 10.000–49.950 → 50;
  ≥ 50.000 → 100; HNX 100; UPCOM 100 — research `vn-price-bands.md` §2), dùng bước giá tại mức giá trần (bước lớn hơn). Giá VN là **VND đầy
  đủ** ở biên domain (roadmap §5.1; mẫu tài liệu SSI dùng VND).
- **D5 MCP** (`.agents/mcp_config.json`): bỏ CẢ `trading` và `watchlists` khỏi `ALPACA_TOOLSETS` (`watchlists` có 5 thao tác ghi — research
  `mcp-pins.md` §2) — làm ở Phase 1; xoá entry `wandb` (PyPI 404, không pin được, Phase 7) và pin mọi ref — làm ở Phase 5.
- **D6 Code map.** Owner chọn tạo; `codemap.yaml` được sinh 2026-10-21 bằng `hs-code init run --apply`, đang untracked — Phase 1 commit nguyên
  trạng.
- **Mode.** `--hard` + `--tdd`, nhánh `claude/phase0b-foundations`.

## Ràng buộc (constraint-scan)
- `ownership.yaml:9 zone=docs -> docs/ [IN]` — `docs/system-architecture.md`, `docs/code-standards.md`, `docs/project-roadmap.md`.
- `ownership.yaml:16 zone=plans -> plans/ [IN]` — chính các file của plan này.
- `ownership.yaml:14 zone=state -> .harness/state/`, `ownership.yaml:15 zone=standards -> harness/standards/` — plan không ghi vào đó.
- `src/`, `tests/`, `.github/`, `.agents/`, file cấu hình ở gốc: không thuộc zone nào `[OUT]`, nhưng `ownership.yaml:6-7` ghi "This table
  only constrains HARNESS SCRIPTS disciplining themselves" → đây là mã nguồn dự án, không xung đột zone.
- `stage-policy.yaml:59` push `requires: [verification]`; `stage-policy.yaml:64,69,73` pr/merge/ship `requires: [verification,
  review-decision, plan-approval]` → mỗi phase phát `verification-P<n>.json` (khai ở `post` trong `plan-graph.yaml`); PR cần thêm
  review-decision + plan-approval: draft PR mở ở cuối Phase 1 (VL-1; ở local chỉ advisory, hooks OFF), còn "Ready for review" + merge ngoài plan.
- `.gitattributes:4` `harness/** -text` → không sửa file nào dưới `harness/` (verify byte-exact với `harness/manifest.json`).
- `.gitignore:48-55` là block do installer sinh (`harness/install/_target_files.py:28` `_GITIGNORE_BEGIN`) → Phase 5 không đụng.
  `.gitignore:57-58` (comment + `!harness/**/data/`) viết tay: `grep -rn -F '!harness/**/data/' harness/` (py/sh/yaml) → 0 hit (OBSERVED
  2026-10-21); `install.sh` không nằm trong repo (OBSERVED `ls`).
- `harness/hooks/write_guard.py:148-219` GUARD_LIST không chứa file nào của plan → sửa trực tiếp, không cần staging-cp;
  `harness/data/write-guard.yaml:12` `extra_guarded: []`; `harness/data/write-deny-policy.yaml:14` `soft_rules: []`.
- `harness/data/protected-branches.yaml:11-14` bảo vệ `main` → mọi commit trên `claude/phase0b-foundations`, merge qua PR.
- Claude Code hooks đang OFF trong dự án (AGENTS.md:113) → không hook nào gate local; gate là các lệnh ghi trong từng phase.
- Schema artifact: cook ghi `verification-P<n>.json` theo `harness/schemas/artifact-verification.json`; plan không đặt ra artifact mới.
- `/hs:test`: `hs-run test next` → exit 11, `state: ready`, `next_command: "pytest"`, stack `python`/`pip`, `ci: []` (OBSERVED 2026-10-21);
  nguồn là `harness/scripts/detect_techstack.py:55` (`"test_cmd": "pytest"`); `harness/data/test-strategy.yaml` chỉ áp cho suite của chính
  harness. `pytest` không có trên PATH (`which pytest` → không thấy, OBSERVED).
- Ngôn ngữ: prose của plan là tiếng Việt (`output_config.py --resolved` → `language: vi`); code, `AGENTS.md` và `docs/` viết tiếng Anh
  (AGENTS.md:141, §9.12); text tiếng Việt cho người dùng (disclaimer) giữ dấu, chuẩn NFC.

## Nơi chạy cook
Cook chạy ở **main checkout** `H:\study\Project\FinSec`, không dùng worktree (red-team r1 RT4). Bước 0 của Phase 1:
`git switch -c claude/phase0b-foundations` từ `main` @ `7092637`; `plans/` và `codemap.yaml` là untracked nên đi theo nhánh mới. Lý do:
- `.worktreeinclude` chỉ mang `harness/**` + `.claude/settings.json` sang worktree mới (OBSERVED 2026-10-21), nên worktree không có plan này
  lẫn `codemap.yaml` — R1.7 "commit nguyên trạng" không làm được ở đó.
- Trên một tree chưa chạy installer, `verify_install --strict` báo "478 file(s) drifted", exit 1 (OBSERVED 2026-10-21, red-team r1 trên bản
  clone sạch `redteam-r1/clone`), nên gate của Phase 5 sẽ đỏ vì môi trường chứ không vì code.
- Engine cook làm việc trên tree mà nó được gọi từ đó (`harness/scripts/hs_run_cook.py:3597-3636` chỉ cảnh báo khi nhánh khai báo đang ở
  worktree khác); `hs-run` gọi bằng đường dẫn trần sẽ đi ngược lên tìm `plans/` (CLAUDE.md).
- Nếu buộc phải dùng worktree: copy `plans/261021-1314-phase0b-foundations/` và `codemap.yaml` từ main checkout vào, chạy installer trong
  worktree trước gate strict của P5, và đặt `HARNESS_BIN_ROOT`/`HARNESS_DATA_ROOT` cho worktree đó.

## Features
- `pytest-suite` — `python -m pytest -q` thu và chạy toàn bộ test của dự án (≥ 30), local trong `.venv` và từ Python toàn cục.
- `ci-workflow` — GitHub Actions chạy suite trên Python 3.11 và 3.13 cho mọi pull request và mọi push lên `main`.
- `order-execution-guard` — test đỏ khi `src/`, `scripts/` hoặc `tests/` có client/lệnh/URL REST giao dịch, hoặc bất kỳ server MCP Alpaca
  nào bật toolset ghi/giao dịch (kể cả server từ xa), hoặc cờ paper bị tắt.
- `agents-md-path-guard` — test đỏ khi AGENTS.md nhắc tới một đường dẫn không tồn tại.
- `vn-band-validation` — tick có số không hữu hạn (mọi market), hoặc tick VN có sàn lạ/thiếu sàn, thiếu giá tham chiếu, hoặc giá ngoài khoảng
  cho phép thì raise lỗi có tên.
- `strict-connector-parsing` — connector Alpaca/SSI từ chối payload thiếu/sai trường thay vì bịa giá trị; endpoint SSI đúng.
- `api-input-validation` — `POST /v1/ticks/ingest` trả 422 cho input sai; mọi response nghiên cứu mang disclaimer song ngữ.
- `rag-as-of` — RAG chỉ trả lời "tại thời điểm `as_of`"; tài liệu sau `as_of` không ảnh hưởng gì tới kết quả.
- `forensics-guards` — điểm Altman/Beneish/Piotroski là `None` kèm lý do cho tổ chức tài chính, loại tổ chức chưa biết, và tỷ số không xác định.
- `repo-hygiene` — `.gitignore` neo gốc, compose/MCP được pin, chỉ bind 127.0.0.1, không mật khẩu cứng, rule `.agents/` trỏ về AGENTS.md.

## Hợp đồng thay đổi (System Architect)
Caller lấy từ `hs-run plan next --fact-subjects …` (`code.callers`, snapshot `70926379`, là cận dưới) cộng đọc tay `tests/test_end_to_end.py`.

| Hợp đồng | Trước | Sau | Phase | Caller đã liệt kê |
|---|---|---|---|---|
| `requires-python` | `>=3.10` (`pyproject.toml:10`) | `>=3.11` | P1 | — |
| `MarketTick.exchange` | `str = "IEX"` (`domain/models.py:31`) | `Optional[str] = None` | P2 | `api/server.py:30`, `connectors/us_alpaca_edgar.py:21`, `connectors/vn_ssi_vnstock.py:19`, `tests/test_end_to_end.py:71,82` |
| `MarketTick.price_limits` | — | `Optional[Tuple[float, float]] = None`, (floor, ceiling), field cuối | P2 | như trên |
| `MarketTick.__post_init__` | không có (`domain/models.py:19-45`) | mọi market: số hữu hạn; VN: kiểm theo D4; raise họ `TickValidationError` | P2 | như trên |
| `vn_ceiling_floor_proximity` | (ceiling, floor), fallback 7% (`domain/models.py:41-45`) | vẫn (ceiling, floor); lấy từ `price_limits` nếu có; bỏ fallback | P2 | `api/server.py:48`, `tests/test_end_to_end.py:59,158` |
| Parser Alpaca / SSI | default bịa (`us_alpaca_edgar.py:19-31`, `vn_ssi_vnstock.py:19-33`) | trường bắt buộc → `PayloadFieldError`; quote Alpaca phải cùng `S` và có `t` ≤ `t` của trade | P2 | `tests/test_end_to_end.py:40,44` |
| `POST /v1/ticks/ingest` | default bịa, thiếu key → 500 (`api/server.py:28-49`) | trường bắt buộc, lỗi → 422, thêm `disclaimer` | P2 | không có HTTP client trong repo |
| `GET /v1/rag/search` | `routed_model, router_ucb_scores, results` (`api/server.py:52-66`) | thêm `disclaimer` (P2); query `as_of` tuỳ chọn, echo `as_of` (P3) | P2, P3 | không có HTTP client trong repo |
| `HybridTemporalRAGEngine.search` | `reference_time: Optional[float] = None` (`rag/hybrid_temporal_rag.py:102-112`) | `*, as_of: float` bắt buộc, keyword-only | P3 | `api/server.py:61`, `tests/test_end_to_end.py:146` |
| `FinancialStatementSnapshot` | mutable, không có loại tổ chức (`domain/models.py:62-95`) | `frozen=True`, `issuer_type: IssuerType = IssuerType.UNKNOWN` (field cuối) | P4 | `forensics/xbrl_bctc_auditor.py:25`, `tests/test_end_to_end.py:106` |
| `ForensicAuditResult` | mutable, điểm không Optional (`forensics/xbrl_bctc_auditor.py:9-18`) | `frozen=True`, điểm `Optional`, thêm `unavailable_reasons` | P4 | `forensics/xbrl_bctc_auditor.py:94`, `tests/test_end_to_end.py:120` |

**Tương thích ngược.** Không có dữ liệu lưu trữ, không có consumer ngoài repo (API chỉ chạy local, không client nào trong repo). Mọi thay đổi
phá hợp đồng là nội bộ, và caller được sửa trong cùng phase (bảng trên). 500 → 422 là chặt hơn nhưng đúng hơn. Không cần migration.

## Luồng dữ liệu (sau plan)
```
Alpaca trade+quote dict ──AlpacaStreamAdapter (thiếu/sai trường → PayloadFieldError; t có offset; quote cùng S, t ≤ trade)──┐
SSI dict ──SSIFastConnectAdapter (thiếu trường → PayloadFieldError; Ceiling+Floor → price_limits)────────────────────────────┤
POST /v1/ticks/ingest JSON ──ingest_tick (KeyError/TypeError/ValueError → 422)──────────────────────────────────────────────┤
                                                                                                                         ▼
                                      MarketTick.__post_init__ (mọi market: số hữu hạn; VN: exchange ∈ {HOSE,HNX,UPCOM}; ref > 0; price trong khoảng D4)
                                                                                                                         ▼
                                      StreamingQuantEngine.process_tick → {"signal", "vn_ceiling_floor", "disclaimer"}

GET /v1/rag/search?as_of= ──decision_time = as_of hoặc giờ request (đọc đồng hồ CHỈ ở API)──► search(q, …, as_of=decision_time)
   → V = {doc: published_at <= as_of} → BM25 (N, df, avg_dl tính trên V) + dense/MaxSim từng doc → rank trên V ∩ bộ lọc
   → {"routed_model", "router_ucb_scores", "results", "as_of", "disclaimer"}

FinancialStatementSnapshot(issuer_type) ──cổng: chỉ NON_FINANCIAL mới tính──► guard từng điểm (số không hữu hạn / mẫu số ≤ 0 → None + reason)
   → ForensicAuditResult(frozen; điểm Optional; red_flags chỉ từ điểm đã tính; unavailable_reasons)
```

## Phases
| # | Theme | Phụ thuộc | Cỡ |
|---|---|---|---|
| 1 | Test Infra — pytest config, dev extra, CI, 2 guard test, MCP bỏ `trading`/`watchlists`, commit `codemap.yaml` | — | M (11 tệp: 7 code/config + 4 docs) |
| 2 | Tick Correctness — kiểm biên độ VN (D4), bỏ default bịa ở connector/API, 422 + disclaimer, fixture VND | P1 | L (15 tệp: 11 code/test + 4 docs) |
| 3 | RAG as-of — `as_of` bắt buộc, lọc trước khi rank, thống kê BM25 trên tập thấy được | P2 | S (9 tệp: 5 code/test + 4 docs) |
| 4 | Forensics Guards — `IssuerType` + cổng fail-closed, guard mẫu số, kết quả Optional | P3 | M (9 tệp: 5 code/test + 4 docs) |
| 5 | Hygiene — `.gitignore`, compose, Makefile, `.env.example`, pin MCP, rule `.agents/` | P4 | M (12 tệp: 8 config/test + 4 docs) |

Cạnh phụ thuộc: P1 → P2 vì P2+ cần pytest config, `.venv` và dev extra của P1. P2 → P3 vì cả hai sửa `api/server.py`,
`tests/test_api_server.py`, `tests/test_end_to_end.py` (P3 xây trên disclaimer của P2). P3 → P4 và P4 → P5 chủ yếu vì file dùng chung
(`domain/models.py`, `domain/__init__.py`, `tests/test_end_to_end.py`, `.agents/mcp_config.json` từ P1, và 4 file docs ở mọi phase). Không có
phase nào cần runtime của phase song song.

## Ma trận test
| File test | Phase | Loại | Spec class | Python toàn cục (không fastapi) |
|---|---|---|---|---|
| `tests/test_end_to_end.py::test_end_to_end` | P1 (sửa P2-P4) | e2e smoke | test | chạy |
| `tests/test_no_order_execution.py` | P1 | invariant repo | invariant | chạy |
| `tests/test_agents_md_paths.py` | P1 | invariant repo | invariant | chạy |
| `tests/test_vn_price_band.py` | P2 | unit domain | test | chạy |
| `tests/test_connectors.py` | P2 | unit connector | test | chạy |
| `tests/test_api_server.py` | P2 (sửa P3) | integration handler (gọi trực tiếp) | test | **skip cả module** (`importorskip("fastapi")`) |
| `tests/test_rag_as_of.py` | P3 | unit + look-ahead | invariant (prefix-invariance) + test | chạy |
| `tests/test_forensics_guards.py` | P4 | unit | test | chạy |
| `tests/test_repo_hygiene.py` | P5 | invariant repo | invariant | chạy |

Không test nào gọi mạng, đọc đồng hồ, hay sleep (code-standards "Testing"). `hypothesis` được cài theo dev extra nhưng chưa test nào import
nó (property test của roadmap §6.4 mục 8 là OUT).

## Out of scope
- Roadmap 0a (spike): `tests/test_edgar_time.py` golden test, probe Alpaca (SIP trên Basic, `adjustment=all`, mã đã huỷ niêm yết), probe vnstock
  (rate limit, độ sâu lịch sử, đơn vị giá), tài khoản SSI + đọc spec R-channel, đối chiếu 30 công bố VN, bảng counts. Hệ quả: exit gate
  Phase 0 của roadmap ("≥ 15 test **gồm cả** EDGAR golden test", "spike table trong AGENTS.md", poller 5 phiên) **chưa** đạt bởi plan này.
- Roadmap 0b.6: poller VN capture-only (`capture/`).
- Chuyển `router/` sang `experimental/`; model ID chưa kiểm (`api/server.py:19`, `tests/test_end_to_end.py:149`); lỗi cold-start của router (AGENTS.md:50).
- Làm tròn trần/sàn theo bước giá (roadmap §5.2 dòng `domain/models.py`), membership sàn theo thời gian, bảng biên độ phiên đặc biệt (caller
  truyền `price_limits`), biên độ phiên chuyển sàn HNX→HOSE (ASSUMED), bước giá ETF/chứng quyền, giao dịch thoả thuận (PT), enum
  `override_reason`, kiểm lỗi đơn vị `price/ref`.
- Probe/chuyển đổi đơn vị giá vnstock; default của `SecEdgarFilingAdapter`/`VnstockBCTCAdapter` (`title`, `section`, `form_type="BCTC_QUY"`,
  `xbrl_metrics`) — thuộc đợt viết lại connector ở roadmap Phase 1.
- Sửa quant/Kalman (foreign flow cộng trên deque `quant/kalman_microstructure.py:99`, mù phiên, OBI ±1 khi khoá biên) và default `0.0` của
  foreign volume ở SSI/API → roadmap Phase 6.
- Sửa công thức forensics (roadmap Phase 2): Piotroski theo tài sản đầu kỳ / đòn bẩy trên tài sản bình quân (cần `prev_prev_total_assets`),
  EQ_OFFER theo phát hành cổ phần, chọn biến thể TATA, Altman Z'', ánh xạ Thông tư 99/200, quy tắc trung tính = 1 của Beneish (AQI/DEPI/SGAI),
  chính sách quý vs năm; phân loại OTHER_FINANCIAL/REIT/SPAC và ánh xạ SIC/GICS/ICB (refdata).
- Chuyển timestamp domain sang datetime tz-aware + `information_time`/`first_seen_at`/… (hợp đồng PIT, roadmap Phase 1).
- `.agents/agents/*.md`, `.agents/hooks.json`, `scripts/validate_command_safety.sh`; `EDGAR_IDENTITY` trong mcp_config và `.env.example`
  (liên hệ thật); `ALPACA_DATA_FEED` (roadmap §5.2 dòng `.env.example` — chọn feed sau spike 0a, roadmap Phase 1); khoá dependency bắc cầu
  của MCP server.
- Lint gate (ruff) và type-check: repo không có `[tool.ruff]`/mypy config (`pyproject.toml:1-35`, OBSERVED); ruff chỉ được pin trong dev extra.
- Viết lại API read-only + auth + pydantic request model (roadmap Phase 4).
- Docstring "O(1)" của `quant/deduplicator.py` (roadmap §5.2, không thuộc §7 0b.3-0b.5); extra `research`/`deferred` và pin vnstock trong
  `pyproject.toml` (chờ 0a).
- Chuỗi "harness v7.2.0" đã cũ ở `docs/system-architecture.md:35` và `docs/code-standards.md:40` (AGENTS.md:62 ghi v7.3.0) — thấy nhưng không sửa.
- Cache pip và job lint trong CI.

## Acceptance (toàn plan)
Mỗi mục có spec class (five-hat): `test` = lệnh/assert tự động; `invariant` = tính chất luôn đúng, kiểm bằng cấu trúc/grep; `manual` = cần hệ
ngoài sống.
- [x] [test] Mỗi phase red→green TDD; suite xanh sau mỗi phase bằng lệnh thật của repo: `.venv/Scripts/python -m pytest -q` (bash; PowerShell
  `.\.venv\Scripts\python -m pytest -q`) exit 0, và `PYTHONPATH=src python tests/test_end_to_end.py` exit 0.
- [x] [test] Lint + type-check: **N/A** — repo không có cấu hình ruff/mypy (`pyproject.toml` không có `[tool.ruff]`/`[tool.mypy]`, OBSERVED
  2026-10-21) và thêm gate lint là OUT (D1 không gồm). Build: bước build duy nhất là `.venv/Scripts/python -m pip install -e ".[dev]"` exit 0.
- [x] [test] Trong `.venv`: ≥ 30 test được thu và **pass, 0 skipped** (ghi số thật vào `verification-P5.json`).
- [x] [test] Python toàn cục (3.13.14, không fastapi): `python -m pytest -q` pass, skip **chỉ** `tests/test_api_server.py`.
- [x] [test] Chân 3.11 local (venv ngoài repo, xem Phase 1 bước 6) pass cùng suite trước khi mở draft PR (Phase 1) và ở gate Phase 5.
- [x] [manual] CI xanh trên 3.11 + 3.13 cho commit cuối của PR — remote **xác nhận**, không **phát hiện**: lần chạy đầu là draft PR cuối
  Phase 1 (VL-1), mỗi push sau đó chạy lại; `[ASSUMED]` tới run đầu đó. Neo `manual_test_anchor.py`; hooks đang OFF (AGENTS.md:113)
  nên hook không ghi anchor — bằng chứng là `gh run view <run-id> --json conclusion,jobs` (conclusion `success`, 2 job).
- [x] [test] Compose: `POSTGRES_PASSWORD=x docker compose config -q` exit 0; `POSTGRES_PASSWORD= docker compose config -q` exit ≠ 0 với
  "required variable POSTGRES_PASSWORD is missing a value: set POSTGRES_PASSWORD in .env" (chỉ `config`, không bao giờ `up`).
- [x] [test] `python3.13 harness/scripts/verify_install.py --root . --strict` → "verify_install OK" exit 0.
- [x] [test] Hai standards check exit 0 ("conform: … matches the … section-set").
- [x] [invariant] Grep gate rỗng (mỗi lệnh exit 1, không output) — baseline OBSERVED 2026-10-21 có hit ở chỗ ghi trong ngoặc:
  - G1 `grep -rn --include=*.py "datetime.now" src/finsec_alpharadar/connectors src/finsec_alpharadar/rag` (3 hit: `us_alpaca_edgar.py:20`, `vn_ssi_vnstock.py:22`, `hybrid_temporal_rag.py:112`) — sạch sau P3;
  - G2 `grep -rn --include=*.py "100\.0" src/finsec_alpharadar/connectors src/finsec_alpharadar/api` (6 hit) — sạch sau P2;
  - G3 `grep -rnE --include=*.py 'get\("[A-Za-z]+", *"(IEX|HOSE)"\)|exchange: *str *= *"IEX"' src/finsec_alpharadar` (4 hit) — sạch sau P2;
  - G4 `grep -rnE --include=*.py 'get\("(bp|ap|BidPrice1|AskPrice1|bid_price|ask_price)", ' src/finsec_alpharadar` (6 hit) — sạch sau P2;
  - G5 `grep -nE "max\((1e-4|1\.0)," src/finsec_alpharadar/forensics/xbrl_bctc_auditor.py` (7 hit: dòng 31,45,48,52,56,77,78) — sạch sau P4;
  - G6 `grep -rnE --include=*.py 'get\("market", *"US"\)' src/finsec_alpharadar` (1 hit `api/server.py:32`) — sạch sau P2;
  - G7 `grep -rn --include=*.py "stream.ssi.com.vn" src/` (2 hit `vn_ssi_vnstock.py:11,15`) — sạch sau P2.
- [x] [invariant] Ba danh sách debt nhất quán (AGENTS.md "Current Status", `docs/system-architecture.md` "Known gaps", `docs/code-standards.md`
  "Debt"): `grep -nE "silently gets 7%|pytest collects 0|reference_time or now|stream\.ssi\.com\.vn|no zero-division guards|0\.0\.0\.0|:latest|SSI_PRIVATE_KEY_PATH|still lists .trading.|fastapi is not installed" AGENTS.md docs/system-architecture.md docs/code-standards.md`
  (mẫu `fastapi is not installed` thêm theo red-team r1 RT8)
  → mọi hit còn lại phải là ghi chú lịch sử có ngày ("was …; fixed <date>"), không còn câu mô tả debt ở thì hiện tại.
- [x] [invariant] `tests/test_agents_md_paths.py` xanh sau mỗi lần sửa AGENTS.md (nằm trong suite, nên mọi regression gate đều kiểm).

## Rollback
- Mỗi phase là đúng một commit trên `claude/phase0b-foundations`. Hoàn tác theo thứ tự ngược: `git revert <sha-P5>` … `git revert <sha-P1>`
  (phase sau xây trên file của phase trước; revert lẻ một phase giữa chuỗi có thể conflict ở `AGENTS.md`/docs — khi đó revert luôn các phase sau
  nó). Sau mỗi revert chạy lại regression gate của phase đứng trước.
- **Merge PR bằng "Create a merge commit"**, không squash/rebase, để SHA từng phase còn trên `main` (red-team r1 RT6; OBSERVED 2026-10-21:
  repo cho phép squash và rebase, và `main` không có branch protection trên GitHub — `gh api repos/tqanh123/FinSec/branches/main` →
  `"protected":false`). `harness/data/protected-branches.yaml` chỉ là cổng local của harness (pre-push), khác branch protection của GitHub.
  CI xanh trên cả hai chân là điều kiện merge do người merge tự kiểm; biến CI thành required check là cài đặt GitHub của owner, ngoài plan.
- Rollback **sau khi merge**: nhánh mới từ `main`, `git revert -m 1 <merge-sha>` (hoặc revert từng commit phase theo thứ tự ngược), rồi đưa
  qua PR — không push thẳng lên `main`.
- Revert P1 xoá `.github/workflows/ci.yml` và bỏ track `codemap.yaml` (file sinh lại được bằng `hs-code init run --apply`).
- Môi trường local: xoá `.venv/` (gitignored, `.gitignore:21`) và `src/finsec_alpharadar.egg-info/` (gitignored, `.gitignore:12`); venv 3.11
  nằm ngoài repo.
- Không có dữ liệu, migration hay trạng thái remote nào cần hoàn tác (chưa có client mạng, chưa có store).

## Risks
| Rủi ro | Khả năng × Tác động | Giảm thiểu |
|---|---|---|
| Kiểm VN chặt làm vỡ caller / phiên đặc biệt (ngày đầu niêm yết, giao dịch lại, ngày quyền, 2026-12-28) | Trung bình × Cao | Override `price_limits`; ưu tiên `Ceiling`/`Floor` của feed; nới 1 bước giá bao trường hợp bump giá thấp (research `vn-price-bands.md` §3); thông điệp lỗi nêu symbol/trường/giá trị/khoảng và nhắc `price_limits`; chưa có feed tick VN sống trước Phase 6 (§9.3) nên không ảnh hưởng vận hành. |
| Test đường dẫn AGENTS.md giòn (token mới, path chỉ có ở local) | Trung bình × Thấp | `NOT_PATHS`/`ALIASES` có lý do; bỏ qua token bắt đầu bằng `/`; bỏ qua path bị gitignore; thông điệp liệt kê mọi token thiếu; **không** backtick path chưa track (vd `plans/…`) trong AGENTS.md. |
| `/hs:test` chạy `pytest` trần, không có trên PATH | Cao × Thấp | Kích hoạt venv trong CÙNG một lệnh shell; gate ràng buộc vẫn là `.venv/Scripts/python -m pytest -q`; ghi kết quả thật ở Phase 1. |
| Gọi handler FastAPI trực tiếp | Thấp × Trung bình | OBSERVED 2026-10-21 (probe scratch, fastapi 0.156.4, 3.11.16 + 3.13.14): decorator trả về hàm gốc, `HTTPException` nổi lên với `status_code`; tham số `Query` bị bỏ qua nhận object `Query`, KHÔNG phải `None` → luôn truyền đủ tham số. fastapi không pin (`>=0.115.0`) → nếu bản mới bọc hàm: fallback `TestClient` + `httpx2` trong dev extra. |
| Lần chạy CI đầu tiên chưa kiểm | Trung bình × Trung bình | Chạy đúng các lệnh CI ở local trên 3.11 + 3.13 trước; action pin SHA; `ubuntu-24.04` (tránh đợt chuyển 26.04, research `ci-and-tooling.md` §1); draft PR ngay sau Phase 1 (VL-1) để CI chạy thật từ phase đầu, và mỗi phase sau chỉ bắt đầu khi CI của lần push trước xanh; nhãn `[ASSUMED]` tới run đầu. |
| Docs lệch nhau giữa 3 file + trích dẫn `file:line` bị dời | Cao × Trung bình | Mỗi phase liệt kê delta docs chính xác + "citation sweep" theo file; sửa fixture smoke **trong dòng** để dòng 1-163 không dời; standards check; grep debt ở Acceptance. |
| Neo `.gitignore` làm lộ file | Thấp × Trung bình | OBSERVED 2026-10-21: không có thư mục `data/`/`models/` nào ngoài `harness/` (và `.claude/worktrees`, đã ignore bởi `.git/info/exclude:7`); so `git status --porcelain --ignored` và số `git ls-files` trước/sau; P5 thêm mẫu artefact dữ liệu theo đuôi file (`*.jsonl.gz`, `*.parquet`, `*.duckdb`, `*.sqlite*`, `*.db`) để thư mục dữ liệu lồng sau này vẫn bị ignore (red-team r1 RT12). |
| Đổi đơn vị fixture (nghìn VND → VND) đổi tín hiệu quant | Thấp × Trung bình | OBSERVED 2026-10-21: `alert_reasons` giống hệt ở ×1 và ×1000 (volume z, CUSUM log-return, OBI, foreign flow đều không phụ thuộc mức giá). |
| Dependency lõi không pin (`fastapi>=0.115.0`, `numpy>=1.26.0`) trôi giữa local và CI | Trung bình × Thấp | CI cài mới mỗi lần; lỗi do trôi hiện ra như test đỏ có tên; pin là OUT. |
| Phase 2 lớn (15 tệp) | Trung bình × Trung bình | TDD theo module (domain → connector → API → fixture), chạy test đích sau mỗi bước, chỉ commit khi cả gate xanh. |

## Kiểm tra plan-graph (planner tự chạy, OBSERVED 2026-10-21)
- `python3 harness/scripts/plan_graph.py plans/261021-1314-phase0b-foundations --require` → exit 0:
  - `parallel batches: [['P1'], ['P2'], ['P3'], ['P4'], ['P5']]` (chuỗi tuyến tính, không cặp song song nào);
  - `import-edge-scan: snapshot 20261021-124907-70926379 … measured an uncommitted state that may differ from the current one` (thông tin);
  - `phase-files: P5 declares .env.example in plan-graph.yaml; its phase file phase-5-hygiene.md never names it under Related Code Files` —
    advisory đã biết: đuôi `example` không thuộc `_FILE_EXTS` (`harness/scripts/plan_graph.py:597-601`), nên parser không nhận được tên này
    dưới bất kỳ cách viết nào; file vẫn được khai để quyền sở hữu đầy đủ (giải thích ở Phase 5).
- `PYTHONIOENCODING=utf-8 python3 harness/scripts/plan_graph.py plans/261021-1314-phase0b-foundations --facts` → exit 0:
  `declared_files: 32`, `directories: 11`, `phases: 5`, `independent_clusters: 1`, `longest_chain: 5`, `collector_phases: []`, `old_plan_count: 0`
  (không có `PYTHONIOENCODING=utf-8` lệnh này crash `UnicodeEncodeError` trên console cp1252 — lỗi môi trường, không phải lỗi sidecar).

## Validation Log
Validate gate chạy ở main ngày 2026-10-21 (`skill_config.py --resolved` → `plan.validation.mode: prompt`, 3-8 câu); owner trả lời qua
AskUserQuestion. Quyết định chép nguyên văn lựa chọn của owner.

| VL | loại | chủ đề | quyết định của owner | ghi DEC? / nơi plan mang nó |
|---|---|---|---|---|
| VL-1 | Assumptions / Risks | Thời điểm mở PR (lần chạy CI đầu là `[ASSUMED]`) | "Draft PR sau Phase 1" | Không ghi DEC (quy trình riêng của plan này). Phase 1 bước 12 + R1.5 + Success Criteria; Regression Gate của P2-P5 (push, CI xanh trước phase sau); bullet Tests của AGENTS.md ở P2-P5; plan.md "Ràng buộc", Acceptance, Risks. |
| VL-2 | Architecture (quy trình) | Cách merge PR | "Create a merge commit" | Không ghi DEC. plan.md "Rollback" (đã có từ RT6); Phase 5 Regression Gate. |
| VL-3 | Assumptions | Nơi chạy cook | "Main checkout" | Không ghi DEC. plan.md "Nơi chạy cook"; Phase 1 bước 0 (đã có từ RT4). |
| VL-4 | Trade-offs | Giá VN nhập nhầm bằng nghìn VND lọt qua D4 | "Chỉ ghi giới hạn vào docs" | Không ghi DEC riêng: giới hạn nằm trong dòng Decisions D4 mà Phase 2 thêm vào AGENTS.md và trong docstring `MarketTick` (Phase 2 R2.1, đã có từ RT10). |

- Không ghi DEC qua `decision_register.py`: luật Agent Memory Protocol của dự án (AGENTS.md) đặt mọi quyết định bền vững vào AGENTS.md
  "Decisions" — cook thêm các dòng đó ở P1, P2, P4, P5; một sổ DEC thứ hai sẽ thành nguồn song song.
- Mặc định không hỏi (owner có thể đổi lúc duyệt): thư mục `plans/` vẫn untracked, không đẩy lên repo public; bật "required CI check" cho
  `main` trên GitHub là cài đặt của owner, ngoài plan (hôm nay `main` không có branch protection — red-team r1 RT6).
- Các `[ASSUMED]` còn lại đều có cổng kiểm và không cần owner quyết: lần chạy CI đầu (VL-1, Phase 1 bước 12); `/hs:test` với venv kích hoạt
  (Phase 1 bước 8); đơn vị size IEX và việc Alpaca luôn gửi đủ trường (Phase 6); tên trường stub SSI (Phase 6, spec R-channel); tag ảnh compose
  (lần `pull` đầu); `docker compose down` cần profile (truyền sẵn); uv nhận SHA 40 ký tự (chưa chạy server lạ — cố ý).

### Whole-Plan Consistency Sweep
- Files reread (đọc lại hết, không chỉ diff): `plan.md`, `phases/phase-1-test-infra.md`, `phases/phase-2-tick-correctness.md`,
  `phases/phase-3-rag-as-of.md`, `phases/phase-4-forensics-guards.md`, `phases/phase-5-hygiene.md`; hai lượt — sau red-team r1 và sau validate.
- Decision deltas checked: 23 (D1-D6, VL-1..VL-4, 13 Accept của red-team r1) — không phase nào trái một quyết định đã khoá.
- Reconciled stale references: 14 — N ≥ 8 ở Phase 1; 4 cite dời trong bảng disposition (tính 1 mục);
  rủi ro `.gitignore` ở plan.md và Phase 5 (thêm RT12); thời điểm CI ở R1.5, bước 7, Acceptance, Risks, "Ràng buộc", Acceptance chân 3.11
  (VL-1); luồng dữ liệu forensics (thêm số không hữu hạn); dòng Decisions forensics ở Phase 4 (thêm "non-finite input"); delta docs Phase 2
  cho kiểm số hữu hạn + ghép cặp quote/trade; trích `stage-policy.yaml` sửa thành `:62-64` / `:51-57`.
- Gap mới tìm thấy và đã sửa: trường chuỗi bắt buộc nhận `None` sẽ thành `"None"` (`S`, `x` Alpaca; `Symbol`, `Exchange` SSI; `symbol` API) —
  cùng lớp lỗi với RT13a → Phase 2 R2.5, R2.6, R2.7 + `test_null_string_fields_raise`, `test_null_symbol_returns_422`.
- Name-honesty / SRP: `PayloadFieldError` (giờ gồm cả lỗi ghép cặp quote/trade — vẫn là lỗi trường của payload), `TickValidationError`
  (giờ gồm số không hữu hạn ở mọi market), `vn_tick_size`, `vn_price_limits`, `IssuerType`, `unavailable_reasons`, `DISCLAIMER`,
  `tests/test_no_order_execution.py` (quét thêm `scripts/`, `tests/`) — tên vẫn đúng và đủ trách nhiệm. Không thuật ngữ nghiệp vụ mới nào cần
  `glossary_register.py` (các tên mới là định danh code).
- Unresolved contradictions: 0.

## Red-team disposition — r1
Báo cáo `reports/from-code-reviewer-to-planner-red-team-r1-plan-review-report.md`: verdict REVISE, 13 finding (4 M, 9 L), 4 persona.
Main tự kiểm lại bằng chứng ngày 2026-10-21 trước khi quyết (cột cuối). Không finding nào bị Reject; RT10 chỉ nhận phần tài liệu.

| id | sev | quyết định | phase | nơi plan đã đổi | kiểm của main / ghi chú |
|---|---|---|---|---|---|
| RT1 | M | Accept | P2 | `phases/phase-2-tick-correctness.md:35` «0. **Mọi market, trước mọi kiểm khác** (red-team r1 RT1): … timestamp … ,»; test `phases/phase-2-tick-correctness.md:270` «reference_price=inf … ; HOSE … price_limits=(1.0, inf) … ) →», `phases/phase-2-tick-correctness.md:282` «red-team r1 RT1; RED.», `phases/phase-2-tick-correctness.md:309` «- [x] … test_non_finite_price_returns_422 … (tick US … "price": float("nan")»; hợp đồng `plan.md:128` «MarketTick.__post_init__ … không có ( … domain/models.py:19-45 … )» | Bước 0 của `__post_init__`: mọi trường số hữu hạn, cho mọi market; ca NaN/inf nằm trong 3 file test sẵn có (không thêm file, plan-graph giữ 32 file). |
| RT2 | M | Accept | P4 | `phases/phase-4-forensics-guards.md:43` «- **Trước mọi guard của một điểm: số hữu hạn** (red-team r1 RT2). Mọi field số c»; test `phases/phase-4-forensics-guards.md:146` «property_plant_equipment=0 … → Beneish (5 ca cuối hôm nay … ZeroDivisionError» | Kiểm `math.isfinite` đứng trước mọi guard của từng điểm, reason `<field>_not_finite`; các guard viết dạng `not (x > 0)`. |
| RT3 | M | Accept | P1 | `phases/phase-1-test-infra.md:36` «*.py … dưới … tests/ … **trừ** chính … tests/test_no_order_execution.py», `phases/phase-1-test-infra.md:41` «ALPACA_TOOLSETS»; features `plan.md:110` «nào bật toolset ghi/giao dịch (kể cả server từ xa), hoặc cờ paper bị tắt.» | Main kiểm: 8 mẫu trên src/, scripts/, tests/ → 0 hit (không báo nhầm); hôm nay chỉ `alpaca-us` dùng `alpaca-mcp-server`; test toolset vẫn là test đỏ duy nhất ở bước 3. |
| RT4 | M | Accept | plan, P1 | `plan.md:95` «Cook chạy ở **main checkout** … H:\study\Project\FinSec»; bước 0 `phases/phase-1-test-infra.md:137` «0. **Nơi chạy (red-team r1 RT4).** Cook chạy ở main checkout» | Main kiểm: `.worktreeinclude` chỉ mang `harness/**` + `.claude/settings.json`; engine cook chạy trên tree được gọi (hs_run_cook.py dòng 3597-3636), không tự tạo worktree hay nhánh. |
| RT5 | L | Accept | P5 | `phases/phase-5-hygiene.md:252` «WARN phụ thuộc PATH của shell (red-team r1 RT5; OBSERVED 2026-10-21: 4 WARN ở sh»; baseline `phases/phase-5-hygiene.md:158` «regression gate — red-team r1 RT5); lưu vào scratchpad, không vào repo.» | Gate: exit 0 + "verify_install OK" + không FAIL/DRIFT; WARN so sau khi lọc "not reachable on PATH", baseline chụp cùng shell. |
| RT6 | L | Accept | plan | `plan.md:250` «- **Merge PR bằng "Create a merge commit"**, không squash/rebase, để SHA từng ph», `plan.md:254` «- Rollback **sau khi merge**: nhánh mới từ … main … ,» | Merge commit + rollback sau merge qua PR. Biến CI thành required check trên GitHub là cài đặt của owner, ngoài plan — nêu cho owner lúc duyệt. |
| RT7 | L | Accept | P3 | `phases/phase-3-rag-as-of.md:69` «ký** (red-team r1 RT7) để RED là RED theo hành vi, không phải … TypeError» | Shim chữ ký trước khi ghi RED; `test_document_published_exactly_at_as_of_is_visible` thành LOCK (sau shim code cũ cũng qua). |
| RT8 | L | Accept | P1-P5 | `phases/phase-1-test-infra.md:203` «with the dev setup; the global Python may still lack it" (red-team r1 RT8).», `phases/phase-1-test-infra.md:215` «setup" (red-team r1 RT8); phần còn lại của dòng để Phase 2 sửa.»; grep `plan.md:242` «(mẫu … fastapi is not installed … thêm theo red-team r1 RT8)»; số test `phases/phase-2-tick-correctness.md:197` «docs/code-standards.md … "Current state" (red-team r1 RT8).», `phases/phase-3-rag-as-of.md:84` «docs/code-standards.md … "Current state" (red-team r1 RT8).», `phases/phase-4-forensics-guards.md:116` «docs/code-standards.md … "Current state" (red-team r1 RT8).», `phases/phase-5-hygiene.md:182` «docs/code-standards.md» | Main kiểm: hai câu "fastapi is not installed" có thật ở docs/code-standards.md dòng 35 và docs/system-architecture.md dòng 30. |
| RT9 | L | Accept | P3 | `phases/phase-3-rag-as-of.md:82` «information_time … " (red-team r1 RT9); dòng 61 ( … api/server.py … ) thêm "», `phases/phase-3-rag-as-of.md:89` «first_seen_at … (roadmap Phase 1)" — red-team r1 RT9: bộ lọc chỉ dựa trên» | Viết lại, không xoá ràng buộc RAG. Báo cáo trích AGENTS.md dòng 84-86 cho giờ JSON EDGAR; dòng đúng là AGENTS.md dòng 92 (phase đã trích dòng đúng). |
| RT10 | L | Accept (chỉ tài liệu) | P2 | `phases/phase-2-tick-correctness.md:56` «biết** (red-team r1 RT10): kiểm D4 giả định VND đầy đủ — một tick mà cả giá lẫn », `phases/phase-2-tick-correctness.md:194` «- VN prices are full VND at the domain boundary. The check assumes full VND: a t» | Không nhận kiểm lưới bước giá: tham chiếu UPCOM là VWAP phiên trước và chưa rõ có làm tròn theo bước (research vn-price-bands.md dòng 29, 117), nên có thể raise sai tick thật; HOSE/HNX có làm tròn (dòng 27) nhưng kiểm lưới vẫn để lọt giá nghìn VND nằm trên lưới (vd 140.0). Kiểm đơn vị giữ ở Out of scope. |
| RT11 | L | Accept | P4 | `phases/phase-4-forensics-guards.md:149` «**Độc lập** (red-team r1 RT11): mỗi ca còn assert rằng các điểm **không** bị ảnh» | Baseline tính bằng `evaluate(base)` ngay trong test, không hard-code. |
| RT12 | L | Accept | P5 | `phases/phase-5-hygiene.md:22` «- **Mẫu artefact dữ liệu** (red-team r1 RT12): sau khi neo, thư mục dữ liệu lồng»; test `phases/phase-5-hygiene.md:222` «- [x] … test_data_artefacts_are_ignored … (red-team r1 RT12) —» | Main kiểm: không file tracked nào khớp mẫu mới; 5 path test hôm nay exit 1, `capture/data/2026-10-21.jsonl.gz` exit 0 (bị ignore nhờ `data/` chưa neo). |
| RT13 | L | Accept | P2 | (a) `phases/phase-2-tick-correctness.md:120` «"NONE" … — red-team r1 RT13a), … reference_price … , … price_limits … (», test `phases/phase-2-tick-correctness.md:311` «- [x] … test_vn_null_exchange_reports_missing … (tick VN … "exchange": None»; (b) `phases/phase-2-tick-correctness.md:75` «- **Ghép cặp trade/quote** (red-team r1 RT13b; luật "No look-ahead", AGENTS.md:1», fixture `phases/phase-2-tick-correctness.md:132` «sớm hơn trade (R2.5 ghép cặp, red-team r1 RT13b); vẫn một dòng.», test `phases/phase-2-tick-correctness.md:280` «sau trade … t … 1 ms) → … PayloadFieldError … — red-team r1 RT13b; RED.»; (c) `phases/phase-2-tick-correctness.md:121` «nhận … [floor, ceiling] … còn response trả … vn_ceiling_floor … theo thứ tự» | (b) theo research alpaca-timestamps.md dòng 92 (quote có `S`, `t` bắt buộc) và luật "No look-ahead" (AGENTS.md dòng 124). |
