# Changelog

All notable public changes are recorded here. Private deployment receipts,
mutable project memory, and raw construction logs do not belong in this file.

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
