#!/usr/bin/env python3
"""build_all.py — orchestrator: chạy tuần tự showcase → flat-md → excel → pdf → render-check.

Tạo dist/ nếu chưa có, sau đó gọi từng script con. Bước render-check (multi-viewport
layout, `render_check.py`) SKIP (không tính lỗi) khi máy không có trình duyệt headless —
không bao giờ báo im lặng thành xanh.
In tổng kết artifact cuối cùng.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[5]

sys.path.insert(0, str(HERE))
from build_showcase import check_index_precondition  # noqa: E402

# Best-effort shared UTF-8 console (DRY: harness/scripts/encoding_utils) — the
# orchestrator prints Vietnamese labels; degrades to a no-op outside a harness tree.
_HARNESS_SCRIPTS = REPO / "harness" / "scripts"
if _HARNESS_SCRIPTS.is_dir():
    sys.path.append(str(_HARNESS_SCRIPTS))
try:
    from encoding_utils import configure_utf8_console  # noqa: E402
except Exception:  # noqa: BLE001
    def configure_utf8_console():
        return None


def _run(script: str, label: str) -> bool:
    """Chạy script con, in kết quả. Trả về True nếu thành công."""
    print(f"\n{'='*60}")
    print(f">> {label}")
    print(f"{'='*60}")
    result = subprocess.run([sys.executable, str(HERE / script)], check=False)
    ok = result.returncode == 0
    status = "OK" if ok else f"FAIL (exit {result.returncode})"
    print(f"<< {label}: {status}")
    return ok


def _render_check_outcome(returncode: int) -> "bool | None":
    """Classify render_check.py's exit code: 0 -> pass, {1, 2} -> a real layout
    or launch failure, 3 -> this host cannot run the check at all — a SKIP,
    which must count as neither pass nor fail (never silently green, never a
    build-breaking failure the host has no way to fix)."""
    if returncode == 3:
        return None
    return returncode == 0


def _run_render_check(label: str) -> "bool | None":
    """Multi-viewport layout check over public/ — see render_check.py's own
    preflight for why a returncode of 3 is reported as SKIPPED rather than
    folded into OK/FAIL."""
    print(f"\n{'='*60}")
    print(f">> {label}")
    print(f"{'='*60}")
    result = subprocess.run([sys.executable, str(HERE / "render_check.py"),
                            "--public", str(REPO / "public")], check=False)
    outcome = _render_check_outcome(result.returncode)
    if outcome is None:
        print(f"<< {label}: SKIPPED (host cannot run a headless-browser layout check)")
    else:
        print(f"<< {label}: {'OK' if outcome else f'FAIL (exit {result.returncode})'}")
    return outcome


def main():
    configure_utf8_console()
    reason = check_index_precondition(REPO)
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(2)
    # Đảm bảo dist/ tồn tại (các script con cũng tự tạo, nhưng giữ an toàn)
    dist = REPO / "dist"
    dist.mkdir(parents=True, exist_ok=True)

    results = {}
    results["showcase"] = _run("build_showcase.py", "Bước 1/4 — Showcase HTML (data từ _index)")
    results["flat_md"] = _run("build_flat_md.py", "Bước 2/4 — Flat MD (dist/aiop-docs.md)")
    results["excel"] = _run("build_excel.py", "Bước 3/4 — Excel catalog (dist/aiop-catalog.xlsx)")
    results["pdf"] = _run("build_pdf.py", "Bước 4/4 — PDF (dist/aiop-docs.pdf)")
    results["render_check"] = _run_render_check("Bước 5/5 — Render-check (bố cục nhiều cỡ màn hình)")

    # ------------------------------------------------------------------
    # Tổng kết artifact
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print("TỔNG KẾT ARTIFACT")
    print(f"{'='*60}")

    artifacts = [
        (REPO / "public" / "index.html", "public/index.html"),
        (REPO / "dist" / "aiop-docs.md", "dist/aiop-docs.md"),
        (REPO / "dist" / "aiop-catalog.xlsx", "dist/aiop-catalog.xlsx"),
        (REPO / "dist" / "aiop-docs.pdf", "dist/aiop-docs.pdf"),
    ]
    for path, label in artifacts:
        if path.is_file():
            size = path.stat().st_size
            print(f"  [OK]  {label} ({size:,} bytes)")
        else:
            print(f"  [--]  {label} (chưa có)")

    # Đếm page
    pages_dir = REPO / "public" / "pages"
    page_count = len(list(pages_dir.glob("*.html"))) if pages_dir.is_dir() else 0
    print(f"\n  public/pages/: {page_count} file")

    skipped = [k for k, v in results.items() if v is None]
    failed = [k for k, v in results.items() if v is False]
    if skipped:
        print(f"\n[SKIP] Không chạy được (không tính là lỗi): {', '.join(skipped)}")
    if failed:
        print(f"\n[WARN] Một số bước thất bại: {', '.join(failed)}")
        sys.exit(1)
    else:
        print("\n[ALL DONE] Mọi bước hoàn tất.")


if __name__ == "__main__":
    main()
