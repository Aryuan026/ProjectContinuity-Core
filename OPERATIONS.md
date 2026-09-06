# Operations and maintenance

## Capability truth

Report these states separately:

```text
source available -> installed -> configured -> front running
-> MCP/Skill discovered -> authenticated read observed
-> authorized write observed -> restart readback -> restore observed
```

Do not call an installation usable because `/health` responds.

## Routine maintenance

- Keep one online canonical front and one online writer for each project.
- Back up Store, Cognee, receipt, and config metadata as separate classes.
- Never synchronize two live database directories in both directions.
- Keep credentials outside data snapshots and back them up through the owner's
  secret system, not inside this repository.
- After durable engineering work, update the owning Stage with one CAS replace.
- When the durable fact belongs to code, decisions, or collaboration, use the
  same authenticated `update` tool with the owning target, its supported
  operation, and the exact authority revision. GitHub delivery remains
  read-only through ProjectContinuity.
- Treat `backend_timeout` plus `operation_state=in_progress` as an unfinished
  authority operation, not a failed write. Preserve its `operation_id`, keep
  health and Stage reads available, and replay the exact request after the
  retained worker reaches terminal state.
- TeamAI contributions freeze the authenticated actor, request, and source
  revision in a private prepared receipt. The donor runs from an exact-source
  checkout and may stop after its native branch push; ProjectContinuity then
  creates the PR through its existing GitHub authority. Durable branch and PR
  transitions let a restarted front resume without creating a second
  contribution. The donor title is an internal full-request-digest marker used
  only to bind its learning filename; the controller supplies the human PR
  title. A committed receipt is accepted only after GitHub and Git agree on the
  single PR head, parent, request marker, content, author, and committer. The
  donor supervisor inherits the existing command lock, so a front crash cannot
  release a still-running donor for a second attempt; branch-bound PR recovery
  queries GitHub by the receipt's exact same-repository head rather than by a
  recent-PR window.
- Promote only a coherent reviewed revision; reuse the same idempotency key after
  any interrupted attempt.

## Upgrade a donor or ProjectContinuity

1. Read the donor's exact source, tests, release notes, license, and current
   ProjectContinuity adapter before changing a version.
2. Update the exact dependency coordinate and lock in an isolated checkout.
3. Run the donor's relevant upstream suite and the full ProjectContinuity suite.
4. Build a new immutable release; never patch the active release in place.
5. Stop the front, take a cold snapshot and content manifest, then start the new
   release against the same approved data roots.
6. Verify health, current Stage, keyword Case, exact Case, one authorized CAS,
   restart readback, and reader-role rejection.
7. Keep the prior release until rollback has been exercised.

For every upstream change, update `THIRD_PARTY_NOTICES.md` with the exact
coordinate, license, and relationship. Preserve the distinction between:

- runtime dependencies: Turritopsis, Cognee, and the optional TeamAI CLI lock;
- transport dependency: MCP Python SDK;
- authority integrations: OpenSpec and Graphify.

Do not replace an upstream component from memory. Read its current source,
tests, docs, and license first; list the behavior and edge cases the adapter
keeps, changes, or intentionally drops; then prove those choices in tests.

## Backup

The minimum recoverable set is:

- one Turritopsis Store per project, including changelog and backups;
- Cognee data and system roots;
- the promotion receipt database;
- operator config without credential values;
- exact source commit/tag and dependency locks;
- a separately managed credential recovery path.

Backups are offline recovery material, not a second writable ProjectContinuity.
Record file counts and digests without storing private contents in Git.

## Refresh managed truth projections

After an authority repository merges a reviewed change, stop the front and
fast-forward only the selected managed projections:

```bash
uv run project-continuity \
  --config /absolute/private/config/config.toml \
  truth-refresh --project-id project-alpha \
  --layer delivery --layer openspec --layer teamai
```

The operation fetches and preflights every selected layer, pins all exact target
commits, writes a durable receipt, and only then advances the first checkout. If
it returns a partial receipt, repair the reported cause and replay the exact same
project and layer set; do not change the layer set or chase a newer remote. Keep
the front stopped until the receipt is complete, all checkouts read back exact,
and the restarted front reports the expected layer identities.

## Restore or move the canonical front

1. Stop the source front and prove the port is closed.
2. Freeze one final cold snapshot and manifest.
3. Restore into empty target roots and verify bytes before mutation.
4. With the target front still stopped, run `project-continuity relocate-cognee`
   if Case file paths changed. The relocation shares the front lifetime lock and
   must complete before the graph engine opens for normal traffic.
5. Start exactly one target front.
6. Read current Stage and historical Cases from a remote client, perform one
   normal CAS, restart, and read again.
7. Leave the former data only as a dormant cold snapshot; do not resume its writer.

## Failure interpretation

- SSH tunnel or client timeout: the corridor may be down while the canonical
  front and data remain healthy. Check server health before touching data.
- `forbidden`: inspect principal-to-role mapping; do not bypass ACL by reporting
  an actor in the request.
- revision conflict: reread and reconcile; never force the old body.
- `capability_unavailable` for semantic search: use keyword/exact retrieval or
  configure embeddings through a separate provider gate.
- promotion left `prepared`: repair the provider/backend and replay the exact
  request and idempotency key. Do not invent a new promotion.
- archive backend error: Stage operations should remain available; investigate
  Cognee without moving current cognition into a second store.
- truth-plane refresh partial: keep the front stopped and replay the same
  project/layer set so the receipt-bound target pins converge; do not edit
  checkout HEADs by hand.

## Rollback

Stop only ProjectContinuity, return to the preceding immutable release, preserve
all data/receipt roots, and repeat health/Stage/Case/ACL readback. Remove an MCP
table or Skill symlink only when it is still the exact managed projection. Never
restore an entire shared Agent config file over unrelated changes.

## Maintainer closeout checklist

Before handing the runtime to another human or Agent:

1. Update the current handoff with one concise CAS replace; remove facts already
   superseded by later checkpoints instead of appending a construction diary.
2. Point historical explanations to exact reviewed Cases, commits, decisions,
   and delivery receipts; do not paste raw transcripts or logs.
3. Record capability truth as separate states: source, installed, configured,
   discovered, authenticated read, authorized write, restart readback, restore.
4. Run the full tests, compileall, `git diff --check`, Skill validation, and a
   secret/private-path scan.
5. Confirm one canonical writer, a restorable cold snapshot, and an exercised
   rollback before retiring the preceding release.
6. Leave credentials and mutable databases out of Git, issues, Stage bodies,
   Cases, and model prompts.
