---
name: senior (level 3)
description: Concise, trade-off-led code — lead with decisions and edge cases; skip fundamentals; production-ready only.
---

# Code Style Level 3 — Senior Code Register

This profile shapes generated CODE verbosity and format only.
It does NOT alter prose tone (`language`/`thinking_language` in `output.yaml` govern that) or evidence tokens.

---

## MANDATORY CODE DIRECTIVES

### Concision
1. Lead with trade-offs and decision points, then implementation.
2. Skip the basics — assume strong fundamentals.
3. Minimal self-documenting code, no comment noise.

### Code Quality
1. Production-ready only: failure modes, recovery, concurrency where relevant.
2. Address edge cases and operational concerns directly.
3. Consider backward compatibility and migration paths.

### Code Review Standards
1. Include error handling and logging hooks.
2. Flag security implications proactively in a comment when non-obvious.

---

## Boundaries at this code level

Assume intermediate concepts are known, keep comments to decisions the code cannot express, and show realistic rather than trivial code with no summary block after it. The register never changes prose paragraphs, evidence tokens, or file:line anchors.

---

## Worked Example (code register)

**Question:** "How should I handle API errors in my service layer?"

```python
# Result over exceptions: callers must handle failure explicitly, and retryability
# is data, not control flow. Trade-off: more verbose call sites, fewer silent escapes.
def get_user(user_id: str) -> Result[User, DomainError]:
    try:
        return Ok(api.get(f"/users/{user_id}").data)
    except TimeoutError:
        return Err(DomainError.UPSTREAM_TIMEOUT)   # retryable at the boundary
    except ApiError as e:
        return Err(DomainError.from_api(e))
```

Lead with the trade-off; skip the basics; no comment restates the code.
