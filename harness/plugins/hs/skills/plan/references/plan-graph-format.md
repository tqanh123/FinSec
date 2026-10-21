# `plan-graph.yaml` — the shape, and what each state is telling you

Routed at `absent`, `malformed` and `invalid`: the three states where `plan
graph` cannot answer and the next move is an edit to this file. Not routed at
`ok` — there is nothing to fix there.

Read the state the envelope printed, then the matching section. Every rule below
is the parser's actual behaviour (`"${HARNESS_BIN_ROOT:-.}"/harness/scripts/plan_graph.py`),
not a style preference.

## The file

Three top-level keys, all optional, each with a required type when present:

```yaml
edges:
  - {from: P1, to: P2}        # P1 is a PREREQUISITE OF P2 — not the reverse
subtasks:
  P1:
    file: phases/phase-1.md
    post: [verification-P1.json]
    files_to_create: [src/new.py]
  P2:
    file: phases/phase-2.md
    post: [verification-P2.json]
    files_to_modify: [src/new.py]
merger_owned: [harness/manifest.json, docs/decisions.yaml]
```

A subtask may also declare `resources:` — a list of names for what two phases can collide on
without sharing a file (`- db:noti-schema`, `- ledger:outside-repo`; quote a name that holds `:`
inside `[...]`). Two phases with no path between them that name the same resource are a
`parallel-conflict`, like a shared file: add the edge, or split the work.

`edges` must be a **list**, `subtasks` must be a **mapping**, `merger_owned`
(when present) must be a **list of literal file paths**. The decision register belongs there
rather than behind an edge: `decision_register.py` reserves each id across every worktree of the
clone, so two parallel phases add distinct records and the merge keeps both. Either of the first two may be
absent, or present with an empty value (`edges:` alone) — a single-phase plan has
no edges, and that is normalised to an empty container rather than treated as
corruption. What is NOT accepted is the wrong type: `subtasks:` authored as a
list lands in `malformed` with the message naming the key and the type it found.
The parser rejects the wrong type outright: a silently discarded graph would print "parallel batches: []" and exit 0.

Edge direction is the one thing everybody gets backwards. `{from: P1, to: P2}`
means **the `from` node must finish before the `to` node starts**. Reverse it
and the ordering checks still run — against the wrong order, silently.

Node names are free-form; a node exists if it appears as a `subtasks` key OR at
either end of an edge. An edge naming a node with no `subtasks` entry is legal
and creates that node — which is also how a typo becomes an invisible extra node
with no `post`, so `invalid` fires below.

### Per-node keys

| Key | Type | What reads it |
|---|---|---|
| `file` | str, optional | the phase document this node is about — a path relative to the plan dir. `hs-run cook prompt developer --phase <id>` hands it to the implementer (in `artifacts` and the prompt). A declared `file:` that is not a string, is absolute, resolves outside the plan dir, or names no file prints a `phase-file:` line; `--require` and cook's preflight refuse it |
| `post` | list[str], non-empty | **mandatory** — the end-of-phase artifact obligation |
| `files_to_create` | list[str] | ordering-hazard + parallel-conflict checks |
| `files_to_modify` | list[str] | same |
| `files_to_delete` | list[str] | same |
| `paused` | bool | drops the phase from the batch, the completion denominator, and the gate's snapshot sweep; every phase downstream of it is **blocked** |
| `paused_reason` | str | **mandatory when `paused: true`** — printed wherever the smaller count is |
| `reopen_when` | str | what has to happen before the pause lifts; printed with the reason |

The three `files_to_*` lists are what let the graph say "these two phases cannot
run in parallel because both write `src/api.py`". Leaving them out does not make
a plan invalid; it makes those checks silent, which is worse than a complaint.

### Pausing a phase

```yaml
subtasks:
  p3:
    post: [verification-p3.json]
    paused: true
    paused_reason: "upstream vendor API is down; nothing to integrate against"
    reopen_when: "the vendor sandbox answers again"
```

A pause says *this work is deliberately deferred*. Three consumers act on it at
once: the phase is not handed out as runnable work, the plan is not held open
waiting for it, and the stage gate stops demanding a verification snapshot it was
never going to write.

An edge means **needs**. Every phase reachable downstream of a paused one is
**blocked**: it leaves the parallel batches and is reported with the pause it waits
on — `blocked: p4 waits on paused p3: <paused_reason>`. A blocked phase is NOT
paused: nobody deferred it, it simply cannot start. So it stays in the completion
denominator and the gate still owes its snapshot — the plan does not read as done
while one remains. When nothing runnable is left and a blocked phase remains, cook
stops at `phases_blocked`.

A phase that does not actually need the paused one must not carry the edge. There
is no opt-out key: remove the edge, and the phase is schedulable again.

Two things a pause is NOT:

* **Not a way to retire a red phase.** If the phase ran and its snapshot says FAIL,
  that snapshot is still read and still blocks. Pausing changes what the plan
  *expects*, never what already happened.
* **Not silent.** `paused: true` with no `paused_reason` is a finding — a count that
  got smaller with no sentence attached is one nobody can check. So is
  `paused_reason` left behind on a phase whose flag was cleared: it reads as paused
  to a human and does not to the machine.

## Parallel-safety: every unordered pair, not just one topological batch

`find_parallel_conflicts` compares every pair of phases with NO path between
them in either direction of the DAG (`unordered_phase_pairs`) — not only
pairs that happen to land in the same `find_parallel_batches` level. Two
phases can share no edge at all and still land in *different* topological
levels purely because one of them has other predecessors the other lacks; a
same-level-only scan never compares that pair, and a shared file between them
goes unreported.

A shared FILE is not the only hazard: two phases can own no file in common and
still be unsafe run concurrently, when one's file imports the other's. The
scan auto-selects the project's own hs-code snapshot of HEAD from the store —
the same store_base+select_snapshot selection every other hs-run fact
provider already uses (schema- and freshness-checked) — so there is no
snapshot directory to name here:

```bash
python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/plan_graph.py <plan_dir> --require
```

Prints `import-edge-conflict:` lines for a disjoint-file pair joined by a real
import edge (`find_import_edge_conflicts`) — the same check
`cook_parallel_plan.py`'s own auto-selected snapshot runs, so a plan-time read
and a cook-time partition never disagree about what "safe in parallel" means.
No fresh snapshot of HEAD in the store (nothing adopted, or none current): the
scan is simply skipped. `--units-json <path>`
stays available as an EXPLICIT override, for a fixture or a snapshot other
than the project's own HEAD one.

### `merger_owned:` — files a human reconciles at merge time

A **top-level, literal-path-only** list (not a glob, not per-node): files two
otherwise-unordered lanes may both declare, because a human merger reconciles
them when the lanes land — a manifest regeneration, a shared DEC ledger, a
hand-kept per-domain table several lanes each add one row to. Naming a file
here is a claim that plan_graph cannot verify: the graph only ever sees a
whole FILE as declared, never a line, so it genuinely cannot tell "two lanes
edit the same line" (the merger's job) from "two lanes edit two different
lines of the same file" (no real collision at all). That is also its limit —
this key is scoped at file granularity on purpose, and a plan whose two lanes
really do need line-level reconciliation on a file NOT in this list still
gets flagged.

Two things this key does **not** do:

* It does **not** suppress the finding. A pair sharing a `merger_owned` file
  is dropped from `find_parallel_conflicts`'s own list (never counted as a
  parallel-safety conflict, never blocks anything) but is always reported
  separately, by `find_merger_owned_overlaps` — printed under its own
  `merger-owned:` line by the CLI. Exempt from blocking, never exempt from
  being seen.
* It is **not** authored per-phase. One list, read once, applied to every
  pair — a phase does not opt in or out of it individually.

## `absent` — no sidecar at all

Exit 0 plain, **exit 2 under `--require`**. Not an error on its own: a plan that
has not been decomposed yet has no graph. It becomes one the moment anything asks
for parallel batches or file ownership — which is what `--require` is for.

Write the file at the plan directory root, next to `plan.md`. The minimum that
parses AND passes `--require`:

```yaml
edges: []
subtasks:
  P1: {file: phases/phase-1.md, post: [verification-P1.json]}
```

## `malformed` — the file exists and cannot be read

Exit 0 plain, **exit 2 under `--require`** — and cook's preflight hard-stops on
this parser message regardless of the flag, so a malformed sidecar stops a run
just as surely as `invalid` does. `next_action` carries the parser's own message;
read it first, since it distinguishes three failures that look alike from
outside:

* **YAML will not parse** — a tab, an unquoted `:` inside a value, a broken
  indent. The message quotes the parser's line and column.
* **the top level is not a mapping** — the file opens with a list, or a bare
  scalar. Usually a stray leading `- `.
* **`edges:` is not a list / `subtasks:` is not a mapping** — the message names
  which key and what type it found. The common one is writing `subtasks` as a
  list of single-key maps, which is valid YAML and the wrong shape here.

## `invalid` — it parses, and a node has no `post`

Exit **2** in both modes — the only state that stops a run with no flag needed.
`next_action` lists the offending nodes by name.

`post` is the end-of-phase artifact obligation: the file a phase must leave
behind for anything downstream to check it happened. "Missing" covers three
cases, and they do NOT all read the same in the message:

* the node has no `subtasks` entry at all (usually a node created by a typo'd
  edge — check the spelling in `edges` before adding an entry for it);
* the entry has no `post` key — the bare `missing-post:` line, nothing else;
* `post` is present with the wrong shape — `post: verification.json` as a bare
  scalar, or `post: []`. These print a `status-leak:` line FIRST, naming the
  default that already applied, and only then the `missing-post:` line. Two
  lines, one node: read the leak line as "the default is live right now", not
  as a second problem.

Watch the block-vs-inline distinction: `post:` on its own line under a block
mapping reaches `invalid` as above, but `{file: a.md, post:}` inline is a YAML
syntax error and lands in `malformed` instead.

This is an AUTHORING gate. The runtime has a default (`verification-<node>.json`)
so nothing crashes without it — which is exactly why the gate exists: a default
that quietly applies is a decision nobody made.

## Plan-size facts (`plan_facts.py`, via `plan_graph.py --facts`)

The machine reports FACTS about a plan's size, never a split recommendation:
the connectivity rule this graph implements is too weak a signal to act on
alone. `harness/scripts/plan_facts.py`
reports declared files, directories, phases, independent clusters (the
connectivity rule above, after dropping every collector phase), the longest
chain, and a rank against old plans — with a plain description, never advice.

`plan_graph.py --facts <plan_dir> [--root <project_root>]` is the one real
call path — `plan_facts.py` has no standalone CLI of its own on purpose: it
never imports `plan_graph` (that would close a two-file import cycle, since
`plan_graph.py`'s own CLI already calls into `plan_facts`), so it has no
default source for the `plan_graph` functions it needs and cannot stand
alone. `plan_graph.py --facts` supplies them as arguments
(`parse_phase_graph`, `graph_size_facts`, `root_for_plan_dir`) and prints the
exact JSON `plan_facts.describe` returns (no other advisory line mixed in),
from the same CLI cook/hs-run already invoke for cycles/batches/conflicts —
any future reader (a plan-approval step, `hs-run`) reads that one output
shape instead of re-deriving it:

```bash
python3 "${HARNESS_BIN_ROOT:-.}"/harness/scripts/plan_graph.py <plan_dir> --facts [--root <project_root>]
```

A human decides whether to split, at the plan's existing approval step — this
tool has no opinion of its own.

## After a split

The parent, its children, the edges between them and a waived edge: `plan-split.md`, beside this file.
