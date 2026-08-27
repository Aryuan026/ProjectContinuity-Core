# ProjectContinuity

[![CI](https://github.com/Aryuan026/ProjectContinuity-Core/actions/workflows/ci.yml/badge.svg)](https://github.com/Aryuan026/ProjectContinuity-Core/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

Portable, authority-aware project memory for coding agents.

ProjectContinuity lets a new coding session, another device, or another Agent
resume a software project without replaying old chats. It keeps the project's
current handoff separate from reviewed historical explanations, while code,
formal decisions, collaboration, delivery, event streams, and personal memory
remain with their existing owners.

## Why it is different

- **Current and history do not blur.** Turritopsis Stages hold what the project
  currently believes; Cognee Cases hold reviewed historical cause-and-effect.
- **Five tools, not a tool warehouse.** Agents see only `list`, `search`, `get`,
  `update`, and `promote`.
- **Normal work is short.** A routine handoff is one `get` followed by one CAS
  `update(expected_revision)`.
- **No vector requirement for ordinary use.** Stage search and keyword Case
  search work without an embedding model. Semantic search is opt-in.
- **One online writer.** Devices and Agents share an authenticated front instead
  of synchronizing multiple writable databases.
- **Donor-first.** ProjectContinuity reuses mature projects and adds only the
  authority, authentication, ACL, CAS, evidence, promotion, and routing seams.
- **Agent-readable by design.** The repository ships an MCP adapter, a compact
  Skill, a cold-start document, and separate installation/operations manuals.

## Mental model

```text
coding agent / reviewer / assistant
              |
        MCP or HTTP client
              |
   authenticated five-tool front
        /                 \
Turritopsis Store      Cognee archive
current Stages          reviewed Cases
        |                  |
        +------ stable references ------>
          OpenSpec / Graphify / TeamAI / GitHub /
          external events / personal-memory authority
```

ProjectContinuity is the map and handoff desk. It is not the owner of every
place shown on the map.

## Tool and role surface

| Role | Tools |
| --- | --- |
| `reader` | `list`, `search`, `get` |
| `writer` | reader tools + `update` |
| `promoter` | writer tools + `promote` |

The front derives the principal, actor, and project role from a private token.
Clients cannot self-report identity. Every update carries the exact Stage
revision it read; stale writes fail instead of overwriting newer work.

## Quick start

The complete, agent-readable procedure is in [INSTALL.md](INSTALL.md). In short:

```bash
git clone https://github.com/Aryuan026/ProjectContinuity-Core.git
cd ProjectContinuity-Core
uv sync --frozen --extra turritopsis-front --extra cognee-archive --extra codex-mcp
cp config/project-continuity.example.toml /absolute/private/path/config.toml
uv run project-continuity --config /absolute/private/path/config.toml validate
```

Create one owner-only token file per configured principal, then run the front:

```bash
uv run project-continuity-front \
  --config /absolute/private/path/config.toml \
  --credentials-dir /absolute/private/path/credentials \
  --host 127.0.0.1 --port 8766
```

Keep the listener on loopback. Remote clients should use an SSH tunnel or a
separately reviewed authenticated HTTPS gateway; do not expose the donor
services or the front as a raw public management port.

## Everyday Agent path

```text
list(project_id)
  -> get(project.handoff)
  -> work against current code and cited authorities
  -> get(project.handoff) again
  -> update(expected_revision, mode=replace)
```

Use `search(scope="cases", match="keyword")` when the current handoff points to
an old failure or design history. Use `get(promotion_id=...)` when the Case ID is
already known.

## Promotion and optional AI providers

`promote` is a rare archival action, not a richer update. It freezes one
reviewed Stage revision with provenance and a review authority, then writes a
deterministic Case identity through a recoverable `prepared -> committed`
lifecycle.

Creating a new Cognee Case requires a configured LLM and embedding provider.
This requirement does **not** affect normal Stage reads/writes, keyword Case
search, or exact Case reads. If the provider is unavailable, the promotion
remains recoverable and normal project work continues.

## Upstream relationships

ProjectContinuity distinguishes software it executes from systems it only
references or integrates as an authority. “Built with” does not mean “source
copied”: no source file from the projects below is included in
`src/project_continuity`.

### Built with / integrated

| Upstream | Relationship | Code included here |
| --- | --- | --- |
| [Turritopsis](https://github.com/anhe2021212-spec/Turritopsis) | Exact runtime dependency for one Store per project: Stage revisions, handoff, lexical search, changelog, and backup. ProjectContinuity adds the authenticated ACL/CAS boundary and evidence hygiene around it. | 0 upstream source files copied; called through a thin Python adapter. |
| [Cognee](https://github.com/topoteretes/cognee) | Exact runtime dependency for project-scoped, reviewed engineering Cases. ProjectContinuity adds deterministic promotion identity, provenance, recovery receipts, and keyword retrieval without requiring vectors for ordinary reads. | 0 upstream source files copied; called through a thin Python adapter. |
| [TeamAI CLI](https://github.com/Tencent/teamai-cli) | Exact npm consumer lock for reviewed workstream/assignment collaboration. ProjectContinuity verifies the native config, approved repository identity, and explicit-command boundary rather than replacing TeamAI’s Git/PR flow. | 0 upstream source files copied; the npm package is installed from its own distribution. |

The [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) supplies
the standard stdio transport used to expose ProjectContinuity's five tools. It
is an infrastructure dependency, not a project-memory authority.

### Architecture references / authority integrations

| Upstream | Relationship | Code included here |
| --- | --- | --- |
| [OpenSpec](https://github.com/Fission-AI/OpenSpec) | Formal design decisions remain in OpenSpec. ProjectContinuity stores validated stable references to those decisions; it does not copy the decision ledger. | 0 upstream source files copied. |
| [Graphify](https://github.com/safishamsi/graphify) | Exact-commit code reality remains in a clean Graphify artifact. ProjectContinuity validates and queries that artifact while hiding managed filesystem paths and rejecting learning sidecars. | 0 upstream source files copied; the integration invokes an independently installed exact Graphify executable. |

Exact versions, reviewed commits, licenses, notices, and the boundary between
runtime use, adapter code, and architectural reference are recorded in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Documentation

- [AI_START_HERE.md](AI_START_HERE.md) — cold start for an unfamiliar Agent.
- [INSTALL.md](INSTALL.md) — first installation and positive/negative canary.
- [OPERATIONS.md](OPERATIONS.md) — upgrade, backup, restore, and incident handling.
- [CHANGELOG.md](CHANGELOG.md) — public release changes without private runtime data.
- [ARCHITECTURE.md](ARCHITECTURE.md) — ownership and request/data flow.
- [SCHEMA.md](SCHEMA.md) — config, tool, Stage, Case, and receipt contracts.
- [SECURITY.md](SECURITY.md) — threat boundary and vulnerability reporting.
- [AGENTS.md](AGENTS.md) — repository editing rules for coding Agents.

## Release truth

Version `0.1.1` is a source-first alpha. The core front, five-tool MCP path,
role/CAS boundary, current Stage flow, keyword/exact Case retrieval, recoverable
promotion lifecycle, and offline Case relocation are covered by the repository
test suite. Prebuilt `.venv`, `node_modules`, databases, credentials, and
machine-specific service installers are intentionally not distributed. The
canonical installation path is an exact Git checkout because the release-owned
Skill and operations manuals intentionally remain visible beside the code.

## License and credits

Project license and copyright appear in `LICENSE` and `NOTICE`. Third-party
licenses and exact donor provenance appear in `THIRD_PARTY_NOTICES.md`; human
thanks appear separately in `ACKNOWLEDGEMENTS.md`.
