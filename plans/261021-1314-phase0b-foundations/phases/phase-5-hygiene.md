---
phase: 5
title: "Hygiene"
plan: 261021-1314-phase0b-foundations
created: 2026-10-21
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Phase 5 — Hygiene

## Overview
Dọn cấu hình repo theo roadmap §7 0b.4 (và gạch cuối của 0b.5 về `.agents/rules`): neo `.gitignore`, compose bỏ Redpanda + profile + pin tag +
chỉ bind 127.0.0.1 + mật khẩu lấy từ env, Makefile bind 127.0.0.1, `.env.example` bỏ khoá SSI thừa và thêm URL SSI, pin mọi MCP server và xoá
`wandb` (D5 phần 2), viết lại rule `.agents/` thành con trỏ về AGENTS.md (§9.13); khoá tất cả bằng một test text-level. Kết thúc plan bằng
harness strict verify. Phụ thuộc Phase 4 (thứ tự chuỗi; dùng chung docs với mọi phase và `.agents/mcp_config.json` với Phase 1).

## Requirements
**R5.1 `.gitignore`**: dòng 32 `data/` → `/data/`; dòng 33 `models/` → `/models/`; xoá dòng 56-58 (dòng trống, comment "`data/` above is for
local market data …", `!harness/**/data/`) vì hết thừa; **không đụng** dòng 48-55 (block do installer sinh, `harness/install/_target_files.py:28`).
- **Mẫu artefact dữ liệu** (red-team r1 RT12): sau khi neo, thư mục dữ liệu lồng như `capture/data/` hết bị ignore (`git check-ignore -q
  capture/data/2026-10-21.jsonl.gz` hôm nay exit 0, sau khi neo exit 1 — OBSERVED 2026-10-21, `redteam-r1/clone`), mà repo là PUBLIC: một lần
  push dữ liệu vnstock có bản quyền không gỡ khỏi lịch sử được. Sau khi xoá dòng 56-58, chèn ngay sau dòng 36 (`*.safetensors`), tức ngoài block
  installer, các dòng sau (tiếng Anh, giữ nguyên):
  ```
  # Data artefacts anywhere in the tree (the /data/ anchor no longer catches nested data dirs)
  *.jsonl.gz
  *.parquet
  *.duckdb
  *.duckdb.wal
  *.sqlite
  *.sqlite3
  *.sqlite-wal
  *.sqlite-shm
  *.db
  *.db-wal
  *.db-shm
  ```
  Không thêm `!tests/fixtures/**` (chưa có fixture dữ liệu; một fixture sau này cần dòng `!` riêng cho đúng file). Không file tracked nào bị
  ảnh hưởng: `git ls-files | grep -E '(\.jsonl\.gz|\.parquet|\.duckdb|\.duckdb\.wal|\.sqlite3?|\.db|-wal|-shm)$'` → không hit (OBSERVED
  2026-10-21). Dòng 32-33 không dời (chèn sau dòng 36), nên kiểm (4) bên dưới giữ nguyên số dòng.
- Baseline OBSERVED 2026-10-21: `git status --porcelain --ignored` (bỏ `__pycache__`) = `?? codemap.yaml`, `?? plans/`, `!! .claude/`,
  `!! .harness/`, `!! .pytest_cache/`, `!! harness/state/`; `find . -type d \( -name data -o -name models \)` (bỏ `.git`, `.venv`) chỉ thấy thư mục
  dưới `harness/` và dưới `.claude/worktrees/` (đã ignore bởi `.git/info/exclude:7`); `git check-ignore -v src/finsec_alpharadar/models/x.py` →
  `.gitignore:33:models/` (đúng là lỗi cần sửa).
- Kiểm trước/sau: (1) `git status --porcelain --ignored | grep -v __pycache__ > <scratch>/before.txt` rồi `after.txt`, `diff` → không có mục
  `??` mới (sau Phase 1 sẽ thấy thêm `!! .venv/` và `!! src/finsec_alpharadar.egg-info/` ở **cả hai** bản — đúng kỳ vọng); (2) `git ls-files | wc -l`
  bằng nhau trước/sau; (3) `git check-ignore -v src/finsec_alpharadar/models/x.py` → không output, exit 1; (4) `git check-ignore -v data/x models/x`
  → `.gitignore:32:/data/` và `.gitignore:33:/models/`; (5) `git check-ignore -v harness/plugins/hs/data/new.yaml` → không output, exit 1.

**R5.2 `docker-compose.yml`** — nội dung đích (bản nháp đã probe, OBSERVED 2026-10-21 với Compose v5.4.0, chỉ `config`):
```yaml
services:
  qdrant:
    image: qdrant/qdrant:v1.19.2
    container_name: finsec-qdrant
    profiles: ["rag"]
    ports:
      - "127.0.0.1:6333:6333"
      - "127.0.0.1:6334:6334"
    volumes:
      - qdrant_data:/qdrant/storage

  timescaledb:
    image: timescale/timescaledb:2.31.0-pg16
    container_name: finsec-timescaledb
    profiles: ["ticks"]
    environment:
      POSTGRES_USER: finsec
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}
      POSTGRES_DB: alpharadar
    ports:
      - "127.0.0.1:5432:5432"
    volumes:
      - timescale_data:/var/lib/postgresql/data

volumes:
  qdrant_data:
  timescale_data:
```
- Bỏ khoá `version:` lỗi thời (hôm nay `docker compose config -q` in cảnh báo "the attribute `version` is obsolete", exit 0 — OBSERVED) và service
  `redpanda` (§9.9). Profile `ticks` = TimescaleDB (Phase 6), `rag` = Qdrant (Phase 7). Interpolation chạy trên toàn file kể cả profile chưa bật
  (research `ci-and-tooling.md` §5), nên mọi lệnh compose đều cần `POSTGRES_PASSWORD`.
- Probe bản nháp: `POSTGRES_PASSWORD=x docker compose config -q` → exit 0; `POSTGRES_PASSWORD= docker compose config -q` và khi unset → exit 1,
  "error while interpolating services.timescaledb.environment.POSTGRES_PASSWORD: required variable POSTGRES_PASSWORD is missing a value: set
  POSTGRES_PASSWORD in .env"; không profile → không service nào; `--profile ticks --profile rag config --services` → `qdrant`, `timescaledb`;
  mọi cổng render `host_ip: 127.0.0.1`. Repo không có `.env` (OBSERVED `ls`), nên ca âm tính không bị `.env` local che. Tag ảnh do main session
  cung cấp (2026-10-21); `config` không pull ảnh nên tồn tại của tag không được kiểm ở đây (`[ASSUMED]` tới lần `pull` đầu, ngoài plan).

**R5.3 `Makefile`**: `test:` → `python -m pytest -q` (pytest tự thêm `src` qua `pythonpath`); `run-api:` → `PYTHONPATH=src uvicorn
finsec_alpharadar.api.server:app --host 127.0.0.1 --port 8000 --reload`; `docker-up:` → `docker compose --profile ticks --profile rag up -d`;
`docker-down:` → `docker compose --profile ticks --profile rag down` (không profile thì `up` không khởi động gì — OBSERVED probe; với `down`,
việc cần profile là `[ASSUMED]`, truyền cho chắc). Giữ `verify:` và `finetune-dry-run:`; giữ `run-api` ở dòng 10.

**R5.4 `.env.example`**: xoá dòng 11 `SSI_PRIVATE_KEY_PATH=…` (FC Data không dùng private key, AGENTS.md:100); thêm
`SSI_API_URL=https://fc-data.ssi.com.vn/v2.0/Market` và `SSI_STREAM_URL=https://fc-datahub.ssi.com.vn/v2.0` (tên biến theo roadmap §5.2 dòng
250; chưa code nào đọc chúng); thêm `POSTGRES_PASSWORD=` (để trống — compose từ chối giá trị rỗng, fail-closed); `TIMESCALE_DSN=
postgresql://finsec@127.0.0.1:5432/alpharadar` (không mật khẩu trong DSN); `QDRANT_URL=http://127.0.0.1:6333`. Giữ dòng 4 (`ALPACA_PAPER_TRADE=true`)
và dòng 5 (`EDGAR_IDENTITY`) đúng chỗ (được docs trích). `ALPACA_DATA_FEED` và liên hệ thật cho `EDGAR_IDENTITY` là OUT.

**R5.5 `.agents/mcp_config.json`** (JSON không có comment; bảng SHA → tag/HEAD ghi vào AGENTS.md):
- `alpaca-us.args` → `["--from", "alpaca-mcp-server==2.4.0", "alpaca-mcp-server"]` (CLI 2.4.0 không có subcommand, research `mcp-pins.md` §2);
  `ALPACA_TOOLSETS` giữ giá trị Phase 1; `ALPACA_PAPER_TRADE` giữ `"true"`.
- `edgartools-us.args` → `["--from", "edgartools[ai]==5.62.0", "edgartools-mcp"]` (research §3).
- `maverick-quant.args[1]` → `git+https://github.com/wshobson/maverick-mcp@ba170996494a139a9c611ad0ec4d69cb692ac8a3` (= tag v1.1.0).
- `vnstock-vn.args[1]` → `git+https://github.com/mrgoonie/vnstock-agent.git@e7eaf08c5bea6f99556c5b66952b9700faf7cbbf` (= HEAD 2026-04-01).
- `ssi-fastconnect-vn.args[1]` → `git+https://github.com/archiephan78/ssi-stock-mcp-server.git@b93442adac835ff55048bba484b402b720bc79e1` (= HEAD
  2025-08-06); giữ `"disabled": true` (gói này phụ thuộc `ssi_fc_data`, một SSI client — §9.5).
- Giữ hậu tố `.git` ở chỗ đang có (thay đổi tối thiểu); dạng `git+https://…@<sha40>` là VERIFIED theo docs uv (research §1); việc uv chấp nhận SHA
  đủ 40 ký tự là `[ASSUMED]` (chưa chạy server lạ — cố ý).
- `playwright.args` → `["@playwright/mcp@0.0.83"]`; giữ `"disabled": true`.
- Xoá hẳn entry `wandb` (dòng 60-64; PyPI 404, repo không có tag/release ký — research §5).
- Entry `serverUrl` từ xa (`context7`, `github`, `huggingface`) không pin được — giữ, ghi chú trong AGENTS.md.

**R5.6 `.agents/rules/quant-and-market-rules.md` và `.agents/rules/db-and-xbrl-migrations.md`** (tiếng Anh; §9.13): giữ nguyên các khoá
frontmatter (`trigger`, `globs` / `trigger`, `description`; được sửa `description` cho đúng nội dung mới); thân file là con trỏ ngắn tới các mục
AGENTS.md ("Engineering Rules (binding)" → "No look-ahead", "VN", "No order execution anywhere", "Storage (§9.9)", "Point-in-time contract",
"Complexity gate"; luật biên độ D4 ở "Decisions"), câu "AGENTS.md wins where this file and AGENTS.md differ"; **bỏ** rule 3 (BM25 + dense + MaxSim
bắt buộc trong mọi `*.py`, `quant-and-market-rules.md:11`) và toàn bộ văn bản TimescaleDB hypertable / `DOUBLE PRECISION`
(`db-and-xbrl-migrations.md:5-7`). `.agents/agents/*.md`, `.agents/hooks.json` là OUT.

**R5.7 `tests/test_repo_hygiene.py` (mới, text-level, không PyYAML — không phải dependency khai báo)** — xem "Tests Before".

## Related Code Files
| Hành động | Tệp |
|---|---|
| Modify | `.gitignore` |
| Modify | `docker-compose.yml` |
| Modify | `.../Makefile` |
| Modify | `.env.example` |
| Modify | `.agents/mcp_config.json` |
| Modify | `.agents/rules/quant-and-market-rules.md` |
| Modify | `.agents/rules/db-and-xbrl-migrations.md` |
| Create | `tests/test_repo_hygiene.py` |
| Modify | `AGENTS.md` |
| Modify | `docs/system-architecture.md` |
| Modify | `docs/code-standards.md` |
| Modify | `docs/project-roadmap.md` |
| read | `plans/261021-1314-phase0b-foundations/research/mcp-pins.md` |
| read | `plans/261021-1314-phase0b-foundations/research/ci-and-tooling.md` |
| run | `docker compose config -q` (chỉ `config`, không bao giờ `up`) |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

Ghi chú parser: `.../Makefile` chính là `Makefile` ở gốc repo — tên không đuôi viết trần không được `_is_file_token`
(`harness/scripts/plan_graph.py:633-641`) nhận là file, còn dạng rút gọn `.../` thì được `_names` khớp với `Makefile` khai trong
`plan-graph.yaml`. `.env.example` có đuôi `example` không nằm trong `_FILE_EXTS` (`plan_graph.py:597-601`) nên không cách viết nào được nhận;
`plan_graph.py --require` vì vậy in một dòng advisory `phase-files: P5 declares .env.example …` — đã biết, không chặn (exit 0), file vẫn được khai
trong `files_to_modify` để quyền sở hữu file đầy đủ.

Challenge luật ">8 tệp": 12 tệp = 8 config/test + 4 docs. Bảy file cấu hình là các gạch đầu dòng của cùng một mục roadmap (0b.4) và cùng được khoá
bởi một test; tách ra chỉ nhân bản delta docs → giữ.

## Implementation Steps
1. **Baseline `.gitignore`** (R5.1 bước (1)-(2)) và **baseline `verify_install --strict`** (toàn bộ output, chạy trong đúng shell sẽ chạy
   regression gate — red-team r1 RT5); lưu vào scratchpad, không vào repo.
2. **Tests-before (RED).** Viết `tests/test_repo_hygiene.py`; chạy `.venv/Scripts/python -m pytest -q tests/test_repo_hygiene.py` → RED (lý do
   từng test dưới).
3. **Implement → xanh** (R5.1-R5.6), theo thứ tự `.gitignore` → compose → Makefile → `.env.example` → mcp_config → rules; chạy lại test sau mỗi file.
4. **Kiểm `.gitignore` sau** (R5.1 bước (1)-(5)).
5. **Gate compose (thủ công, chỉ config):** `POSTGRES_PASSWORD=x docker compose config -q` → exit 0; `POSTGRES_PASSWORD= docker compose config -q`
   → exit ≠ 0 với thông điệp "set POSTGRES_PASSWORD in .env"; `POSTGRES_PASSWORD=x docker compose --profile ticks --profile rag config --services`
   → `qdrant` + `timescaledb`. (Bản nháp cùng nội dung đã cho đúng các kết quả này — OBSERVED 2026-10-21.)
6. **JSON hợp lệ:** `python -c "import json; json.load(open('.agents/mcp_config.json', encoding='utf-8'))"` exit 0 (cũng được test kiểm).
7. **Delta docs + citation sweep** (dưới).
8. **Regression gate** (dưới, gồm harness strict verify) → commit duy nhất, ví dụ
   `chore: anchor gitignore, pin compose and MCP refs, bind localhost` + trailer `Co-Authored-By:`.
9. **Nghiệm thu toàn plan** (plan.md "Acceptance"): đếm test (≥ 30, 0 skipped trong `.venv`), chân 3.11, grep G1-G7, grep debt, ghi vào
   `verification-P5.json`.

### Delta docs (cùng commit; tiếng Anh; ghi ngày)
- **AGENTS.md**
  - "Decisions": dòng mới trên cùng:
    ```
    - 2026-10-21 — MCP config: the `wandb` entry is removed (not on PyPI, no signed release; Phase 7 at the earliest), and every `uvx`/`npx` server is pinned to an exact version or a 40-hex commit.
    ```
  - "Current Status" dòng 45: "the docker-compose services (Qdrant, TimescaleDB, Redpanda) are unused" → "the docker-compose services (Qdrant,
    profile `rag`; TimescaleDB, profile `ticks`) are unused; Redpanda was removed (<date>)".
  - "Current Status": bullet **Tests** — cập nhật `<N>` cuối cùng và ngày OBSERVED (đếm khi chạy regression gate trước commit), và ghi kết quả CI của lần push P4 (run id, OBSERVED — VL-1); cùng số đó ở
    `docs/code-standards.md` "Current state" (red-team r1 RT8); bước 9 kiểm số này khớp `verification-P5.json`.
  - "Verified Facts" dòng 109 (`.gitignore`): thay bằng dòng mới có ngày OBSERVED: "`/data/` and `/models/` are anchored to the repo root;
    data artefacts (`*.jsonl.gz`, `*.parquet`, `*.duckdb`, `*.sqlite*`, `*.db`) are ignored anywhere in the tree; `git check-ignore` no longer
    matches a `models/` package under `src/finsec_alpharadar/`." — test đường dẫn bỏ qua token bắt đầu bằng `/`
    và `models/` nằm trong `NOT_PATHS`; **không** backtick một path không tồn tại như `src/finsec_alpharadar/models/` (sau khi neo, nó không còn bị
    ignore nên `tests/test_agents_md_paths.py` sẽ báo thiếu).
  - "Verified Facts": thêm một dòng ghim MCP: "**MCP pins (OBSERVED 2026-10-21).** maverick-mcp `ba17099` = tag v1.1.0; vnstock-agent `e7eaf08`
    = HEAD of 2026-04-01; ssi-stock-mcp-server `b93442a` = HEAD of 2025-08-06; alpaca-mcp-server 2.4.0; edgartools 5.62.0; @playwright/mcp 0.0.83.
    Remote `serverUrl` entries cannot be pinned."
  - "Engineering Rules" dòng 131 ("Package names"): thêm ghi chú có ngày "(precondition met <date>: the patterns are anchored; `modeling/` stays
    the planned name)" — không đổi nội dung luật.
  - "Build & Verification Commands": thêm **Docker** (`POSTGRES_PASSWORD` must be set in `.env`; bash `docker compose --profile ticks --profile rag
    up -d`; PowerShell giống hệt), **API** đổi thành `--host 127.0.0.1` (dòng 148), dạng PowerShell cho smoke/API (`$env:PYTHONPATH='src'; …`),
    và ghi `make test` = `python -m pytest -q`.
- **docs/system-architecture.md**: dòng 13 (`make test` không còn gọi `pytest` trần — `Makefile:6-7`); dòng 34 (`docker-compose.yml`: Qdrant +
  TimescaleDB sau profile, Redpanda đã bỏ); dòng 116 (compose: tag pin, mật khẩu từ env, chỉ 127.0.0.1); dòng 120 (`make run-api` bind
  127.0.0.1 — bỏ câu cảnh báo mạng LAN); dòng 109 (`SSI_PRIVATE_KEY_PATH` đã bỏ khỏi `.env.example`); dòng 130 (rule `.agents/` đã là con trỏ, rule
  3 đã bỏ); dòng 149 (Key decisions: `.gitignore` đã neo — luật đặt tên đã thoả điều kiện).
- **docs/code-standards.md**: dòng 20 (bỏ trích `.agents/rules/db-and-xbrl-migrations.md:7`, văn bản đó đã xoá); dòng 41-42 (`.agents/`: rule đã
  là con trỏ; bỏ ví dụ rule 3); dòng 44 (Package names: patterns đã neo, OBSERVED `git check-ignore`; `modeling/` vẫn là tên dự kiến); dòng 52
  (Makefile: `make test` = `python -m pytest -q`); dòng 94 (bỏ câu `SSI_PRIVATE_KEY_PATH` `.env.example:11`); dòng 101 (xoá debt "unpinned MCP git
  refs, `:latest` images and a hard-coded password"); dòng 103 (Network exposure: đã bind 127.0.0.1).
- **docs/project-roadmap.md**: §7 0b mục 4 (dòng 382) thêm "Done <date> (Phase 0b plan, phase 5)"; dòng 393 ("fix `.agents/rules` rule 3 and the
  band rule") thêm "(done <date>)".

### Citation sweep (kiểm kê OBSERVED 2026-10-21)
`grep -nE "\.gitignore:[0-9]|docker-compose\.yml:[0-9]|Makefile:[0-9]|\.env\.example:[0-9]|mcp_config\.json:[0-9]|quant-and-market-rules\.md:[0-9]|db-and-xbrl-migrations\.md:[0-9]" AGENTS.md docs/system-architecture.md docs/code-standards.md docs/project-roadmap.md`
- `.gitignore`: code-standards:44 (`:32-33`), :90 (`:20`, `:26-29`); system-architecture:123 (`:20`), :149 (`:32-33`) — sửa trong dòng nên không dời.
- `docker-compose.yml:17` (system-architecture:116) — câu viết lại.
- `Makefile:10` (code-standards:103, system-architecture:81, :120), `Makefile:3-7` (code-standards:52), `Makefile:6-7` (system-architecture:13).
- `.env.example:4` (code-standards:92, system-architecture:124) và `:5` (code-standards:96, system-architecture:102) — giữ đúng dòng; `:11`
  (code-standards:94, system-architecture:109) và `:15` (system-architecture:116) — câu viết lại.
- `.agents/mcp_config.json:23` (system-architecture:102) — kiểm lại sau khi đổi `args` (sửa trong dòng, kỳ vọng không dời).
- `.agents/rules/quant-and-market-rules.md:11` (system-architecture:130), `.agents/rules/db-and-xbrl-migrations.md:7` (code-standards:20) — câu viết lại.

### Tests Before
`tests/test_repo_hygiene.py`:
- [x] `test_gitignore_patterns_are_anchored` — không dòng nào (strip) đúng bằng `data/` hoặc `models/`; có `/data/` và `/models/`; RED (dòng 32-33).
- [x] `test_src_models_package_is_not_ignored` — `git check-ignore -q src/finsec_alpharadar/models/x.py` trả 1 (exit khác 0/1 → fail); RED (hôm nay
      trả 0, OBSERVED).
- [x] `test_data_artefacts_are_ignored` (red-team r1 RT12) — `git check-ignore -q` trả 0 cho `capture/raw/2026-10-21.jsonl.gz`,
      `research/x.parquet`, `warehouse.duckdb`, `ops.sqlite`, `ops.db` (không nằm dưới thư mục `data/` nào) và cho `capture/data/2026-10-21.jsonl.gz`
      (đúng kịch bản RT12); exit khác 0/1 → fail; RED (hôm nay năm path đầu trả 1, path cuối trả 0 — OBSERVED 2026-10-21).
- [x] `test_compose_has_no_floating_tags_or_redpanda` — không `:latest`, không `redpanda`, không dòng `^version\s*:`; RED.
- [x] `test_compose_ports_bind_localhost` — mọi mục cổng dạng `- "<a>:<b>"`/`- "<ip>:<a>:<b>"` dưới `ports:` bắt đầu bằng `127.0.0.1:`; RED.
- [x] `test_compose_password_comes_from_env` — dòng `POSTGRES_PASSWORD:` có giá trị `${POSTGRES_PASSWORD:?…}`, không literal; RED
      (`docker-compose.yml:17`).
- [x] `test_env_example_has_no_private_key_or_inline_password` — không `SSI_PRIVATE_KEY_PATH`; `TIMESCALE_DSN` không khớp `://[^:@/]+:[^@]+@`; có
      `SSI_API_URL=https://fc-data.ssi.com.vn/v2.0/Market` và `SSI_STREAM_URL=https://fc-datahub.ssi.com.vn/v2.0`; RED.
- [x] `test_mcp_servers_are_pinned` — mọi server `command == "uvx"`: có một arg khớp `==\d` (spec PyPI) hoặc `@[0-9a-f]{40}$` (git); mọi server
      `command == "npx"`: arg gói khớp `@\d+\.\d+\.\d+$`; không arg nào kết thúc bằng `@latest`; không có khoá `wandb`; RED.
- [x] `test_makefile_binds_localhost` — dòng lệnh của `run-api` có `--host 127.0.0.1`, không có `0.0.0.0`; dòng lệnh của `test` chứa
      `python -m pytest`; RED (`Makefile:7,10`).

### Implement
- Bước 3 ở trên.

### Tests After
- [x] Toàn bộ `tests/test_repo_hygiene.py` xanh; `tests/test_no_order_execution.py` (Phase 1) vẫn xanh với mcp_config đã pin; `tests/test_agents_md_paths.py`
      xanh sau khi sửa AGENTS.md.
- [x] Kiểm `.gitignore` bước 4 và gate compose bước 5 cho đúng kết quả đã nêu.

### Regression Gate
- `.venv/Scripts/python -m pytest -q` (bash; PowerShell `.\.venv\Scripts\python -m pytest -q`) MUST pass, 0 skipped.
- `python -m pytest -q` (Python toàn cục) pass; skip đúng `tests/test_api_server.py`.
- `PYTHONPATH=src python tests/test_end_to_end.py` exit 0.
- `python3.13 harness/scripts/scaffold_standards.py --type system-architecture --check docs/system-architecture.md` và
  `python3.13 harness/scripts/scaffold_standards.py --type code-standards --check docs/code-standards.md` exit 0.
- `python3.13 harness/scripts/verify_install.py --root . --strict` → "verify_install OK: manifest + hook registration consistent", exit 0,
  không dòng `FAIL`/`DRIFT` nào. So WARN với baseline chụp **trong cùng shell** ở bước 1, sau khi lọc các dòng "not reachable on PATH", vì số
  WARN phụ thuộc PATH của shell (red-team r1 RT5; OBSERVED 2026-10-21: 4 WARN ở shell của planner — `hs-secrets`/`hs-usage` không có trên PATH,
  `component-policy.yaml` và `harness-hooks.yaml` lệch hash vì là cấu hình deployer-localized — và 8 WARN ở shell của red-team, thêm
  `hs-cli`/`hs-run`/`hs-code`/`hs-rops` "not reachable on PATH"). Không được có WARN mới sau khi lọc.
- Chân 3.11: `"$PY311/Scripts/python" -m pytest -q` (venv ngoài repo từ Phase 1, cài lại `-e ".[dev]"` nếu cần) pass.
- Sau commit: `git push` (draft PR tự chạy CI — VL-1); run CI của commit cuối này `success` trên 3.11 + 3.13
  (`gh run view <id> --json conclusion,jobs`). Chuyển PR sang "Ready for review" và merge (bằng merge commit — VL-2) là việc sau cook, chỉ
  khi có review-decision PASS.

## Success Criteria
- [x] RED ghi lại cho 9 test trước khi implement; sau implement tất cả xanh.
- [x] `diff before.txt after.txt` không có mục `??` mới; `git ls-files | wc -l` không đổi; `git check-ignore -v src/finsec_alpharadar/models/x.py`
      → exit 1.
- [x] Gate compose: exit 0 / exit ≠ 0 đúng thông điệp / 2 service khi bật cả hai profile.
- [x] `grep -n "wandb\|@latest" .agents/mcp_config.json` → không hit; `grep -n "0.0.0.0" Makefile docker-compose.yml` → không hit.
- [x] `verify_install --strict` exit 0, "verify_install OK", không `FAIL`/`DRIFT`; WARN (đã lọc "not reachable on PATH") không mới so với
      baseline bước 1.
- [x] Nghiệm thu toàn plan (plan.md "Acceptance") đã kiểm và ghi số liệu thật (số test, kết quả 3.11, grep G1-G7 rỗng, grep debt chỉ còn ghi chú lịch sử).

## Risk Assessment
| Rủi ro | Khả năng × Tác động | Xử lý |
|---|---|---|
| Neo `.gitignore` làm lộ/track nhầm file | Thấp × Trung bình | Baseline OBSERVED không có `data/`/`models/` ngoài `harness/`; diff trước/sau + đếm `git ls-files`; mẫu artefact dữ liệu theo đuôi file (R5.1, red-team r1 RT12) + `test_data_artefacts_are_ignored`; không `git add -A` — chỉ add đúng file trong bảng. |
| Pin MCP sai cú pháp làm server không khởi động | Trung bình × Thấp | Cú pháp theo docs uv (VERIFIED); SHA 40 ký tự `[ASSUMED]`; MCP chỉ dùng để khám phá, không phải nguồn dữ liệu pipeline (roadmap §5.2); test chỉ kiểm dạng pin. |
| Tag ảnh compose không tồn tại | Thấp × Thấp | Không service nào được dùng trước Phase 6/7; `config` không pull; lỗi sẽ hiện ở lần `pull` đầu. |
| Thiếu `.env` làm mọi lệnh compose lỗi | Trung bình × Thấp | Có chủ đích (fail-closed); `.env.example` có `POSTGRES_PASSWORD=` và AGENTS.md ghi cách đặt. |
| Viết lại rule `.agents/` làm công cụ agent khác mất ngữ cảnh | Thấp × Thấp | Con trỏ trỏ tới đúng mục AGENTS.md; quyết định §9.13. |
