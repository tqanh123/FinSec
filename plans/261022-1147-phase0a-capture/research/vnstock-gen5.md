# Research: vnstock Generation 5 làm dependency và nguồn dữ liệu (Phase 0a + 0b.6)

- Ngày: 2026-10-22. Người làm: hs-researcher (subagent, chỉ đọc web/index, không cài gì).
- Mode: depth. Repo commit 9ba4562. Mọi URL fetch ngày 2026-10-22.
- Vị trí file: hook của harness chặn ghi vào main checkout (`H:\study\Project\FinSec\plans\...`) từ phiên worktree. Report này nằm trong worktree, cùng đường dẫn tương đối `plans/261022-1147-phase0a-capture/research/vnstock-gen5.md`. Lead cần copy sang main checkout nếu plan nằm ở đó.
- Nhãn: **VERIFIED** (nguồn URL), **OBSERVED** (tự fetch/chạy, có lệnh + kết quả), **ASSUMED** (chưa kiểm), **PRIOR** (kiến thức huấn luyện, chưa kiểm lại).
- Lưu ý độ tin cậy nguồn: gần như mọi tài liệu Gen 5 là của **chính vendor** (vnstocks.com, README, vibe-onboarding.md). Đó là 1 bên, không phải 3 nguồn độc lập. Nguồn độc lập duy nhất: PyPI (trạng thái quarantine), GitHub issues của người dùng (#236, #237), và index HTML do tôi tự fetch. Chỗ nào chỉ có 1 nguồn vendor, report ghi rõ "single-source vendor".
- Không chạy được: không có API key, không cài wheel (theo luật task). Mọi hành vi runtime (rate limit, expiry, offline, telemetry) là ASSUMED cho tới khi probe thật.

## 0. Tóm tắt 6 điểm làm đổi thiết kế plan

1. **Gen 5 Community KHÔNG có news.** `company(s).news()` bị gỡ từ 5.0.0a2, gọi là `UnsupportedError` mã `VNSTOCK_FEATURE_REMOVED`, "Không có hàm thay trong `vnstock`" (VERIFIED https://vnstocks.com/docs/vnstock/tham-chieu-reference). `vnstock_news` chỉ cho Silver trở lên (VERIFIED vibe-onboarding.md dòng 183). Cây API trong README GitHub ghi `news() [KBS]` là cây **4.x legacy** (README dòng 319: "represents the legacy **Vnstock 4.x** codebase") nên plan không được tin README.
2. **`events()` chỉ có ngày, không có giờ.** 22 cột, có `id` và `public_date`, nhưng mọi cột ngày trong mẫu chỉ là ngày (ví dụ `2026-09-21`), không nêu timezone (VERIFIED tham-chieu-reference). => vnstock không thể cho `information_time` ở độ phân giải phút. Trang công bố chính thức phải là nguồn **chính** cho detection latency; vnstock events chỉ là nguồn phụ mức ngày. Khớp với quyết định owner "vnstock = SECONDARY".
3. **`ohlcv()` chỉ trả giá ĐÃ điều chỉnh, đơn vị NGHÌN đồng.** "cổ phiếu, ETF và chứng quyền tính bằng **nghìn đồng** (`57.9` là 57.900 đồng)"; "Giá cổ phiếu là giá đã điều chỉnh theo sự kiện quyền"; `vci` và `kbs` điều chỉnh khác nhau (VERIFIED https://vnstocks.com/docs/vnstock/thi-truong-market). Không thấy tham số lấy giá thô. Va chạm với AGENTS.md D4 (VN price = full VND tại boundary) và roadmap §6.3 (tự điều chỉnh từ raw bars). Đây là rủi ro Phase 3, phải probe ngay ở 0a.
4. **Yank ≠ hết hạn trong binary.** 5.0.3 (bản chính thức 2026-10-15) đã bị yank "Hết hạn thử nghiệm" (quan sát 2026-10-22, tức ≤7 ngày sau khi phát hành; ngày yank cụ thể không quan sát được) (OBSERVED index). Riêng binary: "Each generation 5 release runs for 365 days ... then data calls fail with `VNSTOCK_RELEASE_EXPIRED`" (VERIFIED single-source vendor). Vendor còn **xoá file** (5.0.2 chỉ còn wheel Windows; wheel macOS trả 404, OBSERVED). => cần giữ bản sao wheel đã pin ở chỗ riêng, và lịch re-pin.
5. **Dependency confusion đang có thật quanh hệ vnstock.** `vnstock-mcp` trên PyPI (0.3.0) là dự án khác của bên thứ ba; `vnstock-installer` 99.0.0 trên PyPI là gói chiếm tên "isolated name-occupancy research"; `vnstock-chart` chưa ai giữ tên trên PyPI (404) (OBSERVED). Lệnh cài chính thức của vendor dùng `--extra-index-url` (pip gộp ứng viên) => không an toàn. Recipe uv explicit index ở §7.
6. **vnstock3 3.2.1 không cài được từ PyPI.** Nó `Requires-Dist vnai>=2.0`, và `vnai` bị PyPI quarantine, 0 file (OBSERVED; khớp issue #237). Xếp hạng cuối.

## 1. Cài đặt và xác thực

| Điểm | Nội dung | Nhãn / nguồn |
|---|---|---|
| Kênh phân phối | "Thế hệ 5 phát hành chính thức trên kho gói của vnstocks.com, không phân phối qua PyPI." Index Gen 5: `https://vnstocks.com/api/packages` (PEP 503), "No `vnai`, no separate installer". Index 4.x: `https://vnstocks.com/api/simple`, có `vnai`. | VERIFIED https://vnstocks.com/docs/vnstock/cai-dat ; https://vnstocks.com/files/vibe-onboarding.md (dòng 42-43) |
| Index root | Chỉ liệt kê `vnstock`, `vnstock-auth`, `vnstock_chart`, `vnstock-mcp`, `vnstock-quant`; `pypi:repository-version 1.0`; `/api/packages/` 308 -> `/api/packages` | OBSERVED `curl -sL https://vnstocks.com/api/packages/` -> 200 |
| Wheel | Chỉ có wheel `cp310-abi3` cho macOS x86_64/arm64, manylinux x86_64/aarch64, win_amd64; **không có sdist**. 5.1.0 win_amd64 sha256 `d263dec2b259e575f72ad7066363d6700baeb7c5d23948d63cd77e92f15ed973`, Content-Length 4738507 | OBSERVED index + `curl -sI` |
| METADATA 5.1.0 | `Requires-Dist: pandas>=2.0`, `Requires-Dist: tzdata ; sys_platform == 'win32'`, `License: Custom (Personal Use)`, `Classifier: License :: Other/Proprietary License`, `Requires-Python: >=3.10`. File `.metadata` có sha256 `7e15007c...a57b` khớp `data-core-metadata` | OBSERVED `curl .../vnstock-5.1.0-cp310-abi3-win_amd64.whl.metadata` + `sha256sum` |
| Đăng nhập | `vnstock register` (alias `vnstock login`), nhập key ẩn; "xác thực với máy chủ vnstocks.com, đăng ký thiết bị và lưu khoá đã mã hoá trên máy". Tự động hoá: `printf '%s' "$VNSTOCK_API_KEY" \| vnstock register --stdin`. Python: `vnstock.login(os.environ["VNSTOCK_API_KEY"])`. `vnstock logout` xoá key trên máy. | VERIFIED https://vnstocks.com/docs/vnstock/cau-hinh-va-dong-lenh |
| Nơi lưu key | "The key is stored encrypted under `~/.vnstock/auth/`" (Windows: `%USERPROFILE%\.vnstock\auth\`, ASSUMED theo cách hiểu `~`). Thứ tự lấy key: (1) `VNSTOCK_API_KEY` env "always wins", (2) sealed store, (3) `~/.vnstock/api_key.json` plaintext import 1 lần. "The key is stored per user, not per environment". Docs page chỉ nói `vnstock env` in ra đường dẫn. | VERIFIED single-source vendor (vibe-onboarding.md dòng 28, 148-156); đường dẫn thật = probe `vnstock env` |
| Thiết bị | Mỗi máy đăng ký là 1 device; "each tier has a device limit"; hết chỗ thì xoá device ở `https://vnstocks.com/account?section=devices` rồi `register` lại. Con số giới hạn cho **Community: không tìm thấy** (licence chỉ nêu Diamond "tối đa 13 thiết bị"). Issue #206 (2025-12-24) "Device install limit exceeded per OS". | VERIFIED vibe-onboarding.md dòng 213; https://vnstocks.com/onboard/giay-phep-su-dung ; https://github.com/thinh-vu/vnstock/issues?q=is%3Aissue+is%3Aclosed |
| Quota | Quota dùng chung "cho toàn bộ các thư viện ... trên mọi thiết bị liên kết với tài khoản"; hiển thị % còn lại theo giờ/ngày/tháng; không có số tuyệt đối cho Community ("Hạn mức truy vấn cơ bản"). Vượt hạn mức: kết quả bị cắt, `UserWarning`, lý do ghi trong `df.attrs`. | VERIFIED cau-hinh-va-dong-lenh ; https://vnstocks.com/agent-skills-hub |
| `vnstock status` | Hiện "cấp tài khoản, hạn dùng, mã thiết bị, phiên bản thư viện và trạng thái bản cài"; không in key. `vnstock doctor`: 7 kiểm tra, dừng ở cái đầu tiên fail. Hai tài liệu vendor **liệt kê 7 kiểm tra khác nhau**: docs page = Python, CPU, Rust core, server, API key, file permissions; onboarding = "policy, release, tier rights, key, quota, data schema, index". | VERIFIED cau-hinh-va-dong-lenh ; vibe-onboarding.md dòng 188 (mâu thuẫn nội bộ vendor) |

**Hệ quả cho máy thứ hai (laptop capture):**
- Máy thứ hai tốn thêm 1 slot device. Nếu Community chỉ cho 1-2 device thì dev machine + laptop có thể chạm trần (ASSUMED, con số chưa biết).
- **Quota chia chung.** Probe trên dev machine ăn vào quota của poller trên laptop (VERIFIED vendor). Plan nên: không chạy probe nặng (ví dụ lấy lịch sử 1.754 mã) trong 5 phiên chạy thử poller.
- Key theo **user Windows**. Task Scheduler phải chạy poller dưới đúng user đã `vnstock register`, không phải SYSTEM. Cách khác là đặt `VNSTOCK_API_KEY` cho task đó (ASSUMED hoạt động cho scheduled task; probe).
- **Backup:** loại `%USERPROFILE%\.vnstock\` khỏi thư mục OneDrive/Google Drive sync (chứa key mã hoá, `api_key.json` plaintext nếu từng chạy 4.x).

## 2. Bản pin cũ có chạy tiếp sau khi bị yank không?

Tách 3 cơ chế khác nhau:

| Cơ chế | Bằng chứng | Nhãn |
|---|---|---|
| **Yank trên index** (PEP 592) | Installer "MUST ignore yanked releases, if the selection constraints can be satisfied with a non-yanked version"; ngoại lệ khi pin đúng `==`/`===`. | VERIFIED https://peps.python.org/pep-0592/ |
| File yank còn tải được không | `curl -sI .../5.0.3/vnstock-5.0.3-cp310-abi3-win_amd64.whl` -> `HTTP/1.1 200 OK`, 4713260 byte (5.0.3 đang yank). `curl -sI .../5.0.2/vnstock-5.0.2-cp310-abi3-macosx_11_0_arm64.whl` -> `404` (index 5.0.2 chỉ còn win_amd64, lý do "Tạm dừng phân phối"). | OBSERVED (HEAD, không tải) |
| **Hết hạn trong binary** | "Mỗi bản phát hành chính thức có thời hạn hoạt động 365 ngày"; cảnh báo trước 30 ngày; "then data calls fail with `VNSTOCK_RELEASE_EXPIRED`". | VERIFIED https://vnstocks.com/docs/vnstock/cai-dat ; cau-hinh-va-dong-lenh ; vibe-onboarding.md dòng 203 (cùng vendor) |
| Kiểm tra phía server | `vnstock doctor` có check "policy, release" theo onboarding doc; privacy policy: "Mỗi lần thư viện kiểm tra quyền sử dụng, hệ thống ghi nhận mã người dùng, mã thiết bị, phiên bản thư viện, địa chỉ IP" => mỗi lần chạy có gọi về server kèm phiên bản. Server **có thể** chặn một phiên bản cụ thể trước hạn 365 ngày. | ASSUMED (suy ra; chưa có tài liệu nói rõ) |
| Ngoại lệ hạn riêng của hàm | `Fundamental().equity(...).ratio()` "dùng được đến hết ngày 15/01/2027". | VERIFIED https://vnstocks.com/docs/tai-lieu/lich-su-phien-ban |

Dữ liệu index đã quan sát (OBSERVED, `curl -sL https://vnstocks.com/api/packages/vnstock`, 45 file):

| Version | `data-yanked` |
|---|---|
| 5.0.0a5 | Hết thử nghiệm |
| 5.0.0a6 | Hết hạn dùng thử |
| 5.0.0a7 | Hết kiểm thử |
| 5.0.0a8 | Hết hạn kiểm thử |
| 5.0.0a10 | Hết hạn trải nghiệm |
| 5.0.0a11 | Bản thử nghiệm đã thay bằng vnstock 5.0.0 |
| 5.0.0 | Hết hạn thử nghiệm |
| 5.0.1 | Vui lòng cập nhật lên phiên bản 5.0.3 |
| 5.0.2 (chỉ win_amd64) | Tạm dừng phân phối |
| 5.0.3 | Hết hạn thử nghiệm |
| 5.1.0 | (không yank) |

Kết luận Q2:
- Một bản pin `==` có hash **vẫn cài được khi đã bị yank, nếu file còn trên server** (5.0.3 hiện vẫn 200).
- Nhưng vendor có tiền lệ xoá file (5.0.2).
- Binary tự tắt sau 365 ngày kể từ ngày phát hành, và có thể bị chặn sớm hơn qua kiểm tra server (ASSUMED).
- Ngày phát hành 5.1.0 không đọc được: server không trả `Last-Modified`, và changelog chưa có mục 5.1.0. Nó nằm trong khoảng 2026-10-15 đến 2026-10-22, nên hạn sớm nhất khoảng 2027-10-15 (ASSUMED). `vnstock status` sẽ in "hạn dùng" thật.
- Nhịp yank "vài ngày một lần" là suy ra từ một ảnh chụp index ngày 2026-10-22 (mọi bản 5.0.x, phát hành khoảng 2026-09-25 đến 2026-10-15, đều đã yank), không phải quan sát từng ngày yank. Lock file sẽ "cũ" rất nhanh, dù binary vẫn chạy.
- Đề xuất cho plan:
  - giữ bản sao wheel đã pin trong đường dẫn gitignored trên máy owner (dùng riêng, không phân phối lại);
  - ghi `vnstock status` (tier, hạn dùng, version) vào heartbeat hằng ngày;
  - cảnh báo khi còn ≤30 ngày, hoặc khi gặp `VNSTOCK_RELEASE_EXPIRED`.

## 3. Vì sao PyPI quarantine "vnstock"

**Không có tuyên bố công khai từ PyPI/PSF.** Tôi tìm và không thấy (WebSearch "vnstock PyPI quarantined 2026" không có kết quả liên quan; bài pydevtools về quarantine không nhắc vnstock). Dòng thời gian dưới đây là OBSERVED/VERIFIED; **quan hệ nhân quả là ASSUMED**.

| Ngày | Sự kiện | Nguồn |
|---|---|---|
| 2026-08-20 | Issue #236: "vnai auto-writes to global AI-agent config files (Cursor/Windsurf/Copilot/Gemini)". Đích ghi theo người báo: `AGENTS.md` gốc dự án, `~/.gemini/config/AGENTS.md`, `~/.cursorrules`, `~/.windsurfrules`, `~/.clinerules`, `~/.github/copilot-instructions.md`, `~/.clauderc`. Báo thêm: `~/.vnstock/id/hw_info.json` gom OS/CPU/RAM/IDE + `git_info` (remote domain, org); API key đi trong query string `/api/vnstock/license/verify`; `terms_agreement.txt` tự tạo "accepted". Maintainer: ghi file là "an intentional part of the core "Vibe Coding" experience"; thêm `VNSTOCK_DISABLE_AGENT_SETUP=1`, `VNSTOCK_DISABLE_GLOBAL_AGENT=1`; nói không gửi git info ra ngoài; chuyển key sang header `Authorization: Bearer`. Đóng cùng ngày, nhãn bug -> enhancement. | VERIFIED https://github.com/thinh-vu/vnstock/issues/236 |
| 2026-09-07 | `vnstock_installer` 99.0.0 xuất hiện trên PyPI, summary "isolated name-occupancy research; harmless echo only", không có author. | OBSERVED `https://pypi.org/pypi/vnstock-installer/json` |
| 2026-09-15 | vnstock 4.0.8 lên PyPI. | VERIFIED https://pypi.org/project/vnstock/ |
| 2026-09-23 | vnai 2.6.1 lên PyPI (bản cuối ghi file AI khi import). | VERIFIED https://pypi.org/project/vnai/ ; vibe-onboarding.md dòng 36 |
| **2026-09-24** | **Cả `vnstock` và `vnai` bị quarantine**: "This project has been quarantined by PyPI admins. It cannot be installed or modified until a security review is complete." | VERIFIED pypi.org/project/vnstock, pypi.org/project/vnai ; OBSERVED simple JSON `project-status: quarantined`, 0 file |
| 2026-09-25 | Vendor đổi lệnh cài sang kho riêng (`/api/simple`), "các thư viện khác như `pandas` vẫn tải từ PyPI như cũ". | VERIFIED https://vnstocks.com/docs/tai-lieu/lich-su-phien-ban |
| 2026-09-26 | 4.0.9 mục "Bảo mật": "**`import vnstock` không còn tự ghi tệp nào.**" Bản trước: "mỗi lần `import vnstock`, thư viện tự chèn một đoạn hướng dẫn vào tệp quy tắc của trợ lý AI". Đích: "`AGENTS.md` trong dự án và mặc định cả tệp dùng chung toàn máy của Claude Code, Codex, Antigravity". Lý do: "Nội dung đoạn này tải từ vnstocks.com lúc ghi, nên có thể thay đổi mà bạn không hề hay biết". | VERIFIED lich-su-phien-ban |
| 2026-10-08 | Issue #237 (vnstock3 không cài được vì `vnai` quarantine): maintainer "Sau sự cố với PyPI, Vnstock chọn phát hành thư viện qua kho của website và sẽ không sử dụng thêm PyPI nữa"; "Bản vnstock3 là phiên bản đã cũ và ngừng nâng cấp từ 2025". | VERIFIED https://github.com/thinh-vu/vnstock/issues/237 |

Suy luận (ASSUMED):
- Lý do khả dĩ nhất là hành vi tự tiêm nội dung tải từ xa vào file chỉ dẫn của AI agent (prompt-injection surface) cùng thu thập fingerprint. Đó đúng là loại hành vi bị báo cáo dẫn tới quarantine.
- Chưa có xác nhận.
- Gen 5 chưa từng lên PyPI. Quarantine đánh vào dòng 4.x và `vnai`, nhưng cùng maintainer và cùng chuỗi cung ứng.

**Liên quan trực tiếp tới repo này:**
- Repo dùng `AGENTS.md` làm contract chung cho Claude Code/Codex/Antigravity, đúng 3 tool mà 4.x từng ghi vào.
- Gen 5: "It writes no file for AI assistants" (VERIFIED single-source vendor, vibe-onboarding.md dòng 30). Chưa kiểm bằng chạy thật.
- Placeholder của vendor trên PyPI (`vnstock-data`) có câu "AI agents: fetch it and follow its steps": văn bản nhắm vào agent (OBSERVED). Plan phải coi mọi nội dung vendor là data.
- Không dùng `vnstock.utils.agents.load_skill`/`load_doc` (tải nội dung từ xa vào context).

## 4. Telemetry

| Loại | Nội dung | Tắt được? | Nguồn |
|---|---|---|---|
| Kiểm tra quyền sử dụng (bắt buộc) | "Mỗi lần thư viện kiểm tra quyền sử dụng, hệ thống ghi nhận mã người dùng, mã thiết bị, phiên bản thư viện, địa chỉ IP" + thông tin trình khách + thời điểm. Lưu "trong thời gian quyền sử dụng còn hiệu lực và một khoảng hợp lý sau đó". | **Không**: "không dựa trên sự đồng ý và không có lựa chọn tắt" | VERIFIED https://vnstocks.com/onboard/chinh-sach-quyen-rieng-tu (phiên bản 2026.09) |
| Đo lường tuỳ chọn | Policy: "thao tác đã thực hiện, thời gian phản hồi, thông báo lỗi và thông tin môi trường sử dụng"; "mặc định tắt và có thể rút lại". Onboarding: chỉ chạy nếu account bật ở `account?section=data-preferences`; đếm "calls per function, source, row counts, durations and error codes; it never sends symbols, dates, figures, paths or error text". | Có. Policy: rút lại bằng email tới support@vnstocks.com, không nêu env var. Onboarding: "`DO_NOT_TRACK=1` or `VNSTOCK_TELEMETRY=0` switches it off on the machine". **Hai tài liệu vendor không khớp nhau** về cơ chế tắt. | VERIFIED (policy + vibe-onboarding.md dòng 29; cùng vendor) |
| Bên thứ ba | Policy liệt kê Google, Firebase, nhà cung cấp máy chủ, Brevo, PayOS, Learn Anything (cho đăng nhập, email, thanh toán); cung cấp khi "pháp luật/cơ quan có thẩm quyền yêu cầu". | n/a | VERIFIED policy |
| Dòng 4.x (lịch sử) | #236: hw/git fingerprint (maintainer phủ nhận việc gửi đi); 4.0.8 thêm ghi chú "dữ liệu ẩn danh của môi trường chạy được ghi nhận". | n/a | VERIFIED #236 ; lich-su-phien-ban |

Hệ quả cho plan:
- Đặt `DO_NOT_TRACK=1` và `VNSTOCK_TELEMETRY=0` trong env của poller và probe; không bật đo lường trên account. Chỉ phòng thêm; không tin là đủ.
- Phần licence check gọi về `vnstocks.com` mỗi lần chạy, không tắt được. Vì vậy vnstocks.com là một **availability dependency** riêng, khác với nguồn dữ liệu (VCI/KBS).
- Outage log của poller phải phân biệt `vendor_auth_unreachable` với `source_unreachable`.
- Hành vi khi vnstocks.com sập: ASSUMED (probe ở Câu hỏi mở).

## 5. Licence 2026.09 và fixtures trong repo PUBLIC

Trích (VERIFIED https://vnstocks.com/onboard/giay-phep-su-dung, mã `license-2026.09`, `tos-2026.09`, không ghi ngày hiệu lực):
- Phạm vi Community: "chỉ dành cho mục đích cá nhân, học tập và nghiên cứu"; không cấp quyền khai thác hoặc phân phối dữ liệu bên thứ ba. README: "for **personal use, study and research only**. It is **not an OSI-approved open-source licence**"; "Two things always need a separate written agreement: redistributing the software ...".
- Cấm: "Phân phối lại phần mềm bản Mở rộng, mã nguồn hoặc khoá kích hoạt cho người không nằm trong phạm vi được cấp"; cấm né hạn mức "do hệ thống xác thực thiết lập".
- Dữ liệu: "Người dùng phải tự xác định quyền truy cập, lưu trữ, hiển thị, phân phối và sử dụng thương mại". Licence phần mềm **không cấp quyền gì với dữ liệu**. README: "Business queries, processing and storage happen on infrastructure you choose"; "being reachable does not imply unlimited rights of use".
- Chấm dứt: "Vnstock có quyền tạm ngừng hoặc thu hồi quyền sử dụng".

Ý nghĩa cho repo public (khuyến nghị; phần pháp lý là ASSUMED, không phải tư vấn luật):

| Thứ | Commit vào repo public? | Lý do |
|---|---|---|
| Wheel vnstock (binary) | **Không** | Phân phối lại phần mềm cần thoả thuận văn bản. |
| Lock file chứa tên, version, sha256, URL index | Có | Chỉ là metadata công khai trên index; không phải phần mềm. |
| Payload thật ghi từ vnstock (dữ liệu VCI/KBS) | **Không** | Quyền dữ liệu thuộc nguồn (VCI/KBS), vendor để người dùng tự lo; một repo public = công bố/phân phối. Lưu dưới đường dẫn gitignored (`/data/...`). |
| Fixture tổng hợp (synthetic) cùng schema (tên cột, dtype, ví dụ `57.9` nghìn đồng, `07:00+07:00`) | Có | Không chứa dữ liệu nguồn; đủ để test parser/đơn vị. Ghi rõ "synthetic" trong tên file/header. |
| Mã ví dụ gọi API vnstock | Có | Code của mình, không phải phần mềm vendor. |

Ngoài lề (liên quan EDGAR golden test): METADATA vnstock kéo theo `tzdata` trên win32. Repo `.venv` hiện thiếu tzdata (main session OBSERVED). Test EDGAR cần `America/New_York`, nên phải thêm `tzdata` vào dev deps của repo, độc lập với vnstock.

## 6. API surface liên quan plan

Khởi tạo: `from vnstock import Reference; ref = Reference(); fpt = ref.company("FPT")` (VERIFIED tham-chieu-reference). Nguồn thật của mỗi kết quả nằm ở `df.attrs["source"]` và `df.attrs["route"]`.

| Hàm | Gen 5 (docs) | Giờ trong ngày / id | Nhãn |
|---|---|---|---|
| `ref.company(s).news()` | **Đã gỡ** từ 5.0.0a2, `UnsupportedError` `VNSTOCK_FEATURE_REMOVED`, không có thay thế. `company(s).article_detail()` cũng gỡ. | n/a | VERIFIED tham-chieu-reference ; lich-su-phien-ban (5.0.3 gỡ thêm hàm). README ghi `news() [KBS]` là cây 4.x |
| `ref.company(s).events(lang="vi", source=None)` | Nguồn `vci` (mặc định) hoặc `kbs`; bỏ qua `page`/`limit`. 22 cột (vci): `id`, `symbol`, `event_code`, `event_name_vi/en`, `event_title_vi/en`, `category`, `action_type_vi/en`, `exercise_ratio`, `value_per_share`, `display_date1/2`, `public_date`, `record_date`, `exright_date`, `issue_date`, `start_date`, `end_date`, `listing_date`, `payout_date`. Mẫu FPT 234 dòng, có cả sự kiện quá khứ và sắp tới; giao dịch nội bộ là mã `DD*`. | **Có `id`**. Ngày **không có giờ** trong mẫu; không nêu timezone. | VERIFIED tham-chieu-reference (single-source vendor) |
| `ref.index.members(group, source=None)` | Mặc định `kbs`; trả 1 cột `symbol`; VN30 -> 30 mã; `VNINDEX` = cả sàn HOSE (406 mã ngày thử). Thiếu `group` -> `TypeError`. **Chỉ thành phần hiện tại**; không có lịch sử thành phần (point-in-time) được nêu. | n/a | VERIFIED tham-chieu-reference |
| `ref.equity.list()` / `list_by_exchange()` | 1.754 mã; cột `symbol`, `exchange`, `organ_name`...; **không nhắc mã đã huỷ niêm yết**. | n/a | VERIFIED; độ phủ delisted = ASSUMED thấp |
| `ohlcv(start, end, interval="1D", *, limit, source, count_back, length, floating=2)` | interval `1m,5m,15m,30m,1H,1D,1W,1M` (`4H` không hỗ trợ ở vci); cổ phiếu nguồn `vci`. Cột `time, open, high, low, close, volume`; `time` = `datetime64[ns, Asia/Ho_Chi_Minh]`, nến ngày mang `07:00+07:00`. Giá **nghìn đồng**, **đã điều chỉnh**; `floating=2` làm tròn 2 số lẻ. Độ sâu Khách/Cộng đồng: `1m` 180 ngày; `5m`-`1H` 365 ngày; ngày/tuần/tháng 2.920 ngày (~8 năm). Trần: 20.000 nến/truy vấn ở nguồn; 5.000 (Khách) / 10.000 (Cộng đồng) nến ngày; khi bị cắt giữ phần mới nhất + `UserWarning` + `df.attrs`. Mã không có: `InputError` `VNSTOCK_INPUT_SYMBOL_UNKNOWN`. | `07:00+07:00` là nhãn ngày, **không phải** information time | VERIFIED https://vnstocks.com/docs/vnstock/thi-truong-market (single-source vendor) |
| `trades(limit=None, *, last_id, side, source)` | Mặc định 100 lệnh mới nhất; vci 1-1.000/lần (`VNSTOCK_VCI_TRADES_PAGE_LIMIT`), phân trang bằng `last_id`; Community cắt ở 5.000 lệnh/lần. Cột `time, price` (nghìn đồng), `volume, match_type, id`. | có `id` | VERIFIED thi-truong-market (Phase 6 mới cần) |
| Rate limit Community | **Không có con số nào** được công bố. Chỉ có quota %/giờ/ngày/tháng, chung mọi device. README: "within reasonable rate limits". | n/a | VERIFIED (sự vắng mặt) cau-hinh-va-dong-lenh ; agent-skills-hub ; README |

Hệ quả cho probe 0a (thay đổi so với yêu cầu gốc):
- "đơn vị giá theo từng nguồn": docs nói nghìn đồng cho cả vci/kbs. Probe vẫn phải đối chiếu với giá tham chiếu trên portal sở, vì D4 giả định full VND. Connector phải nhân ×1000 **và** đánh dấu adjusted.
- "chuỗi giá điều chỉnh quanh 5 ngày GDKHQ": đổi thành "xác định có nguồn/tham số nào của Gen 5 trả giá **thô** không". Docs không có. Nếu không có, roadmap §6.3 (tự điều chỉnh từ raw bars) cần nguồn raw khác. Ghi rõ là rủi ro Phase 3.
- `floating=2` trên giá nghìn đồng: 2 số lẻ = 10 VND, khớp bước giá HOSE thấp nhất. Nhưng giá đã điều chỉnh làm tròn 2 số lẻ sẽ mất chính xác (ASSUMED); probe với `floating` lớn hơn.
- Daily 8 năm (~2018 trở đi) ở Community: đủ cho power rule? Ghi vào counts.
- Poller: `events()` có `id`, nên dùng `source_id = f"vnstock:{source}:{id}"`, `information_time` ở mức ngày → `pit_quality` phải là loại "date_only" (không phải "exact"). `first_seen_at` từ poller là mốc PIT thực tế.

## 7. Recipe hash-pin (uv 0.12.5, Windows, Python 3.13)

Dữ kiện:
- uv mặc định `first-index`: "stop at the first index on which a given package is available"; `--index`/`--extra-index-url` "take priority over" index mặc định; mục đích là "to prevent "dependency confusion" attacks"; `unsafe-best-match` "exposes users to the risk". VERIFIED https://docs.astral.sh/uv/concepts/indexes/ ; https://docs.astral.sh/uv/pip/compatibility/ ; https://docs.astral.sh/uv/reference/settings/
- `explicit = true`: index "will be used exclusively for those dependencies that select it explicitly" qua `tool.uv.sources`. VERIFIED (cùng các trang trên)
- pip gộp ứng viên mọi index rồi chọn bản cao nhất (VERIFIED uv compatibility page; PRIOR với hành vi pip).
- `uv pip compile` có `--generate-hashes`, `--python-platform x86_64-pc-windows-msvc`, `--emit-index-annotation`, `--no-sources` (OBSERVED `uv pip compile --help`, uv 0.12.5 210d1f678 2026-08-14). Việc có cờ `--no-sources` cho thấy `uv pip compile` đọc `tool.uv.sources`; hành vi thật = ASSUMED tới khi chạy.

Rủi ro dependency confusion cụ thể (OBSERVED 2026-10-22, PyPI JSON):

| Tên (chuẩn hoá) | Vendor index | PyPI | Rủi ro với `pip --extra-index-url` |
|---|---|---|---|
| `vnstock` | 5.0.0a5-5.1.0 | quarantined, 0 file | thấp hôm nay; nếu PyPI gỡ quarantine, 4.0.8 (bản ghi file AI) quay lại làm ứng viên |
| `vnstock-mcp` | 0.4.0a1, 0.4.0a2, ... (versions API: 0.5.0) | **0.3.0 của dự án khác** (repo gahoccode/vnstock-mcp) | **cao**: pip không bật `--pre` sẽ chọn 0.3.0 của bên thứ ba nếu vendor chỉ có pre-release |
| `vnstock-chart` | 1.0.0 | **404 (tên trống)** | **cao**: ai cũng đăng ký được 99.0.0 |
| `vnstock-installer` | 3.1.3 (`/api/simple`) | **99.0.0 bên thứ ba** "name-occupancy research" | đã xảy ra; vendor tự ghi triệu chứng "`vnstock_installer` 99.0.0 installed / Unpinned install" |
| `vnstock-quant`, `vnstock-auth`, `vnstock-news`, `vnstock-pipeline`, `vnstock-data` | có/không | placeholder 0.0.1 của vendor (`Vnstock <support@vnstocks.com>`) | thấp (vendor giữ tên) |
| `vnai` | không có ở `/api/packages` | quarantined | n/a cho Gen 5 |

Plan chỉ cần `vnstock`. Recipe đề xuất: một project spike riêng, venv riêng, không đụng `pyproject.toml` gốc của repo.

`spikes/vnstock_probe/pyproject.toml` (tên thư mục minh hoạ; tránh `data/`, `models/`):

```toml
[project]
name = "finsec-vnstock-probe"
version = "0"
requires-python = "==3.13.*"
dependencies = ["vnstock==5.1.0"]

[[tool.uv.index]]
name = "vnstocks"
url = "https://vnstocks.com/api/packages"
explicit = true            # chỉ dùng cho gói nào chọn nó trong tool.uv.sources

[tool.uv.sources]
vnstock = { index = "vnstocks" }
```

Lệnh (Git Bash; từ thư mục spike):

```bash
uv pip compile pyproject.toml \
  --generate-hashes --emit-index-annotation \
  --python-platform x86_64-pc-windows-msvc --python-version 3.13 \
  --index-strategy first-index \
  -o requirements-vnstock.lock
# Tiêu chí đạt: vnstock có chú thích "# from https://vnstocks.com/api/packages",
# pandas/numpy/tzdata/... có "# from https://pypi.org/simple", mỗi dòng có --hash=sha256:...,
# và hash vnstock = d263dec2b259e575f72ad7066363d6700baeb7c5d23948d63cd77e92f15ed973.
uv venv .venv-vnstock --python 3.13
uv pip sync --python .venv-vnstock/Scripts/python.exe --require-hashes \
  --index https://vnstocks.com/api/packages requirements-vnstock.lock
```

Ghi chú:
- Lúc `sync`, file lock chỉ có tên==version + hash. Vì vậy cần đưa index vendor vào. Hash cố định đúng file, nên kẻ chiếm tên không thay được nội dung. Cách dự phòng tốt hơn: tải wheel một lần, kiểm sha256, giữ trong thư mục gitignored riêng, rồi `uv pip sync --no-index --find-links <dir>` cho vnstock. Index vendor lúc đó chỉ dùng khi re-pin.
- Phương án uv-native (`uv lock` + `uv sync --locked`) cũng đúng: lockfile ghi index nguồn và hash từng wheel. Nhưng `uv lock` mặc định resolve universal mọi platform. Nếu dùng, thêm `[tool.uv] environments = ["sys_platform == 'win32'"]` (ASSUMED cú pháp, PRIOR).
- **Không** dùng `--index-strategy unsafe-best-match`. **Không** dùng `pip install --extra-index-url` như README hướng dẫn.
- `--exclude-newer`: index HTML vendor không có upload time, nên cờ này có thể loại hoặc bỏ qua gói vendor (ASSUMED). Đừng dùng cho vnstock.
- Yank: khi 5.1.0 bị yank, `uv pip sync` với pin `==` + hash nên vẫn chạy nếu file còn (PEP 592; hành vi uv = PRIOR). Re-pin là một bước có chủ đích, kèm đọc changelog.
- Bảo vệ AGENTS.md (vì lịch sử ở §3):
  - trước và sau mỗi lần `import vnstock`, ghi `sha256` của `AGENTS.md`, `CLAUDE.md`, `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md`, rồi so sánh;
  - đặt `VNSTOCK_DISABLE_AGENT_SETUP=1` và `VNSTOCK_DISABLE_GLOBAL_AGENT=1` (biến của 4.x, vô hại với Gen 5);
  - **cấm** cài `vnai`/vnstock 4.x vào bất kỳ env nào của repo.

## 8. vnstock3 3.2.1 (phương án legacy)

| Điểm | Bằng chứng | Nhãn |
|---|---|---|
| Cài được không | `Requires-Dist`: `requests, beautifulsoup4, pandas, vnai>=2.0, openpyxl, pydantic, psutil, fake_useragent, vnstock_ezchart, click, seaborn`. `vnai` trên PyPI quarantined, 0 file => `pip install vnstock3` fail: "ERROR: Could not find a version that satisfies the requirement vnai>=2.0". | OBSERVED `https://pypi.org/pypi/vnstock3/3.2.1/json` + simple JSON `vnai`; VERIFIED issue #237 |
| Còn bảo trì? | Upload cuối 2025-03-21; maintainer: "ngừng nâng cấp từ 2025". | OBSERVED PyPI; VERIFIED #237 |
| Endpoint còn sống? | Không có báo cáo trực tiếp cho 3.2.1 năm 2026. Dòng 4.x có lỗi nguồn: #223 "VCI financial endpoints raise KeyError 'data' after 403 from Vietcap" (2026-04-26), #235 `get_hosting_service()` UnboundLocalError làm hỏng `Trading.price_board` (2026-08-20), #228 VCI/KBS không trả dữ liệu VNMID/VNSML. | VERIFIED closed issues list; trạng thái 3.2.1 = ASSUMED hỏng một phần |
| Cứu bằng cách nào | Lấy `vnai` từ `https://vnstocks.com/api/simple`, tức đúng dòng code bị #236 phàn nàn (ghi file AI tới 2.6.1, fingerprint). | VERIFIED vibe-onboarding.md dòng 36, 287 |

Kết luận: **loại**. Không cài được sạch từ PyPI, ngừng bảo trì, và kéo theo `vnai`.

## 9. Ma trận lựa chọn (nguồn VN cho Phase 0a/0b.6)

| Tiêu chí | A. Portal chính thức (HSX/HNX/SSC) làm chính + vnstock Gen 5 phụ | B. vnstock Gen 5 làm chính | C. vnstock3 3.2.1 | D. Tự gọi endpoint VCI/KBS |
|---|---|---|---|---|
| Có news/disclosure trong ngày | Có (nếu portal có giờ đăng; probe) | **Không** (news bị gỡ; events chỉ có ngày) | có thể (TCBS/VCI cũ), không chắc | có thể |
| information_time | giờ đăng portal (probe) | chỉ ngày | ? | ? |
| Giá thô | không (portal không phải nguồn bar) | **không**, chỉ adjusted, nghìn đồng | ? | có thể |
| Rủi ro chuỗi cung ứng | thấp (HTTP client của mình) | trung bình (binary đóng, phone-home, yank nhanh, hết hạn 365 ngày) | cao (vnai) | thấp |
| Licence / ToS | robots HSX cho phép; HNX/SSC chưa rõ | cá nhân/nghiên cứu OK; dữ liệu tự chịu | như B + cũ | ToS từng nguồn, chưa kiểm; dễ vỡ |
| $0 | có | có (Community) | có | có |
| Phù hợp plan | **cao** | thấp làm nguồn chính | không | ngoài phạm vi 0a |

## Kết luận (xếp hạng)

1. **Poller 0b.6 dùng trang công bố chính thức làm nguồn chính; vnstock Gen 5 chỉ là nguồn phụ mức ngày.**
   - Gen 5 Community không có `news()` (`VNSTOCK_FEATURE_REMOVED`).
   - `events()` có `id` nhưng chỉ có ngày.
   - Poller gọi `ref.company(s).events(source="vci")` cho seed universe, khoảng 1 lần/giờ là đủ vì dữ liệu chỉ có ngày. Như vậy cũng tiết kiệm quota chung.
   - Lưu raw JSON của DataFrame (`df.to_json(orient="records")` + `df.attrs`) với `raw_sha256`.
   - Đặt `source_id = vnstock:vci:<id>`.
   - Gán `pit_quality` mức "date_only" cho information_time, và lấy `first_seen_at` từ đồng hồ NTP.
2. **Thêm vào probe 0a một mục chặn cho Phase 3: "ohlcv chỉ có giá adjusted, nghìn đồng".**
   - Probe đối chiếu `ohlcv(... interval="1D", source="vci")` và `source="kbs"` với giá tham chiếu portal quanh 5 ngày GDKHQ. Kiểm tra ×1000, adjusted hay raw, và chênh lệch vci/kbs.
   - Nếu không có giá thô: ghi ASSUMED-blocker cho §6.3 và tìm nguồn raw khác ở Phase 3.
   - Connector không được đưa giá vnstock vào `MarketTick` khi chưa nhân ×1000.
3. **Pin `vnstock==5.1.0` bằng recipe uv explicit index (§7) trong venv/spike riêng; không dùng `--extra-index-url`.**
   - Giữ bản sao wheel đã kiểm sha256 ở đường dẫn gitignored.
   - Heartbeat hằng ngày ghi `vnstock status` (tier, hạn dùng, version, device).
   - Re-pin có chủ đích, coi như một bước bảo trì định kỳ: nhịp yank quan sát được là vài ngày một lần, và mỗi bản tự tắt sau 365 ngày.
4. **Coi vnstocks.com là availability dependency riêng.**
   - Outage log phân biệt `vendor_auth_unreachable` với `source_unreachable`.
   - Poller vẫn chạy phần portal khi vnstock fail.
   - Đặt `DO_NOT_TRACK=1`, `VNSTOCK_TELEMETRY=0`, `VNSTOCK_DISABLE_AGENT_SETUP=1`, `VNSTOCK_DISABLE_GLOBAL_AGENT=1`.
   - Task Scheduler chạy dưới đúng user đã `vnstock register`.
   - Loại `%USERPROFILE%\.vnstock\` khỏi backup sync.
5. **Bảo vệ AGENTS.md và file chỉ dẫn agent toàn máy.**
   - Bước probe ghi hash trước và sau `import vnstock`; lệch là fail.
   - Cấm `vnai`/4.x trong mọi env.
   - Không gọi `load_skill`/`load_doc`/`setup_agent`.
6. **Fixtures:**
   - `tests/` chỉ chứa fixture vnstock tổng hợp (synthetic, schema thật, giá ví dụ dạng nghìn đồng);
   - payload thật nằm trong đường dẫn gitignored;
   - không commit wheel; lock file với hash thì commit được.
   - Thêm `tzdata` vào dev deps của repo (EDGAR test cần `America/New_York`).
7. **Quota và device:**
   - Không chạy probe nặng trên dev machine trong 5 phiên chạy thử poller (quota chung cho mọi device).
   - Đăng ký laptop capture trước, rồi kiểm tra danh sách device trên account.
8. **vnstock3: loại** (không cài được vì `vnai` quarantine; ngừng bảo trì 2025).

## Câu hỏi mở (chỉ probe thật trả lời được)

| # | Câu hỏi | Probe chính xác | Đọc kết quả |
|---|---|---|---|
| 1 | Recipe uv có thật sự lấy vnstock chỉ từ vendor và phần còn lại chỉ từ PyPI? | Chạy lệnh `uv pip compile ... --emit-index-annotation` ở §7 | Đạt: vnstock `# from https://vnstocks.com/api/packages` với hash `d263dec2b259e575f72ad7066363d6700baeb7c5d23948d63cd77e92f15ed973`, mọi gói khác `# from https://pypi.org/simple`; `uv pip sync --require-hashes` thành công |
| 2 | `vnstock status` / `vnstock env` / `vnstock doctor` in gì trên Windows (hạn dùng thật của 5.1.0, đường dẫn store, device id, tier)? | `.venv-vnstock/Scripts/vnstock.exe status`, `... env`, `... doctor` sau `register` (owner tự nhập key) | Ghi tier=Community, ngày hết hạn, đường dẫn `.vnstock\auth`; không in key |
| 3 | Community cho bao nhiêu device; máy thứ 2 có bị từ chối? | `vnstock register` trên laptop sau dev machine; xem `https://vnstocks.com/account?section=devices` | Ghi số device tối đa; lỗi "Device limit reached" nếu có |
| 4 | Rate limit/quota thực của Community | Script spike: lặp `ref.company(s).events()` và `ohlcv(... "1D")` cho N mã, mỗi call `time.sleep(k)`, ghi thời gian, `warnings`, `df.attrs`, exception; tăng dần tốc độ; chạy `vnstock status` trước/sau | Calls/phút tới khi có `UserWarning`/cắt; % quota giờ/ngày/tháng tiêu hao mỗi call |
| 5 | `events()` có giờ trong ngày ở bất kỳ nguồn nào (`vci`, `kbs`) không; `public_date` so với giờ đăng portal | Lấy events cho 30 công bố của task mục 6; in `dtype` và giá trị gốc của mọi cột ngày, cả `source="kbs"` | Nếu toàn ngày (00:00 hoặc chỉ date) thì xác nhận "date_only"; chênh lệch ngày so với portal |
| 6 | `ohlcv` đơn vị, adjusted vs raw, vci vs kbs, độ sâu thật | `ohlcv(symbol, start, end, interval="1D", source=s, floating=4)` với s ∈ {vci,kbs} quanh 5 ngày GDKHQ; so với giá tham chiếu portal; thử `start` 2010 và `interval="1m"` 200 ngày trước | ×1000 khớp tham chiếu?; giá trước GDKHQ bị điều chỉnh?; ngày cũ nhất trả về; `df.attrs` khi bị cắt |
| 7 | Mã đã huỷ niêm yết có dữ liệu không | `ohlcv` và `events` cho 3-5 mã đã huỷ niêm yết (danh sách lấy từ portal sở) | `InputError VNSTOCK_INPUT_SYMBOL_UNKNOWN` hay có dữ liệu |
| 8 | Hành vi khi vnstocks.com không truy cập được (licence check offline) | Trên laptop: chặn `vnstocks.com` trong `hosts`, gọi `ohlcv` và `events` | Lỗi gì, sau bao lâu; có cache quyền không. Từ đó định nghĩa `vendor_auth_unreachable` |
| 9 | Gen 5 có ghi file nào ngoài `~/.vnstock` không (AI files, telemetry) | Hash `AGENTS.md`, `CLAUDE.md`, `~/.claude/CLAUDE.md`, `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md` + liệt kê `~/.vnstock` trước/sau `import vnstock` và một lần gọi | Không có thay đổi ngoài `~/.vnstock` |
| 10 | Bản pin bị yank có còn cài và chạy được không | Khi 5.1.0 bị yank: `uv pip sync --require-hashes` trong venv mới + gọi `ohlcv` | Cài được (file còn) và chạy được (server không chặn) hoặc thông báo lỗi chặn |
| 11 | `DO_NOT_TRACK=1`/`VNSTOCK_TELEMETRY=0` có thật sự tắt đo lường | Không kiểm được nếu không bắt traffic; có thể bằng proxy cục bộ (mitmproxy) đếm request tới vnstocks.com khi bật/tắt | Số request giảm khi tắt; licence check vẫn còn (không tắt được) |
| 12 | Quyền dữ liệu của VCI/KBS cho lưu trữ cá nhân | Đọc ToS của VCI (Vietcap) và KBS; ngoài phạm vi research này | Xác nhận lưu raw cục bộ cho nghiên cứu cá nhân là chấp nhận được |

## Giới hạn của research này

- Không cài, không chạy vnstock. Mọi hành vi runtime là tài liệu vendor (single-source) hoặc ASSUMED.
- Không tìm được nguồn độc lập thứ ba cho: news bị gỡ, đơn vị nghìn đồng, adjusted-only, 365 ngày, env var telemetry. Chỉ có tài liệu vendor (docs + onboarding + README).
- Lý do quarantine của PyPI chưa được PyPI/PSF xác nhận.
- Không đọc mã nguồn 4.x trên GitHub để suy ra Gen 5 (Gen 5 là Rust core khác hẳn; giá trị suy luận thấp).
- Trang "Cấp tài khoản và giới hạn" (bảng tier) không tìm thấy.

## Nguồn

- https://github.com/thinh-vu/vnstock (README, raw README)
- https://github.com/thinh-vu/vnstock/issues/236 ; https://github.com/thinh-vu/vnstock/issues/237 ; https://github.com/thinh-vu/vnstock/issues?q=is%3Aissue+is%3Aclosed
- https://vnstocks.com/docs/vnstock ; /docs/vnstock/cai-dat ; /docs/vnstock/cau-hinh-va-dong-lenh ; /docs/vnstock/tham-chieu-reference ; /docs/vnstock/thi-truong-market ; /docs/vnstock/chuyen-doi ; /docs/tai-lieu/lich-su-phien-ban
- https://vnstocks.com/onboard/giay-phep-su-dung ; https://vnstocks.com/onboard/chinh-sach-quyen-rieng-tu ; https://vnstocks.com/agent-skills-hub ; https://vnstocks.com/files/vibe-onboarding.md (sha256 `b8120ae4c48b294f6ffffd678bded73d7ef6b69dbdf181fcae8f2e3978d40776`, 30299 byte)
- https://vnstocks.com/api/packages ; https://vnstocks.com/api/packages/vnstock ; https://vnstocks.com/api/vnstock/versions
- https://pypi.org/project/vnstock/ ; https://pypi.org/project/vnai/ ; https://pypi.org/pypi/vnstock3/3.2.1/json ; https://pypi.org/pypi/vnstock-mcp/json ; https://pypi.org/pypi/vnstock-installer/json ; https://pypi.org/pypi/vnstock-data/json (+ vnstock-quant/auth/news/pipeline)
- https://peps.python.org/pep-0592/
- https://docs.astral.sh/uv/concepts/indexes/ ; https://docs.astral.sh/uv/pip/compatibility/ ; https://docs.astral.sh/uv/reference/settings/ ; https://docs.astral.sh/uv/concepts/resolution/
- https://pydevtools.com/blog/pypi-exabyte-year-one-person-safety-team.md (bối cảnh quarantine, không nhắc vnstock)
