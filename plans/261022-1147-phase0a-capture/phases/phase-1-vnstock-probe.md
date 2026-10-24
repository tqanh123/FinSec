---
phase: 1
title: "Vnstock Probe"
plan: 261022-1147-phase0a-capture
created: 2026-10-22
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Phase 1 — Vnstock Probe

## Overview
Đo thật vnstock Gen 5 (nguồn PHỤ, D2) trong một venv cô lập ngoài clone, dưới một Windows user riêng (D6), và probe các cổng công bố
chính thức (HOSE, HNX, VSDC, SSC); ghim vnstock bằng lock có hash (vnstock chỉ từ index vendor, mọi gói khác chỉ từ PyPI); thêm guard
`finsec_alpharadar.data_root` để mọi byte dữ liệu có bản quyền chỉ rơi vào thư mục ngoài repo. Kết thúc bằng bước owner chọn cổng
(BLOCKED) và một bảng "design inputs" mà Phase 2 tiêu thụ. Phụ thuộc: cạnh liên plan `261021-1314-phase0b-foundations/P5`.

Nhãn bằng chứng như plan.md. `file:line` đọc trên main checkout @ `c9168f0`; HEAD `a76e7c9` của nhánh 0b chỉ đổi số test (267→274) ở
`AGENTS.md:51,169` và hai docs — số dòng không đổi (OBSERVED `git diff c9168f0 a76e7c9`, 2026-10-22). Cook đọc lại ở base của nhánh mình.

## Requirements
**R1.1 Guard thư mục dữ liệu ngoài repo — `src/finsec_alpharadar/data_root.py` (mới, stdlib).**
- `DATA_ROOT_ENV = "FINSEC_DATA_ROOT"` (thiết kế của main: một biến môi trường, đường dẫn tuyệt đối, ngoài clone, dùng chung cho spike
  và capture).
- `resolve_external_dir(env: Mapping[str, str], var_name: str, *, work_tree_of=find_work_tree, check_ignore=git_check_ignore) -> Path` là lõi
  dùng chung; `resolve_data_root(env)` = `resolve_external_dir(env, "FINSEC_DATA_ROOT")`, và Phase 2 gọi cùng hàm cho `FINSEC_BACKUP_DIR`
  (red-team r1 LT7). Thiếu/rỗng → `DataRootError("<VAR> is not set ...")`; không tuyệt đối → `DataRootError`; gọi `Path.resolve()` TRƯỚC khi
  đi ngược cha (một junction trỏ vào clone qua mặt được walk lexical — OBSERVED red-team r1 LT7); nếu đường dẫn đã resolve nằm trong một git
  work tree (đi ngược cha tìm `.git` là file hoặc thư mục — `find_work_tree`, thuần filesystem) thì hỏi `git check-ignore -q <root>/raw/probe.jsonl`
  (cwd = work tree): exit 0 → chấp nhận; exit 1 → `DataRootInsideWorkTreeError` nêu work tree và gợi ý "a path outside the clone, or under
  /data/"; exit khác → `DataRootError` kèm stderr. Không tạo thư mục nào (caller tự quyết).
- Hỏi một FILE `.jsonl` bên trong, không hỏi thư mục: với `core.autocrlf=true` một truy vấn `<dir>/` khớp mẫu rỗng của dòng trống CRLF
  (AGENTS.md:123), và `.jsonl` không bị ignore toàn cục — chỉ dưới `/data/` (`.gitignore:32`; 5 file `.jsonl` tracked dưới
  `harness/data/port-control/` nên KHÔNG thêm `*.jsonl` toàn cục — fact 7 của brief). OBSERVED 2026-10-22 trên main checkout:
  `git check-ignore -v data/capture/raw/hose_news/2026-10-22.jsonl` → `.gitignore:32:/data/`; `git check-ignore -v capture-root/raw/x.jsonl`
  → exit 1 (sẽ bị từ chối — đúng ý).
- Thông điệp lỗi chỉ nêu đường dẫn, không in giá trị env nào khác.

**R1.2 `pyproject.toml`.** Bỏ `"vnstock>=3.4.0"` khỏi extra `prod` (`pyproject.toml:25`); thêm extra mới ngay sau `finetune`
(`pyproject.toml:28-32`), nguyên văn (tiếng Anh; khối thụt 2 dấu cách chỉ để dòng `#` không bị đọc thành tiêu đề — cook bỏ thụt khi chép):
  ```toml
  # vnstock Gen 5 is not on PyPI (the PyPI project is quarantined with 0 files; OBSERVED 2026-10-22). It installs only from
  # the vendor index through the hash-pinned lock requirements/vnstock/win-py313.lock (AGENTS.md "Build & Verification
  # Commands"); `pip install ".[capture]"` from PyPI cannot resolve it. Never install vnai, vnstock 4.x or vnstock3.
  capture = [
      "vnstock==5.1.0",
  ]
  ```
- Phiên bản = bản đã probe. 5.1.0 là bản duy nhất chưa bị yank hôm nay (fact 2 của brief, OBSERVED main 2026-10-22). Nếu tới lúc cook
  5.1.0 đã bị yank: chạy lại bước compile (R1.3) cho bản mới nhất chưa yank, probe đúng bản đó, ghim bản đó, và ghi lý do vào AGENTS.md.
- CI không đổi: `.github/workflows/ci.yml` chỉ cài `".[dev]"` (OBSERVED), không bao giờ cài `capture`. Không test nào resolve gói — test
  đọc file bằng `tomllib`.

**R1.3 Lock project — `requirements/vnstock/pyproject.toml` + `requirements/vnstock/win-py313.lock` (mới).** Một project riêng chỉ để
compile lock (không bao giờ được cài như package; `pyproject.toml` gốc của repo không có `[tool.uv]`), nội dung:
```toml
[project]
name = "finsec-vnstock-env"
version = "0"
requires-python = "==3.13.*"
dependencies = ["vnstock==5.1.0"]

[[tool.uv.index]]
name = "vnstocks"
url = "https://vnstocks.com/api/packages"
explicit = true

[tool.uv.sources]
vnstock = { index = "vnstocks" }
```
Lệnh compile (Git Bash, từ `requirements/vnstock/`):
`uv pip compile pyproject.toml --generate-hashes --emit-index-annotation --python-platform x86_64-pc-windows-msvc --python-version 3.13 --index-strategy first-index -o win-py313.lock`
— **OBSERVED 2026-10-22** (planner chạy đúng nội dung trên trong scratchpad, uv 0.12.5): exit 0; 6 gói `numpy==2.5.4`, `pandas==3.0.6`,
`python-dateutil==3.0.0`, `six==1.17.0`, `tzdata==2026.5`, `vnstock==5.1.0`; chỉ `vnstock` mang `# from https://vnstocks.com/api/packages`,
5 gói còn lại `# from https://pypi.org/simple`; 135 dòng `--hash=sha256:`; khối vnstock có 5 hash (một cho mỗi wheel nền tảng), trong đó
có `d263dec2b259e575f72ad7066363d6700baeb7c5d23948d63cd77e92f15ed973` (wheel `cp310-abi3-win_amd64`, khớp fact 2). Phiên bản PyPI có thể trôi
tới lúc cook — commit đúng file cook sinh ra, header của uv ghi lại lệnh.
- **Bộ wheel riêng = nguồn cài DUY NHẤT** (research `vnstock-gen5.md` §2: vendor từng xoá file đã yank; red-team r1 RT10): agent (dev user, chỉ
  tải, không cài, không chạy) tải MỌI wheel của lock vào `$FINSEC_DATA_ROOT/wheels/win-py313/`:
  `.venv/Scripts/python -I -m pip download --require-hashes --no-deps -r requirements/vnstock/win-py313.lock --only-binary=:all: --platform win_amd64 --python-version 3.13 --implementation cp --extra-index-url https://vnstocks.com/api/packages -d "$FINSEC_DATA_ROOT/wheels/win-py313"`
  — **OBSERVED 2026-10-22** (planner, lock trong scratchpad): exit 0, "Successfully downloaded numpy pandas python-dateutil six tzdata vnstock" (6 wheel,
  pip kiểm hash khi tải). Mọi lần cài sau đó (user probe ở đây, user `capture` ở Phase 3) dùng `--no-index --find-links <bộ wheel> --require-hashes`,
  không chạm index nào: xoá file trên vendor không còn chặn được triển khai. Bộ wheel không bao giờ vào repo (phần mềm có licence) và không vào
  thư mục cloud; chép sang laptop bằng ổ USB hoặc mạng nội bộ (Phase 3).

**R1.4 Môi trường probe cô lập (D6).** Mọi lệnh chạy dưới user probe là bước OWNER-RUN (agent không gõ được mật khẩu hay API key):
- OB5 (owner, PowerShell elevated trừ khi ghi khác):
  - tạo user Windows chuẩn cục bộ (ví dụ `vnprobe`) và `Set-LocalUser vnprobe -PasswordNeverExpires $true` — chính sách máy dev là "Maximum password
    age (days): 42" (OBSERVED main 2026-10-22, `net accounts`; red-team r1 RT3); bằng chứng `net user vnprobe` → "Password expires Never";
  - chặn user probe khỏi clone: `icacls H:\study\Project\FinSec /deny vnprobe:(OI)(CI)(R,W,D)` — hôm nay clone và `C:\` kế thừa
    `NT AUTHORITY\Authenticated Users:(M)` (OBSERVED main 2026-10-22, `icacls`; red-team r1 RT4), nên "user riêng" không cô lập gì nếu thiếu bước này;
    lưu output `icacls H:\study\Project\FinSec` làm bằng chứng;
  - thư mục chung ngoài cả hai profile và ngoài clone, ví dụ `C:\finsec-probe\` (Modify cho cả hai user); con: `kit\` (agent copy
    `scripts/spike/vnstock_probe.py`, `requirements/vnstock/win-py313.lock` và bộ wheel `wheels\` của R1.3 — user probe không cần đọc clone), `venv\`,
    `work\` (cwd của probe), `out\<UTC-date>\`. Hash của dev user KHÔNG nằm ở đây (R1.5, RT5);
  - **Python cho chính user probe** (red-team r1 RT6): trong phiên `vnprobe`, cài Python 3.13 x64 từ python.org cho user hiện tại (máy dev không có `py.exe`,
    và Python 3.13 hiện có là gói Store cài theo từng user — OBSERVED red-team r1: `.venv/pyvenv.cfg` `home = C:\Users\tqanh\AppData\Local\Microsoft\WindowsApps\…`).
- Cài (owner-run trong terminal của `vnprobe`, agent soạn sẵn lệnh): `%LOCALAPPDATA%\Programs\Python\Python313\python.exe -m venv C:\finsec-probe\venv`, rồi
  `C:\finsec-probe\venv\Scripts\python -I -m pip install --require-hashes --no-deps --no-index --find-links C:\finsec-probe\kit\wheels -r C:\finsec-probe\kit\win-py313.lock`.
  Không index nào được chạm lúc cài; hash khoá đúng byte. OBSERVED 2026-10-22 (planner): cùng dạng lệnh `--require-hashes --no-deps --no-index --find-links`
  cài được `tzdata==2026.5` từ bộ wheel đã tải trong một venv scratch và `ZoneInfo('Asia/Ho_Chi_Minh')` nạp được; với cả 6 gói thì `[ASSUMED]` tới lần
  owner-run đầu (planner cố ý không cài vnstock dưới dev user — D6). Không có fallback uv (uv nằm trong profile của owner). Bằng chứng:
  `type C:\finsec-probe\venv\pyvenv.cfg` → `home` nằm trong profile của chính `vnprobe`, không trong profile user khác.
- OB4a (owner): tạo tài khoản vnstock Community + API key, rồi trong terminal `vnprobe`: `C:\finsec-probe\venv\Scripts\vnstock.exe register`
  và tự gõ key. Key không bao giờ nằm trong chat, file trong repo, hay file agent đọc.
- Probe đặt `DO_NOT_TRACK=1`, `VNSTOCK_TELEMETRY=0`, `VNSTOCK_DISABLE_AGENT_SETUP=1`, `VNSTOCK_DISABLE_GLOBAL_AGENT=1` vào `os.environ`
  TRƯỚC `import vnstock` (research §4, §7; hai biến cuối là của 4.x, vô hại với Gen 5). Không gọi `load_skill`/`load_doc`/`setup_agent`.

**R1.5 `scripts/spike/vnstock_probe.py` (mới; stdlib + vnstock + pandas; KHÔNG import `finsec_alpharadar`; chạy bằng `python -I`).**
- `hash [--repo <clone>] --out <dir> --label <name>` (KHÔNG import vnstock — chạy được trước cả khi cài; `--repo` tuỳ chọn): sha256 của `<repo>/AGENTS.md`,
  `<repo>/CLAUDE.md`, mọi file dưới `<repo>/.agents/`, mọi file `<repo>/.git/hooks/*` và mọi `<repo>/.venv/**/*.pth` (red-team r1 RT4), và các
  file chỉ dẫn agent toàn máy của user hiện tại: `~/.claude/CLAUDE.md`, `~/.claude/settings.json`, `~/.codex/AGENTS.md`, `~/.codex/config.toml`,
  `~/.gemini/GEMINI.md`, `~/.gemini/config/AGENTS.md`, `~/.gemini/settings.json`, `~/.cursorrules`, `~/.windsurfrules`, `~/.clinerules`,
  `~/.github/copilot-instructions.md`, `~/.clauderc` (vắng → `"absent"`; danh sách từ issue #236, research §3); cộng liệt kê tên + cỡ
  của `~/.vnstock/` và của cwd. Chỉ hash, không chép nội dung. Không hash cả `~/.claude/**` (log phiên đổi liên tục). Đường dẫn quy tắc
  toàn cục của Antigravity: `[ASSUMED]` nằm dưới `~/.gemini/`.
- `status --out <dir>`: chạy `vnstock status`, `vnstock env`, `vnstock doctor` (exe cạnh `sys.executable`) với redaction như bước 2 dưới, ghi
  `vnstock-status.txt`. Dùng ở đây (OB4a) và trên laptop (Phase 3, OB4b).
- `probe --out <dir> --symbols <list|VN30> [--exdates <file>]`, theo thứ tự, ghi `probe-summary.json` (chỉ metadata và số dẫn xuất, không dòng
  dữ liệu) + `raw/*.json` (dữ liệu có bản quyền — chỉ ngoài repo). `--symbols VN30` (VL-1) = probe tự lấy thành phần qua `ref.index.members`
  ngay sau bước 3 và ghi danh sách + ngày as-of vào `probe-summary.json` (đây là seed mà design inputs R1.10 chép sang P2):
  1. hash-before trong tiến trình; đặt env (R1.4); `import vnstock`; ghi `vnstock.__version__`, đường dẫn module; hash-after-import.
  2. `vnstock status`, `vnstock env`, `vnstock doctor` qua subprocess (exe trong `venv\Scripts`); trước khi ghi ra file, thay mọi chuỗi
     `[A-Za-z0-9_\-]{24,}` bằng `<redacted-token>` (vendor nói các lệnh này không in key — VERIFIED single-source; redaction là phòng thêm để
     key không bao giờ vào ngữ cảnh agent). Ghi tier, hạn dùng (expiry) thật của bản cài, device, đường dẫn store.
  3. Bề mặt API: `dir(vnstock)`, các thuộc tính public của đối tượng tham chiếu/thị trường tìm được, `inspect.signature` của
     `events`, `news`, `ohlcv`, `trades` (đường import Gen 5 `from vnstock import Reference` là VERIFIED tài liệu vendor; đối tượng thị trường
     `[ASSUMED]` — probe tự tìm).
  4. `news()`: gọi cho 1 mã, kỳ vọng `VNSTOCK_FEATURE_REMOVED`; ghi lớp lỗi, thuộc tính `code`, 200 ký tự đầu của message.
  5. `events()`: 5 mã (3 HOSE, 1 HNX, 1 UPCOM; chọn lúc cook từ danh sách sàn) × nguồn {`vci`, `kbs`}: cột, dtype, số dòng, có cột `id` và
     id có duy nhất không, MỌI cột ngày/giờ có giá trị giờ khác nửa đêm không (time-of-day), timezone.
  6. `ohlcv()`: đơn vị và điều chỉnh, trên 5 ngày GDKHQ đã biết (lấy từ cột `exright_date` của bước 5, ưu tiên cổ tức bằng cổ phiếu / thưởng
     cổ phiếu vì cho tín hiệu rõ): `interval="1D"`, cửa sổ ex−10..ex+10 ngày lịch, `floating=4`, nguồn {`vci`, `kbs`}; ghi `close(T−1)/open(T)`
     so với tỉ lệ chia (chuỗi thô sẽ tụt đúng tỉ lệ; chuỗi đã điều chỉnh thì không); độ lớn giá so với giá tham chiếu công khai trên cổng sở
     của cùng ngày → nghìn hay VND đầy đủ. Trả lời câu hỏi **"có nguồn/tham số Gen 5 nào cho bar THÔ không?"** (chữ ký `ohlcv`, `trades()`).
     Nếu không có: ghi vào AGENTS.md như một blocker của Phase 3 (roadmap §6.3, AGENTS.md:153-154), không giải ở đây.
  7. Độ sâu: `ohlcv` ngày từ `start="2000-01-01"` tới một ngày cố định trong quá khứ (không suy từ đồng hồ máy); `interval="1m"` 200 ngày
     trước một ngày cố định; ghi ngày cũ nhất, `df.attrs`, cảnh báo.
  8. Mã đã huỷ niêm yết: 3-5 mã chọn lúc cook từ thông báo huỷ niêm yết của HOSE/HNX 2024-2026 (ghi URL nguồn); `events` + `ohlcv`; ghi lỗi
     (ví dụ `VNSTOCK_INPUT_SYMBOL_UNKNOWN`) hay dữ liệu.
  9. Rate limit: gọi `events()` tuần tự trên danh sách seed (VN30 tại ngày probe — VL-1), nhịp 6 s → 3 s → 2 s → 1 s → 0.5 s (10 lệnh mỗi nhịp, tối đa 50); DỪNG ở tín hiệu
     throttle đầu tiên (exception, `UserWarning`, ghi chú cắt trong `df.attrs`, lỗi HTTP); `vnstock status` trước/sau để đọc % quota.
     Quota dùng chung mọi device của tài khoản (research §1) — giữ probe nhỏ.
  10. Danh mục lỗi: mọi lỗi thấy được → `type(e).__name__`, `getattr(e, "code", None)`, đầu message; P2 dùng bảng này để phân loại outage.
  11. hash-after trong tiến trình (globals của user probe + cwd + `~/.vnstock`) — chỉ để tham khảo; bằng chứng quyết định là các lần `hash` chạy
      trong tiến trình `python -I` RIÊNG trước `vnstock register` và sau khi probe xong (Implementation Steps 5-8; red-team r1 RT5).
- Tuỳ chọn (OWNER-RUN, cần admin; trả lời câu hỏi mở 8 của research): tạm thêm `127.0.0.1 vnstocks.com` vào `hosts`, chạy một lệnh `events()`,
  ghi lỗi + thời gian tới khi fail, rồi gỡ dòng hosts. Kết quả định nghĩa `vendor_auth_unreachable` cho P2; nếu bỏ qua, P2 xếp lỗi chưa biết
  vào `source_unreachable` kèm tên lớp lỗi.

**R1.6 `scripts/spike/portal_probe.py` (mới; dev user, `.venv`; dùng `_spikelib` + `finsec_alpharadar.data_root`).**
- Ứng viên (research `vn-disclosure-portals.md` §1-§4): HOSE API
  `https://api.hsx.vn/n/api/v1/1/news/securitiesType/0?pageIndex=1&pageSize=20&startDate=<D>&endDate=<D>` (kèm `Origin`/`Referer`
  `https://www.hsx.vn/`); HOSE RSS `https://api.hsx.vn/n/api/v1/News/NewsByCateFeed/21`; HNX RSS feed 3
  `https://hnx.vn/3/vi_vn/thong-tin-cong-bo-tu-to-chuc-phat-hanh.rss`; HNX POST `https://hnx.vn/ModuleArticles/ArticlesCPEtfs/NextPageTinCPNY_CBTCPH`
  (`pNumRecord=50`); VSDC `https://vsdc.vn/vi/tin-thi-truong-co-so`; SSC chỉ robots + 1 GET trang đầu.
- robots.txt của `www.hsx.vn` (đã OBSERVED: cho phép tất cả), `api.hsx.vn`, `hnx.vn`, `vsdc.vn`, `congbothongtin.ssc.gov.vn` → status + 2 KB đầu.
- `--rounds 12 --interval 300` trong giờ giao dịch: mỗi vòng 1 request mỗi ứng viên; ghi status, thời gian tới header (≈ TTFB) và tổng,
  bytes, sha256, Content-Type, `Date` header → skew = giữa (local trước, local sau) − Date (độ phân giải 1 s), cookie/HTML kiểu WAF, có giờ
  trong ngày (giây hay phút), có id, cỡ trang, số item trùng giữa hai vòng liên tiếp (độ sâu so với cadence).
- HOSE: mỗi vòng gửi CẢ HAI dạng request mà Phase 2 dùng (red-team r1 RT13) — `startDate=endDate=<hôm nay ICT>` và `startDate=<hôm qua ICT>&endDate=<hôm nay ICT>`,
  cùng `pageSize` dự định ghi vào config — và ghi độ trễ từng dạng. Research đã thấy một lần gọi khoảng 2 ngày với `pageSize=50` treo 135 s
  (`research/vn-disclosure-portals.md` §1). Số đo vào design inputs; P2 chỉ dùng dạng hai ngày ở poll đầu tiên của một ngày ICT (P2 R2.8).
- `--history-check`: HOSE API `pageSize=1` cho một ngày giao dịch mỗi năm 2018..2026 → `totalCount` (ECM chạy từ 2024-08-15, research §1)
  → cho P6 biết lịch sử giờ đăng có sâu tới đâu.
- Lịch sự: ≥ 5 s giữa hai request cùng host; User-Agent trung tính không PII (`FinSecAlphaRadar-research/0.1 (personal, non-commercial)`);
  connect timeout ≥ 30 s cho HNX (connect ~20 s qua WARP, research §2). Body thô → `$FINSEC_DATA_ROOT/spike/portal/<UTC-date>/` (tiêu đề chứa tên
  người thật — PDPL, research §5). Báo cáo chỉ chép số đếm/độ trễ, không tiêu đề, không tên.

**R1.7 `scripts/spike/_spikelib.py` (mới, stdlib + `finsec_alpharadar.data_root`)** — dùng chung cho `portal_probe.py` (P1),
`record_edgar_headers.py` (P4), `alpaca_probe.py` (P5), `vn_reconcile.py` (P6): `load_env_file(path) -> dict[str, str]` (KEY=VALUE, bỏ ngoặc,
bỏ comment, KHÔNG bao giờ in giá trị), `require_env(name, values, *, placeholders)` (lỗi chỉ nêu tên biến), `spike_dir(kind) -> Path`
(`<root>/spike/<kind>/<UTC-date>/`), `fetch(...) -> FetchResult` (urllib; status, headers, body, thời điểm local trước/sau, thời gian tới
header), `append_jsonl`, `sha256_hex`, `skew_from_date_header`. Trùng ~30 dòng với fetcher của `capture/sources.py` (P2) là chấp nhận:
`capture/` không được phụ thuộc `scripts/` và spike không được phụ thuộc capture.

**R1.8 `.env.example`.** Thêm sau dòng 12 (khối VN), tiếng Anh (bỏ thụt khi chép):
  ```
  # --- Local data (outside the git clone) ---
  # Absolute path for raw captures and spike output. Never inside the clone unless git ignores it (AGENTS.md).
  FINSEC_DATA_ROOT=
  ```

**R1.9 `docs/phase0-spike-report.md` (mới, tiếng Anh, chỉ sự kiện).** Mục: 0 Purpose, labels, where raw data lives (the external root,
never the repo); 1 vnstock (bảng: lock + hash, version, expiry, tier, device, `news()` removed code, events columns/id/time-of-day, ohlcv unit
& adjusted, raw-bar availability, depth, delisted, rate limit + signal, exception catalogue, agent-file hashes before/after); 2 Portals (bảng
ứng viên: URL, robots, render, time-of-day precision, id, depth/round, latency p50/max, skew, WAF, history depth); 3 Design inputs for P2
(bảng dưới); 4 EDGAR (P4); 5 Alpaca (P5); 6 VN reconcile, counts, go/no-go (P6); 7 Phase 0 exit (P7). Không dòng dữ liệu, không tiêu đề,
không tên người; mọi dòng có nhãn + ngày.

**R1.10 Bảng "design inputs" (cuối mục 3 của báo cáo, P2 đọc):** safe vnstock calls/min (= 50% mức đo được; 20/min tới khi đo — roadmap §5.3,
`docs/project-roadmap.md:267`); seed universe = thành phần VN30 tại ngày probe (`ref.index.members`, ghi ngày as-of; VL-1, owner 2026-10-24)
× cadence quét vòng; quy tắc `source_id` mỗi nguồn (HOSE `id`, HNX `<guid>`,
vnstock `vci:<id>`); URL cổng được chọn + `pageSize`/`max_pages`; timeout/connect; bảng lỗi vnstock → loại outage; ngày hết hạn bản vnstock
(từ `vnstock status`).

**R1.11 BLOCKED (owner) — chọn cổng (OB3).** Sau khi báo cáo mục 2 xong, owner chọn từ bảng ứng viên. Khuyến nghị (research portals, kết luận
1-2): HAI nguồn chính song song — HOSE API listing + HNX RSS feed 3 (cần cả hai để phủ HOSE và HNX/UPCOM); SSC loại (chỉ có ngày, ADF có
state, đồng hồ lệch ~6 phút); VSDC để Phase 3. Owner tự mở `https://www.hsx.vn/dieu-khoan-su-dung` (SPA, cần trình duyệt) và chép câu liên
quan. Cook chỉ ghi dòng Decisions từ câu trả lời của chính owner trong phiên (AskUserQuestion ở main) — không bao giờ tự giả định.

**R1.12 `.gitignore` — ignore `plans/` (red-team r1 RT8, phán quyết của main; owner xác nhận VL-4, 2026-10-24).** `plans/` hiện không track mà cũng không bị ignore (OBSERVED red-team r1:
`git check-ignore -v plans/x.md` → exit 1, `git ls-files plans` → 0 dòng) và chứa email của owner (frontmatter `author:`) cùng ghi chú research; một
`git add -A` sẽ đẩy chúng lên repo PUBLIC. Thêm NGOÀI block installer (`.gitignore:60-67` ở `c9168f0`), ngay sau block data artefacts (`.gitignore:37-48`),
tiếng Anh (bỏ thụt khi chép):
  ```
  # Plans hold research notes, owner contacts and probe details; they stay local (red-team r1 RT8, 2026-10-22).
  /plans/
  ```
`hs-run`/`plan_approval` đọc `plans/` từ đĩa; cook kiểm lại E3 (`hs-run plan next --plan plans/261022-1147-phase0a-capture` vẫn trả envelope hợp lệ) ngay
sau khi sửa — `[ASSUMED]` không bước harness nào cần `plans/` được track, tới lần chạy đó. Tên người thật trong `research/vn-disclosure-portals.md:130`
đã được thay bằng `<insider-A>`, `<insider-B>` (2026-10-22).

## Related Code Files
| Hành động | Tệp |
|---|---|
| Create | `src/finsec_alpharadar/data_root.py` |
| Create | `tests/test_data_root.py` |
| Create | `tests/test_dependency_pins.py` |
| Create | `requirements/vnstock/pyproject.toml` |
| Create | `requirements/vnstock/win-py313.lock` |
| Create | `scripts/spike/_spikelib.py` |
| Create | `scripts/spike/vnstock_probe.py` |
| Create | `scripts/spike/portal_probe.py` |
| Create | `docs/phase0-spike-report.md` |
| Modify | `pyproject.toml` |
| Modify | `.env.example` |
| Modify | `.gitignore` |
| Modify | `tests/test_repo_hygiene.py` |
| Modify | `AGENTS.md` |
| Modify | `docs/system-architecture.md` |
| Modify | `docs/code-standards.md` |
| Modify | `docs/project-roadmap.md` |
| read | `plans/261022-1147-phase0a-capture/research/vnstock-gen5.md` |
| read | `plans/261022-1147-phase0a-capture/research/vn-disclosure-portals.md` |
| run | `uv pip compile` + `pip download` (R1.3), owner-run `vnstock_probe.py` dưới user probe |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

Ghi chú parser: `.env.example` có đuôi `example` không thuộc `_FILE_EXTS` (`harness/scripts/plan_graph.py:597-601`), nên
`plan_graph.py --require` in một dòng advisory "P1 declares .env.example …" — đã biết (như plan 0b P5), không chặn.

Challenge luật ">8 tệp": 17 tệp = 9 mới (2 guard/test, 2 lock, 3 spike, 1 báo cáo, 1 module) + 3 config (`pyproject.toml`, `.env.example`,
`.gitignore`) + 1 test sửa + 4 docs. Tách thêm phase chỉ nhân
bản delta docs; guard `data_root` phải có TRƯỚC lần ghi dữ liệu có bản quyền đầu tiên (chính các probe của phase này) → giữ.

## Implementation Steps
0. **Nơi chạy và nhánh.** Main checkout `H:\study\Project\FinSec`, sau khi PR của plan 0b đã merge: `git fetch origin` rồi
   `git switch -c claude/phase0a-capture origin/main`; `plans/` untracked đi theo. Cài lại `.venv`: `.venv/Scripts/python -m pip install -e ".[dev]"`.
   Đặt `FINSEC_DATA_ROOT` (dev) tới một thư mục tuyệt đối ngoài clone (ví dụ `D:\finsec-data`).
1. **Tests-before (RED)** — viết `tests/test_dependency_pins.py` và `tests/test_data_root.py`; chạy
   `.venv/Scripts/python -m pytest -q tests/test_dependency_pins.py tests/test_data_root.py` → RED (lý do từng test ở "Tests Before").
2. **Implement guard** `data_root.py` → `tests/test_data_root.py` xanh.
3. **Lock + pin:** tạo `requirements/vnstock/pyproject.toml`, chạy lệnh compile R1.3, sửa `pyproject.toml` (R1.2) → `tests/test_dependency_pins.py`
   xanh. Tải bộ wheel vào `$FINSEC_DATA_ROOT/wheels/win-py313/` bằng `pip download --require-hashes` (R1.3).
   Thêm `/plans/` vào `.gitignore` (R1.12) + test `test_plans_dir_is_ignored` (RED trước); kiểm lại E3 ngay sau đó.
4. **Spike scripts:** `_spikelib.py`, `vnstock_probe.py`, `portal_probe.py`. Chạy `.venv/Scripts/python -m pytest -q tests/test_no_order_execution.py`
   (guard quét `scripts/**/*.py` — `tests/test_no_order_execution.py:59-64`) → xanh.
5. **BLOCKED (owner) OB5** — user probe (PasswordNeverExpires), `icacls … /deny vnprobe`, Python per-user, thư mục chung; agent copy kit (R1.4); owner
   tạo venv + cài từ bộ wheel. Phát hiện: `net user vnprobe`, output `icacls` và `pyvenv.cfg` trong `C:\finsec-probe\out\<date>\ob5\`. Chưa có → DỪNG.
6. **Hash TRƯỚC mọi lần import vnstock (red-team r1 RT5)** — cả hai đều là tiến trình `python -I` mới, chạy TRƯỚC `vnstock register`:
   agent (dev user) `.venv/Scripts/python -I scripts/spike/vnstock_probe.py hash --repo . --out "%USERPROFILE%\finsec-hash\<date>" --label before-dev`
   (thư mục trong profile của dev user — user probe không ghi được); owner (user probe)
   `C:\finsec-probe\venv\Scripts\python -I C:\finsec-probe\kit\vnstock_probe.py hash --out C:\finsec-probe\out\<date> --label before-probe` (không `--repo`:
   `vnprobe` đã bị chặn khỏi clone).
7. **BLOCKED (owner) OB4a + OWNER-RUN:** `vnstock.exe register` (tự gõ key) → `vnstock_probe.py status --out …` → `cd C:\finsec-probe\work` →
   `C:\finsec-probe\venv\Scripts\python -I C:\finsec-probe\kit\vnstock_probe.py probe --out C:\finsec-probe\out\<date> --symbols VN30` (VL-1; và tuỳ chọn bước hosts).
   Phát hiện: `vnstock-status.txt` (tier + device, đã redact) và `probe-summary.json`. Agent không chạy các lệnh này.
8. **Hash SAU (tiến trình `python -I` mới):** owner chạy `hash --label after-probe`; agent chạy `hash --label after-dev` vào `%USERPROFILE%\finsec-hash\<date>`
   rồi `git diff --exit-code HEAD -- AGENTS.md CLAUDE.md .agents` (exit 0). So before/after của cả hai user. Lệch ở bất kỳ file nào trong phạm vi hash
   (gồm `.git/hooks/*`, `.venv/**/*.pth`) = FAIL phase (khôi phục từ git/backup, ghi OBSERVED, không cài vnstock ở đâu nữa cho tới khi owner quyết).
9. **Portal probe (agent):** `.venv/Scripts/python scripts/spike/portal_probe.py --rounds 12 --interval 300` trong giờ giao dịch, rồi `--history-check`.
10. **Báo cáo + design inputs** (R1.9, R1.10) từ `probe-summary.json` và output portal; chỉ số đếm/độ trễ/mã lỗi.
11. **BLOCKED (owner) OB3 — chọn cổng** (R1.11). Cook ghi dòng Decisions có ngày. Chưa có câu trả lời → phase chưa xong (Phase 2 bị chặn).
12. **Delta docs** (dưới) → **Regression gate** → một commit, ví dụ `feat: pin vnstock, guard the data root and record the 0a probes`
    + trailer `Co-Authored-By:`; push; mở draft PR để CI chạy (VL-2, owner 2026-10-24: một PR cho cả plan, draft sau P1, merge commit sau P7).

### Delta docs (cùng commit; tiếng Anh; có ngày)
- **AGENTS.md**
  - "Decisions" (trên cùng, ngày cook): D2 + D6 nguyên văn:
    `- <date> — vnstock (owner, plan 261022-1147): Gen 5 is a SECONDARY VN source; the official portals carry the capture, and a vnstock failure is a logged source outage. vnstock runs only in a hash-pinned environment outside the clone, under a separate Windows user on the dev machine (the probe) or the capture user on the laptop; agent instruction files are hashed before and after every first import.`
    và dòng chọn cổng (OB3), ví dụ `- <date> — VN capture sources (owner): primary <HOSE API listing> and <HNX RSS feed 3>; vnstock events secondary; SSC excluded (date only); VSDC deferred to Phase 3.`
    và thiết kế của main: `- <date> — External data root: raw captures and spike output live under the absolute path in FINSEC_DATA_ROOT, outside the clone by default; finsec_alpharadar.data_root refuses a root inside a git work tree unless git ignores it.`
  - "Verified Facts" dòng 122 (vnstock licence): giữ câu licence; thay "Rate limits, history depth and price units: ASSUMED until the Phase 0 probe"
    bằng kết quả probe OBSERVED (rate limit + tín hiệu, độ sâu, đơn vị, adjusted, raw bars có/không, time-of-day của events, delisted, expiry).
    Thêm dòng **vnstock distribution (OBSERVED 2026-10-22)**: PyPI `vnstock` quarantined (0 files); Gen 5 only on `https://vnstocks.com/api/packages`;
    every release below 5.1.0 yanked; each release stops 365 days after publication (VERIFIED vendor docs); pinned `==5.1.0` in the `capture`
    extra via `requirements/vnstock/win-py313.lock`. Thêm dòng **VN disclosure portals (OBSERVED <date>)**: robots, giờ đăng, độ trễ — trỏ tới
    `docs/phase0-spike-report.md`. Nếu không có bar thô: dòng "**Phase 3 blocker (OBSERVED <date>)**: vnstock Gen 5 serves adjusted prices only …".
  - "Package Layout": thêm `data_root.py` (guard of the external data root, 2026-…).
  - "Build & Verification Commands": thêm **vnstock environment** (lock path, lệnh compile R1.3, lệnh cài hash R1.4; "never `pip install vnstock`
    from PyPI") và **Spikes** (`scripts/spike/*.py` reach the network, are never collected by pytest, and need `FINSEC_DATA_ROOT`).
  - Số test do regression gate in ra, ở ĐỦ năm chỗ (red-team r1 LT12): `AGENTS.md:51` (Current Status), `AGENTS.md:169` (Build & Verification),
    `docs/code-standards.md:50` và `:56`, `docs/system-architecture.md:13`. Quy tắc này áp cho mọi phase thêm test (P1, P2, P4, P5, P6) và P7.
  - Chỉ backtick path đã tồn tại trong commit này (`tests/test_agents_md_paths.py`); không backtick `plans/…` hay data root.
- **docs/system-architecture.md**: dòng 11 ("No … environment-variable read … anywhere in `src/`") → `data_root.py` đọc `FINSEC_DATA_ROOT` (<date>);
  bảng as-built thêm hàng `data_root.py`; External dependencies vnstock (dòng 115-118): Gen 5 vendor index, quarantine, pin trong extra `capture`
  (không còn ở `prod`, dòng 118 trích `pyproject.toml:25` cũ), lock; Boundaries "Untrusted inputs" (dòng 131-134): nội dung vendor là data.
- **docs/code-standards.md**: dòng 85 ("No code reads environment variables yet") → `data_root.py`; Dependencies dòng 93 → pin `==` trong
  `capture` + lock hash, cấm `vnai`/4.x/vnstock3, cài chỉ bằng `--require-hashes`; Testing: số test + quy ước "spike scripts under `scripts/spike/`
  reach the network and are never collected by pytest (`testpaths = ["tests"]`); their raw output goes under `FINSEC_DATA_ROOT` only"; dòng 30 trích
  `pyproject.toml:40-41` (`[tool.setuptools.packages.find]`) bị R1.2 làm dời — trích lại số dòng thật sau khi sửa (red-team r1 LT12); dòng 44-45
  (Package names / `.gitignore:37-48`) thêm `/plans/` (R1.12).
- **docs/project-roadmap.md**: §7 0a gạch vnstock probe (dòng 363-369) "Done <date> (plan 261022-1147, phase 1)"; §5.2 hàng `pyproject.toml`
  (dòng 246) "Pin vnstock … done <date> (`capture` extra + hash lock)"; §4 hàng vnstock (dòng 171) và hàng cổng (dòng 172): trỏ kết quả probe.

### Tests Before
`tests/test_dependency_pins.py` (đọc file bằng `tomllib`, không resolve, không mạng):
- [ ] `test_vnstock_is_pinned_only_in_the_capture_extra` — không requirement nào tên `vnstock` (chuẩn hoá tên) trong `dependencies` hay extra nào
      ngoài `capture`; `capture` có đúng một `vnstock==X.Y.Z`; RED (hôm nay `prod` có `vnstock>=3.4.0`, `pyproject.toml:25`, và không có `capture`).
- [ ] `test_vnstock_lock_is_hash_pinned_and_split_by_index` — `requirements/vnstock/win-py313.lock` tồn tại; mỗi khối `name==version` có ≥ 1
      `--hash=sha256:<64 hex>`; chú thích `# from https://vnstocks.com/api/packages` chỉ ở khối `vnstock`, mọi khối khác `# from https://pypi.org/simple`;
      version vnstock = pin trong `capture`, major 5; không khối nào tên `vnai`, `vnstock3`, `vnstock-installer`, `vnstock-mcp`; RED (file chưa có).
- [ ] `test_lock_project_routes_only_vnstock_to_the_vendor_index` — `requirements/vnstock/pyproject.toml`: index `vnstocks` có `explicit = true`,
      `[tool.uv.sources]` đúng một khoá `vnstock`, `dependencies == ["vnstock==<cùng pin>"]`; repo `pyproject.toml` không có `[tool.uv]`; RED.

`tests/test_data_root.py` (RED cả module: `ModuleNotFoundError: finsec_alpharadar.data_root`):
- [ ] `test_unset_or_empty_root_is_refused`, `test_relative_root_is_refused`.
- [ ] `test_root_outside_any_work_tree_is_accepted` — `work_tree_of` giả trả `None`.
- [ ] `test_find_work_tree_walks_up_to_a_dot_git_dir_or_file` — thư mục tạm có `.git/` (và một cái có file `.git`), không cần git.
- [ ] `test_unignored_root_inside_a_work_tree_is_refused` — `git init` trong `tmp_path` (subprocess git như `tests/test_repo_hygiene.py:23-34`) →
      `DataRootInsideWorkTreeError`, thông điệp nêu work tree.
- [ ] `test_ignored_root_inside_a_work_tree_is_accepted` — cùng repo tạm với `.gitignore` chứa `/capture-data/`.
- [ ] `test_git_failure_is_an_error_not_an_acceptance` — `check_ignore` giả trả 128 → `DataRootError` nêu exit.
- [ ] `test_env_example_declares_an_empty_data_root` — `.env.example` có dòng `FINSEC_DATA_ROOT=` giá trị rỗng; RED.
- [ ] `test_junction_into_a_work_tree_is_resolved_before_the_walk` (red-team r1 LT7) — trỏ một junction (`cmd /c mklink /J` trên win32, `os.symlink`
      nơi khác — không skip) từ thư mục ngoài vào repo git tạm → bị từ chối; RED.
- [ ] `test_backup_dir_variable_uses_the_same_guard` — `resolve_external_dir(env, "FINSEC_BACKUP_DIR")` thiếu/tương đối/trong work tree → lỗi nêu đúng tên biến; RED.

`tests/test_repo_hygiene.py` (thêm):
- [ ] `test_plans_dir_is_ignored` (R1.12) — `git check-ignore -q plans/x.md` trả 0; RED (hôm nay exit 1 — OBSERVED red-team r1).

### Implement
- Bước 2-4 ở trên.

### Tests After
- [ ] Hai module trên xanh; `tests/test_no_order_execution.py` xanh với 3 script mới; `tests/test_agents_md_paths.py` xanh sau khi sửa AGENTS.md;
      `tests/test_repo_hygiene.py` vẫn xanh với `.env.example` mới.

### Regression Gate
- `.venv/Scripts/python -m pip install -e ".[dev]"` (pyproject đổi) rồi `.venv/Scripts/python -m pytest -q` (PowerShell `.\.venv\Scripts\python -m pytest -q`)
  MUST pass, 0 skipped.
- `python -m pytest -q` (Python toàn cục) pass; skip đúng `tests/test_api_server.py`.
- `PYTHONPATH=src python tests/test_end_to_end.py` exit 0.
- `python3.13 harness/scripts/scaffold_standards.py --type system-architecture --check docs/system-architecture.md` và
  `python3.13 harness/scripts/scaffold_standards.py --type code-standards --check docs/code-standards.md` exit 0.
- Sau push: CI của commit `success` trên 3.11 + 3.13 (`gh run view <id> --json conclusion,jobs`).

## Success Criteria
- [ ] [test] 14 test mới RED trước implement, xanh sau; cả suite xanh trong `.venv`, 0 skipped.
- [ ] [test] Lệnh compile R1.3 tái lập lock: chỉ khối vnstock từ index vendor (đã OBSERVED trong scratchpad 2026-10-22; cook ghi lại kết quả của mình).
- [ ] [manual] Output owner-run có mặt trong `C:\finsec-probe\out\<date>\`; hash `before-*` (chụp TRƯỚC `vnstock register`) == `after-*` cho cả hai user,
      `git diff --exit-code HEAD -- AGENTS.md CLAUDE.md .agents` exit 0, bằng chứng OB5 (`net user vnprobe` "Password expires Never", `icacls` có dòng deny,
      `pyvenv.cfg` `home` trong profile của `vnprobe`) (neo `manual_test_anchor.py`).
- [ ] [invariant] Không byte dữ liệu vnstock/cổng nào vào repo: `git status --porcelain` (mặc định, KHÔNG `--ignored`; `plans/` giờ bị ignore — red-team r1 RT8)
      chỉ liệt kê đúng các file trong bảng; `git ls-files plans` rỗng; `git ls-files | grep -iE "\.(jsonl|parquet|whl)$"` chỉ ra 5 file `.jsonl` có sẵn dưới
      `harness/data/port-control/` (fact 7). Cook không bao giờ `git add -A`: chỉ add đúng file trong bảng.
- [ ] [manual] Báo cáo mục 1-3 điền đủ, mỗi dòng có nhãn + ngày; bảng design inputs đủ 7 mục R1.10 (neo `manual_test_anchor.py`; người đọc đối chiếu với
      `probe-summary.json` và output portal).
- [ ] [manual] Dòng Decisions chọn cổng có trong AGENTS.md, ghi từ câu trả lời của owner (neo `manual_test_anchor.py`).

## Risk Assessment
| Rủi ro | Khả năng × Tác động | Xử lý |
|---|---|---|
| `import vnstock` ghi vào AGENTS.md / file chỉ dẫn agent toàn máy (hành vi 4.x trước 4.0.9) | Thấp × Cao | User Windows riêng, cwd ngoài clone, hash before/after cả hai user, env tắt agent setup; lệch = FAIL + khôi phục. |
| Bản 5.1.0 bị yank hoặc file bị xoá trước khi cook | Trung bình × Trung bình | Pin `==` + hash vẫn cài được khi còn file (PEP 592); bản sao wheel riêng dưới data root; nếu mất: re-pin có chủ đích (R1.2). |
| Probe tiêu quota chung với laptop | Trung bình × Thấp | Probe chạy trước khi laptop được đăng ký; rate ramp dừng ở throttle đầu; tối đa ~50 lệnh. |
| WAF HOSE chặn probe | Thấp × Trung bình | ≥ 5 s/host, `pageSize ≤ 20`, 12 vòng; ghi 403/captcha làm dữ kiện chọn cổng. |
| Owner chậm ở OB3/OB4a/OB5 | Trung bình × Cao (chặn cả chuỗi) | Danh sách bước owner nêu ngay lúc duyệt plan (plan.md); phase dừng rõ ràng, không giả định. |
| Key vnstock lọt vào ngữ cảnh agent qua output CLI | Thấp × Cao | Owner tự gõ key; redact chuỗi token dài trong output trước khi ghi file. |
| User probe sửa clone/`.venv`/hook git (ACL `Authenticated Users:(M)` kế thừa — OBSERVED main) | Thấp × Cao | `icacls … /deny vnprobe`; hash phủ `.git/hooks/*`, `.venv/**/*.pth`; hash dev user lưu trong profile dev (red-team r1 RT4, RT5). |
| `plans/` (email owner, ghi chú research) bị `git add -A` đẩy lên repo public | Thấp × Cao | `/plans/` trong `.gitignore` + test; G5 `git ls-files plans` rỗng; tên người trong research đã thay (red-team r1 RT8). |
