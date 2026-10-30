---
id: 261022-1147-phase0a-capture
title: "Phase 0a verification spike and 0b.6 VN capture-only poller"
status: in_progress
mode: hard
tdd: true
branch: claude/phase0a-capture
created: 2026-10-22
author: user:tqanh123
decisions: []
phases:
  - phases/phase-1-vnstock-probe.md
  - phases/phase-2-capture-poller.md
  - phases/phase-3-capture-deploy.md
  - phases/phase-4-edgar-golden-test.md
  - phases/phase-5-alpaca-probe.md
  - phases/phase-6-vn-reconcile.md
  - phases/phase-7-phase0-exit-audit.md
harness_version: 7.3.0
harness_kit_digest: f3739cf6b09e75b96349a5aa5ecd988a782e74601df0468415e63ac82a833331
harness_schema_version: 1.0
---

# Plan: Phase 0a verification spike and 0b.6 VN capture-only poller

> hs:cook đọc file này làm hợp đồng. Nhãn bằng chứng theo `harness/rules/verification-mechanism.md`:
> **OBSERVED** (đã chạy/đọc, kèm ngày), **VERIFIED** (URL nguồn, lấy từ research), **DERIVED** (suy từ OBSERVED, nêu cơ chế),
> **[PRIOR]** (kiến thức có sẵn), **[ASSUMED]** (chưa kiểm, kèm cổng kiểm). Mọi `file:line` đọc trên main checkout `H:\study\Project\FinSec` @ `c9168f0`
> (nhánh `claude/phase0b-foundations`). HEAD của nhánh đó đã sang `a76e7c9`, chỉ đổi số test (267→274) tại `AGENTS.md:51,169` và hai docs, số dòng giữ nguyên
> (OBSERVED `git diff c9168f0 a76e7c9`, 2026-10-22). Cook đọc lại ở base nhánh của mình (sau khi PR 0b merge) trước khi sửa một dòng được trích.
> Research của plan: `research/vnstock-gen5.md`, `research/vn-disclosure-portals.md`, `research/capture-host-windows.md`, `research/vn-calendar-sessions.md`,
> `research/edgar-header.md`, `research/alpaca-probe.md` (trích là `research/<file>.md §n`).

## Tổng quan
Plan này làm roadmap §7 Phase 0a (trừ SSI, §9.5) và 0b.6 (`docs/project-roadmap.md:357-372`, `:394-398`), theo thứ tự ưu tiên owner chốt (D1): (1) probe vnstock,
(2) poller capture, (3) runbook + triển khai + 5 phiên, (4) golden test EDGAR, (5) probe Alpaca, (6) đối chiếu 30 công bố + bảng counts + go/no-go cho từng
target VN. Mục đích: đóng exit gate của Phase 0 (`docs/project-roadmap.md:400-404`) — bốn tiêu chí E1-E4 — và bắt đầu thu dấu thời gian VN, thứ không bao giờ
backfill được (roadmap §8, `docs/project-roadmap.md:562`).

Research đổi thiết kế ở bốn chỗ, đều là hệ quả của dữ kiện chứ không phải đổi scope:
- vnstock Gen 5 Community **không còn `news()`** (gỡ từ 5.0.0a2, `VNSTOCK_FEATURE_REMOVED`; `vnstock_news` trả phí — `research/vnstock-gen5.md` §0.1, §6), nên nguồn
  vnstock của poller là `events()` (có cột `id`, mọi cột ngày chỉ có ngày). "vnstock news/events" của yêu cầu thành "events" — DERIVED.
- PyPI `vnstock` bị quarantine (0 file) và mọi bản dưới 5.1.0 trên index vendor đã bị yank (fact 1-2 của brief, OBSERVED main 2026-10-22); mỗi bản tự tắt sau 365 ngày
  (`research/vnstock-gen5.md` §2) → vnstock chỉ cài từ index vendor bằng lock có hash, và poller phải sống sót khi vnstock chết (D2).
- `ohlcv()` chỉ trả giá đã điều chỉnh, đơn vị nghìn đồng (`research/vnstock-gen5.md` §0.3) — va với D4 (VND đầy đủ, AGENTS.md:16) và roadmap §6.3 (tự điều chỉnh từ bar
  thô, AGENTS.md:153-154). Phase 1 trả lời "Gen 5 có nguồn bar thô nào không"; không có thì ghi thành blocker của Phase 3, không giải ở đây.
- Hai cổng chính thức, không phải một: HOSE JSON API + HNX RSS feed 3 (`research/vn-disclosure-portals.md` kết luận 1-2); owner chốt ở cuối Phase 1 (OB3).

Chế độ: `--hard --tdd` (frontmatter `mode: hard`, `tdd: true`). **Không áp `--deep`, không áp `--parallel`**: chuỗi phase tuyến tính tuyệt đối — mọi phase đều sửa
`AGENTS.md` (và hầu hết sửa ba docs), nên không cặp nào chạy song song được nếu không khai `merger_owned`; một người làm, lợi ích song song ≈ 0. Phần scout file-level
của `--deep` được thay bằng bảng "Related Code Files" + citation chính xác trong từng phase. Không `--grid`.

### Mục tiêu và thước đo (five-hat)
- **JTBD (Product).** Owner cần một kho dấu thời gian VN đáng tin bắt đầu chạy NGAY, và các sự thật nền (vnstock, EDGAR, Alpaca) được đo thật, để Phase 1-4 xây trên
  dữ kiện thay vì giả định.
- **North star (Strategist).** E4: poller chạy 5 phiên giao dịch liên tiếp, có heartbeat mỗi ngày, không khoảng trống chưa ghi. Thước đo phụ: độ trễ phát hiện VN
  (trung vị `first_seen_at − giờ đăng`, mục tiêu < 10 phút — AGENTS.md:75) đo lần đầu ở Phase 6.
- **Guardrail.** Không dữ liệu có bản quyền (vnstock/Alpaca), không key, không liên hệ EDGAR, không tên người từ công bố VN trong file commit; không test nào chạm
  mạng/đồng hồ/sleep; không lệnh giao dịch; $0.
- **UX của owner.** Owner là người duy nhất làm được các bước có mật khẩu/key/máy laptop; mỗi bước của owner để lại một FILE mà cook kiểm được (mục "Bước chặn của owner").
- **Kiến trúc / giao hàng.** Xem "Hợp đồng thay đổi", "Luồng dữ liệu", "Phases"; mỗi tiêu chí nghiệm thu mang spec class `test` / `invariant` / `manual`
  (manual luôn neo `manual_test_anchor.py`).

## Quyết định đã khoá
**Owner, 2026-10-22 — không re-litigate:**
- **Scope (yêu cầu của user).** Roadmap §7 Phase 0a (trừ SSI, §9.5) + 0b.6, theo thứ tự ưu tiên (1)-(6) ở trên; kết quả vào AGENTS.md có ngày và nhãn. Nhánh
  `claude/phase0a-capture` chỉ tạo lúc cook, từ `main`, sau khi plan 0b merge. Mọi phase chạm `tests/`, `pyproject.toml` hoặc `AGENTS.md` có cạnh liên plan
  `{from: 261021-1314-phase0b-foundations/<phase>, to: <phase>}`. Probe mạng nằm trong script spike riêng, không bao giờ trong pytest mặc định (CI public, không key,
  không mạng); test chỉ dùng fixture ghi sẵn hoặc tổng hợp. Không viết lại connector, không chuyển timestamp domain sang PIT (Phase 1) ngoài cái poller cần.
  $0; lưu trữ theo §9.9; các trường PIT tz-aware UTC; không lệnh giao dịch; licence vnstock Community = chỉ nghiên cứu cá nhân, không phân phối lại; dữ liệu thu được
  nằm ở đường dẫn bị ignore; không package tên `data/`/`models/`; code và docs tiếng Anh.
- **D1 Scope: HOLD** — đúng sáu mục.
- **D2 vnstock:** nhận Gen 5 làm nguồn PHỤ. Owner tạo tài khoản Community + API key miễn phí và đăng ký máy dev (probe) và laptop capture. Poller ghi lỗi vnstock là
  outage của nguồn; cổng chính thức gánh việc thu.
- **D3 Heartbeat + backup:** thư mục cloud sync (OneDrive / Google Drive) trên laptop; heartbeat hằng ngày là một file JSON nhỏ trong thư mục đó (owner xem từ điện
  thoại); không kênh mới.
- **D4 Exit gate:** phase cuối chờ 5 phiên giao dịch liên tiếp; triển khai sớm và cook EDGAR / Alpaca / đối chiếu VN trong lúc chờ; phase cuối chạy audit khoảng
  trống và ghi exit vào AGENTS.md.
- **D5 Lịch:** giữ lịch roadmap (2-5 phút 08:00-17:30 ICT ngày giao dịch, hằng giờ ngoài khung; cadence theo từng nguồn trong config). Lý do được chấp nhận: tin đăng
  sau 17:30 vẫn có day 0 là phiên kế tiếp; ít request tới WAF HOSE hơn. Đề xuất 24/7 của `research/vn-calendar-sessions.md` §1.3 KHÔNG áp dụng.
- **D6 Nơi probe vnstock:** một user Windows chuẩn riêng trên máy dev; venv probe và thư mục làm việc ngoài clone; sha256 của AGENTS.md, CLAUDE.md, `.agents/**` và
  file agent toàn máy (~/.claude, ~/.codex, Antigravity) của CẢ HAI user trước và sau `import vnstock`. Agent không gõ được mật khẩu hay API key → mọi lệnh chạy dưới
  user đó là bước OWNER-RUN, output ghi vào thư mục cả hai user đọc được.
- **D7 Tài khoản poller trên laptop:** user chuẩn cục bộ riêng `capture`, Sysinternals Autologon (mật khẩu là LSA secret, admin giải mã được — ghi đánh đổi); scheduled
  task chạy dưới user đó; vnstock đăng ký dưới user đó; client OneDrive/Google Drive chạy trong phiên đó; loại `%USERPROFILE%\.vnstock` khỏi backup.
- **D8 Fixture Form 4:** thay tên / địa chỉ / điện thoại của reporting owner bằng placeholder, giữ nguyên CIK và `<ACCEPTANCE-DATETIME>`; manifest ghi
  `sha256_original` và `sha256_committed`.
- **D9 Plans tree được commit (owner, 2026-10-24; AGENTS.md "Decisions", merge ở PR tqanh123/FinSec#7, OBSERVED ở main `9214447`).** Nguyên văn:
  "- 2026-10-24 — The plans tree is committed (plans, research, harness artifacts; it was untracked before). Harness records name the owner `user:tqanh123`, not a personal email: each clone sets `HARNESS_USER=tqanh123` (the harness adds the `user:` prefix), e.g. in the gitignored `.claude/settings.local.json`; git commits use 52412090+tqanh123@users.noreply.github.com."
  D9 thay thế VL-4 và phần `.gitignore` của red-team r1 RT8: P1 không ignore `plans/` và không có `test_plans_dir_is_ignored` (P1 R1.12); rủi ro PII của RT8 do
  scrub danh tính (`user:tqanh123`, email noreply), placeholder `<insider-*>` trong research và G5 (Acceptance) xử lý.

**Thiết kế của main session (ghi rõ là của main; red-team có thể thách):**
- M1 `tzdata` là dependency lõi không marker (P2). M2 Một biến môi trường cho data root ngoài repo, `FINSEC_DATA_ROOT` (tuyệt đối, ngoài clone), dùng chung cho spike và
  capture (P1). M3 Script spike dưới `scripts/spike/`. M4 Fixture EDGAR dưới `tests/fixtures/edgar_headers/` + `.gitattributes` `tests/fixtures/edgar_headers/** -text`
  (P4). M5 Luật go/no-go chính = luật đăng ký trước của roadmap t ≈ IC·√N_eff ở t = 2 (MDE = 2/√N_eff), bậc GO ≤ 0.05 < LIMITED ≤ 0.10 < NO-GO; MDE power 80%
  (≈ 2.8/√N_eff) chỉ là dòng độ nhạy (P6).

**Thiết kế của planner (chưa ai duyệt — để validate/red-team):**
- PL1 Guard `src/finsec_alpharadar/data_root.py` là module cấp package (capture + spike dùng chung), viết ở P1 vì chính probe P1 là lần ghi dữ liệu có bản quyền đầu tiên.
  `vnstock_probe.py` và `vn_counts.py` tự đứng (không import repo) vì chạy dưới user probe bằng `python -I`.
- PL2 Lock project `requirements/vnstock/{pyproject.toml, win-py313.lock}` (định tuyến index tường minh qua `[tool.uv.sources]` mà không đụng `pyproject.toml` gốc);
  venv laptop = cài đúng lock này; capture chạy từ clone qua `PYTHONPATH`, không `pip install -e`.
- PL3 Schema bản ghi thô v1 (P2 R2.2): `information_time` tạm = `first_seen_at` có `information_time_basis`; khoá seen gồm `raw_sha256` (giữ mọi bản sửa).
- PL4 Khoảng trống = quá `gap_deadline(p) = next_due(next_due(p)) + max_poll_duration_s` (bước lịch hai lần từ poll thành công trước — chặt ở ranh giới 08:00/17:30
  và đúng với ngày nghỉ trong config), phủ bằng HỢP các outage (P2 R2.5, R2.12); quy tắc `pit_quality = late` (P2 R2.6).
- PL5 Không parser XML cho RSS (trích guid bằng regex có giới hạn cỡ) — không mở rộng entity.
- PL6 Task Scheduler logon type Password (đăng ký qua bộ tham số `User`/`Password`, không `Principal`) + wrapper `.cmd` (`python -P`) + WorkingDirectory =
  data root (P3 R3.3) — `[ASSUMED]` tới cổng burn-in (poll `run_mode = 'run'` sau reboot).
- PL7 Heartbeat mỗi 15 phút + một file mỗi ngày UTC; backup ngày UTC đã đóng lúc 07:15 ICT; ghi vào thư mục sync có retry, lỗi thành DEGRADED.
- **Sau red-team r1 (2026-10-22; main chấp nhận mọi finding RT1-RT15, LT1-LT12, kèm phán quyết):** `/plans/` vào `.gitignore` (RT8 — bị D9 thay thế
  2026-10-24: `plans/` được commit, P1 không chạm `.gitignore`); fsync mọi file đã
  chạm trước commit + đối soát đuôi archive vào `seen` lúc khởi động + luật "first_seen_at sớm nhất thắng" (RT12); watchdog trên daemon thread riêng,
  deadline tổng mỗi request, worker vnstock có timeout, tick giữa các nguồn (RT2/LT1); Python cài cho chính user chạy nó, không fallback uv (RT6);
  P3 một commit trước triển khai, dòng mở cửa sổ ghi ở commit phase đầu tiên sau khi cửa sổ mở (P4-P7), canary chụp lúc `run` + `canary-reset` (LT4); thủ tục khởi động lại cửa sổ / hotfix, dòng mới
  nhất thắng, `sc.exe` (LT2).
- PL8 Luật power là module có test `quant/power_rule.py`; R1-VN dùng cùng bậc (luật IC cho target phân loại là quy ước, ghi rõ).
- PL9 Mẫu 30 công bố lấy trước từ dữ liệu poller của cửa sổ (đo luôn độ trễ phát hiện), thiếu thì bù từ lịch sử cổng.
- PL10 Phân loại phiên US (`us_session_bucket`) nằm trong P4 vì roadmap §6.4 mục 2 gộp nó vào golden test EDGAR (`docs/project-roadmap.md:327`).
- PL11 Draft PR sau P1 để CI chạy từ phase đầu (như 0b VL-1); merge bằng merge commit (như 0b VL-2). Owner xác nhận ở VL-2 (2026-10-24): một PR cho
  cả plan, merge sau P7.

## Bước chặn của owner (OWNER BLOCKING — không bao giờ coi là đã xong)
Mỗi bước để lại một FILE hoặc một exit code mà cook kiểm; lời xác nhận miệng không đủ. `<backup-mirror>` = đường dẫn phía máy dev của thư mục sync D3 (P3 R3.1).

| OB | Ai | Việc | Chặn | Cook phát hiện xong bằng |
|---|---|---|---|---|
| OB1 | owner | Alpaca paper keys `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` trong `.env` (thay placeholder; giữ `ALPACA_PAPER_TRADE=true`) | P5 | `.venv/Scripts/python scripts/spike/alpaca_probe.py --check-env` → exit 0, in `ok`; request đầu HTTP 200 |
| OB2 | owner | Laptop capture cấu hình theo runbook mục 1-8 (power, lid, active hours + pause update, W32Time, user `capture` PasswordNeverExpires, ACL `C:\finsec`, Python 3.13 cài cho `capture`, venv từ bộ wheel, clone) | P3 | `<backup-mirror>\ops\laptop-config-<yyyyMMdd>.txt` đạt từng dòng P3 R3.4 (gồm `net user capture`, `icacls`, `pyvenv.cfg`) |
| OB3 | owner | Chọn trang/endpoint cổng chính thức từ bảng ứng viên P1 (đọc ToS HOSE trong trình duyệt) | cuối P1 → chặn P2 | dòng Decisions có ngày trong AGENTS.md, cook ghi từ câu trả lời của chính owner (AskUserQuestion ở main) |
| OB4a | owner | Tài khoản vnstock Community + API key; `vnstock register` dưới user probe trên máy dev (tự gõ key) | P1 | `C:\finsec-probe\out\<date>\vnstock-status.txt` (đã redact) có tier + device |
| OB4b | owner | `vnstock register` dưới user `capture` trên laptop | P3 | `<backup-mirror>\ops\vnstock-status.txt` + hai file hash before/after bằng nhau |
| OB5 | owner | User Windows chuẩn riêng trên máy dev (PasswordNeverExpires) + `icacls <clone> /deny vnprobe:(OI)(CI)(R,W,D)` + `icacls H:\study\Project\FinSec_data /deny vnprobe:(OI)(CI)(R,W,D)` (data root dev; cùng ACL kế thừa `Authenticated Users:(M)` — OBSERVED 2026-10-25) + Python 3.13 cài cho `vnprobe` + venv từ bộ wheel + thư mục chung `C:\finsec-probe\` | P1 | `C:\finsec-probe\out\<date>\ob5\` có `net user vnprobe` ("Password expires Never"), output `icacls` của clone và của data root, `pyvenv.cfg` (`home` trong profile `vnprobe`); hash `before-probe` chụp trước `register` |
| OB6 | owner | `EDGAR_IDENTITY` thật trong `.env` (không phải `research@yourdomain.com`) | P4 | `.venv/Scripts/python scripts/spike/record_edgar_headers.py --check-env` → exit 0, in `ok`; request đầu không phải 403 |
| OB7 | owner | Thư mục backup (tài khoản cloud phụ) + `FINSEC_BACKUP_DIR` + quyết định Autologon trên laptop + thư mục đọc được từ máy dev | P3 | `heartbeat.json` xuất hiện trong `<backup-mirror>`; owner thấy trên điện thoại (manual, neo `manual_test_anchor.py`); lựa chọn Autologon ghi trong dòng D7 |
| OB8 | owner | Lệnh OWNER-RUN: `vnstock_probe.py probe` (P1), `vn_counts.py events` (P6) dưới user probe; `laptop-head-<yyyyMMdd>.txt` (P7) | P1, P6, P7 | file output tương ứng trong thư mục chung / `<backup-mirror>\ops\` |

Owner nhận danh sách này lúc duyệt plan, để các bước của P3-P6 xong trước khi chuỗi tới đó (chuỗi tuyến tính: một bước chặn đứng thì mọi phase sau chờ).

## Ràng buộc (constraint-scan)
Main chạy ngày 2026-10-22:
- `harness/data/ownership.yaml:8-17` — zone `docs` → `docs/` [IN cho `docs/capture-runbook.md`, `docs/phase0-spike-report.md` và ba docs], `plans` → `plans/` [IN],
  `state` → `.harness/state/`, `standards` → `harness/standards/` (plan không ghi). `src/`, `tests/`, `scripts/`, `requirements/`, `configs/`, `.gitattributes`, `.gitignore`
  ngoài mọi zone; `ownership.yaml:6-7`: bảng chỉ ràng buộc script của harness → không xung đột.
- `harness/data/stage-policy.yaml:59` push `requires: [verification]`; `:64`, `:69`, `:73` pr/merge/ship `requires: [verification, review-decision, plan-approval]` → mỗi
  phase phát `verification-P<n>.json` (khai ở `post` trong `plan-graph.yaml`).
- `harness/hooks/write_guard.py:148` GUARD_LIST chỉ canh file harness, `docs/decisions.*` và `plans/*/artifacts/plan-approval.*` → không file nào của plan bị canh;
  `harness/data/write-guard.yaml:12` `extra_guarded: []`; `harness/data/write-deny-policy.yaml:14` `soft_rules: []`.
- `harness/data/protected-branches.yaml` bảo vệ `main`, `master`, `release/*` → commit trên `claude/phase0a-capture`, merge qua PR (merge commit, như 0b VL-2).
- Claude Code hooks đang OFF (AGENTS.md:127) → gate local là các lệnh ghi trong từng phase.
- `.gitattributes` chỉ có `harness/** -text` (OBSERVED, dòng 4) → P4 thêm luật fixture EDGAR.
- Không thêm `*.jsonl` vào `.gitignore` toàn cục: 5 file `.jsonl` tracked dưới `harness/data/port-control/` (fact 7) → data root ngoài clone, guard từ chối gốc trong
  work tree trừ khi bị ignore.
- `tests/test_no_order_execution.py:19-35` quét `src/**/*.py`, `scripts/**/*.py|*.sh`, `tests/**/*.py` → probe Alpaca chỉ `https://data.alpaca.markets`, urllib, không
  SDK, không chạm host trading kể cả tra asset.
- `tests/test_agents_md_paths.py:17-19,83-94` → chỉ backtick một path mới trong commit tạo ra nó; không backtick `plans/…` hay data root.
- Ngôn ngữ: prose plan tiếng Việt (`output_config.py --resolved` → `language: vi`); code, AGENTS.md, docs tiếng Anh (AGENTS.md:158); trích dẫn giữ nguyên văn.
- Không symbol nào đang có bị đổi chữ ký: plan chỉ thêm module (`data_root`, `capture/*`, `connectors/edgar_acceptance`, `quant/power_rule`), thêm tên vào `__all__`
  của `connectors/__init__.py` và `quant/__init__.py`, và thêm tham số vào `tests/test_repo_hygiene.py:48-61` → không cần liệt kê caller (`code.callers`).

## Nơi chạy cook
Cook chạy ở **main checkout** `H:\study\Project\FinSec`, sau khi PR của plan 0b đã merge vào `main`: `git fetch origin` rồi
`git switch -c claude/phase0a-capture origin/main`; `plans/` được track trên `main` (D9; OBSERVED ở `9214447`: 108 file) nên có sẵn trên nhánh mới.
**Điều kiện trước** (sau `git fetch origin`, trước `git switch`): amendment 2026-10-25 và lần duyệt lại đã merge vào `main`;
`git show origin/main:plans/261022-1147-phase0a-capture/phases/phase-1-vnstock-probe.md | grep -c "Supersede[d] by D9"` → 1 (`[d]` để chính lệnh này không tự khớp),
khác → DỪNG (bản trên `main` còn là hợp đồng cũ ignore `plans/`). Lý do chạy ở main checkout như plan 0b
"Nơi chạy cook", trừ vế `plans/` (một worktree giờ có `plans/` qua git): worktree không có state của installer (`.worktreeinclude` chỉ mang `harness/**` +
`.claude/settings.json`) và strict verify trên tree chưa cài báo drift. Gọi `hs-run` với `HARNESS_DATA_ROOT` tuyệt đối và KHÔNG đặt `HARNESS_BIN_ROOT` bằng repo (OBSERVED 2026-10-22: đặt thế thì `hs-run` trả
`state: error` "SHARED bin root … refusing"; chỉ `HARNESS_DATA_ROOT` → exit 10 bình thường — P7 R7.5). Sau mỗi lần `pyproject.toml` đổi (P1, P2):
`.venv/Scripts/python -m pip install -e ".[dev]"`. Nếu plan 0b đã được archive vào `plans/archive/` trước khi cook: sửa mỗi `from:` liên plan thành
`archive/261021-1314-phase0b-foundations/P5` (một dòng mỗi cạnh), không waive. Máy dev cần `FINSEC_DATA_ROOT` = `H:\study\Project\FinSec_data` (owner tạo 2026-10-25, ngoài clone; OBSERVED: `git -C H:/study/Project/FinSec_data rev-parse`
→ "not a git repository"); OB5 chặn `vnprobe` khỏi thư mục này. **Trước commit đầu:** `git config user.email` → `52412090+tqanh123@users.noreply.github.com` và
`HARNESS_USER` = `tqanh123` (D9; OBSERVED 2026-10-25 trong worktree: cả hai đúng — địa chỉ noreply đến từ config local của repo, mọi worktree dùng chung; config
toàn cục của máy KHÔNG phải noreply); khác → DỪNG.

## Features
- `vnstock-pinned-env` — môi trường vnstock Gen 5 khoá hash: vnstock chỉ từ index vendor, mọi gói khác chỉ từ PyPI, cô lập dưới user riêng.
- `external-data-root` — mọi byte dữ liệu có bản quyền hay có tên người chỉ rơi vào `FINSEC_DATA_ROOT` ngoài repo; gốc trong work tree bị từ chối trừ khi git ignore nó.
- `vn-source-facts` — sự thật đo được về vnstock (rate limit, độ sâu, đơn vị, điều chỉnh, giờ trong ngày, mã huỷ niêm yết, hạn dùng) và các cổng VN (robots, giờ đăng,
  độ trễ, độ sâu).
- `vn-disclosure-capture` — poller chỉ-thu-thập: HOSE API + HNX RSS feed 3 + vnstock events; bản ghi thô v1 append-only có `first_seen_at` UTC; outage log;
  `pit_quality = late` sau khoảng trống.
- `capture-heartbeat` — heartbeat JSON trong thư mục cloud, owner xem từ điện thoại.
- `capture-backup` — backup đêm ra ngoài máy, có manifest sha256 đã kiểm, không bao giờ xoá.
- `capture-audit` — lệnh audit: khoảng trống chưa ghi, heartbeat mỗi ngày, độ lệch đồng hồ, chuỗi ngày giao dịch đạt.
- `capture-laptop-runbook` — laptop dự phòng chạy như máy chủ: không ngủ, NTP, tự khởi động lại, Autologon, cửa sổ 5 phiên có luật đóng băng.
- `edgar-information-time` — `<ACCEPTANCE-DATETIME>` → UTC tz-aware, raise thay vì đoán, phân loại phiên US, golden test trên bốn accession thật.
- `alpaca-facts` — sự thật đo được về SIP lịch sử trên Basic, `adjustment`, độ phủ mã huỷ niêm yết; feed đã chọn trong `.env.example`.
- `vn-counts-go-no-go` — đối chiếu 30 công bố, bảng counts, độ trễ phát hiện VN, và go/no-go cho R1/D2/D4 theo luật power đăng ký trước.
- `phase0-exit-record` — exit Phase 0 ghi có ngày khi E1-E4 cùng đạt.

## Hợp đồng thay đổi (System Architect)
| Hợp đồng | Trước | Sau | Phase |
|---|---|---|---|
| `pyproject.toml` extra `prod` | có `vnstock>=3.4.0` (`pyproject.toml:25`; không resolve được từ PyPI) | bỏ vnstock | P1 |
| `pyproject.toml` extra `capture` | — | `vnstock==5.1.0` (bản đã probe) + comment "chỉ từ index vendor qua lock" | P1 |
| Lock vnstock | — | `requirements/vnstock/pyproject.toml` (index `vnstocks` explicit) + `win-py313.lock` (`--generate-hashes --emit-index-annotation`) | P1 |
| `finsec_alpharadar.data_root` | — (không code nào đọc env, `docs/code-standards.md:85`) | `resolve_data_root(env)` đọc `FINSEC_DATA_ROOT`; `DataRootError`, `DataRootInsideWorkTreeError` | P1 |
| `.env.example` | dòng 1-20 | + `FINSEC_DATA_ROOT=` (P1), `FINSEC_BACKUP_DIR=` (P2), `ALPACA_DATA_FEED=<sip\|iex>` (P5) | P1, P2, P5 |
| `pyproject.toml` `dependencies` | 5 gói (`pyproject.toml:11-17`), không tzdata | + `"tzdata"`, không marker, không cận dưới (P2 R2.16, red-team r1 LT11) | P2 |
| Schema bản ghi thô v1 | — | P2 R2.2 (bảng trường) + R2.3 (bố cục đĩa); nguồn sự thật trong `docs/system-architecture.md` | P2 |
| State capture SQLite v1 | — | P2 R2.4 (`seen`, `polls`, `outages`, `blobs`, `alive`, `heartbeats`, `backups`, `kv`, `meta`) | P2 |
| CLI `python -m finsec_alpharadar.capture` | — | `run`, `once [--all]`, `heartbeat`, `backup --day`, `audit …`, `check-root`, `canary-reset`; khoá một tiến trình cho `run`/`once`/`heartbeat`/`backup`; exit 0/1/2/3/4 | P2 |
| `configs/capture.toml` | — | `[schedule]`, `[[holidays]]`, `[[sources]]`, `[heartbeat]` (TOML, `tomllib`) | P2 (giá trị cuối trong commit P3; sau đó chỉ qua hotfix R3.9) |
| Heartbeat `capture-heartbeat/1` | — | JSON khoá cố định, `OK\|DEGRADED\|DOWN`, canary chụp lúc `run`, `wu_pause_expiry` (P2 R2.10) | P2 |
| Backup đêm | — | `daily/<d>/{capture-<d>.tar.gz, state-<d>.sqlite (journal DELETE), manifest-<d>.json}` + đọc an toàn `iter_backup_members` (P2 R2.11) | P2 |
| Audit | — | đọc snapshot `mode=ro&immutable=1`; cặp ảo hai mép; ≥ 1 poll ok mỗi nguồn cổng trong 08:00-17:30 mỗi ngày giao dịch (P2 R2.12) | P2 |
| Dòng cửa sổ trong AGENTS.md | — | "capture window started/restarted <date> at <sha>", dòng mới nhất thắng (ghi lần đầu ở commit phase đầu tiên sau khi cửa sổ mở: P4, P5, P6 hoặc P7; sau đó theo R3.9) | P4-P7, R3.9 |
| `finsec_alpharadar.connectors.edgar_acceptance` | — | `extract_acceptance_raw`, `parse_acceptance_datetime`, `us_session_bucket`, họ `EdgarAcceptanceError` | P4 |
| `.gitattributes` | `harness/** -text` | + `tests/fixtures/edgar_headers/** -text` | P4 |
| `data_root.resolve_external_dir` | — | resolve() trước khi walk; dùng cho cả `FINSEC_DATA_ROOT` và `FINSEC_BACKUP_DIR` | P1 (dùng ở P2) |
| `finsec_alpharadar.quant.power_rule` | — | `effective_n`, `mde`, `assess`, `PowerTier`, `PowerAssessment` | P6 |
| Tài liệu | — | `docs/phase0-spike-report.md` (P1, nối ở P4-P7), `docs/capture-runbook.md` (P3) | P1, P3-P7 |

**Tương thích ngược.** Không dữ liệu lưu trữ cũ, không consumer ngoài repo. Không code nào import vnstock hôm nay (`docs/system-architecture.md:118`), và
`pip install ".[prod]"` vốn đã không resolve được (PyPI quarantine), nên bỏ vnstock khỏi `prod` không làm vỡ gì. CI chỉ cài `".[dev]"`. Schema v1 là hợp đồng mới
cho dữ liệu sẽ thu: thay đổi về sau là v2 thêm trường, không bao giờ sửa bản ghi v1 đã ghi.

## Luồng dữ liệu (sau plan)
```
LAPTOP (user "capture", Task Scheduler, cwd = data root, PYTHONPATH = clone\src, venv = lock vnstock)
  configs/capture.toml ─► schedule (ICT, ngày giao dịch, D5) ─► mỗi nguồn đến hạn:
     HOSE API listing ──UrllibFetcher──► body ─┐                (id JSON)
     HNX RSS feed 3   ──UrllibFetcher──► body ─┼─► trích id ─► seen(source, source_id, raw_sha256)?
     vnstock events() ──(import lười)──► rows ─┘                 │ mới / sửa
                                                                  ▼
            snapshot (body đổi) → blobs/<d>/<sha>.bin   item → raw/<source>/<ngày UTC>.jsonl  (schema v1; pit_quality first_seen|late)
            lỗi → outages (source_* / vendor_*);  khởi động / tick → process_down | host_stalled | clock_step
            fsync MỌI blob/JSONL đã chạm → commit state (WAL, FULL) ─► heartbeat.json (15', retry; lỗi → DEGRADED) ─► backup ngày UTC đã đóng (07:15 ICT)
            watchdog (daemon thread, monotonic) ─► os._exit(70) nếu vòng chính kẹt > 900 s; request có deadline tổng; vnstock trong worker có timeout
            khởi động: sửa đuôi JSONL → đối soát đuôi archive vào seen → process_down → đóng outage mồ côi
                                                              │ thư mục cloud sync (D3)
DEV MACHINE ◄─────────────────────────────────────────────────┘
  điện thoại owner đọc heartbeat.json;  audit --state <snapshot> ─► E4 (P3 burn-in, P7 exit)
  vn_reconcile.py (P6): capture-<d>.tar.gz + lịch sử cổng ─► sample (data root) ─► reconcile-summary ─┐
  user probe: vnstock_probe.py (P1), vn_counts.py (P6) ─► C:\finsec-probe\out ──────────────────────────┴─► power_rule.assess ─► go/no-go
  record_edgar_headers.py (P4) ─► SEC (UA = EDGAR_IDENTITY, ≤ 5 rps) ─► data root ─► fixtures (giảm PII Form 4) ─► test_edgar_time
  alpaca_probe.py (P5) ─► data.alpaca.markets ─► data root (body) + summary (số dẫn xuất) ─► AGENTS.md
Mọi số/sự thật ─► docs/phase0-spike-report.md + AGENTS.md "Verified Facts" (có ngày, có nhãn). Không dữ liệu thô nào vào repo.
```

## Phases
| # | Theme | Phụ thuộc | Cỡ |
|---|---|---|---|
| 1 | Vnstock Probe — guard data root, lock hash + extra `capture`, probe vnstock (user riêng) + probe cổng, báo cáo spike, owner chọn cổng (OB3) | 0b/P5 | L (15 tệp: 9 mới, 2 config, 4 docs; OB3, OB4a, OB5, OB8) |
| 2 | Capture Poller — `capture/` 12 module, schema v1, state, heartbeat, backup, audit, `configs/capture.toml`, tzdata | P1, 0b/P5 | XL (29 tệp: 12 module, 10 test, config, 2 cấu hình gốc, 4 docs) |
| 3 | Capture Deploy — runbook + config cuối (một commit, TRƯỚC triển khai), rồi laptop (OB2, OB4b, OB7), task, burn-in 24 h, mở cửa sổ 5 phiên + đóng băng, thủ tục hotfix R3.9 | P2, 0b/P5 | M (5 tệp; phần lớn vận hành) |
| 4 | Edgar Golden Test — script ghi, 8 fixture + manifest, `edgar_acceptance.py`, `test_edgar_time.py`, `.gitattributes` (OB6) | P3, 0b/P5 | M (19 tệp; 9 fixture ghi máy) |
| 5 | Alpaca Probe — script data-host-only, `ALPACA_DATA_FEED` (OB1) | P4, 0b/P5 | S (8 tệp) |
| 6 | Vn Reconcile — 30 công bố, counts, độ trễ phát hiện, `power_rule.py`, go/no-go (OB8) | P5, 0b/P5 | M (10 tệp) |
| 7 | Phase0 Exit Audit — chờ ≥ 5 phiên kể từ dòng cửa sổ mới nhất; E1-E4; ghi exit; hotfix R3.9 nếu audit thấy khoảng trống do code | P6, 0b/P5, ≥ 5 phiên | S (5 tệp docs + phạm vi hotfix có điều kiện) |

Cạnh: P1 → P2 vì P2 cần guard `data_root`, quyết định cổng (OB3) và bảng design inputs. P2 → P3 vì P3 triển khai đúng code P2. P3 → P4 → P5 → P6 → P7 chủ yếu vì
file dùng chung (`AGENTS.md`, `docs/*`, `docs/phase0-spike-report.md`, `tests/test_repo_hygiene.py` ở P2/P5, `.env.example` ở P1/P2/P5); P4-P6 được cook trong lúc cửa sổ
5 phiên chạy (D4) và không chạm `capture/` hay `configs/capture.toml` (đóng băng); P6 dùng dữ liệu của cửa sổ; P7 cần ≥ 5 phiên. Mỗi phase có cạnh liên plan từ
`261021-1314-phase0b-foundations/P5` (chuỗi 0b tuyến tính P1→P5; P1 của nó ghi `pyproject.toml` lần cuối, P5 ghi `tests/` và `AGENTS.md` lần cuối).

## Ma trận test
| File test | Phase | Loại | Spec class | Python toàn cục (không fastapi) |
|---|---|---|---|---|
| `tests/test_data_root.py` | P1 | unit + git tạm | test | chạy |
| `tests/test_dependency_pins.py` | P1 (thêm P2) | invariant repo (tomllib, lock) + tzdata | invariant + test | chạy |
| `tests/test_capture_record.py` | P2 | unit schema | test + invariant (round-trip) | chạy |
| `tests/test_capture_schedule.py` | P2 | unit | test | chạy |
| `tests/test_capture_storage.py` | P2 | unit (tmp_path, SQLite) | test | chạy |
| `tests/test_capture_sources.py` | P2 | unit (fetcher/opener giả) | test | chạy |
| `tests/test_capture_poller.py` | P2 | unit (đồng hồ/sleeper giả) | test | chạy |
| `tests/test_capture_ops.py` | P2 | unit (heartbeat, backup + đọc tar an toàn, audit hai mép + snapshot chỉ đọc, ghi bị khoá) | test | chạy |
| `tests/test_capture_cli.py` | P2 | integration CLI (factory giả, git tạm) | test + invariant (config tuân D5) | chạy |
| `tests/test_capture_invariants.py` | P2 | invariant cấu trúc (AST, regex, subprocess `-I`) | invariant | chạy |
| `tests/test_repo_hygiene.py` (tham số mới P2; test mới P5) | P2, P5 | invariant repo | invariant | chạy |
| `tests/test_edgar_time.py` | P4 | golden + âm tính + phân loại phiên | test | chạy |
| `tests/test_power_rule.py` | P6 | unit | test | chạy |
| `tests/test_no_order_execution.py`, `tests/test_agents_md_paths.py` (có sẵn) | mọi phase | invariant (quét script spike mới; path AGENTS.md) | invariant | chạy |

Không test nào gọi mạng, đọc đồng hồ thật hay sleep (code-standards "Testing", `docs/code-standards.md:61-62`); `tests/test_capture_invariants.py` khoá điều đó cho
`capture/`. Script spike nằm ngoài `testpaths = ["tests"]` (`pyproject.toml:45`) nên không bao giờ bị pytest thu. Payload test của capture viết tay trong code (mã giả,
tiêu đề bịa); fixture thật duy nhất là header SEC công khai (P4), đã giảm PII.

## Out of scope
- SSI (§9.5: không tài khoản/client trước Phase 6) — mục 0a duy nhất không làm; P7 ghi rõ.
- Parse/phân loại công bố, tính `information_time` thật (HOSE `postedDate − 25200`, `min(giờ đăng, first_seen_at)`), khử trùng VI/EN — parser Phase 1/3. `store/`,
  `store.as_of()`, `calendar/` (lịch nghỉ trong `configs/capture.toml` chỉ phục vụ cadence), `refdata/`.
- Viết lại connector, client EDGAR/Alpaca thật (Phase 1), adapter vnstock giá/BCTC (Phase 3), lời giải cho bar thô VN (chỉ ghi blocker), chuyển timestamp domain sang PIT.
- Poll 24/7 (D5 giữ lịch roadmap); VSDC (Phase 3); SSC trong vòng poll (chỉ có ngày, ADF có state); trang POST HNX làm nguồn poll (chỉ dùng cho lịch sử ở P6).
- Telegram/kênh cảnh báo (Phase 4, D3); VPS; mọi dịch vụ trả phí; LLM.
- Kiểm `DO_NOT_TRACK`/`VNSTOCK_TELEMETRY` bằng mitmproxy (`research/vnstock-gen5.md` câu mở 11); review pháp lý ToS VCI/KBS và Điều 16/19 Luật 91/2025/QH15
  (giữ biện pháp: dữ liệu thô chỉ ngoài repo, không tên người trong file commit hay cảnh báo).
- Parse Event Log trong `audit` (đối chiếu Event Log là bước tay ở P3/P7); lint/type-check gate (repo không có cấu hình ruff/mypy, `pyproject.toml:33-38`).
- Sửa quant/Kalman, router, API (các phase sau của roadmap).

## Acceptance (toàn plan)
Mỗi mục có spec class (five-hat): `test` = lệnh/assert tự động; `invariant` = tính chất luôn đúng, kiểm bằng cấu trúc/grep; `manual` = cần hệ ngoài sống (neo
`manual_test_anchor.py`).
- [ ] [test] Mỗi phase red→green TDD; suite xanh sau mỗi phase bằng lệnh thật của repo: `.venv/Scripts/python -m pytest -q` (PowerShell
  `.\.venv\Scripts\python -m pytest -q`) exit 0, 0 skipped; `python -m pytest -q` (Python toàn cục) chỉ skip `tests/test_api_server.py`;
  `PYTHONPATH=src python tests/test_end_to_end.py` exit 0; CI `success` trên 3.11 + 3.13 cho commit của mỗi phase.
- [ ] [test] Lint + type-check: **N/A** — không có cấu hình ruff/mypy (`pyproject.toml:33-38`, OBSERVED), thêm gate lint là ngoài scope. Build: bước build duy nhất là
  `.venv/Scripts/python -m pip install -e ".[dev]"` exit 0 (chạy ở P1, P2).
- [ ] [test] Hai lệnh `scaffold_standards.py --check` exit 0 ở mọi phase sửa hai docs đó.
- **Exit gate roadmap Phase 0 (`docs/project-roadmap.md:400-404`), kiểm ở P7:**
  - [ ] [test] **E1** — pytest thu và pass ≥ 15 test, gồm `tests/test_edgar_time.py` (tạo ở P4; hôm nay đã 274 test ở `a76e7c9` nhưng chưa có golden test EDGAR).
  - [ ] [invariant] **E2** — bảng spike nằm trong AGENTS.md "Verified Facts": dòng OBSERVED có ngày cho vnstock + cổng (P1), EDGAR (P4), Alpaca (P5), dấu thời gian VN +
    counts + go/no-go (P6); SSI ghi rõ là ngoài scope.
  - [ ] [test] **E3** — `/hs:plan` chạy: `HARNESS_DATA_ROOT="$PWD/.harness" harness/bin/hs-run plan next --plan plans/261022-1147-phase0a-capture` exit ∈ {0, 10, 11}.
  - [ ] [test] **E4** — `python -m finsec_alpharadar.capture audit --state <snapshot đã kiểm sha> --from <ngày bắt đầu cửa sổ> --to <d_last> --require-days 5` exit 0
    (5 phiên giao dịch liên tiếp, heartbeat mỗi ngày, không khoảng trống chưa ghi), cộng đối chiếu Event Log [manual].
- [ ] [invariant] Không dữ liệu có bản quyền, key, liên hệ EDGAR hay tên người trong file commit:
  - G1 `git ls-files tests/fixtures` → chỉ 9 file dưới `tests/fixtures/edgar_headers/` (baseline OBSERVED 2026-10-22 ở `a76e7c9`: 0 file);
  - G2 `git ls-files | grep -iE "\.(jsonl|parquet|whl|sqlite|db|bin)$" | grep -vE '^harness/data/port-control/cases/[^/]+/port/ledger\.jsonl$|^plans/[^/]+/artifacts/(auto-decisions|phase-commits|record-log|phase-superseded-P[0-9]+|harness-checks-P[0-9]+)\.jsonl$'`
    → rỗng (grep cuối exit 1). Allowlist theo TÊN, không theo thư mục: 5 ledger `.jsonl` có sẵn dưới `harness/data/port-control/` và các tên artifacts harness đã biết
    dưới `plans/*/artifacts/` (D9; tập đóng của cây 0b, cộng `harness-checks-<phase>.jsonl` mà `harness/scripts/harness_checks.py:52` ghi), nên một capture/spike
    `.jsonl` chép tay vào `artifacts/` vẫn bị bắt (baseline OBSERVED 2026-10-25 ở worktree trên `9214447`: bước đầu ra 9 dòng = 5 ledger + 4 của plan 0b; cả lệnh → rỗng);
  - G3 `git grep -n -F "$EDGAR_IDENTITY"`, `git grep -n -F "$ALPACA_API_KEY"`, `git grep -n -F "$ALPACA_SECRET_KEY"` → không hit (shell đã nạp `.env`);
  - G4 `tests/test_edgar_time.py::test_form4_fixture_is_redacted` xanh; diff AGENTS.md/báo cáo không có tiêu đề công bố hay tên người (đọc lại trước mỗi commit P1, P6);
  - G5 không danh tính cá nhân trong file commit (D9): `git grep -n -E 'tqanh6203[t]kqn' -- .` → không hit (baseline OBSERVED 2026-10-24: 0; `[t]` để chính dòng này không tự khớp); research giữ placeholder `<insider-*>`; file plan được add theo đường dẫn tường minh, không bao giờ `git add -A`;
    metadata commit của nhánh: `git log --format='%ae%n%ce' origin/main..HEAD | sort -u` → chỉ `52412090+tqanh123@users.noreply.github.com` (và `noreply@github.com`).
    Không bao giờ viết địa chỉ cá nhân nguyên văn vào file nào dưới `plans/` (G5 sẽ khớp).
- [ ] [invariant] Mỗi ngày giao dịch của cửa sổ có file `<backup-mirror>\heartbeats\heartbeat-<d>.json` đã sync (red-team r1 RT11; kiểm ở P7).
- [ ] [invariant] Không test nào chạm mạng/đồng hồ/sleep: `tests/test_capture_invariants.py` xanh; `grep -rnE "urlopen\(|socket\.socket|requests\.(get|post)|time\.sleep\(" tests/`
  → chỉ hit nằm trong chuỗi mẫu của `tests/test_capture_invariants.py` (baseline OBSERVED 2026-10-22: 0 hit).
- [ ] [invariant] `tests/test_agents_md_paths.py` xanh sau mỗi lần sửa AGENTS.md; ba danh sách nợ (AGENTS.md "Current Status", `docs/system-architecture.md` "Known gaps",
  `docs/code-standards.md` "Debt") nhất quán (`docs/code-standards.md:106`).

## Rollback
- Mỗi phase là đúng một commit trên `claude/phase0a-capture`. Hoàn tác theo thứ tự ngược `git revert <sha-P7>` … `<sha-P1>` (phase sau xây trên file của phase trước;
  revert lẻ giữa chuỗi có thể conflict ở AGENTS.md/docs — khi đó revert luôn các phase sau). Sau mỗi revert chạy lại regression gate của phase đứng trước.
- Artifacts harness của phase N ghi SAU commit của nó (`verification-P<n>.json`, dòng verify của `phase-commits.jsonl`; `verification-P3.json` sau burn-in) đi vào
  commit của phase N+1, add theo đường dẫn tường minh sau khi G5 sạch; sau P7 thêm đúng một commit `chore(plans): record P7 cook artifacts` trước khi PR
  "Ready for review" — ngoại lệ có tên của "một commit mỗi phase". Revert một phase cũng gỡ artifacts nó mang; chạy lại verify thay vì sửa tay.
- Merge PR bằng "Create a merge commit" để SHA từng phase còn trên `main`; rollback sau merge: nhánh mới từ `main`, `git revert -m 1 <merge-sha>`, đưa qua PR.
- **Dữ liệu đã thu KHÔNG BAO GIỜ bị xoá** — kể cả khi revert P2/P3: data root trên laptop, thư mục backup và output spike nằm ngoài repo, append-only; rollback chỉ
  dừng tiến trình (runbook mục 17: `Unregister-ScheduledTask`, `powercfg /import` baseline, tắt Autologon, trả W32Time về trigger-start), không chạm file dữ liệu.
- Revert P2 khi laptop đang chạy: dừng task TRƯỚC (một outage `process_down` sẽ được ghi khi chạy lại), rồi revert; dữ liệu v1 đã ghi vẫn đọc được bằng `from_json_line`
  của commit cũ.
- Revert P1 bỏ pin + lock (vnstock quay về dòng `prod` không cài được — đúng trạng thái trước plan); revert gỡ các file artifacts của plan mà commit P1 đã thêm; phần còn lại của `plans/` không đổi (D9);
  venv probe và laptop nằm ngoài repo, xoá tay nếu cần.
- P3 có đúng một commit (runbook + config cuối) đứng TRƯỚC triển khai (red-team r1 LT4); revert nó không gỡ triển khai — gỡ bằng runbook mục 17. Mỗi
  hotfix R3.9 là một commit `fix(capture): …` riêng, revert được độc lập (rồi ghi dòng "window restarted" mới).
- Môi trường local: `.venv/` tái tạo bằng `pip install -e ".[dev]"`; `C:\finsec-probe\` và `C:\finsec\venv` ngoài repo.

## Risks
| Rủi ro | Khả năng × Tác động | Giảm thiểu |
|---|---|---|
| vnstock ghi vào AGENTS.md / file chỉ dẫn agent toàn máy (hành vi 4.x) | Thấp × Cao | D6: user riêng, cwd ngoài clone, hash before/after cả hai user; laptop: cwd = data root, heartbeat hash AGENTS.md/CLAUDE.md của clone (canary). |
| vnstock bị yank / hết hạn 365 ngày / server chặn bản cũ | Trung bình × Trung bình | D2 (nguồn phụ, lỗi = outage đã ghi); pin + hash; bản sao wheel riêng; heartbeat báo `days_left ≤ 30`. |
| Laptop ngủ (Modern Standby), Windows Update reboot, đồng hồ lệch | Trung bình × Cao | Runbook (powercfg + power request, active hours + pause, W32Time KB 2385818); burn-in 24 h là cổng; mọi gián đoạn tự ghi outage. |
| Một khoảng trống chưa ghi phá chuỗi 5 phiên | Trung bình × Trung bình | Quy tắc khoảng trống theo lịch + outage tự động (process_down, host_stalled, clock_step); audit; không làm mềm, đếm lại. |
| Owner chậm ở bước chặn → chuỗi tuyến tính đứng | Trung bình × Cao | Danh sách OB đưa lúc duyệt; mỗi phase DỪNG rõ ràng; tuần sạch cho cửa sổ kéo tới 18/12/2026. |
| Backfill P6 ăn quota vnstock chung với laptop trong cửa sổ | Trung bình × Trung bình | Chạy đêm, rate dưới ngưỡng an toàn, dừng ở quota < 30%; lỗi quota trên laptop là outage đã ghi của nguồn phụ. |
| Dữ liệu có bản quyền / tên người / key lọt vào repo public — kể cả `plans/`, public từ D9 | Thấp × Cao | Guard data root; payload test tổng hợp; fixture SEC giảm PII; text plan, research và artifacts không bao giờ mang dữ liệu có bản quyền, key, liên hệ EDGAR hay tên người (D9); grep G1-G5 ở Acceptance (G2, G3, G5 chạy trên toàn repo nên phủ cả `plans/`); pre-push gate quét secret (AGENTS.md:126). |
| HOSE đổi API SPA / WAF chặn | Trung bình × Trung bình | Outage có tên; HNX chạy song song; probe P1 đo WAF; `pageSize ≤ 20`, ≥ 5 s giữa trang. |
| Schema v1 thiếu thứ Phase 1/3 cần | Trung bình × Cao | Giữ byte gốc (blob + item nguyên văn + sha + descriptor); v2 chỉ thêm trường. |
| Phase 2 lớn (29 tệp) | Trung bình × Trung bình | TDD theo thứ tự module, test đích sau mỗi module, một commit khi cả gate xanh. |
| Poll/vnstock treo đứng cả poller; thư mục sync khoá file làm chết tiến trình (RT2, RT11) | Trung bình × Cao | Watchdog daemon thread + deadline tổng + worker có timeout; ghi heartbeat/backup có retry, lỗi → DEGRADED. |
| Audit PASS dù không thu được gì / im lặng sau poll cuối (RT1) | Thấp × Cao | Cặp ảo hai mép + ≥ 1 poll ok mỗi nguồn cổng trong giờ mỗi ngày giao dịch. |
| Mật khẩu user `capture`/`vnprobe` hết hạn sau 42 ngày (RT3) | Trung bình × Cao | `PasswordNeverExpires` + bằng chứng `net user`; lệnh cứu không gõ mật khẩu literal. |
| "User riêng" không cô lập vì ACL kế thừa `Authenticated Users:(M)` (RT4) | Thấp × Cao | `icacls … /deny vnprobe`; `C:\finsec` bỏ kế thừa, `capture` chỉ RX; hash `.git/hooks`, `.venv/**/*.pth`. |
| Crash giữa archive và state sinh bản trùng mang `late` (RT12) | Thấp × Trung bình | fsync mọi file trước commit + đối soát đuôi archive vào `seen` + luật "first_seen_at sớm nhất thắng". |
| Cửa sổ gãy giữa chừng, không có đường làm lại (LT2) | Trung bình × Trung bình | Thủ tục khởi động lại cửa sổ / hotfix R3.9; dòng mới nhất thắng; kiểm hạn pause Windows Update. |
| Không có bar thô VN trong Gen 5 | Cao × Trung bình (Phase 3) | P1 ghi blocker có ngày vào AGENTS.md; Phase 3 tìm nguồn khác; không giải ở đây. |
| `.venv` thiếu tzdata (CI ubuntu che) | Cao × Thấp | tzdata lõi (P2) + test fail-không-skip + `pip install -e` trong gate. |

## Kiểm tra plan-graph (planner tự chạy, OBSERVED 2026-10-22)
- Trên bản mirror trong worktree: `PYTHONIOENCODING=utf-8 python H:\study\Project\FinSec\harness\scripts\plan_graph.py <mirror>\plans\261022-1147-phase0a-capture --require`
  → exit 2, chỉ vì bảy dòng `cross-plan: cross-plan edge 261021-1314-phase0b-foundations/P5 -> P<k> names no plan under …\worktrees\…\plans` (mirror không có plan 0b — lỗi
  đã dự kiến); ngoài ra `parallel batches: [['P1'], ['P2'], ['P3'], ['P4'], ['P5'], ['P6'], ['P7']]`, `import-edge-scan: no fresh snapshot of HEAD …` (thông tin), và ba
  dòng advisory `phase-files: P1|P2|P5 declares .env.example …` — đã biết: đuôi `example` không thuộc `_FILE_EXTS` (`harness/scripts/plan_graph.py:597-601`), file vẫn
  được khai để quyền sở hữu đầy đủ (như plan 0b P5).
- Mô phỏng bố cục thật (copy plan này + plan 0b vào cùng một `plans/` trong scratchpad): cùng lệnh `--require` → **exit 0**, chỉ còn ba advisory `.env.example`.
- Chạy lại sau khi áp red-team r1 (2026-10-22): mirror → cùng 7 lỗi "names no plan" + 3 advisory `.env.example`; bố cục mô phỏng → exit 0. P7 khai
  thư mục `src/finsec_alpharadar/capture/` và glob `tests/test_capture_*.py` cho hotfix có điều kiện — chiều ngược bỏ qua thư mục/glob nên không có dòng
  mismatch.
- `--facts` (cùng bố cục mô phỏng, sau red-team r1) → `declared_files: 53`, `directories: 11`, `phases: 7`, `independent_clusters: 1`, `longest_chain: 7`, `collector_phases: []`,
  `old_plan_count: 0`.
- Khai thư mục `tests/fixtures/edgar_headers/` thay vì từng fixture (P4 "Ghi chú parser") — không sinh dòng mismatch nào (OBSERVED ở cả hai lần chạy).
- Main chạy lại ở main checkout, nơi plan 0b tồn tại.

## Validation Log
Validate gate chạy ở main ngày 2026-10-24 (`skill_config.py --resolved` → `plan.validation.mode: prompt`, 3-8 câu, focus: assumptions, risks, tradeoffs,
architecture); owner trả lời qua AskUserQuestion. Lượt hỏi đầu (2026-10-22) bị owner từ chối khi chưa trả lời; owner yêu cầu hỏi lại và trả lời lượt này.
Quyết định chép nguyên văn lựa chọn của owner.

| VL | loại | chủ đề | quyết định của owner | ghi DEC? / nơi plan mang nó |
|---|---|---|---|---|
| VL-1 | Assumptions / Scope | Seed universe cho nguồn `vnstock_events` trên laptop (không có chủ trước validate) | "VN30 tại ngày P1" | Không ghi DEC (tham số config). `phases/phase-1-vnstock-probe.md` R1.10 + bước probe rate limit; `phases/phase-2-capture-poller.md` R2.15 (`symbols`, comment ngày as-of). |
| VL-2 | Architecture (quy trình) | Thời điểm PR / merge | "Một PR, merge sau P7" | Không ghi DEC. `plan.md` PL11; `phases/phase-1-vnstock-probe.md` bước 12 (draft PR); `phases/phase-7-phase0-exit-audit.md` bước 5 (merge commit sau cook). |
| VL-3 | Risks / Trade-offs | Hotfix `capture/` giữa cửa sổ 5 phiên | "Đếm lại từ đầu" | Không ghi DEC riêng: thành một vế của dòng Decisions D4 mà Phase 3 thêm vào AGENTS.md. `phases/phase-3-capture-deploy.md` R3.7 (restart cùng SHA = outage đã ghi; SHA mới = đếm lại), R3.9 (Ghi lại), delta AGENTS.md D4; `phases/phase-7-phase0-exit-audit.md` Risk. Trước đó R3.9 ghi "redeploy có chủ đích thì đếm tiếp" — đã sửa theo VL-3. |
| VL-4 | Assumptions / Risks | Ignore `/plans/` (red-team r1 RT8) | "Ignore /plans/" — superseded by D9 (owner, 2026-10-24) | Không ghi DEC. Superseded by D9: không còn nơi nào mang VL-4 — `phases/phase-1-vnstock-probe.md` R1.12 là ghi chú superseded; G5 nay là kiểm danh tính của D9. |

- Không ghi DEC qua `decision_register.py`: luật Agent Memory Protocol của dự án đặt mọi quyết định bền vững vào AGENTS.md "Decisions" — cook thêm các
  dòng D2-D8 + dòng cửa sổ ở P1-P7 như từng phase ghi; một sổ DEC thứ hai sẽ thành nguồn song song (như plan 0b).
- `[ASSUMED]` còn lại đều có cổng kiểm và không cần owner quyết: logon type Password của Task Scheduler (burn-in P3), header `Origin`/`Referer` của HOSE
  (probe P1), lệnh cài offline đủ 6 gói (lần owner-run OB5), ngày nghỉ 2026-11-24 của sàn (thông báo HOSE/HNX), endpoint tìm CIK issuer Form 4 và ngày
  của ba accession Apple (lúc ghi fixture P4), đường import Gen 5 và tên nhóm `ref.index.members` (probe P1), dấu offset `w32tm /stripchart` (burn-in P3),
  thư mục quy tắc toàn cục của Antigravity (probe P1), ρ = 0.10 (P6, thay bằng ρ̂ ở Phase 3), khoá registry hạn pause Windows Update (P3), bảng mã lỗi
  vnstock (probe P1), chính sách mật khẩu laptop (đã vô hiệu bằng `PasswordNeverExpires`), `mklink /J` không cần admin (RED đầu của P1).
- Hai quyết định owner đưa ra lúc cook, không điền trước: cặp cổng (OB3, cuối P1; đề xuất HOSE API + HNX RSS feed 3) và đường dẫn thư mục backup (OB7).

**Amendment 2026-10-25 (owner approved).** D9 (AGENTS.md "Decisions", 2026-10-24: `plans/` được commit, danh tính `user:tqanh123`, email noreply) áp vào plan; mọi
quyết định khác giữ nguyên (gồm luật pin vnstock). Thay đổi: thêm D9 vào "Quyết định đã khoá"; P1 bỏ thay đổi `.gitignore` và `test_plans_dir_is_ignored` (R1.12 thành
ghi chú "superseded by D9"; `.gitignore` và `tests/test_repo_hygiene.py` rời "Related Code Files" của P1 và `files_to_modify` P1 trong `plan-graph.yaml`; P1 còn 15 tệp,
13 test mới); bỏ hàng `.gitignore` của "Hợp đồng thay đổi"; sửa các dòng coi `plans/` là untracked/ignored (bullet red-team r1 trong "Thiết kế của planner",
"Nơi chạy cook", bảng Phases P1, Ma trận test, G2 và G5 của Acceptance, Rollback P1, Risks; P1 bước 0 và 3, delta `docs/code-standards.md`, Success Criteria,
Risk); `FINSEC_DATA_ROOT` dev = `H:\study\Project\FinSec_data` (owner tạo, ngoài clone); VL-4 ghi superseded; hàng RT8 của disposition r1 viết lại cột cuối.

**Amendment 2026-10-25, lần 2 (sửa sót của cùng amendment sau một lần kiểm độc lập).** Điều kiện trước "đã merge vào `main`" (grep `Supersede[d] by D9`) ở
"Nơi chạy cook" và P1 bước 0; kiểm `git config user.email` + `HARNESS_USER` trước commit đầu và kiểm metadata commit trong G5; G2 (và Success Criteria P1)
allowlist theo TÊN artifacts harness thay vì cả `plans/*/artifacts/`; OB5 thêm `icacls … /deny vnprobe` cho data root dev `H:\study\Project\FinSec_data`; invariant
`git status --porcelain` của P5 cho phép file của plan; Rollback có commit sở hữu cho artifacts ghi sau commit (P3 câu thứ tự commit, P7 bước 5 trỏ về đó) và
sửa câu "revert không chạm `plans/`"; bỏ vế "`hs-run` đường dẫn trần đi ngược lên `plans/`" khỏi lý do "Nơi chạy cook"; cột cuối VL-4; cite RT5/LT12 theo số dòng
mới của P1 và RT8 thêm «quote».

### Whole-Plan Consistency Sweep
- Files reread end to end (không chỉ diff), ở main ngày 2026-10-24, sau validate: `plan.md`, `phases/phase-1-vnstock-probe.md`, `phases/phase-2-capture-poller.md`,
  `phases/phase-3-capture-deploy.md`, `phases/phase-4-edgar-golden-test.md`, `phases/phase-5-alpaca-probe.md`, `phases/phase-6-vn-reconcile.md`,
  `phases/phase-7-phase0-exit-audit.md`.
- Decision deltas checked: 55 (8 D, 5 M, 11 PL, 4 VL, 15 RT + 12 LT của red-team r1) — mỗi phase đối chiếu với từng quyết định đã ghi.
- Reversed decision found and fixed: 1 — P3 R3.9 ghi "redeploy có chủ đích thì chuỗi đếm tiếp", trái VL-3 → sửa ở R3.7, R3.9, dòng Decisions D4 của P3
  và Risk của P7.
- Reconciled stale references: 12 — dòng mở cửa sổ "ghi ở P4" (bản vá LT4 của main: commit phase đầu tiên sau khi cửa sổ mở) ở `plan.md` PL list và bốn chỗ
  trong P3, cộng P4 (system-architecture/roadmap "exit window started"); cận dưới `tzdata>=2026.5` trong bảng hợp đồng `plan.md` (P2 R2.16 đã bỏ cận dưới theo
  LT11); test `test_hose_requests_yesterday_and_today_in_ict` của P2 (trái RT13) → `test_hose_request_dates_are_ict_calendar_days`; dòng Risk "trùng được,
  khử ở Phase 1" của P2 (trái RT12); danh sách lệnh capture trong delta AGENTS.md của P2 thiếu `canary-reset`; `powercfg /requests` "dưới SYSTEM" ở P3
  (ý đúng: mục `SYSTEM:`, tiến trình chạy dưới user `capture`, D7); seed `--symbols <seed>` của P1 chưa nói nguồn → `--symbols VN30` (VL-1).
- Name-honesty / SRP: `us_session_bucket` là logic phiên US mà nằm trong `connectors/edgar_acceptance.py` — giữ cho Phase 0 (PL10), ghi rõ ở P4 R4.3 là
  chuyển sang `calendar/` ở Phase 1. Các tên khác vẫn đúng và đủ trách nhiệm: `data_root` (`resolve_external_dir` canh cả data root lẫn thư mục backup —
  cùng một luật "dữ liệu chỉ ở ngoài clone"), `capture/host.py` (module duy nhất chạm đồng hồ/ngủ/khoá/power request/lệnh hệ thống), `quant/power_rule.py`,
  `iter_backup_members`, `canary-reset`. Không thuật ngữ nghiệp vụ mới nào cần `glossary_register.py` (tên mới là định danh code).
- Unresolved contradictions: 0.
- Amendment 2026-10-25: reread `plan.md`, `phases/phase-1-vnstock-probe.md` và `plan-graph.yaml` toàn văn, grep `phases/phase-2..7` cho `plans/`, `.gitignore`,
  `ls-files`, G1-G5 (không chỗ nào khác coi `plans/` là ignored); decision deltas checked: 1 (D9 thay VL-4 + phần `.gitignore` của RT8); reconciled stale
  references: 2 ngoài danh sách của amendment — G2 và Success Criteria P1 đếm `.jsonl` tracked là 5, nay là 9 vì artifacts plan 0b được track (D9); unresolved
  contradictions: 0. OBSERVED 2026-10-25 từ gốc worktree: `plan_graph.py … --require` → exit 0 (chỉ 3 advisory `.env.example`); `--facts` → `declared_files: 52`
  (53 trước amendment: `.gitignore` rời P1); `git grep -n -E 'tqanh6203[t]kqn' -- .` → exit 1 (G5 không tự khớp).
- Amendment 2026-10-25, lần 2: grep `phases/phase-1..7` + `plan.md` thêm cho `git status --porcelain`, `git add`, `verification-P`, `hs-run`, `icacls`; reconciled
  stale references: 10 (P5 `git status --porcelain`, G2 ×2, Rollback ×2 + P3 + P7, VL-4, "Nơi chạy cook" vế `hs-run`, cite RT5/LT12/RT8); decision deltas checked: 1
  (D9 — thêm phần email/`HARNESS_USER` mà G5 cũ không kiểm); unresolved contradictions: 0. OBSERVED 2026-10-25 từ gốc worktree: G2 mới → rỗng; `grep -c
  "Supersede[d] by D9" phases/phase-1-vnstock-probe.md` → 1; `icacls H:\study\Project\FinSec_data` → `Authenticated Users:(I)(OI)(CI)(IO)(M)`; `declared_files` vẫn 52.

## Red-team disposition — r1
Báo cáo `reports/from-code-reviewer-to-planner-red-team-r1-plan-review-report.md`: verdict REVISE, 4 persona (Security Adversary, Failure Mode Analyst,
Bad-day Operator, Maintainer 6-months-later / Feasibility), 15 finding trong bảng gate (6 H, 9 M) + 12 mục long tail (LT1-LT12). Main tự kiểm lại bằng chứng
ngày 2026-10-22 trước khi quyết: `net accounts` → "Maximum password age (days): 42"; `icacls H:\study\Project\FinSec` và `icacls C:\` → `Authenticated Users`
có `(M)` kế thừa; Python 3.13.14 `tarfile.TarFile.extraction_filter` → `None`; `(Get-Command Register-ScheduledTask).ParameterSets` → bộ `Principal` không có
`-Password`; `py.exe` không có trên máy dev. Không finding nào bị Reject: mọi mục đều không đụng quyết định đã khoá (D1-D8, M1-M5) và có bằng chứng đứng được.
Long tail không thuộc gate nhưng cũng được Accept (sửa rẻ, cùng họ lỗi). Sửa đổi do planner áp vào phase file; main vá thêm LT4 (phase nào ghi dòng cửa sổ).

| id | sev | quyết định | phase | nơi plan đã đổi | kiểm của main / ghi chú |
|---|---|---|---|---|---|
| RT1 | H | Accept | P2 (dùng ở P7) | `phases/phase-2-capture-poller.md:227` «a snapshot from … backup», `phases/phase-2-capture-poller.md:232` «trống chưa ghi; MỖI nguồn cổng đang bật có ≥ 1 poll … ok» | Audit thêm cặp ảo ở hai mép và đòi mỗi nguồn cổng đang bật có ≥ 1 poll `ok` trong 08:00-17:30 của mỗi ngày giao dịch; 0 item không còn PASS. |
| RT2 | H | Accept | P2 | `phases/phase-2-capture-poller.md:186` «**Watchdog trên daemon thread riêng** (red-team r1 RT2, phán quyết của main):», `phases/phase-2-capture-poller.md:150` «SourceError(source_unreachable)» | Watchdog trên daemon thread riêng gọi `os._exit(70)`; deadline tổng mỗi request; vnstock trong worker có timeout (phán quyết của main). |
| RT3 | H | Accept | P3, P1 | `phases/phase-3-capture-deploy.md:53` «6. Accounts (D7) — tạo user cục bộ CHUẨN … capture … và», `phases/phase-1-vnstock-probe.md:89` «- tạo user Windows chuẩn cục bộ (ví dụ … vnprobe … ) và» | Main kiểm: chính sách máy dev 42 ngày. `PasswordNeverExpires` cho `capture` và `vnprobe`; chính sách laptop vẫn `[ASSUMED]` nhưng không còn ảnh hưởng. |
| RT4 | H | Accept | P1, P3 | `phases/phase-1-vnstock-probe.md:91` «- chặn user probe khỏi clone:», `phases/phase-3-capture-deploy.md:35` «Python per-user của admin không chạy được dưới user khác); … C:\finsec\» | Main kiểm: `Authenticated Users:(M)` kế thừa trên clone và `C:\`. `icacls /deny` cho user probe; `C:\finsec` bỏ kế thừa, `capture` chỉ RX ngoài data root. |
| RT5 | H | Accept | P1 | `phases/phase-1-vnstock-probe.md:255` «6. **Hash TRƯỚC mọi lần import vnstock (red-team r1 RT5)** — cả hai đều là tiến », `phases/phase-1-vnstock-probe.md:264` «rồi … git diff --exit-code HEAD -- AGENTS.md CLAUDE.md .agents» | Hash trước mọi lần import vnstock (trước `vnstock register`), tiến trình `python -I` mới, hash của dev user ngoài thư mục chung, cộng `git diff --exit-code`. |
| RT6 | H | Accept | P1, P3 | `phases/phase-1-vnstock-probe.md:97` «- **Python cho chính user probe** (red-team r1 RT6): trong phiên … vnprobe», `phases/phase-3-capture-deploy.md:34` «1. Prerequisites — Windows 11, cắm AC, Ethernet nếu có; Python 3.13 x64 từ pytho» | Main kiểm: không có `py.exe`, Python là gói Store. Python 3.13 từ python.org cài cho chính user chạy nó; bỏ fallback uv (planner: không chép `uv.exe`). |
| RT7 | M | Accept | P2 | `phases/phase-2-capture-poller.md:173` «- **Quota không raise** (red-team r1 RT7): vendor trả frame bị cắt +» | Bắt `UserWarning` + đọc `df.attrs`; cắt quota → outage `vendor_quota_exceeded`, không tiến con trỏ. Giới hạn: `catch_warnings` không thread-safe → đúng một worker vnstock (ghi ở R2.8). |
| RT8 | M | Accept | P1, plan | `phases/phase-1-vnstock-probe.md:203` «**R1.12 Superseded by D9 (owner, 2026-10-24) — P1 không chạm», `plan.md:316` «- G5 không danh tính cá nhân trong file commit (D9):» | Bản sửa `.gitignore` (`/plans/` + test hygiene) bị D9 (owner, 2026-10-24) thay thế: `plans/` được commit, P1 không chạm `.gitignore`. Rủi ro PII giờ do scrub danh tính (`user:tqanh123`, email noreply), placeholder `<insider-*>` trong `research/vn-disclosure-portals.md` và kiểm G5 xử lý. |
| RT9 | M | Accept | P2, P6 | `phases/phase-2-capture-poller.md:219` «- **Đọc backup an toàn — … iter_backup_members(archive_path, manifest_path)», `phases/phase-6-vn-reconcile.md:47` «finsec_alpharadar.capture.backup.iter_backup_members(archive, manifest)» | Main kiểm: `extraction_filter` mặc định `None` ở 3.13.14. Đọc tar qua `iter_backup_members` (kiểm sha với manifest trước, whitelist member, không `extractall`). |
| RT10 | M | Accept | P1, P3 | `phases/phase-1-vnstock-probe.md:79` «- **Bộ wheel riêng = nguồn cài DUY NHẤT** (research … vnstock-gen5.md», `phases/phase-3-capture-deploy.md:61` «8. Install — chép bộ wheel … $FINSEC_DATA_ROOT\wheels\win-py313\» | Bộ wheel riêng là nguồn cài duy nhất (`--no-index --find-links --require-hashes`); planner OBSERVED `pip download` đủ 6 wheel. |
| RT11 | M | Accept | P2, P7 | `phases/phase-2-capture-poller.md:204` «atomic (tạm + … os.replace … ) cả … heartbeat.json … và», `phases/phase-7-phase0-exit-audit.md:34` «- File heartbeat đã sync: với MỖI ngày giao dịch của cửa sổ,» | Retry có giới hạn khi `os.replace` gặp `PermissionError`, DEGRADED thay vì chết; P7 kiểm file heartbeat đã sync cho từng ngày. |
| RT12 | M | Accept | P2 | `phases/phase-2-capture-poller.md:101` «- **Bền trước khi commit (red-team r1 RT12, làm CẢ HAI theo phán quyết của main)», `phases/phase-2-capture-poller.md:85` «**Luật đọc của schema v1 (red-team r1 RT12, phán quyết của main):** với cùng một» | Làm cả hai (phán quyết của main): fsync blob + JSONL trước commit và kiểm sha blob cũ; startup đối soát đuôi archive vào `seen`; schema: `first_seen_at` sớm nhất thắng. |
| RT13 | M | Accept | P1, P2 | `phases/phase-1-vnstock-probe.md:162` «- HOSE: mỗi vòng gửi CẢ HAI dạng request mà Phase 2 dùng (red-team r1 RT13) —», `phases/phase-2-capture-poller.md:155` «- … HoseNewsApiSource … : URL» | P1 probe đúng request của P2; P2 chỉ kèm hôm qua ở poll đầu của một ngày ICT. |
| RT14 | M | Accept | P2 | `phases/phase-2-capture-poller.md:209` «**R2.11 Backup — … backup.py … (D3).**», `phases/phase-2-capture-poller.md:439` «- [ ] … test_only_host_reads_the_clock_or_sleeps … (regex trên … capture/*.py» | `run_backup(..., *, now)` từ đồng hồ tiêm; regex invariant thêm `date.today(`, `utcnow(`, `perf_counter(`, `time_ns(`, `from time import`. |
| RT15 | M | Accept | P2 (dùng ở P3, P7) | `phases/phase-2-capture-poller.md:213` «VERIFIED https://sqlite.org/howtocorrupt.html §1.2) rồi chạy», `phases/phase-2-capture-poller.md:225` «là SNAPSHOT ( … state-<d>.sqlite … của backup): mở bằng URI» | Snapshot chuyển `journal_mode=DELETE` trước khi tính sha; audit mở `mode=ro&immutable=1`, test chứng minh không tạo file cạnh snapshot. |
| LT1 | M | Accept | P2 | `phases/phase-2-capture-poller.md:138` «không sinh … host_stalled … giả (red-team r1 LT1). … host_stalled», `phases/phase-2-capture-poller.md:408` «test_busy_multi_source_cycle_does_not_log_host_stalled» | Tick giữa từng nguồn + ngân sách mỗi poll; test chu kỳ bận không sinh `host_stalled`. |
| LT2 | M | Accept | P3, P7, P2 | `phases/phase-3-capture-deploy.md:146` «**R3.9 Thủ tục khởi động lại cửa sổ / hotfix (red-team r1 LT2, phán quyết của ma», `phases/phase-7-phase0-exit-audit.md:21` «**R7.1 Điều kiện vào (kiểm trước mọi việc khác).** Đọc ngày bắt đầu cửa sổ từ dò» | Thủ tục khởi động lại cửa sổ / hotfix có tên (`/hs:fix` ngoài chuỗi, dòng "restarted" mới nhất thắng); kiểm hạn pause Windows Update; `sc.exe`. |
| LT3 | M | Accept | P2, P3 | `phases/phase-2-capture-poller.md:110` «status 'ok'», `phases/phase-3-capture-deploy.md:97` «- **"Device limit" của vnstock** (red-team r1 LT3): … register» | `run_mode` + `pid` trong `polls`; cổng R3.3 chỉ tính poll `run`; sự cố "Device limit". |
| LT4 | M | Accept | P3, P4-P7, P2 | `phases/phase-3-capture-deploy.md:20` «**Thứ tự commit (red-team r1 LT4, phán quyết của main — giữ "một commit mỗi phas», `phases/phase-4-edgar-golden-test.md:150` «- **Từ Phase 3 (red-team r1 LT4, phán quyết của main):** dòng Decisions MỚI» | Giữ một commit mỗi phase (phán quyết của main); dòng cửa sổ do commit phase đầu tiên sau khi cửa sổ mở ghi (main vá P4-P7 + `plan.md`); `canary-reset`. |
| LT5 | M | Accept | P6 | `phases/phase-6-vn-reconcile.md:123` «GO … , … mde_80 … chỉ được báo. Ca cũ " … mde_80 > 0.10 … mà … mde ≤ 0.05» | Sửa ca test: `mde_80/mde = 1.4` nên ca cũ không tồn tại. |
| LT6 | L | Accept | P3 | `phases/phase-3-capture-deploy.md:71` «PYTHONDONTWRITEBYTECODE=1 … (venv và clone chỉ RX), rồi» | Wrapper dùng `python.exe -P -m`. |
| LT7 | L | Accept | P1, P2 | `phases/phase-1-vnstock-probe.md:28` «(red-team r1 LT7). Thiếu/rỗng → … DataRootError("<VAR> is not set ...")», `phases/phase-2-capture-poller.md:39` «capture/config.py … đọc … configs/capture.toml … ( … tomllib … ) +» | `Path.resolve()` trước khi đi ngược cha; `FINSEC_BACKUP_DIR` qua cùng guard. Junction test chạy không skip (`mklink /J` không cần admin: `[ASSUMED]` tới RED đầu). |
| LT8 | L | Accept | P2 | `phases/phase-2-capture-poller.md:112` «và độ dài của message vendor, không bao giờ text nguyên văn (red-team r1 LT8);» | Chỉ lưu tên lớp, `code`, sha256 và độ dài message vendor. |
| LT9 | L | Accept | P2 | `phases/phase-2-capture-poller.md:152` «so … clock.monotonic() … với … deadline_s … → quá hạn = … source_unreachable» | Giới hạn cỡ áp khi đọc và khi giải nén, có test gzip bom. |
| LT10 | L | Accept | P2 | `phases/phase-2-capture-poller.md:249` «chạy — red-team r1 LT4). **Khoá một tiến trình cho mọi lệnh ghi archive/outage**» | Mọi lệnh ghi lấy `SingleInstanceLock`. |
| LT11 | L | Accept | P2 | `phases/phase-2-capture-poller.md:264` «Lý do đúng (red-team r1 LT11): zoneinfo tìm … TZPATH … trước rồi mới tới gói» | Viết lại lý do của `tzdata` (TZPATH trước, gói sau); cận dưới là bản lock resolve được. |
| LT12 | L | Accept | P1, P5 (và P2, P4, P6, P7) | `phases/phase-1-vnstock-probe.md:287` «- Số test do regression gate in ra, ở ĐỦ năm chỗ (red-team r1 LT12):», `phases/phase-5-alpaca-probe.md:70` «Ghi chú parser: … .env.example … → advisory đã biết. … docs/code-standards.md» | Số test ở đủ năm chỗ trong docs; sửa trích dẫn `pyproject.toml` sau khi P1 đổi file. |
