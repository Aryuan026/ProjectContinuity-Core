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
- `update` has two closed forms:
  - current Stage: `stage_id`, `body`, `expected_revision`; optional `mode` and
    `target="current"`;
  - external authority: `target`, `operation`, `parameters`, and
    `expected_revision`. Supported pairs are `code/register_committed`,
    `code/register_overlay`, `decisions/prepare_change`,
    `decisions/archive_change`, and `collaboration/contribute`. `delivery`
    returns a typed read-only refusal.

Authority operation parameters are closed:

| Target / operation | Parameters |
| --- | --- |
| `code/register_committed` | `{"commit_sha":"<40 lowercase hex>"}` |
| `code/register_overlay` | `base_sha`, `evidence_time`, `overlay_digest`, `snapshot_id` |
| `decisions/prepare_change` | `change_id` and 1–12 `artifacts`, each containing only `artifact_id`, `relative_output`, and `body` |
| `decisions/archive_change` | `{"change_id":"<bounded change id>"}` |
| `collaboration/contribute` | `{"title":"<bounded title>","body":"<reviewed body>"}` |

The request never accepts a repository path, credential, principal, or actor.
Graph selection revisions come from the selected Graph StableRef (or `absent`);
OpenSpec and TeamAI revisions are the exact managed checkout HEAD reported by
their read/status result.
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
the frozen engineering narrative. New provider-free Cases carry
`project_continuity_archive_mode=keyword` and become ready only when Cognee's
add pipeline reports completion and exact readback succeeds. Explicit semantic
Cases carry `semantic` and require cognify completion. Legacy rows without a
mode retain the cognify-ready rule, so an old partial add is never silently
reclassified as ready.
The front defaults `PROJECT_CONTINUITY_CASE_ARCHIVE_MODE` to `keyword`; only
the closed values `keyword` and `semantic` are accepted.

## Receipts

Promotion receipts use a SQLite recovery ledger with `prepared` and `committed`
states. The prepared row temporarily holds the frozen Case payload. After commit,
the payload is cleared while identity and outcome remain auditable.

Truth-plane refresh receipts are operator-owned JSON checkpoints under the
private state root. They freeze each selected layer's before/target/after state
and a controller-owned Git ref before the first checkout advances. A partial
refresh is replayed with the exact same project and layer set until it converges;
it never follows a newer remote target during recovery.

TeamAI contribution receipts are owner-private JSON records under
`state_root/authority/teamai/<project_id>/`. `prepared` freezes the authenticated
principal-derived actor, exact source revision, and canonical authority request
digest before donor side effects. `branch_published` freezes the donor's exact
branch and head; `pr_created` freezes the GitHub PR identity; `committed` adds
the review state only after exact Git/GitHub readback. The donor-generated
learning path begins with a 50-character base-36 encoding of the receipt's
complete SHA-256 request digest. Exact replay returns the same
`authority:<digest>` operation identity, resumes the first unfinished
transition, and verifies committed work through the immutable PR head even if
the source branch has since been deleted. While the donor process is live, an
inherited command-lock descriptor keeps the operation exclusively owned across
front-process death. Once `branch_published` exists, PR reconciliation is scoped
to the receipt-bound same-repository branch rather than a time-ordered page.
