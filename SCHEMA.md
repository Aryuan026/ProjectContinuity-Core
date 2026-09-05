# Contracts and schemas

## Operator config

The TOML config has one schema version, three absolute roots, projects, and
principals. Each project ID and principal ID is opaque and bounded. Repository
URLs are credential-free HTTPS URLs.

```toml
schema_version = 1

[paths]
install_root = "/absolute/path/install"
data_root = "/absolute/path/data"
state_root = "/absolute/path/state"

[[projects]]
id = "project-alpha"
repo_url = "https://github.com/example-org/project-alpha"

[[principals]]
id = "agent-reader"
actor = "review-agent-b"

[principals.roles]
project-alpha = "reader"
```

The private credential directory contains one `<principal_id>.token` file for
every configured principal. The server derives identity from the matched token.

## HTTP request

```json
{
  "tool": "get",
  "project_id": "project-alpha",
  "arguments": {"stage_id": "project.handoff"}
}
```

Unknown top-level keys are rejected. Tool arguments are also closed per tool.

## Tools

- `list`: optional `current`.
- `search`: required `query`; optional `scope`, `match`, `current`, `stage_id`,
  `context`, `limit`, `case_sensitive`, `selector`. The default `auto` scope
  reports partitioned current/history/code/decisions/collaboration/delivery
  results and explicit per-layer coverage. Focused scopes remain available.
- `get`: exactly one of `stage_id`, `promotion_id`, or `resource_ref`. A
  `resource_ref` is a complete StableRef previously returned by `list` or
  `search`; the front routes it back to its owning authority.
- `update`: `stage_id`, `body`, `expected_revision`; optional `mode`.
- `promote`: exact source revision, stable idempotency key, provenance,
  review authority, and optional `corrects`/`supersedes` Case IDs.

## StableRef

A StableRef contains trimmed strings for `authority`, `object_id`, `version`,
`producer`; a lowercase SHA-256 digest; immutable unique-key provenance; and an
optional bounded projection. Secrets and unbounded payloads are rejected before
promotion.

## Stages and Cases

Stage revisions are exact 16-character lowercase hex identities produced by
Turritopsis. A Case identity is `promotion:<64 lowercase hex>`. The Case content
records the source project, Stage, revision, review/provenance references, and
the frozen engineering narrative. A Case does not become ready until Cognee's
cognify pipeline reports completion and exact readback succeeds.

## Receipts

Promotion receipts use a SQLite recovery ledger with `prepared` and `committed`
states. The prepared row temporarily holds the frozen Case payload. After commit,
the payload is cleared while identity and outcome remain auditable.
