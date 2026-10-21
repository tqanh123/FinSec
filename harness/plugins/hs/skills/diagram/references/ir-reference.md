# hs:diagram IR field and enum reference

Every document starts with the shared envelope:

```json
{
  "schema_version": 1,
  "diagram_type": "architecture | workflow | sequence | dataflow | lifecycle",
  "meta": {
    "title": "System Overview",
    "subtitle": "optional",
    "description": "optional, for a11y",
    "visual_preset": "classic | blueprint",
    "theme": "light | dark"
  }
}
```

`meta.title` is the only required `meta` field. IDs everywhere match `^[a-zA-Z0-9_-]{1,64}$` and
must be unique across the whole document (schemas/common.schema.json `$defs.id`).

## 1. Architecture (`architecture`)

```json
{
  "components": [{ "id": "gw", "label": "API Gateway", "role": "gateway", "layer": 0 }],
  "boundaries": [{ "id": "vpc", "label": "Production VPC", "role": "vpc", "components": ["gw"] }],
  "connections": [{ "from": "gw", "to": "auth", "label": "Validate Token", "kind": "sync" }]
}
```

- `components[]` (1-250): `role` in `frontend | backend | database | cache | queue | storage |
  gateway | auth | external | worker`. `layer`: integer 0-10, picks the layout column; omitted
  nodes take the longest path from their predecessors.
- `boundaries[]`: `role` in `cloud | vpc | cluster | trust-boundary | private-network |
  external-zone`; `components` lists member component ids (rendered as a dashed frame).
- `connections[]` (<=1000): `kind` in `sync | async | stream | fallback | bi-directional`.

## 2. Workflow (`workflow`)

```json
{
  "lanes": [{ "id": "ci", "label": "CI System", "role": "system" }],
  "steps": [{ "id": "build", "label": "Build", "lane": "ci", "kind": "action" }],
  "transitions": [{ "from": "build", "to": "review", "label": "Ready", "kind": "normal" }]
}
```

- `lanes[]`: `role` in `user | frontend | orchestrator | worker | approver | system`.
- `steps[]` (1-250): `kind` in `start | action | decision | wait | subprocess |
  terminal-success | terminal-failure`; `lane` must name a declared lane id.
- `transitions[]` (<=1000): `kind` in `normal | branch-true | branch-false | retry | exception`.

## 3. Sequence (`sequence`)

```json
{
  "participants": [{ "id": "client", "label": "Web Client", "role": "client" }],
  "messages": [{ "from": "client", "to": "api", "label": "POST /login", "kind": "sync-call" }]
}
```

- `participants[]` (2-50): `role` in `client | service | database | gateway | external | queue`.
- `messages[]` (<=500): `label` required; `kind` in `sync-call | async-signal | return |
  self-call | error`. Rows render top to bottom in authored order, one row per message.

## 4. Dataflow (`dataflow`)

```json
{
  "stages": [{ "id": "ingest", "label": "Ingestion", "order": 0 }],
  "nodes": [{ "id": "events", "label": "Clickstream", "role": "source", "stage": "ingest" }],
  "flows": [{ "from": "events", "to": "etl", "label": "Kafka Stream" }]
}
```

- `stages[]`: `order` (integer) picks the layout column; nodes without a `stage` fall back to
  the graph's derived rank.
- `nodes[]` (1-250): `role` in `source | transform | store | sink | consumer | filter |
  governance`; `classification` in `public | internal | confidential | pii | restricted`.
- `flows[]` (<=1000).

## 5. Lifecycle (`lifecycle`)

```json
{
  "states": [{ "id": "created", "label": "Created", "kind": "initial" }],
  "transitions": [{ "from": "created", "to": "processing", "event": "Submit" }]
}
```

- `states[]` (1-250): `kind` in `initial | active | waiting | failure-recoverable |
  failure-fatal | terminal-success | terminal-cancelled`. An `initial` state opens the graph.
- `transitions[]` (<=1000): `kind` in `normal | retry | timeout | cancel | fail`.

## What the compiler derives (never authored)

| Archetype | Column (rank) | Frames |
|---|---|---|
| `architecture` | `layer`, else longest path from sources | `boundaries` |
| `dataflow` | `stage.order`, else derived rank | `stages` |
| `workflow` | longest path from `start` steps | `lanes` |
| `lifecycle` | longest path from `initial` states | none |
| `sequence` | authored participant order (fixed, not ranked) | none |

## Validation errors

`python3 scripts/diagram_ir.py <file> --json` returns `{"valid": bool, "errors": [{"path",
"message", "hint"}]}`; every error is reported at once (never just the first). Common hints:
"use one of: ..." for a bad enum, "ids match ^[a-zA-Z0-9_-]{1,64}$" for a bad id, "point it at
an id declared earlier in this document" for a dangling `from`/`to`.
