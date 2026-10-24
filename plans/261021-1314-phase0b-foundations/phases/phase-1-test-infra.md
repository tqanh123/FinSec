---
phase: 1
title: "Test Infra"
plan: 261021-1314-phase0b-foundations
created: 2026-10-21
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Phase 1 — Test Infra

## Overview
Biến pytest từ 0 test (OBSERVED 2026-10-21: `python -m pytest -q -o pythonpath=src tests/` → "no tests ran", exit 5) thành một suite thật chạy
được trong `.venv`, từ Python toàn cục, và trên GitHub Actions (3.11 + 3.13); thêm hai guard test cho luật binding (không đặt lệnh, đường
dẫn AGENTS.md tồn tại); nâng `requires-python` lên `>=3.11` (D3); bỏ `trading`/`watchlists` khỏi MCP Alpaca (D5 phần 1); commit
`codemap.yaml` (D6). Phase đầu, không phụ thuộc phase nào; mọi phase sau dùng hạ tầng test của nó.

## Requirements
**Chức năng**
- R1.1 `pyproject.toml`:
  - dòng 10 `requires-python = ">=3.10"` → `requires-python = ">=3.11"` (giữ trên dòng 10);
  - thêm extra `dev = ["pytest==9.1.1", "hypothesis==6.170.5", "ruff==0.16.4"]` vào `[project.optional-dependencies]`, đặt **sau** `finetune`
    (dòng 28-32) để `pyproject.toml:10` và `pyproject.toml:25` (vnstock, được docs trích) không dời dòng;
  - thêm ở cuối file: `[tool.pytest.ini_options]` với `pythonpath = ["src"]` và `testpaths = ["tests"]` (bảng `ini_options` có từ pytest 6.0,
    `pythonpath` từ 7.0 — VERIFIED research `ci-and-tooling.md` §4). `testpaths` là bắt buộc: thiếu nó, `pytest` trần ở gốc repo sẽ đệ quy vào
    `harness/tests/` (DERIVED từ cách pytest thu thập thư mục).
  - Ghi chú phải giữ trung thực: chưa test nào import `hypothesis`; chưa có cấu hình/gate ruff (OUT).
- R1.2 `tests/test_end_to_end.py`: thêm `def test_end_to_end() -> None:` gọi `run_modular_verification()`, đặt giữa dòng 163 (cuối
  `run_modular_verification`) và `if __name__ == "__main__":` (dòng 166); giữ nguyên khối `__main__`. **Không thêm/xoá dòng nào trong dòng
  1-163** — docs trích các dòng đó (xem citation sweep).
- R1.3 `tests/test_no_order_execution.py` (mới, chỉ đọc file, không mạng):
  - (a) không file nào trong phạm vi quét khớp (không phân biệt hoa thường) `TradingClient`, `submit_order`, `alpaca\.trading`,
    `place_\w*order`, `fc[\s_-]?trading`, `(paper-)?api\.alpaca\.markets` (host REST giao dịch; host dữ liệu `data.alpaca.markets` không khớp),
    `/v2/orders`, `/v2/positions`; thông điệp lỗi liệt kê `file:line` khớp. Phạm vi quét: `*.py` dưới `src/`; `*.py` và `*.sh` dưới `scripts/`;
    `*.py` dưới `tests/` **trừ** chính `tests/test_no_order_execution.py` (file chứa các mẫu dưới dạng literal). Lý do mở rộng (red-team r1
    RT3): `httpx.post("https://paper-api.alpaca.markets/v2/orders", …)` lọt qua 5 mẫu đầu (OBSERVED 2026-10-21, `redteam-r1/scripts/guard_spec.py`).
    Baseline: `grep -rniE` đủ 8 mẫu trên `src/ scripts/ tests/` → 0 hit, exit 1 (OBSERVED 2026-10-21).
  - (b) `.agents/mcp_config.json` (đọc bằng `json`), áp cho **mọi** entry của `mcpServers` có `command` hoặc một phần tử `args` chứa
    `alpaca-mcp-server` (hôm nay chỉ `alpaca-us`, OBSERVED `grep -n -i alpaca .agents/mcp_config.json`; một entry thứ hai không có
    `ALPACA_TOOLSETS` sẽ bật mọi toolset — red-team r1 RT3), và phải có ít nhất một entry như vậy (để test không xanh rỗng khi gói đổi tên):
    `env["ALPACA_TOOLSETS"]` sau khi tách dấu phẩy + strip phải
    **không rỗng** (rỗng/thiếu = bật TẤT CẢ toolset, kể cả `trading` — research `mcp-pins.md` §2, VERIFIED `server.py:128-132` của
    alpaca-mcp-server 2.4.0); không chứa `trading`, `watchlists`, `account`, `locates` (cả bốn có thao tác ghi — research `mcp-pins.md:28`);
    mọi tên thuộc 11 tên hợp lệ của 2.4.0 (`account`,
    `trading`, `watchlists`, `assets`, `stock-data`, `crypto-data`, `options-data`, `corporate-actions`, `news`, `fixed-income-data`, `locates`
    — tên sai bị server bỏ qua im lặng, research §2); `env["ALPACA_PAPER_TRADE"] == "true"`. Thêm: không entry nào có `serverUrl` chứa
    `alpaca` (server từ xa không pin được, không kiểm được toolset); `mcpServers["ssi-fastconnect-vn"]["disabled"] is True`.
  - (c) `.env.example` có đúng dòng `ALPACA_PAPER_TRADE=true` (hiện ở `.env.example:4`).
- R1.4 `tests/test_agents_md_paths.py` (mới):
  - lấy token trong backtick không có khoảng trắng từ `AGENTS.md`; bỏ hậu tố `:<dòng>` (regex `:\d[\d,\-]*$`); bỏ token bắt đầu bằng `http`
    hoặc `$`; **bỏ token bắt đầu bằng `/`** (mẫu gitignore neo gốc như `/data/` mà Phase 5 có thể viết, không phải path tương đối — tinh chỉnh
    so với spike của main); chỉ giữ token khớp `^[\w.\-/]+(\.(py|md|yaml|yml|json|toml|sh)|/)$`;
  - dict `NOT_PATHS` tường minh, mỗi mục có lý do: `data/`, `models/` (tên package bị cấm — văn bản luật), `modeling/` (package dự kiến,
    roadmap §5.2, chưa tạo); dict `ALIASES`: `disabled-skills/` → `harness/plugins/hs/disabled-skills/`;
  - phân giải theo thứ tự: gốc repo → `src/finsec_alpharadar/` → với basename trần (không có `/`) glob `src/finsec_alpharadar/*/<name>`;
  - không tìm thấy → `git check-ignore -q <token>` (cwd = gốc repo): exit 0 = path runtime theo clone (vd `harness/state/`,
    `.claude/settings.json`, sau này `.venv/`) → bỏ qua; exit 1 → **thiếu**; exit khác (git lỗi) → fail kèm stderr, không được skip im lặng;
  - một assert duy nhất, thông điệp liệt kê **mọi** token thiếu.
  - Kỳ vọng hôm nay: XANH. OBSERVED 2026-10-21 (spike cùng luật, script scratchpad `paths_spike.py`): 30 token dạng path, 0 thiếu; 1 token bỏ
    qua do ignore (`.claude/settings.json`); `harness/state/` tồn tại ở local nhưng trên clone CI sẽ đi nhánh ignore (`.gitignore:49`).
- R1.5 `.github/workflows/ci.yml` (mới) — nội dung chính xác (research `ci-and-tooling.md` §6, ma trận theo D3):
  ```yaml
  name: ci
  on:
    push:
      branches: [main]
    pull_request:
  permissions:
    contents: read
  concurrency:
    group: ci-${{ github.ref }}
    cancel-in-progress: true
  jobs:
    test:
      runs-on: ubuntu-24.04
      timeout-minutes: 15
      strategy:
        fail-fast: false
        matrix:
          python-version: ["3.11", "3.13"]
      steps:
        - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
          with:
            persist-credentials: false
        - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
          with:
            python-version: ${{ matrix.python-version }}
        - run: python -m pip install -e ".[dev]"
        - run: python -m pytest -q
  ```
  OBSERVED 2026-10-21: PyYAML parse bản nháp này → `matrix: ['3.11', '3.13']` (kiểu `str`), `permissions: {'contents': 'read'}`, 4 step đúng
  thứ tự. Parser của GitHub không phải PyYAML → cú pháp trên runner thật là `[ASSUMED]` tới lần chạy đầu: draft PR ở bước 12 (VL-1). Push
  nhánh một mình không kích hoạt CI (`on.push.branches: [main]`), nhưng mỗi push lên nhánh của một PR đang mở (kể cả draft) thì có — sự kiện
  `pull_request` mặc định gồm `opened`/`synchronize`/`reopened` [PRIOR], được kiểm ở bước 12. `ubuntu-24.04` cố định vì `ubuntu-latest` chuyển dần sang 26.04 từ 2026-10-19 tới 2026-11-19
  (VERIFIED research §1). Không `pull_request_target`, không secret.
- R1.6 `.agents/mcp_config.json:16` → `"ALPACA_TOOLSETS": "stock-data,options-data,corporate-actions,news"` — chỉ sửa dòng đó (pin và xoá
  `wandb` để Phase 5).
- R1.7 Commit `codemap.yaml` nguyên trạng (17 dòng, OBSERVED `wc -l`), không sửa nội dung.
- R1.8 Môi trường local (không phải file repo): `.venv` bằng Python toàn cục 3.13.14 + `pip install -e ".[dev]"`; một lần chạy `/hs:test` thật
  với venv được kích hoạt; một lần chạy chân 3.11 trong venv nằm ngoài repo.
- R1.9 Delta docs trong cùng commit (mục "Delta docs" dưới).

**Phi chức năng**
- Test chỉ đọc file và gọi `git check-ignore`; không mạng, không đồng hồ, không sleep; chạy được trên Windows và Ubuntu (dùng `pathlib`,
  `encoding="utf-8"`).
- Mỗi module test mới mở đầu bằng `from __future__ import annotations` và có type hint đầy đủ, kể cả `-> None` (code-standards "Languages").
- Không đụng file nào dưới `harness/` (`.gitattributes:4`).
- `.github/workflows/ci.yml`, các test mới và `codemap.yaml` không bị ignore: `git check-ignore -v .github/workflows/ci.yml codemap.yaml
  tests/test_vn_price_band.py` → không output, exit 1 (OBSERVED 2026-10-21; cook chạy lại với đúng tên file mới).

## Related Code Files
| Hành động | Tệp |
|---|---|
| Modify | `pyproject.toml` |
| Modify | `tests/test_end_to_end.py` |
| Create | `tests/test_no_order_execution.py` |
| Create | `tests/test_agents_md_paths.py` |
| Create | `.github/workflows/ci.yml` |
| Modify | `.agents/mcp_config.json` |
| Create | `codemap.yaml` |
| Modify | `AGENTS.md` |
| Modify | `docs/code-standards.md` |
| Modify | `docs/system-architecture.md` |
| Modify | `docs/project-roadmap.md` |
| read | `plans/261021-1314-phase0b-foundations/research/ci-and-tooling.md` |
| read | `plans/261021-1314-phase0b-foundations/research/mcp-pins.md` |
| run | `.venv/` (tạo bằng `python -m venv .venv`; gitignored `.gitignore:21`) |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

Challenge luật ">8 tệp": 11 tệp = 7 code/config + 4 docs. Bốn file docs là delta bắt buộc của luật đồng bộ debt (`docs/code-standards.md:112`);
tách chúng ra một phase riêng sẽ để một commit sửa debt mà docs vẫn ghi debt đó → giữ nguyên.

## Implementation Steps
0. **Nơi chạy (red-team r1 RT4).** Cook chạy ở main checkout `H:\study\Project\FinSec`, không dùng worktree (lý do ở plan.md "Nơi chạy
   cook"): `git switch -c claude/phase0b-foundations` từ `main` @ `7092637`; `plans/` và `codemap.yaml` là untracked nên đi theo nhánh mới.
   Kiểm: `git branch --show-current` → `claude/phase0b-foundations`; `git status --porcelain` → chỉ `?? codemap.yaml` và `?? plans/`.
1. **Kiểm trước (chỉ đọc).** `git check-ignore -v .github/workflows/ci.yml tests/test_no_order_execution.py tests/test_agents_md_paths.py
   codemap.yaml` → kỳ vọng không output, exit 1. `git check-ignore -v .venv/ src/finsec_alpharadar.egg-info/` → kỳ vọng
   `.gitignore:21:.venv/` và `.gitignore:12:*.egg-info/` (OBSERVED 2026-10-21).
2. **RED baseline.** `python -m pytest -q -o pythonpath=src tests/` → "no tests ran", exit 5 (OBSERVED 2026-10-21).
3. **Tests-before.** Viết `tests/test_no_order_execution.py`, `tests/test_agents_md_paths.py` và wrapper `test_end_to_end()` (R1.2-R1.4).
   Chạy bằng Python toàn cục **trước** khi sửa `pyproject.toml`: `python -m pytest -q -o pythonpath=src tests/` → kỳ vọng đúng 1 test đỏ: kiểm
   toolset (b) fail vì `.agents/mcp_config.json:16` chứa `watchlists` và `trading`; mọi test khác xanh. Ghi lại dòng lỗi thật.
4. **Implement → xanh.** Sửa `.agents/mcp_config.json:16` (R1.6) → chạy lại lệnh bước 3 → tất cả xanh. Sửa `pyproject.toml` (R1.1).
5. **`.venv`.** `python -m venv .venv` rồi `.venv/Scripts/python -m pip install -e ".[dev]"` (PowerShell: `python -m venv .venv`;
   `.\.venv\Scripts\python -m pip install -e ".[dev]"`) — cần mạng; chờ bước 4 (extra `dev` chưa tồn tại thì pip chỉ cảnh báo và exit 0 —
   OBSERVED research §2.4). Sau đó `.venv/Scripts/python -m pytest -q` → tất cả xanh, 0 skipped. Bằng chứng tương đương đã có: bản copy repo ở
   scratchpad với đúng extra `dev` và `ini_options` dự kiến, `uv pip install -e "proj[dev]"` → exit 0, `python -m pytest -q` → "3 passed" trên
   3.11.16 và 3.13.14, cài được fastapi 0.156.4 / hypothesis 6.170.5 / pytest 9.1.1 (OBSERVED 2026-10-21).
6. **Chân 3.11 local** (venv ngoài repo, vì `.venv-3.11/` KHÔNG bị ignore): với `PY311` là một thư mục trong scratchpad của session,
   `uv venv -q --python 3.11 "$PY311"`; `uv pip install -q --python "$PY311" -e ".[dev]"`; `"$PY311/Scripts/python" -m pytest -q` → xanh.
   (Cùng dạng lệnh đã chạy thành công trong probe scratch, OBSERVED 2026-10-21; `uv pip install -e` từ repo ghi `src/finsec_alpharadar.egg-info/`,
   đã ignore.)
7. **CI.** Tạo `.github/workflows/ci.yml` đúng nội dung R1.5. Lần chạy đầu trên GitHub là ở bước 12 (draft PR, VL-1).
8. **`/hs:test` thật.** Trong **một** lệnh bash: `source .venv/Scripts/activate && pytest -q` (`next_command` của `hs-run test next` là
   `pytest`, OBSERVED 2026-10-21). Ghi kết quả (pass, hoặc lỗi chính xác). `[ASSUMED]` cho tới lần chạy này: venv được kích hoạt làm `pytest`
   trần chạy được. Nếu hỏng: gate ràng buộc vẫn là `.venv/Scripts/python -m pytest -q`; ghi lỗi vào verification và vào dòng Build của AGENTS.md.
9. **`git add codemap.yaml`** (nguyên trạng).
10. **Delta docs + citation sweep** (mục dưới).
11. **Regression gate** (mục dưới) → commit duy nhất, ví dụ `chore: add pytest config, CI workflow and guard tests` + trailer
    `Co-Authored-By:` theo `docs/code-standards.md:107`.
12. **Push + draft PR (VL-1, owner chốt 2026-10-21).** Sau commit ở bước 11 và khi đã có `verification-P1.json` (stage `push` cần
    `verification`): `git push -u origin claude/phase0b-foundations`, rồi `gh pr create --draft --base main --head claude/phase0b-foundations`
    với tiêu đề tiếng Anh và body kết thúc bằng dòng `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Stage `pr` còn đòi
    `review-decision` + `plan-approval` (`harness/data/stage-policy.yaml:62-64`); ở local đó chỉ là advisory (`stage-policy.yaml:51-57`, hooks
    OFF), nên draft **không** phải PR sẵn sàng merge — chỉ chuyển "Ready for review" và merge sau review-decision PASS. Sau đó
    `gh run list --branch claude/phase0b-foundations --limit 1` → `gh run watch <id>` → `gh run view <id> --json conclusion,jobs` → kỳ vọng
    `success` với 2 job (3.11, 3.13). Ghi run id + kết quả (OBSERVED) vào báo cáo cook của Phase 1. Đỏ → sửa bằng commit nhỏ trong Phase 1,
    push lại, và chỉ sang Phase 2 khi CI xanh.

### Delta docs (cùng commit; tiếng Anh; ghi ngày mọi dòng AGENTS.md được thêm/sửa; không sửa dòng Decisions cũ)
- **AGENTS.md**
  - "Decisions": thêm dòng mới **trên cùng** (sau dòng 10), không backtick đường dẫn `plans/…` (chưa track — test đường dẫn sẽ đỏ trên CI):
    ```
    - 2026-10-21 — Phase 0b foundations (plan 261021-1314-phase0b-foundations), owner choices:
      - CI: a GitHub Actions workflow (`.github/workflows/ci.yml`) runs pytest on Python 3.11 and 3.13; the repository is public.
      - `requires-python` is `>=3.11` (Python 3.10 reached end of life on 2026-10-01).
      - The Alpaca MCP server runs without the `trading` and `watchlists` toolsets (`watchlists` has write operations).
      - `codemap.yaml` (the hs-code map) is committed.
    ```
  - "Current Status" → "Known bugs": xoá dòng 52 `- pytest collects 0 tests.`; thêm một bullet cấp 1 sau khối "Works" (sau dòng 39):
    `- **Tests (OBSERVED <date>).** pytest collects <N> tests from `tests/`; all pass in `.venv` (Python 3.13) and on 3.11. `.github/workflows/ci.yml` runs them on 3.11 and 3.13 for pull requests and pushes to `main`.` (kết quả CI chỉ có sau khi push ở bước 12 — phase sau ghi nó vào bullet này, xem delta "Tests" của P2-P5).
  - "Build & Verification Commands" (dòng 143-151): xoá dòng 147 ("pytest collects 0 tests until Phase 0 adds a test wrapper."); thêm bullet
    **Dev environment** (bash `python -m venv .venv` + `.venv/Scripts/python -m pip install -e ".[dev]"`; PowerShell `python -m venv .venv` +
    `.\.venv\Scripts\python -m pip install -e ".[dev]"`) và **Tests** (bash `.venv/Scripts/python -m pytest -q`; PowerShell
    `.\.venv\Scripts\python -m pytest -q`; `/hs:test` runs bare `pytest`, so activate the venv first in the same shell —
    `source .venv/Scripts/activate` / `.\.venv\Scripts\Activate.ps1` — kèm kết quả OBSERVED của bước 8); thêm **CI** (`.github/workflows/ci.yml`,
    pull requests and pushes to `main`); dòng 148: thay "fastapi is not installed on the dev machine yet." bằng "fastapi installs into `.venv`
    with the dev setup.".
  - "Verified Facts": thêm `- **Python 3.10** reached end of life on 2026-10-01. VERIFIED https://devguide.python.org/versions/`
- **docs/code-standards.md**
  - dòng 3: "Last checked against the code" → ngày commit.
  - dòng 10 "Version": `requires-python = ">=3.11"` (`pyproject.toml:10`); bỏ câu "New code must still run on 3.10 …"; ghi `datetime.UTC`,
    `enum.StrEnum`, `typing.Self`, `tomllib` được phép (OBSERVED 2026-10-21: import cả bốn thành công trên 3.11.16).
  - dòng 27 "Style tooling": `pyproject.toml` nay có `[tool.pytest.ini_options]` và extra `dev` (pytest, hypothesis, ruff pin); vẫn chưa có cấu
    hình ruff/black/mypy và chưa có lint gate.
  - dòng 30 "`src/` layout": pytest tự thêm `src` qua `pythonpath`; `.venv` cài editable; script smoke ngoài venv vẫn cần `PYTHONPATH=src`;
    cập nhật trích dẫn `pyproject.toml:34-35` sang dòng mới của `[tool.setuptools.packages.find]`.
  - dòng 35 (`api/server.py` — "fastapi is not installed on the dev machine, so it is not importable there") → "fastapi installs into `.venv`
    with the dev setup; the global Python may still lack it" (red-team r1 RT8).
  - dòng 49-52 "Current state" (thay bằng số test thật, lệnh venv, CI), dòng 53-56 "Commands" (lệnh venv bash + PowerShell; `/hs:test` cần
    venv kích hoạt), dòng 57 "Planned": bỏ các mục đã có (`pythonpath`, wrapper, `test_no_order_execution.py`, `test_agents_md_paths.py`),
    giữ `tests/test_edgar_time.py` (0a) và test Phase 1 roadmap.
  - dòng 92 "No order execution": bỏ câu debt "`.agents/mcp_config.json:16` still lists `trading` …" và "A guard test is planned …" → "Enforced
    by `tests/test_no_order_execution.py`".
  - dòng 98 "Dependencies": "(`prod`, `finetune` exist; no `dev` extra yet)" → "(`dev`, `prod`, `finetune`)".
  - dòng 109 "Branches": "there is no `.github/` CI, so the local gate is `/hs:test`" → CI = `.github/workflows/ci.yml`; local gate vẫn là `/hs:test`.
- **docs/system-architecture.md**
  - dòng 3: ngày "Checked against the code".
  - dòng 10: `requires-python >=3.10` → `>=3.11`.
  - dòng 30 (As-built, `api/server.py`): câu "fastapi is not installed on the dev machine yet (…)" → "fastapi installs into `.venv` with the dev
    setup" (red-team r1 RT8); phần còn lại của dòng để Phase 2 sửa.
  - dòng 13: thay câu mô tả "0 tests / exit 5 / collection errors" bằng trạng thái thật (số test, `pythonpath` trong `pyproject.toml`, CI); câu về
    `make test` (`Makefile:6-7`) giữ nguyên tới Phase 5.
  - dòng 124: "A test enforcing this (`tests/test_no_order_execution.py`) is planned for Phase 0." → "`tests/test_no_order_execution.py`
    enforces this (since <date>)."
  - dòng 134 (Known gaps): xoá "`.agents/mcp_config.json:16` still lists `trading` in `ALPACA_TOOLSETS`."
- **docs/project-roadmap.md**: §7 0b mục 3 (dòng 377) thêm "Done <date> (Phase 0b plan, phase 1): GitHub Actions workflow, because a public
  GitHub remote exists."

### Citation sweep (file Phase 1 sửa mà docs có trích dòng)
`grep -nE "pyproject\.toml:[0-9]|mcp_config\.json:[0-9]|test_end_to_end\.py:[0-9]|test:[0-9]" AGENTS.md docs/system-architecture.md docs/code-standards.md docs/project-roadmap.md`
rồi đối chiếu từng hit với file sau khi sửa. Danh sách đã kiểm kê (OBSERVED 2026-10-21):
- `pyproject.toml:10` (code-standards:10, system-architecture:10) — giữ dòng 10; `pyproject.toml:25` (code-standards:99, system-architecture:113)
  — giữ nếu `dev` đặt sau `finetune`; `pyproject.toml:34-35` (code-standards:30) — **dời** → sửa.
- `.agents/mcp_config.json:16` (code-standards:92, system-architecture:134) — các câu đó bị xoá; `.agents/mcp_config.json:23`
  (system-architecture:102) — không dời (sửa trong dòng 16).
- `tests/test_end_to_end.py:…` / `test:…` (code-standards:21,22,61,104; system-architecture:25,29,73-77,115; roadmap:239) — không dời nếu tôn
  trọng R1.2.

### Tests Before
- [x] `test_end_to_end` (wrapper) — khoá hành vi smoke hiện có; chạy → PASS (lock).
- [x] `test_code_has_no_order_execution` — khoá luật "No trading clients in `src/`" trên phạm vi R1.3(a) (`src/`, `scripts/`, `tests/`, 8 mẫu);
      chạy → PASS (lock; grep baseline 0 hit).
- [x] `test_alpaca_toolsets_are_read_only` (mọi entry `alpaca-mcp-server`, có ≥ 1 entry; + kiểm tên hợp lệ, không rỗng) — luật "No `trading`
      MCP toolset" + D5; chạy → **FAIL** vì `.agents/mcp_config.json:16` có `watchlists,trading`.
- [x] `test_no_remote_alpaca_mcp_server` (không `serverUrl` nào chứa `alpaca`) — chạy → PASS (lock).
- [x] `test_alpaca_paper_trade_flag_is_true` (mọi entry `alpaca-mcp-server`), `test_ssi_mcp_server_is_disabled`, `test_env_example_keeps_paper_trade` — khoá phòng thủ
      chiều sâu; chạy → PASS (lock).
- [x] `test_agents_md_paths_exist` — khoá "mọi path AGENTS.md mô tả là có thật"; chạy → PASS (spike OBSERVED 0 thiếu).

### Implement
- Bước 4-9 ở trên.

### Tests After
- [x] Toàn bộ test trên xanh trong `.venv`, từ Python toàn cục và trên chân 3.11.
- [x] `test_agents_md_paths_exist` vẫn xanh **sau** khi sửa AGENTS.md ở bước 10 (bắt token mới như `.github/workflows/ci.yml`, `.venv/`).

### Regression Gate
- `.venv/Scripts/python -m pytest -q` (bash; PowerShell `.\.venv\Scripts\python -m pytest -q`) MUST pass — chờ bước 5.
- `PYTHONPATH=src python tests/test_end_to_end.py` exit 0 — OBSERVED 2026-10-21 trên base: exit 0.
- `python -m pytest -q` (Python toàn cục) pass — Phase 1 chưa có test fastapi nên không có skip.
- Sau khi sửa docs: `python3.13 harness/scripts/scaffold_standards.py --type system-architecture --check docs/system-architecture.md` và
  `python3.13 harness/scripts/scaffold_standards.py --type code-standards --check docs/code-standards.md` — OBSERVED 2026-10-21 trên base: in
  "conform: docs\system-architecture.md matches the system-architecture section-set" / "conform: docs\code-standards.md matches the
  code-standards section-set", exit 0.

## Success Criteria
- [x] Bước 3 ghi được đúng 1 test đỏ (kiểm toolset) với thông điệp nêu `trading`/`watchlists`; sau bước 4 thì xanh.
- [x] `.venv/Scripts/python -m pytest -q` → `<N> passed`, 0 failed, 0 skipped, N ≥ 8 (1 smoke + 6 order-execution + 1 path; ghi N thật).
- [x] `python -m pytest -q` (toàn cục) và `"$PY311/Scripts/python" -m pytest -q` → cùng N passed.
- [x] Kết quả `/hs:test` (bước 8) được ghi vào verification (pass, hoặc lỗi + fallback).
- [x] `PYTHONPATH=src python tests/test_end_to_end.py` exit 0; hai standards check exit 0.
- [x] `grep -n '"ALPACA_TOOLSETS"' .agents/mcp_config.json` → `"stock-data,options-data,corporate-actions,news"`.
- [x] `grep -n 'python-version: \["3.11", "3.13"\]' .github/workflows/ci.yml` → 1 hit; `grep -c "@[0-9a-f]\{40\} # v" .github/workflows/ci.yml` → 2.
- [x] Sau commit: `git ls-files codemap.yaml .github/workflows/ci.yml tests/test_no_order_execution.py tests/test_agents_md_paths.py` liệt
      kê đủ 4 file; `git show --stat HEAD -- codemap.yaml` cho thấy file được thêm (17 dòng).
- [x] `grep -n "pytest collects 0" AGENTS.md docs/system-architecture.md docs/code-standards.md` → không hit.
- [x] Draft PR đã mở (`gh pr view --json isDraft` → `true`); run CI đầu tiên `success` với 2 job (`gh run view <id> --json conclusion,jobs`) — VL-1.

## Risk Assessment
| Rủi ro | Khả năng × Tác động | Xử lý |
|---|---|---|
| `/hs:test` không thấy `pytest` (shell không giữ trạng thái giữa các lệnh) | Cao × Thấp | Kích hoạt venv và chạy `pytest` trong cùng một lệnh; gate thật là `.venv/Scripts/python -m pytest -q`; ghi kết quả. |
| Lần chạy CI đầu tiên lỗi (YAML/runner) | Trung bình × Trung bình | Bước 5-6 chạy đúng lệnh CI trên 3.13 và 3.11 trước; action pin SHA; draft PR ở bước 12 (VL-1) cho CI chạy thật ngay ở phase đầu; nhãn `[ASSUMED]` tới run đó; sửa trong một commit follow-up nhỏ nếu cần. |
| Test đường dẫn AGENTS.md đỏ do token mới hoặc do path chỉ có ở local | Trung bình × Thấp | Luật bỏ qua (ignore, `/`, NOT_PATHS có lý do); không backtick `plans/…`; chạy lại sau mỗi lần sửa AGENTS.md. |
| `pip install -e ".[dev]"` lỗi mạng | Thấp × Thấp | Chạy lại; không file nào được commit phụ thuộc vào bước này. |
| Nâng `>=3.11` chặn người dùng 3.10 | Thấp × Thấp | Quyết định D3 của owner; máy dev là 3.13.14. |
