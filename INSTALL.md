# First installation

This procedure is written for a human-supervised Agent. It installs one
loopback front and projects its MCP/Skill into an existing Agent runtime. It
does not expose a public port or create a second writable database.

## 1. Prerequisites

- Python `>=3.10,<3.15`
- Git
- [uv](https://docs.astral.sh/uv/)
- Node.js `>=20` only when using the TeamAI integration
- enough private storage for Turritopsis and, if enabled, Cognee

Clone the public repository and verify the selected tag or commit before using
it as an accepted release.

The first-install inventory is deliberately finite:

- one immutable source checkout and its exact `uv.lock`;
- Python `>=3.10,<3.15`, plus Node.js `>=20` only for TeamAI;
- four separate absolute roots: accepted release, private config/credentials,
  mutable project/archive data, and runtime state/logs;
- one Turritopsis Store per configured project;
- one private token for each principal and the minimum project role it needs;
- one loopback front, one native stdio MCP registration, and one exact Skill
  projection;
- an LLM and embedding provider only if new Cognee promotions or semantic
  search are intentionally enabled;
- a positive and negative canary before declaring the front canonical.

## 2. Install the exact runtime

```bash
uv sync --frozen --no-dev \
  --extra turritopsis-front \
  --extra cognee-archive \
  --extra mcp-client
```

Do not install donor packages globally. `uv.lock` is the runtime identity for
this source release. A source-first checkout does not include `.venv`,
`node_modules`, provider credentials, or mutable data.

## 3. Create private operator paths

Choose absolute paths outside the release for config, credentials, data, and
state. In the TOML, `install_root` names the accepted source/release room;
`data_root` and `state_root` must be separate writable rooms. Create private
operator paths, copy the example config, then replace project IDs, repository
URLs, principals, actors, roles, and roots.

```bash
install -d -m 700 /absolute/private/config/credentials
install -d -m 700 /absolute/private/data /absolute/private/state
install -m 600 config/project-continuity.example.toml \
  /absolute/private/config/config.toml
```

Create exactly one token file per configured principal without printing it:

```bash
umask 077
openssl rand -hex 32 > /absolute/private/config/credentials/agent-reader.token
```

Token filenames must exactly match principal IDs. Never paste token values into
TOML, MCP arguments, logs, issues, prompts, or Git.

## 4. Validate and start the front

```bash
uv run project-continuity \
  --config /absolute/private/config/config.toml validate

uv run project-continuity-front \
  --config /absolute/private/config/config.toml \
  --credentials-dir /absolute/private/config/credentials \
  --host 127.0.0.1 --port 8766
```

`GET /health` reporting `front_ready` proves only the transport is alive. It
does not prove every donor, dataset, project, or provider is ready.

## 5. Initialize one project Store

Use the pinned Turritopsis runtime to create the initial skeleton in a private
temporary directory, then place that whole donor-owned directory at the one
path ProjectContinuity derives for the configured project:

```text
<data_root>/projects/<project_id>/turritopsis/stages.json
```

For a minimal new-project canary:

```bash
BOOTSTRAP_ROOT="$(mktemp -d)"
uv run turritopsis init "$BOOTSTRAP_ROOT" --yes \
  --name "Project Alpha" --description "First continuity canary"

STORE_PARENT="/absolute/private/data/projects/project-alpha"
test ! -e "$STORE_PARENT/turritopsis"
install -d -m 700 "$STORE_PARENT"
mv "$BOOTSTRAP_ROOT/.turritopsis" "$STORE_PARENT/turritopsis"
chmod -R go-rwx "$STORE_PARENT/turritopsis"
rmdir "$BOOTSTRAP_ROOT"

STORE_PATH="$STORE_PARENT/turritopsis/stages.json"
uv run turritopsis add-current project "Project continuity" --data "$STORE_PATH"
uv run turritopsis add project project.handoff \
  "Current work and handoff" --data "$STORE_PATH"
```

The donor commands create an empty `project.handoff`; they do not invent its
content. Read its initial revision, then fill it through the normal authenticated
revision-protected `update` path. For an existing repository, use Turritopsis's upstream
`turritopsis-onboarding` Skill: perform its local deterministic scan, review
coverage and exclusions, classify the smallest durable Stage suite, apply the
skeleton once, then move the complete donor directory to the same managed path.
Ensure that suite owns exactly one `project.handoff` Stage; add the empty Stage
with the donor CLI if the reviewed skeleton intentionally omitted it.
Do not invent a second Project Map, copy one Store between project IDs, or
combine projects into a giant `stages.json`.

Then use an authenticated reader to run:

```text
list(project-alpha)
get(project-alpha, stage_id=project.handoff)
```

## 6. Install the MCP adapter

For a client that connects to an already-running canonical front, do not run
the full self-hosting steps above. Its exact accepted checkout needs only:

```bash
uv sync --frozen --no-dev --extra mcp-client
```

This produces `project-continuity-mcp` without installing the donor runtimes,
initializing local data, or starting `project-continuity-front`. The old
`codex-mcp` extra remains a deprecated 0.1.x compatibility alias only.

Point the Agent runtime at the accepted release's executable. For Codex:

```toml
[mcp_servers.project-continuity]
command = "/absolute/release/.venv/bin/project-continuity-mcp"
args = [
  "--endpoint", "http://127.0.0.1:8766/v1/invoke",
  "--token-file", "/absolute/private/config/credentials/agent-reader.token"
]
cwd = "/absolute/release"
startup_timeout_sec = 10
tool_timeout_sec = 120
```

For another Agent runtime, use its native stdio MCP configuration with the same
command and arguments. Keep the Agent runtime's outer tool deadline strictly
longer than the MCP adapter's 90-second front timeout. This lets the canonical
front return its bounded 60-second archive timeout together with
`operation_state=in_progress`; an outer timeout must not erase that recovery
state. Do not wrap the five tools in a second tool server.

## 7. Install the Skill

Use a single exact symlink so the release remains the only Skill writer:

```text
$HOME/.agents/skills/project-continuity
  -> /absolute/release/skills/project-continuity
```

For another Agent runtime, project that same directory into its supported Skill
root. Refuse to overwrite an existing unmanaged path. Confirm `SKILL.md` and
`agents/openai.yaml` come from the accepted release.

## 8. Acceptance canary

Use a small non-sensitive project first.

Positive path:

1. A fresh Agent session discovers the Skill and five MCP tools.
2. It calls `list`, reads `project.handoff`, and finds one historical Case by
   keyword or exact ID when available.
3. A writer rereads the handoff and performs one CAS update.
4. Restart the front and repeat Stage/Case reads.

Negative path:

1. A reader's `update` is rejected and the Stage revision stays unchanged.
2. An update with a stale revision is rejected.
3. Semantic Case search without embeddings returns `capability_unavailable`;
   keyword search still works.
4. No credential, raw chat, or private path appears in responses or Git.

Only after both paths pass should this runtime become a project's canonical
front. Keep the preceding writer stopped and retain an immutable cold snapshot
until restore and rollback have been observed.

## 9. Installation record

Record only non-secret recovery facts: accepted source commit/tag, lock digests,
Python/Node versions, absolute managed path policy, Skill/MCP projection digest,
principal IDs and roles (never token values), canary receipts, and rollback
steps. Keep mutable Store/Case data and runtime logs outside Git. A future Agent
should be able to reconstruct the component from this record without learning
any credential.
