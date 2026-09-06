# Changelog

All notable public changes are recorded here. Private deployment receipts,
mutable project memory, and raw construction logs do not belong in this file.

## Unreleased

- Keep a live TeamAI donor uniquely owned across front-process death by passing
  the existing command lock through a bounded per-invocation supervisor. Resume
  branch-published work through an exact GitHub head query instead of a recent
  100-PR window.
- Bound archive acquisition and execution with one server-owned request
  deadline. A timed-out non-cancellable worker remains the sole backend owner;
  retries receive a typed busy receipt until it finishes, while Stage and health
  routes stay available.
- Preserve the exact safe `operation_state=in_progress` marker through the
  client and MCP error surface. The MCP transport timeout now outlives the
  server archive deadline so this uncertainty reaches the Agent.
- Keep every direct Cognee archive request on one dedicated persistent event
  loop. The previous request path serialized calls but created and closed a new
  loop for each request, violating the documented Ladybug loop-affinity
  boundary.
- No Store, Case, receipt, tool, role, or authentication migration is required.
  To roll back, stop the front and return to the preceding immutable release;
  existing data and configuration remain unchanged.

## 0.1.3 — Client-neutral MCP packaging

- Add `mcp-client` as the generic client-only extra for any MCP-capable coding
  Agent connecting to an existing canonical front.
- Retain `codex-mcp` as a deprecated compatibility alias for the public
  0.1.0-0.1.2 install commands; no existing client is forced to migrate in
  place.
- Generalize the release-owned Skill trigger from Codex to coding Agents and
  MCP-capable clients. Codex, OpenCode, WorkBuddy, Claude Code, and future MCP
  hosts remain adapters around the same five tools, not separate products.
- No tool, role, Stage, Case, identity, promotion, or donor contract changed.

## 0.1.2 — Project and donor license separation

- Restore the root `LICENSE` to the canonical Apache-2.0 text without Cognee's
  project-specific applied copyright notice.
- Retain Cognee's complete upstream license in
  `third_party/licenses/COGNEE-LICENSE` and ProjectContinuity's copyright in
  `NOTICE`.
- Ignore common private operator credential and local-config paths.
- No runtime, data, tool, authority, or donor contract changed.

## 0.1.1 — Full-history secret-scan correction

- Run the official Gitleaks CLI against `git --log-opts=--all` instead of
  relying on the GitHub event range selected by the convenience action.
- Verify the exact Gitleaks v8.30.1 release archive before execution.
- No ProjectContinuity runtime, data, tool, authority, or donor contract changed.

## 0.1.0 — Initial public alpha

- Added the authenticated loopback front and fixed five-tool MCP surface.
- Added per-project Turritopsis Stage routing with role ACL and exact CAS writes.
- Added reviewed Cognee Case promotion with deterministic identity, recoverable
  receipts, keyword retrieval, and optional semantic search.
- Added StableRef, evidence hygiene, Graphify/OpenSpec authority integration,
  and the isolated TeamAI CLI consumer contract.
- Added offline Cognee relocation and single-front lifetime locking.
- Added release-owned Agent Skill, cold-start guide, first-install manual,
  operations/restore manual, licenses, and third-party provenance.
