---
trigger: glob
globs: "*.py, **/*.py"
description: "Pointer to the binding look-ahead, VN market and order-execution rules in AGENTS.md."
---
# Quant & Dual-Market Rules (pointer)
This file only points to the project contract. AGENTS.md wins where this file and AGENTS.md differ.
- **No look-ahead:** AGENTS.md "Engineering Rules (binding)" -> "No look-ahead" and "Point-in-time contract (roadmap §5)".
- **VN prices:** AGENTS.md "Engineering Rules (binding)" -> "VN"; the band check itself (decision D4: `price_limits`, the one-tick tolerance, special sessions) is in AGENTS.md "Decisions".
- **No order execution:** AGENTS.md "Engineering Rules (binding)" -> "No order execution anywhere".
- **RAG, the bandit and other optional components:** AGENTS.md "Engineering Rules (binding)" -> "Complexity gate (§9.10)". No retrieval technique is required in any file.
