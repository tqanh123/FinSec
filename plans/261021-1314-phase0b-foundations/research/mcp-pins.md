# Research: pin MCP refs trong `.agents/mcp_config.json`, toolset Alpaca

Ngày: 2026-10-21. Code tải về chỉ được **đọc**, không chạy. Wheel được tải bằng `curl` từ URL trong PyPI JSON, giải nén bằng `python -I -m zipfile -e` vào thư mục riêng trống (`research-probe/dl-alpaca`, `dl-edgar`, `dl-wandb`). Nhãn: VERIFIED (URL) / OBSERVED (ngày, lệnh) / ASSUMED / PRIOR.

## 1. Cú pháp `uvx` để pin

- Theo docs uv 0.12.5, có hai cách pin: `command@<version>` (ví dụ `uvx ruff@0.3.0 check`) và `--from 'ruff==0.3.0' ruff check`. Dùng kèm extra: `uvx --from 'mypy[faster-cache,reports]==1.13.0' mypy`. Docs ghi rõ: "the `@` syntax cannot be used for anything other than an exact version". VERIFIED https://github.com/astral-sh/uv/blob/0.12.5/docs/guides/tools.md (dòng 75-115).
- Git: tag `uvx --from git+https://github.com/httpie/cli@3.2.4 httpie`, commit `…/cli@2843b87 httpie`. VERIFIED cùng file (dòng 133-143).
- Probe không dùng code lạ: tôi tự dựng wheel `probetool` (0.1.0/0.2.0/0.3.0, extra `ai`) bằng `zipfile`, rồi chạy `uvx --no-index --find-links wheels …` với uv 0.12.5. OBSERVED 2026-10-21. Cả 4 dạng đều chạy đúng 0.1.0:
  - `uvx probetool==0.1.0`
  - `uvx probetool@0.1.0`
  - `uvx --from probetool==0.1.0 probetool`
  - `uvx --from probetool[ai]==0.1.0 probetool`
  
  Dạng `pkg==ver` để positional không có trong guide nhưng uv chấp nhận. README của Alpaca cũng dùng nó: "`uvx alpaca-mcp-server==1.x.x serve`" (METADATA dòng 110).
- Không pin thì không tái lập được. Docs nói `uvx` dùng bản mới nhất "on the first invocation", sau đó "will use the cached version … unless … the cache is refreshed". VERIFIED https://github.com/astral-sh/uv/blob/0.12.5/docs/concepts/tools.md (dòng 47-52). Probe với `--find-links` local lại nhặt bản 0.3.0 mới ngay. OBSERVED 2026-10-21. Hai kết quả này mâu thuẫn, có thể do cách uv làm mới find-links khác cách làm mới index; dù theo kiểu nào, bản chạy cũng phụ thuộc trạng thái cache.
- Chưa probe `git+…@<sha>`, vì build từ git sẽ chạy build backend của repo lạ. Cú pháp này là VERIFIED theo docs; dùng SHA đủ 40 ký tự (ASSUMED là uv chấp nhận full SHA giống short SHA).
- Pin version **không khoá dependency bắc cầu**. Ví dụ alpaca-mcp-server 2.4.0 khai `fastmcp==3.4.7` nhưng `httpx>=0.27.0`, `click>=8.1.0`, `python-dotenv>=1.0.0` (METADATA dòng 24-27). VERIFIED wheel (URL ở mục 2).

## 2. alpaca-mcp-server 2.4.0 — đọc từ wheel

Wheel `https://files.pythonhosted.org/packages/ee/eb/05f531fd5134bcb14777cd124bc6f449c2291e935c72f1dc8da845266acf/alpaca_mcp_server-2.4.0-py3-none-any.whl`. sha256 `dc54f75f…3ed2` khớp digest trên PyPI. OBSERVED 2026-10-21 (`sha256sum`). Các dòng dưới là VERIFIED từ mã trong wheel đó.

- **Toolset hợp lệ (11):** `account`, `trading`, `watchlists`, `assets`, `stock-data`, `crypto-data`, `options-data`, `corporate-actions`, `news`, `fixed-income-data`, `locates` (`toolsets.py:10-122`). Bảng "Available toolsets" trong METADATA (dòng 311-327) liệt kê đúng 11 tên này.
- **`trading` có tồn tại và là toolset đặt/huỷ lệnh.** Ops gồm `patchOrderByOrderId`, `deleteOrderByOrderID`, `deleteAllOrders`, `deleteOpenPosition`, `deleteAllOpenPositions`, `optionExercise`… (`toolsets.py:21-38`). Thêm override `place_stock_order`, `place_crypto_order`, `place_option_order` (`overrides.py:70,202,258`), chỉ được đăng ký khi `"trading" in active_ts` (`server.py:204-207`). → Bỏ `trading` là đúng và **bắt buộc**.
- **`stock-data,options-data,corporate-actions,news,watchlists` đều là tên hợp lệ.**
- **`watchlists` CÓ thao tác ghi:** `postWatchlist`→`create_watchlist`, `updateWatchlistById` ("IMPORTANT: this replaces the entire watchlist"), `deleteWatchlistById`, `addAssetToWatchlist`, `removeAssetFromWatchlist` (`toolsets.py:40-51`, `tool_registry.py:139-170`). Đây không phải lệnh giao dịch, nhưng nó ghi lên trading API của tài khoản (spec `"trading"`). Bật nó thì tạo `trading_client` (`server.py:156`), trỏ tới paper vì `ALPACA_PAPER_TRADE` mặc định `"true"` (`server.py:117`).
- `account` có `patchAccountConfig` (ghi); `locates` có `createLocates` (ghi) (`toolsets.py:10-20,122-131`). Hiện config không bật hai toolset này. Giữ nguyên như vậy.
- **Bẫy: biến rỗng hoặc unset nghĩa là BẬT TẤT CẢ.** `_parse_toolsets()` trả `None` khi `ALPACA_TOOLSETS` rỗng (`server.py:128-132`). `get_active_operations`: "None means all" (`toolsets.py:144-158`). README: "By default, all tools are enabled". → Xoá biến này là bật lại `trading`.
- **Tên sai bị bỏ qua, không báo lỗi.** Code chỉ lọc `TOOLSETS` theo tập tên, không kiểm tra tên lạ (`toolsets.py:154-157`). Gõ sai một tên thì toolset đó im lặng mất. Nếu mọi tên đều sai thì tập không rỗng, nên ra 0 tool, chứ không phải tất cả (ASSUMED theo logic đọc được; chưa chạy).
- Tool tìm docs (`register_readme_docs_tools`) luôn được đăng ký, không phụ thuộc toolset (`server.py:212`), và tự mô tả là "Read-only".
- CLI không có subcommand: "Older … configs invoked `alpaca-mcp-server serve`; the CLI has no subcommands". `--transport` mặc định `stdio`. Thiếu `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` thì exit 1 (`cli.py:16-60`). → `args` không cần `serve`.
- `Requires-Python: >=3.10` (METADATA dòng 23).

## 3. edgartools 5.62.0

Wheel sha256 `fff0265b…f3f8` khớp PyPI. OBSERVED 2026-10-21.
- `entry_points.txt`: `edgartools-mcp = edgar.ai.mcp.server:main` và `edgartools = edgar._cli:main`. Console script có **vô điều kiện** trong wheel. Extra `ai` chỉ thêm deps: `mcp<2.0.0,>=1.28.1`, `starlette>=1.3.1`, `tiktoken>=0.10.0`, `uvicorn>=0.27.0` (METADATA dòng 52-56). Module `edgar/ai/mcp/server.py` có `def main` (dòng 424) và đọc `EDGAR_IDENTITY` (dòng 59-71). VERIFIED wheel. Thiếu `[ai]` thì script vẫn được cài nhưng sẽ lỗi khi import `mcp` (ASSUMED).
- README của chính gói dùng đúng dạng `"args": ["--from", "edgartools[ai]", "edgartools-mcp"]` (METADATA dòng 315-316). Bản pin: `["--from", "edgartools[ai]==5.62.0", "edgartools-mcp"]`. Cú pháp extra kèm `==` là VERIFIED theo docs uv và OBSERVED với `probetool[ai]==0.1.0`.

## 4. Repo git

Script và `requires-python`: VERIFIED từ `pyproject.toml` tại đúng SHA, URL dạng `https://raw.githubusercontent.com/<repo>/<sha>/pyproject.toml`. HEAD và ngày commit: OBSERVED 2026-10-21 (`gh api repos/<repo>/commits/HEAD`).

| Server | SHA pin | Script (có tồn tại) | requires-python | Ghi chú |
|---|---|---|---|---|
| maverick | `ba170996494a139a9c611ad0ec4d69cb692ac8a3` (tag v1.1.0) | `maverick-mcp`, `maverick-mcp-server` | `>=3.12` | Tên gói `maverick-mcp-server` 1.1.0. HEAD đã sang `da70d074…` (2026-10-17), nên tag hiện trỏ tới commit cũ hơn HEAD |
| vnstock-agent | `e7eaf08c5bea6f99556c5b66952b9700faf7cbbf` (= HEAD, 2026-04-01) | `vnstock-mcp` | `>=3.10` | không có tag |
| ssi-stock-mcp-server | `b93442adac835ff55048bba484b402b720bc79e1` (= HEAD, 2025-08-06) | `ssi-stock-mcp-server` | `>=3.12` | deps có `ssi_fc_data>=2.2.2`, tức là một SSI client. Quy tắc §9.5 cấm SSI client trước Phase 6 → giữ `"disabled": true` |

## 5. wandb-mcp-server

- PyPI trả 404 cho cả `wandb-mcp-server` và `wandb_mcp_server`. OBSERVED 2026-10-21 (`curl -w %{http_code}`). → Config hiện tại `uvx wandb-mcp-server` **không chạy được** dù có bật.
- Nguồn là GitHub `wandb/wandb-mcp-server` (public, không archived, HEAD `465ea99c421f741187c7b20812a1f5b02de6b792`, 2026-10-16). Repo có **0 tag** và 0 GitHub release. OBSERVED 2026-10-21 (`gh api …/git/matching-refs/tags` → `0`; `gh api …/releases` rỗng).
- `pyproject.toml` tại HEAD: `name = "wandb_mcp_server"`, `version = "0.4.3"`, `requires-python = ">=3.11"`, script `wandb_mcp_server = "wandb_mcp_server.entrypoint:cli"` (gạch dưới). README hướng dẫn `uvx --from git+https://github.com/wandb/wandb-mcp-server@vX.Y.Z wandb_mcp_server` và "Replace vX.Y.Z with a signed, available release from the release index". Release index ghi 0.4.3/0.4.2 là "Candidate", 0.4.1 là "no signed source release". README khuyên dùng server hosted `https://mcp.withwandb.com/mcp`. VERIFIED https://github.com/wandb/wandb-mcp-server/blob/465ea99c421f741187c7b20812a1f5b02de6b792/README.md và `docs/releases/README.md`.
- → Chỉ pin được bằng commit SHA. Theo chính README của họ, SHA đó không phải release đã ký. Không có tag nào để dùng; **đừng bịa `@v0.4.3`**.

## Kết luận (xếp hạng)
1. `alpaca-us`: `"args": ["--from", "alpaca-mcp-server==2.4.0", "alpaca-mcp-server"]`, `"ALPACA_TOOLSETS": "stock-data,options-data,corporate-actions,news"`. Tức là bỏ `trading` và **bỏ luôn `watchlists`** (có ghi, dự án tự quản watchlist theo §9.7, chưa cần cái của Alpaca). Nếu owner muốn giữ `watchlists`, ghi rõ trong AGENTS.md là có 5 thao tác ghi, không phải lệnh. Giữ `"ALPACA_PAPER_TRADE": "true"`.
2. Thêm guard test đọc `.agents/mcp_config.json` (chỉ đọc JSON, không gọi mạng), kiểm tra các điều sau:
   - `ALPACA_TOOLSETS` **không rỗng**;
   - nó không chứa `trading`, `account` hay `locates`;
   - mọi tên nằm trong 11 tên của 2.4.0;
   - `ALPACA_PAPER_TRADE == "true"`;
   - mọi `args` uvx đều có `==` hoặc `@<40-hex>`;
   - `ssi-fastconnect-vn` có `disabled: true`.
3. `edgartools-us`: `["--from", "edgartools[ai]==5.62.0", "edgartools-mcp"]`.
4. Ba repo git: thay `@v1.1.0` (tag có thể bị dời) và URL không ref bằng `git+https://github.com/<repo>@<sha40>` theo bảng ở mục 4. Nên bỏ hậu tố `.git` cho thống nhất (tuỳ chọn, ASSUMED uv chấp nhận cả hai). Trong cùng file hoặc trong AGENTS.md, ghi chú SHA ứng với tag nào hoặc HEAD ngày nào.
5. `playwright`: `["@playwright/mcp@0.0.83"]` (đã established).
6. `wandb`: ưu tiên **xoá entry** (Phase 7 optional, đang disabled và hỏng). Nếu muốn giữ thì `["--from", "git+https://github.com/wandb/wandb-mcp-server@465ea99c421f741187c7b20812a1f5b02de6b792", "wandb_mcp_server"]`, `disabled: true`, kèm comment "unsigned candidate 0.4.3".

## Câu hỏi còn mở
- Client nào đọc `.agents/mcp_config.json` (Antigravity?), và nó có tự expand `${ALPACA_API_KEY}` trong `env` không? Ngoài phạm vi nhưng ảnh hưởng tới việc secret có lọt vào file hay không.
- Có cần khoá cả dependency bắc cầu (ví dụ `uv tool install` kèm constraints, hoặc lockfile) không, hay pin top-level là đủ cho Phase 0b?
- Hành vi cache thật của `uvx` (không pin) với PyPI chưa được probe; chỉ có docs và probe find-links, mà hai kết quả không khớp nhau.
- Có ai kiểm tra ra 0 tool khi mọi tên toolset đều sai không? Mới suy từ code, chưa chạy server (cố ý không chạy code tải về).
