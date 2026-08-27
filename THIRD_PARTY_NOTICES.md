# Third-party notices and technical lineage

ProjectContinuity is an independently written integration layer. The project
does not fork or vendor the upstream source trees listed below. Its adapters
were written against reviewed upstream behavior, public interfaces, tests, and
licenses; the exact relationship for each upstream is recorded here so that
future maintainers can update without erasing technical lineage.

## Built with / integrated

### Turritopsis

- Upstream: <https://github.com/anhe2021212-spec/Turritopsis>
- Reviewed commit: `fd94c75f362260abb81ddd02296f14dc22350e73`
- Runtime version: `0.2.0`
- Upstream copyright: Copyright (c) 2026 Turritopsis contributors
- License: MIT
- Relationship: exact Git runtime dependency. ProjectContinuity calls the
  native Store API for Stage CRUD, revision CAS, lexical search, changelog, and
  backup behavior.
- Copied source: none.
- Local notice copy: `third_party/licenses/TURRITOPSIS-LICENSE`

### Cognee

- Upstream: <https://github.com/topoteretes/cognee>
- Reviewed commit: `a8f9760bb6da90a9956b3be77c0d0534134f533a`
- Runtime version: `1.5.2`
- Upstream notice: Copyright © 2024 Topoteretes UG
- License: Apache License 2.0
- Relationship: exact Git runtime dependency. ProjectContinuity uses Cognee's
  project-scoped dataset and data APIs for reviewed engineering Cases, then
  adds deterministic identity, provenance, promotion receipts, recovery, and a
  keyword-only read path.
- Copied source: none.
- Local notice copies: `third_party/licenses/COGNEE-LICENSE` and
  `third_party/licenses/COGNEE-NOTICE.md`

### TeamAI CLI

- Upstream: <https://github.com/Tencent/teamai-cli>
- Reviewed commit: `6abfc69f454a2b84762cb84a6efcd9dc82f25d88`
- Runtime package: `teamai-cli@0.20.0`
- Upstream copyright: Copyright (C) 2026 Tencent
- License: MIT
- Relationship: exact npm consumer dependency with package integrity and
  selected transitive security overrides frozen in
  `vendor/teamai-runtime/package-lock.json`. TeamAI keeps ownership of its
  native Git/worktree/PR/contribution/recall workflow.
- Copied source: none; `node_modules` is not distributed.
- Local notice copy: `third_party/licenses/TEAMAI-CLI-LICENSE`

### MCP Python SDK

- Upstream: <https://github.com/modelcontextprotocol/python-sdk>
- Runtime version: `1.29.1`
- Upstream copyright: Copyright (c) 2024 Anthropic, PBC
- License: MIT
- Relationship: standard stdio MCP transport for the five-tool adapter.
- Copied source: none.
- Local notice copy: `third_party/licenses/MCP-PYTHON-SDK-LICENSE`

## Architecture references / authority integrations

### OpenSpec

- Upstream: <https://github.com/Fission-AI/OpenSpec>
- Reviewed commit: `f1b521dffac38ed6638689cd28b0c204b1eef0f1`
- Reviewed version: `1.10.0`
- Upstream copyright: Copyright (c) 2024 OpenSpec Contributors
- License: MIT
- Relationship: formal decision authority. ProjectContinuity validates and
  stores stable references; OpenSpec is not a Python or npm runtime dependency.
- Copied source: none.
- Local notice copy: `third_party/licenses/OPENSPEC-LICENSE`

### Graphify

- Upstream: <https://github.com/safishamsi/graphify>
- Reviewed commit: `b2cd36267456c166788c95be6e68574064a92a42`
- Reviewed version: `0.9.48`
- Upstream notice: Copyright 2026 Safi Shamsi and the Graphify contributors
- License: Apache License 2.0; upstream NOTICE also records earlier MIT-licensed
  contributions.
- Relationship: code-reality authority. The optional router invokes an
  independently installed exact executable and validates immutable artifacts;
  Graphify is not installed by this package.
- Copied source: none.
- Local notice copies: `third_party/licenses/GRAPHIFY-LICENSE`,
  `third_party/licenses/GRAPHIFY-NOTICE`, and
  `third_party/licenses/GRAPHIFY-LICENSE-MIT`

## Maintenance rule

An upstream version change is not a blind dependency bump. Read the candidate
source, tests, release notes, and license; compare every ProjectContinuity
adapter assumption; rerun relevant upstream tests and this repository's full
suite; then record the new exact coordinate and relationship here. See
`OPERATIONS.md` for the release and rollback sequence.
