---
phase: {{NUM}}
title: "{{PHASE_TITLE}}"
plan: {{PLAN_ID}}
created: {{CREATED}}
---

# Phase {{NUM}} — {{PHASE_TITLE}}

## Overview
TBD — phase này giao gì (1-2 câu), vì sao, phụ thuộc phase nào.

## Requirements
- TBD — yêu cầu chức năng + phi chức năng, nói rõ từng điều.

## Related Code Files
| Hành động | Tệp |
|---|---|
| Modify | TBD — đường dẫn đầy đủ trong backtick (qua staging-cp nếu file guarded) |

Mỗi hàng Create / Modify / Delete phải khớp `files_to_*` của phase trong
plan-graph.yaml; hàng chỉ đọc/chạy ghi `read`/`run` ở cột đầu.

## Implementation Steps
1. **Tests-before (RED)** TBD — test gì fail trước, fail vì lý do nào.
2. **Implement** → xanh.
3. **Regression** chạy test suite + lint/type-check của project (lệnh thật của
   repo); commit phase khi gate xanh.

## Success Criteria
- [ ] TBD — tiêu chí đo được (output thật, không "commit là xong").

## Risk Assessment
TBD — rủi ro (khả năng x tác động) + cách xử cho mục High.
