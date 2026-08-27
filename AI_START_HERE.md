# AI Start Here

ProjectContinuity is a project handoff and engineering-history service. It is
not a second code repository, decision ledger, chat log, or personal memory.

## Authority map

| Need | Authority |
| --- | --- |
| Current project cognition and handoff | one Turritopsis Store per project |
| Reviewed historical engineering Cases | project-scoped Cognee archive |
| Formal design decisions | OpenSpec or the project's decision authority |
| Code reality | Graphify artifact bound to an exact commit |
| Workstream and assignment | TeamAI / reviewed Git collaboration |
| Commit, PR, release, delivery | GitHub or the project's delivery authority |
| Room, meeting, or external event | External event system |
| Identity, life, and relationship continuity | Personal memory system |

Other layers may hold stable references or explicitly rebuildable projections.
They must not silently become a second owner of the same fact.

## Five tools

The tool surface is fixed: `list / search / get / update / promote`.

- `reader`: `list / search / get`
- `writer`: reader tools plus `update`
- `promoter`: writer tools plus `promote`

Identity comes from the private Bearer token. The request cannot supply its own
principal or actor.

```text
POST http://127.0.0.1:8766/v1/invoke
Authorization: Bearer <private token>
Content-Type: application/json

{"tool":"get","project_id":"project-alpha","arguments":{"stage_id":"project.handoff"}}
```

The JSON object permits only `tool`, `project_id`, and `arguments`.

## Cold start

1. Call `list` for the operator-approved project ID.
2. Call `get(stage_id="project.handoff")`.
3. Verify code claims against the cited checkout, Graphify, and delivery evidence.
4. Search current Stages only for a specific unresolved question.
5. Use keyword Case search for history; it needs no vector provider.
6. Use semantic search only when embeddings are intentionally configured.

After durable work, do one normal handoff update:

```text
get(project.handoff) -> update(expected_revision, mode="replace")
```

On a revision conflict, reread and reconcile. Never blind-write.

## Promote rarely

Promotion requires one coherent exact Stage revision, complete stable-ref
provenance, an explicit review-authority reference, and a stable idempotency
key. Retry an interrupted promotion with the same request and key. Promotion
archives reviewed history and never replaces or rewrites the current Stage.

## Installation and repair

- First installation: read `INSTALL.md` completely.
- Runtime maintenance, backup, restore, or upgrade: read `OPERATIONS.md`.
- Data and request contracts: read `SCHEMA.md`.
- Code changes: read `AGENTS.md` and `ARCHITECTURE.md` before editing.
- Upstream provenance or version changes: read `THIRD_PARTY_NOTICES.md` before
  touching a donor coordinate or adapter.

Do not infer that a source checkout is installed, that a running front has a
ready archive provider, or that a visible tool grants writer permission. Verify
the capability state and role with the documented positive/negative canary.

Never put credentials, raw chat, runtime logs, mutable databases, or
unredacted secret-shaped evidence in a Stage, Case, tool argument, or Git.
