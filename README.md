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
          /          |                 \
Turritopsis Store  Cognee archive   authority-routed plane
 current Stages    reviewed Cases   Graphify / OpenSpec /
                                      TeamAI / GitHub
          \          |                 /
           +---- exact StableRefs ----+
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
Clients cannot self-report identity. Every update carries the exact revision of
the owning authority that it read; stale writes fail instead of overwriting
newer work. The same `update` tool preserves ordinary Stage CAS and can route an
explicit write to Graphify, OpenSpec, or TeamAI. GitHub delivery remains
read-only through ProjectContinuity.

Roles describe work on one project, not a hierarchy between Agent products.
An Agent or device that performs durable project work should normally receive
that project's `writer` role and update the shared handoff itself. A genuinely
review-only participant uses `reader`; rare reviewed-history archival remains a
separate `promoter` capability. The human operator owns these assignments.

## Quick start

The complete, agent-readable procedure is in [INSTALL.md](INSTALL.md). In short:

```bash
git clone https://github.com/Aryuan026/ProjectContinuity-Core.git
cd ProjectContinuity-Core
uv sync --frozen --extra turritopsis-front --extra cognee-archive \
  --extra graphify-code --extra mcp-client
npm ci --ignore-scripts --prefix vendor/openspec-runtime
npm ci --ignore-scripts --prefix vendor/teamai-runtime
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

To connect a new coding Agent to an existing canonical front, install only the
client-neutral stdio adapter:

```bash
uv sync --frozen --no-dev --extra mcp-client
```

This client-only path does not install the Turritopsis/Cognee runtime, create a
local Store or Case archive, or start another front. `codex-mcp` remains a
deprecated compatibility alias for the public 0.1.0-0.1.2 commands; it does not
mean the protocol or product is Codex-specific.

## Everyday Agent path

```text
list(project_id)
  -> get(project.handoff)
  -> work against current code and cited authorities
  -> get(project.handoff) again
  -> update(expected_revision, mode=replace)
```

Use `search(scope="auto")` when the question crosses current state, history,
code, decisions, collaboration, and delivery; its coverage reports which owners
were actually consulted. Resolve returned StableRefs with
`get(resource_ref=...)`. Use `search(scope="cases", match="keyword")` for a
focused historical lookup and `get(promotion_id=...)` when the Case ID is known.

When durable work changes an external authority, use that authority's exact
revision and its bounded operation through the same `update` tool:

| Target | Operations | Result |
| --- | --- | --- |
| `current` | Stage `replace`/supported donor mode | Updates the selected Stage with Turritopsis CAS. |
| `code` | `register_committed`, `register_overlay` | Registers a reviewed Graphify artifact; committed builds use exact Git blob bytes. |
| `decisions` | `prepare_change`, `archive_change` | Creates a review branch through the native OpenSpec lifecycle. |
| `collaboration` | `contribute` | Creates a TeamAI/Git contribution using the authenticated actor. |
| `delivery` | none | Returns a typed read-only refusal; GitHub remains the delivery authority. |

Repository cloning, binding installation, and fast-forward refresh are operator
lifecycle actions (`truth-setup` / `truth-refresh`), not extra MCP tools.

## Promotion and optional AI providers

`promote` is a rare archival action, not a richer update. It freezes one
reviewed Stage revision with provenance and a review authority, then writes a
deterministic Case identity through a recoverable `prepared -> committed`
lifecycle.

The default `keyword` archive mode creates a reviewed Case through Cognee's
native add pipeline without an LLM or embedding provider. The Case records its
archive mode and becomes ready only after the matching donor pipeline reports
completion. Existing semantic Cases keep their original cognify-ready rule.

Semantic Case search and the explicit `semantic` archive mode remain opt-in.
An unconfigured semantic search returns typed `capability_unavailable`; it does
not guess, silently fall back, or treat a keyword hit as semantic approval.
Normal Stage work and provider-free promotion continue independently.

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
| [Graphify](https://github.com/safishamsi/graphify) | Exact-commit code reality remains in a clean Graphify artifact. ProjectContinuity validates and queries that artifact while hiding managed filesystem paths and rejecting learning sidecars. | 0 upstream source files copied; the exact executable is installed by the optional `graphify-code` extra. |

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

Version `0.1.3` is a source-first alpha. The core front, five-tool MCP path,
role/CAS boundary, current Stage flow, keyword/exact Case retrieval, recoverable
promotion lifecycle, authority-routed read/write plane, managed truth setup and
refresh, and offline Case relocation are covered by the repository test suite.
Prebuilt `.venv`, `node_modules`, databases, credentials, and machine-specific
service installers are intentionally not distributed.

An exact Git checkout plus `uv.lock` and the two npm lock directories is the
canonical Linux self-hosting input. A different host OS must separately prove
its pinned Cognee/Ladybug native runtime. The wheel is also a real
client/library arrival artifact: it installs the Python runtime and three
console entry points, and places the same Skill, manuals, lock files, config
example, and third-party
licenses under `share/project-continuity`. Installing a wheel does not run npm,
create a Store, mint credentials, or start a front. See `INSTALL.md` for the
separate clean-wheel smoke and full cold-start procedure.

## License and credits

Project license and copyright appear in `LICENSE` and `NOTICE`. Third-party
licenses and exact donor provenance appear in `THIRD_PARTY_NOTICES.md`; human
thanks appear separately in `ACKNOWLEDGEMENTS.md`.
