---
name: project-continuity
description: Resume, hand off, or investigate a software project through ProjectContinuity's current Stages, reviewed engineering Cases, and stable references to code, decisions, collaboration, and delivery evidence. Use when a coding Agent or MCP-capable client joins a project without prior chat context, needs the current handoff or a cross-layer fault chain, must update durable project cognition after work, or explicitly promote a reviewed exact revision. Also use for cross-device or cross-agent coding continuity without replaying conversations.
---

# Project Continuity

Use the ProjectContinuity MCP as a routing and handoff layer. Keep live code, formal decisions, collaboration, delivery evidence, room events, and identity/life memory with their existing authorities.

## Arrive by the shortest path

1. Identify the operator-approved `project_id`; do not invent one from a directory name.
2. Call `list`, inspect its per-layer coverage, then `get(stage_id="project.handoff")`.
3. Treat the handoff as routing context. For a cross-layer question, call `search(scope="auto")`, inspect coverage, and resolve relevant code, decision, collaboration, or delivery StableRefs with `get(resource_ref=...)` before relying on them.
4. Use `search(scope="stages")` only for a current-only unresolved question, then `get` the owning Stage.
5. For history, prefer exact `get(promotion_id="promotion:...")` when the handoff gives an identity. Otherwise use `search(scope="cases", match="keyword")`; this path needs no vector provider.
6. Use Case `match="semantic"` only when semantic retrieval materially helps. If it returns `capability_unavailable`, continue with keyword search and exact get rather than retrying.

## Keep the project current

After durable project work, update the Stage that owns the changed fact. For ordinary handoff maintenance:

1. `get` the Stage and preserve its complete responsibility.
2. Edit the smallest complete body while retaining evidence, unknowns, next gate, and authority boundaries.
3. Call `update` with the exact returned revision and `mode="replace"`.
4. If CAS reports a conflict, reread and reconcile; never blind-write or silently overwrite another Agent.

A role-authorized routine handoff update is agent-owned. Do not ask for repeated confirmation when the user's task already includes completing and handing off the work.

## Promote rarely

Use `promote` only when all of these already exist:

- one exact Stage revision containing a coherent engineering Case;
- complete stable-ref provenance;
- an explicit review-authority stable ref;
- a stable idempotency key.

Promotion is not a richer `update`. It archives reviewed history and never replaces the current handoff. Reuse the same request and idempotency key after an interrupted promotion.

## Respect authority

- Turritopsis Stage: current project cognition and handoff.
- Project Cognee Case: reviewed historical explanation with provenance.
- OpenSpec: formal design decisions.
- Graphify: exact-SHA code reality.
- TeamAI/Git: workstream, assignment, and reviewed collaboration.
- GitHub: commit, PR, release, and delivery fact.
- External event system: room, meeting, delivery, and ACK fact.
- Personal memory system: identity, life, relationship, and temporal continuity.

If these boundaries or the five-tool request shapes are unclear, read the repo-owned `AI_START_HERE.md`. For first installation, read `INSTALL.md`; for upgrades, backup, restore, or incidents, read `OPERATIONS.md`. For first-time Stage design or repository scanning, use the pinned Turritopsis onboarding Skill instead of recreating its Project Map, search, revision, or backup behavior here.

Never put credentials, raw chat, runtime logs, or unredacted secret-shaped evidence into a Stage, Case, tool argument, or Git.
