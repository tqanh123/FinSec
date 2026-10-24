# Research: CI và tooling (Phase 0b, phase 1 + phase 5)

Ngày: 2026-10-21. Probe chạy trong `scratchpad/research-probe/` (không đụng repo). Nhãn: VERIFIED (URL) / OBSERVED (ngày, lệnh) / ASSUMED / PRIOR.

## 1. setup-python v7.0.0 + checkout v7.0.1 trên ubuntu-latest

- Cả hai action chạy `runs.using: node24`. VERIFIED tại https://raw.githubusercontent.com/actions/checkout/3d3c42e5aac5ba805825da76410c181273ba90b1/action.yml (dòng 115-116) và https://raw.githubusercontent.com/actions/setup-python/5fda3b95a4ea91299a34e894583c3862153e4b97/action.yml (dòng 41-42).
- Runner tối thiểu: "Updated to the node24 runtime — This requires a minimum Actions Runner version of v2.327.1" (README checkout tại SHA đã pin, dòng 24-25); setup-python README dòng 20-21 cùng mức v2.327.1. VERIFIED https://raw.githubusercontent.com/actions/checkout/3d3c42e5aac5ba805825da76410c181273ba90b1/README.md và https://github.com/actions/setup-python/releases/tag/v6.0.0. Runner GitHub-hosted luôn mới nên không phải lo; chỉ self-hosted mới cần kiểm tra (ASSUMED).
- checkout v7.0.0 (2026-06-18) **chặn checkout code fork** khi workflow dùng `pull_request_target`/`workflow_run`, trừ khi đặt `allow-unsafe-pr-checkout`. Trigger `pull_request` thường thì không bị ảnh hưởng. VERIFIED https://github.com/actions/checkout/releases/tag/v7.0.0 và README dòng 7.
- setup-python v7.0.0 (2026-07-20) có ghi "Remove EOL Python versions", nhưng PR #1333 chỉ sửa ma trận test của chính action (`.github/workflows/*`, `__tests__/data/*`), không chặn cài 3.10. VERIFIED https://github.com/actions/setup-python/pull/1333 (đọc bằng `gh pr view`).
- **3.10 cài được.** `versions-manifest.json` có `3.10.22`, `stable: true`, cho linux `22.04`/`24.04`/`26.04` (x64 và arm64). VERIFIED https://raw.githubusercontent.com/actions/python-versions/main/versions-manifest.json (lọc bằng `python -I`, 2026-10-21). Chưa chạy setup-python trên runner thật.
- `ubuntu-latest` hiện là `ubuntu-24.04` (README runner-images dòng 25). VERIFIED https://github.com/actions/runner-images. **Nhưng nó đang chuyển sang 26.04**: "rolled out over a period of several weeks beginning October 19, 2026 … complete the migration by November 19, 2026". VERIFIED https://github.com/actions/runner-images/issues/14748. Cả hai image đều cache sẵn Python `3.10.22, 3.11.17, 3.12.15, 3.13.16, 3.14.8`. VERIFIED Ubuntu2404-Readme.md (image 20261012.334.1) và Ubuntu2604-Readme.md, cùng repo.
- YAML: phải viết `"3.10"` có ngoặc. OBSERVED 2026-10-21: PyYAML `safe_load('v: 3.10')` → `3.1`. Parser YAML của GitHub cũng xử lý như vậy (PRIOR). Ví dụ chính thức đều quote: `['3.9', '3.10', '3.11']` (setup-python `docs/advanced-usage.md` dòng 263).

## 2. Phát hiện mâu thuẫn với giả định của plan (quan trọng)

1. **Python 3.10 đã EOL**, "3.10 | end-of-life | 2021-10-04 | 2026-10-01". VERIFIED https://devguide.python.org/versions/.
2. **`hypothesis 6.170.5` yêu cầu `>=3.11`.** Hypothesis bỏ 3.10 từ `6.169.0` (2026-10-11); bản cuối hỗ trợ 3.10 là `6.168.5` (2026-10-05). VERIFIED https://pypi.org/pypi/hypothesis/json (`requires_python` theo từng release). OBSERVED 2026-10-21: `pip install hypothesis==6.170.5` trên 3.10.21 báo "No matching distribution … Requires-Python >=3.11". → Pin "đã chốt" 6.170.5 hỏng trên leg 3.10.
3. `pytest 9.1.1` có `requires_python >=3.10`, nên chạy được trên 3.10. OBSERVED 2026-10-21 (PyPI JSON; cài và chạy được trên 3.10.21).
4. **`pyproject.toml` chưa có extra `dev`.** `pip install -e ".[dev]"` **không lỗi**: nó in `WARNING: finsec-alpharadar 0.1.0 does not provide the extra 'dev'` và exit 0, rồi bước `pytest` sau đó mới hỏng. OBSERVED 2026-10-21, pip 26.2.2 trên Python 3.10.21 với bản copy của repo. → Phase 1 phải thêm `[project.optional-dependencies].dev`.
5. **FastAPI `TestClient` cần `httpx2` (hoặc `httpx`), mà cả hai đều không nằm trong dependencies.** Starlette 1.7.0 (đi kèm fastapi 0.156.4) thử `import httpx2`, rồi fallback `httpx` kèm `StarletteDeprecationWarning`. Thiếu cả hai thì: `RuntimeError: The starlette.testclient module requires the httpx2 package`. `httpx2 2.15.1` cần `>=3.11`; `httpx 0.28.1` cần `>=3.8`. OBSERVED 2026-10-21: đọc `starlette/testclient.py:33-51` và chạy import trong venv 3.10. → Test API trên 3.10 phải dùng `httpx<0.29` và sẽ gặp deprecation warning, nên **đừng bật `-W error`** cho leg này.
6. `numpy` mới nhất `2.5.4` cần `>=3.12`. Trên 3.10, pip tự chọn `numpy-2.2.6`, vẫn khớp `numpy>=1.26.0`. OBSERVED 2026-10-21 (log cài).

## 3. Editable install (PEP 660) với src layout

- `pip install -e ./proj[dev]` build "editable for finsec-alpharadar … `finsec_alpharadar-0.1.0-0.editable-py3-none-any.whl`". `import finsec_alpharadar.domain.models` trỏ đúng `proj/src/...`. OBSERVED 2026-10-21 (pip 26.2.2, setuptools trong build isolation; venv seed `setuptools==84.2.1`).
- Tác dụng phụ: tạo `src/finsec_alpharadar.egg-info/` trong cây nguồn. `.gitignore` đã có `*.egg-info/`. OBSERVED 2026-10-21 (`ls proj/src`).
- Mọi subpackage đều có `__init__.py` (api, connectors, domain, forensics, quant, rag, router), nên `packages.find where=["src"]` thấy hết. OBSERVED 2026-10-21 (`find src -type d`).

## 4. pytest: `pythonpath`, `testpaths`, `importorskip`

- `pythonpath` có từ **pytest 7.0.0** ("#9114: Added `pythonpath` setting that adds listed paths to `sys.path`", mục 7.0.0rc1 2021-12-06). VERIFIED https://github.com/pytest-dev/pytest/blob/9.1.1/doc/en/changelog.rst. Docs: "Directories will be added to the head of `sys.path` … Paths are relative to the rootdir". VERIFIED reference.rst tại tag 9.1.1, dòng 2305-2327.
- `[tool.pytest.ini_options]` có "since pytest 6.0". Bảng native `[tool.pytest]` chỉ có từ **9.0**. VERIFIED `doc/en/reference/customize.rst` tại 9.1.1, dòng 70-105. → Dùng `ini_options` để không phụ thuộc pytest 9.
- `importorskip(modname, minversion=None, reason=None, *, exc_type=None)`; "Changed in version 9.1: The default for `exc_type` is now ModuleNotFoundError". Vì vậy module đã cài mà import lỗi thì test **error**, không skip (tốt, lỗi không bị giấu). VERIFIED https://docs.pytest.org/en/stable/reference/reference.html, và đọc lại trong source `src/_pytest/outcomes.py:238-253` tại tag 9.1.1 (`if exc_type is None: exc_type = ModuleNotFoundError`).
- Probe: `pythonpath=["src"]`, `testpaths=["tests"]`, cài pytest 9.1.1 + hypothesis 6.168.5 mà **không** cài package → `1 passed, 1 skipped`, và `importorskip("fastapi")` skip với lý do "No module named 'fastapi'". OBSERVED 2026-10-21, Python 3.10.21.
- `tests/test_end_to_end.py` chỉ có `run_modular_verification()` (dòng 36), không có hàm `test_*` nên pytest thu 0 test. Wrapper `def test_end_to_end(): run_modular_verification()` sẽ được thu. OBSERVED 2026-10-21 (grep).

## 5. Docker Compose (v5.4.0 local)

Probe bằng một file nháp có `profiles`, `${POSTGRES_PASSWORD:?…}` và `127.0.0.1:` (chỉ chạy `config`). OBSERVED 2026-10-21:
- `version: "3.9"` → warning nguyên văn: "the attribute `version` is obsolete, it will be ignored, please remove it to avoid potential confusion". Docs: "only informative and you'll receive a warning message that it is obsolete". VERIFIED https://docs.docker.com/reference/compose-file/version-and-name/.
- `${VAR:?msg}` báo lỗi khi biến unset **hoặc rỗng** ("value of VAR if set and non-empty, otherwise exit with error"). VERIFIED https://docs.docker.com/reference/compose-file/interpolation/. Lỗi thực tế: `error while interpolating services.timescaledb.environment.POSTGRES_PASSWORD: required variable POSTGRES_PASSWORD is missing a value: set POSTGRES_PASSWORD in .env`, exit 1.
- **Chú ý:** interpolation chạy trên **toàn bộ file, kể cả service thuộc profile chưa bật**. `--profile rag config` (chỉ qdrant) vẫn fail khi thiếu `POSTGRES_PASSWORD`. Muốn chạy riêng qdrant thì vẫn phải có `.env`.
- `.env` được đọc ở **project directory** (thư mục của file `-f` đầu tiên), không phải CWD. Gọi từ thư mục cha bằng `-f compose/compose-probe.yml` vẫn đọc `compose/.env`. Biến shell thắng `.env` (`POSTGRES_PASSWORD=from-shell` → `from-shell`). VERIFIED https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/ ("Variables from your shell environment" đứng đầu thứ tự ưu tiên).
- `profiles`: "Services without a `profiles` attribute are always enabled"; bật profile bằng `--profile` hoặc `COMPOSE_PROFILES`; service được nêu tên trên CLI thì tự bật profile của nó. VERIFIED https://docs.docker.com/compose/how-tos/profiles/. Khi mọi service đều có profile, `config` (không truyền profile) cho ra `services: {}`, nên `make docker-up` (`docker compose up -d`) sẽ không khởi động gì. OBSERVED. Thông báo của `up` trong trường hợp này: ASSUMED (không chạy `up`).
- `"127.0.0.1:5432:5432"` render thành `host_ip: 127.0.0.1, published: "5432", target: 5432`. OBSERVED. Docs: thiếu HOST thì "binds to all network interfaces (`0.0.0.0`)" và có thể vượt qua firewall của host. VERIFIED https://docs.docker.com/reference/compose-file/services/.

## 6. Workflow tối thiểu, đã harden (đề xuất)

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
        python-version: ["3.10", "3.13"]
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
Lý do: GitHub khuyên "Pin references to actions to a commit SHA" và "set the default permission for the GITHUB_TOKEN to read access only for repository contents". Docs cũng ghi: "If you specify the access for any of these permissions, all of those that are not specified are set to `none`". VERIFIED https://docs.github.com/en/actions/reference/security/secure-use và https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax. Input `persist-credentials` mặc định `true` (action.yml dòng 52-54); đặt `false` để token không nằm lại trong `.git/config` (OBSERVED: đọc action.yml). Không dùng `pull_request_target`. Không cần secret.

## Kết luận (xếp hạng)
1. **Thêm extra `dev` vào `pyproject.toml`** với marker theo phiên bản Python: `pytest==9.1.1`; `hypothesis==6.170.5; python_version >= '3.11'` và `hypothesis==6.168.5; python_version < '3.11'`; `httpx2>=2.0; python_version >= '3.11'` và `httpx>=0.27,<0.29; python_version < '3.11'` (chỉ khi có test FastAPI); `ruff==0.16.4` nếu muốn lint trong CI. Thêm `[tool.pytest.ini_options] pythonpath = ["src"]`, `testpaths = ["tests"]`.
2. **Workflow ở mục 6:** pin SHA kèm comment version, `permissions: contents: read`, `persist-credentials: false`, ma trận `"3.10"` + `"3.13"` (có ngoặc). Ghi `runs-on: ubuntu-24.04` để tránh image đổi trong đợt migrate 26.04 (2026-10-19 → 2026-11-19); chuyển sang `ubuntu-latest` nếu chấp nhận image thay đổi.
3. **Compose:** bỏ `version:` và redpanda; mỗi service có `profiles`; bind `127.0.0.1:`; `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env}`, kèm `.env.example` (đã commit, không chứa secret) vì interpolation cần biến này ngay cả khi chỉ bật profile qdrant. Sửa Makefile `docker-up` để truyền `--profile` (nếu không thì lệnh này không chạy gì).
4. Test API: `fastapi = pytest.importorskip("fastapi")` ở đầu module test. Leg 3.10 đừng dùng `-W error`, vì nhánh `httpx` của starlette phát `StarletteDeprecationWarning`.

## Câu hỏi còn mở
- **Quyết định của owner:** có nâng `requires-python` lên `>=3.11` không? Lý do: 3.10 đã EOL ngày 2026-10-01; hypothesis, httpx2 và numpy mới đều đã bỏ 3.10; 3.11+ `fromisoformat` cũng nhận "Z" và 9 chữ số thập phân. Nâng lên thì bỏ được mọi marker và leg 3.10.
- Có thêm job lint (`ruff check`) hoặc type-check ngay Phase 0b không? Repo chưa có `[tool.ruff]` hay mypy config. Plan nêu "Lint + type-check" nhưng repo chưa có lệnh thật.
- Có bật cache pip của setup-python không? (Nó tự tìm `requirements.txt`/`pyproject.toml` để làm key, VERIFIED README dòng 87.) Dependencies nhỏ nên ít lợi; chưa probe trên runner thật.
- Workflow chưa chạy trên GitHub thật. Mọi kết luận về runner đều đến từ docs và manifest, chưa qua một lần run.
