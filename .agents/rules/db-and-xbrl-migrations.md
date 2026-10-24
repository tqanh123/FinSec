---
trigger: model_decision
description: "Apply when designing storage for filings, financial statements or ticks: points to the binding storage rules in AGENTS.md."
---
# Storage Rules (pointer)
This file only points to the project contract. AGENTS.md wins where this file and AGENTS.md differ.
- **Storage:** AGENTS.md "Engineering Rules (binding)" -> "Storage (§9.9)". Tick storage is Phase 6 work.
- **Restatements and time:** AGENTS.md "Engineering Rules (binding)" -> "No look-ahead" (keep every restated version of a fundamental) and "Point-in-time contract (roadmap §5)".
- **Financial values in storage:** docs/code-standards.md "Precision" (planned: `Decimal`, integer minor units or `NUMERIC`).
