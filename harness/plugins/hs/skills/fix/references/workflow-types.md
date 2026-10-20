# Type Error Fix Workflow

Quick workflow for TypeScript/type errors.

## Commands
```bash
bun run typecheck
tsc --noEmit
npx tsc --noEmit
```

## Rules
- Fix ALL type errors, don't stop at first
- **NEVER use `any` just to pass** - find proper types
- Repeat until zero errors

## Workflow
1. Run typecheck command
2. Fix errors one by one
3. Re-run typecheck
4. Repeat until clean
