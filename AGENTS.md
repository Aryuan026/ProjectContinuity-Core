# Agent instructions

ProjectContinuity is an authority and lifecycle boundary, not a generic memory
bucket. Before changing code, read `AI_START_HERE.md`, `ARCHITECTURE.md`, and
`SCHEMA.md`. For install or runtime work, also read `INSTALL.md` or
`OPERATIONS.md` completely.

## Preserve these contracts

- One Turritopsis Store per project; never create a cross-project `stages.json`.
- Identity is derived from private credentials; requests never supply actor or
  principal IDs.
- The public tool surface remains exactly `list/search/get/update/promote`.
- Ordinary work remains `get -> update(expected_revision)`.
- Cases are explicit reviewed promotions, not automatic copies of every Stage.
- Promotion never rewrites the source Stage and never blocks ordinary Stage work.
- Keyword Case search remains available without embeddings.
- Code, formal decisions, collaboration, delivery, events, and personal memory
  keep their own authorities; ProjectContinuity stores stable references.
- Credentials, raw chat, mutable data, logs, and unredacted evidence never enter Git.

## Reuse before rewriting

Read the exact donor implementation, tests, documentation, and license before
replacing any Turritopsis, Cognee, Graphify, OpenSpec, TeamAI, or MCP behavior.
Preserve upstream edge cases unless a documented test proves the replacement.
Do not fork donor source merely to change a prompt or installation preference.

## Editing and verification

Keep changes bounded. Update tests for the user-visible contract rather than
mocking away a failing boundary. Run focused tests first, then the full suite,
compileall, `git diff --check`, Skill validation, and the public forbidden scan.
Changes to locks, donor versions, tool shape, Store/Case schemas, authentication,
or promotion recovery require an explicit migration and rollback note.
