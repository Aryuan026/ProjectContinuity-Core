# Architecture

## Three rules

1. **Ownership** — every fact has one authoritative owner.
2. **Reference** — other layers keep stable references or declared projections.
3. **Provenance** — historical explanations point back to review and delivery evidence.

## Current and historical cognition

Each project owns one Turritopsis Store. Its Stages answer “what does the project
currently believe and what happens next?” A project-scoped Cognee dataset holds
reviewed Cases that answer “what happened, why, what was tried, and what evidence
settled it?”

The two are deliberately asymmetric:

```text
Stage exact revision --explicit reviewed promotion--> historical Case
       ^                                                   |
       |                                                   |
       +------------- never written back ------------------+
```

Normal Stage changes do not invoke Cognee. A Cognee outage cannot freeze current
project work.

## Request path

```text
MCP tool / HTTP client
  -> FrontClient (no redirects; private Bearer token)
  -> loopback HTTP server
  -> token-to-principal mapping
  -> project role ACL
  -> CognitionFront
       -> TurritopsisAdapter for Stage list/search/get/update
       -> PromotionCoordinator for reviewed archive writes
       -> NativeCogneeBackend for Case get/search/promotion
```

Archive calls are serialized inside one front process because the direct
Ladybug backend owns asynchronous state that must not cross concurrent event
loops. Stage requests remain concurrent.

## Promotion lifecycle

Promotion creates a deterministic identity from the project, exact Stage
revision, review envelope, and idempotency key.

```text
validate exact Stage and references
  -> record prepared with frozen Case payload
  -> write deterministic Cognee Data identity
  -> read back exact ready Case
  -> record committed receipt
```

If the process stops after `prepared`, the same request reuses the frozen payload
and deterministic identity. It does not reread a newer Stage or create a second
Case. Correction and supersession references must resolve to ready Cases in the
same project dataset.

## External authorities

Graphify, OpenSpec, TeamAI/Git, GitHub, event systems, and personal-memory systems
are not copied into ProjectContinuity. A Case may cite their StableRefs; whether
those external objects are current or authoritative remains their owner's job.

## Storage and deployment

Code/release bytes, current Store data, archive data, runtime state, logs, and
credentials belong in separate roots. Cognee writable paths are derived from
the operator config and must not resolve inside the release. The front should
remain loopback-only unless an independently authenticated gateway is reviewed.
