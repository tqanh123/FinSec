---
name: hs:codemap
injectable: false
description: >-
  Set up, refresh and check this repo's code map (codemap.yaml + snapshots) that backs
  the code facts plan, cook, fix and review read. Use when hs-run offers a code map, or
  to see or repair it.
allowed-tools: [Bash, Read, AskUserQuestion]
argument-hint: "[status | set-up | decline | refresh | check]"
metadata:
  compliance-tier: workflow
---

# hs:codemap — the repo's code map, from first setup to upkeep

The code map is `codemap.yaml` (how the repo is laid out) plus snapshots measured from
it (modules, imports, which tests reach what). hs-run's code facts (`code.tests_reached`,
`code.importers`, `code.callers`) read it; without it those facts are `unavailable`.
Every step below runs `hs-code` (`"${HARNESS_BIN_ROOT:-.}"/harness/bin/hs-code`, written
`hs-code` in the prose below); this skill decides nothing the CLI already answers.

## 1. Where it stands

```bash
"${HARNESS_BIN_ROOT:-.}"/harness/bin/hs-code --repo . init status
```

`data.adoption.state` says what to do:

| state | meaning | next |
|---|---|---|
| `adopted` | `codemap.yaml` exists | §4 upkeep; a `graph` row not ready only lacks a snapshot (§4) |
| `offer` | no map, a language it reads, nothing decided this session | §2, ask the user |
| `declined` | the user said not now this session | stop; do not ask again this session |
| `unattended` | the runtime says nobody is at the terminal | stop; never set it up unasked |
| `unsupported` | no file the map can read (`reason` names the suffixes) | stop; say so plainly |

A TypeScript/JavaScript repo is read but never offered yet: it reports `unsupported` until
it declares `languages: {typescript: {engine: tree-sitter|typescript}}` in a hand-written
`codemap.yaml` (keys: `docs/harness/codemap-reference.md` §4); an offered map for it is later work.

Each `initializers` row is one domain's own setup: `graph` (the map) runs here; a row
with a `route` (port → `/hs:port`) belongs to that skill and is only reported.

## 2. Set it up — only with the user's yes

Creating the map writes `codemap.yaml` into the repo, so it is the user's call. Show the
dry run, then ask with AskUserQuestion in non-technical Vietnamese, recommended first:

```bash
"${HARNESS_BIN_ROOT:-.}"/harness/bin/hs-code --repo . init run   # dry: nothing on disk
```

- **Lập bản đồ** (như lập mục lục cho cuốn sách) → `hs-code --repo . init run --apply`
  writes `codemap.yaml` and the first snapshot in one step.
- **Để sau** → §3.

After `--apply`, `init status` exits 0. The layout comes from auto-discover; when the
dry run shows the wrong roots, set it up with `graph init --layout <preset> --apply`
instead (presets: `src-layout`, `python-flat`, `monorepo-workspace`), then
`graph snapshot --apply`. If hs-run sent you here through `codemap_offer`, rerun that
step: its real answer was kept in `deferred`.

Then check the map before trusting it: `graph drift` on the fresh snapshot must say
`drifted: false`, and `graph tests` lists its orphan test files by path — a pile of
orphans means `import_roots` names modules wrong (`src.pkg.mod` instead of `pkg.mod`).
Tests kept beside the code (`<dist>/tests_x/` next to `<dist>/src/`) need no root of
their own: the snapshot measures every test tree no root covers as `test_modules`.

A snapshot stopping on `UNPARSEABLE` that names `requires-python` means the
interpreter running the harness is older than the repo's syntax. Set
`HARNESS_PYTHON` (a path, or a command prefix such as `uv run -q --no-project --python
3.14 --with-requirements "$HARNESS_BIN_ROOT/harness/requirements-runtime.txt" python`;
behind a TLS-intercepting proxy add `--system-certs`, or `--native-tls` on an older
uv) — every launcher honors it (export it, or put it in `settings.local.json` `env`), so
the `facts_stale` refresh command then runs as printed. Never install harness deps into
the target repo's own venv.

## 3. Decline for this session

```bash
"${HARNESS_BIN_ROOT:-.}"/harness/bin/hs-code --repo . init decline
```

Silent for the rest of this session; a later session asks again.

## 4. Keep it current

- Snapshot after the tree moves (hs-run says `facts_stale` with the exact command):
  `hs-code --repo . --apply graph snapshot`.
- Check the map still fits the tree: `hs-code --repo . graph drift` and
  `hs-code --repo . graph tests` (orphan tests usually mean `import_roots` is wrong).
- A new top-level package or a move to `src/`/`packages/`: fix `layout.roots` and
  `import_roots` in `codemap.yaml`, then snapshot again.

## Boundaries

- Never create or rewrite `codemap.yaml` without the user's yes in this session.
- Report the CLI's exit and `reason` as they are; an exit 3 is a refusal, not a failure to retry.
