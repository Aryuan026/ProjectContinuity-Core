# First installation

This procedure is written for a human-supervised Agent. It installs one
loopback front and projects its MCP/Skill into an existing Agent runtime. It
does not expose a public port or create a second writable database.

## 1. Prerequisites

- Python `>=3.10,<3.15`
- Git
- [uv](https://docs.astral.sh/uv/)
- Node.js `24.20.0` (Active LTS) for the full self-hosted OpenSpec/TeamAI truth plane
- enough private storage for Turritopsis and, if enabled, Cognee

The accepted full-front physical baseline is Linux. A macOS or other host may
be used only after its pinned Cognee/Ladybug C API is independently present and
read back; a successful client-wheel install does not prove that native graph
runtime. macOS remains a supported MCP client platform.

Clone the public repository and verify the selected tag or commit before using
it as an accepted release.

The first-install inventory is deliberately finite:

- one immutable source checkout and its exact `uv.lock`;
- Python `>=3.10,<3.15`, plus Node.js `24.20.0` (Active LTS) for the full
  OpenSpec/TeamAI truth plane;
- four separate absolute roots: accepted release, private config/credentials,
  mutable project/archive data, and runtime state/logs;
- one Turritopsis Store per configured project;
- one private token for each principal and the minimum project role it needs;
- one loopback front, one native stdio MCP registration, and one exact Skill
  projection;
- an LLM and embedding provider only if semantic Case search or the explicit
  semantic archive mode is intentionally enabled;
- a positive and negative canary before declaring the front canonical.

## 2. Install the exact runtime

```bash
uv sync --frozen --no-dev \
  --extra turritopsis-front \
  --extra cognee-archive \
  --extra graphify-code \
  --extra mcp-client

npm ci --ignore-scripts --prefix vendor/openspec-runtime
npm ci --ignore-scripts --prefix vendor/teamai-runtime
```

Do not install donor packages globally. `uv.lock` is the runtime identity for
the Python and Graphify runtime. The two package locks independently bind
OpenSpec `1.10.0` and TeamAI CLI `0.20.0`; `--ignore-scripts` prevents package
install hooks from becoming an unreviewed execution path. A source-first
checkout does not include `.venv`, `node_modules`, provider credentials, or
mutable data.

Before creating operator state, read back the installed identities from this
exact checkout:

```bash
node --version
uv run --frozen --no-sync python -c \
  'import project_continuity; print(project_continuity.__version__)'
uv run --frozen --no-sync graphify --version
node -p "require('./vendor/openspec-runtime/node_modules/@fission-ai/openspec/package.json').version"
node -p "require('./vendor/teamai-runtime/node_modules/teamai-cli/package.json').version"
```

The expected values are respectively Node.js `v24.20.0`, ProjectContinuity
`0.1.3`, Graphify `0.9.48`, OpenSpec `1.10.0`, and TeamAI CLI `0.20.0`. A
mismatch is an install failure; do not silently substitute a global executable.

### Clean wheel arrival smoke

The wheel is a client/library arrival artifact, not the canonical full-host
layout. Verify it in a new environment before publishing it:

```bash
uv build
WHEEL_SMOKE_ROOT="$(mktemp -d)"
uv venv "$WHEEL_SMOKE_ROOT/.venv" --python 3.12
uv pip install --python "$WHEEL_SMOKE_ROOT/.venv/bin/python" \
  "project-continuity[mcp-client] @ file:///absolute/release/dist/project_continuity-0.1.3-py3-none-any.whl"
"$WHEEL_SMOKE_ROOT/.venv/bin/python" -c \
  'import project_continuity; print(project_continuity.__version__)'
"$WHEEL_SMOKE_ROOT/.venv/bin/project-continuity-mcp" --help
test -f "$WHEEL_SMOKE_ROOT/.venv/share/project-continuity/skills/project-continuity/SKILL.md"
test -f "$WHEEL_SMOKE_ROOT/.venv/share/project-continuity/uv.lock"
```

The smoke must also inspect the wheel and source archive with
`scripts/verify_distribution.py`. It proves package identity, console-entry
metadata, and arrival files. It does not prove the npm runtimes, a configured
Store, credentials, or a running front. Those belong to the exact-checkout
cold start above and the acceptance canary below.

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

Leave `PROJECT_CONTINUITY_CASE_ARCHIVE_MODE` unset for the provider-free
`keyword` default. A separate reviewed semantic-provider gate may set it to
`semantic` together with the explicit embedding configuration. Any other value
is rejected; never turn a keyword hit into an implicit semantic approval.

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

### Initialize optional authority projections

With the front stopped, place an owner-only absolute JSON declaration outside
the release. Use `null` for an authority the project does not use:

```json
{
  "schema_version": 1,
  "project_id": "project-alpha",
  "openspec": {
    "store_id": "project-alpha-specs",
    "repo_url": "https://github.com/example-org/project-alpha-specs"
  },
  "teamai": {
    "team_id": "project-alpha-team",
    "repo_url": "https://github.com/example-org/project-alpha-team",
    "reviewers": ["review-agent-b"]
  }
}
```

Install the delivery checkout and the selected authority checkouts/bindings:

```bash
chmod 600 /absolute/private/config/project-alpha-truth.json
uv run project-continuity \
  --config /absolute/private/config/config.toml \
  truth-setup \
  --declaration /absolute/private/config/project-alpha-truth.json
```

The command is replay-safe for an identical declaration, never starts the
front, and refuses changed bindings or dirty/unsafe checkouts. It is the
operator lifecycle seam for repository installation; it does not add an MCP
tool. After starting the front, `list` must report the configured layers and
their actual availability before any authority write is attempted.

## 6. Install the MCP adapter

For a client that connects to an already-running canonical front, do not run
the full self-hosting steps above. Its exact accepted checkout needs only:

```bash
uv sync --frozen --no-dev --extra mcp-client
```

This produces `project-continuity-mcp` without installing the donor runtimes,
initializing local data, or starting `project-continuity-front`. The old
`codex-mcp` extra remains a deprecated 0.1.x compatibility alias only.
The clean-wheel command in section 2 is the equivalent client-only install
when the accepted input is a built wheel instead of a source checkout.

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
front return its bounded 60-second archive or authority-write timeout together
with `operation_state=in_progress` and, for writes, a stable `operation_id`;
an outer timeout must not erase that recovery state. Long Graphify, OpenSpec,
and TeamAI work remains owned by one retained server worker after that response.
Replay the exact request rather than inventing a replacement operation. Do not
wrap the five tools in a second tool server.

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

The public CI repeats the core arrival path on the accepted Linux baseline
with fresh exact-source dependencies, a fresh wheel MCP client, and the
packaged Skill. It performs one real provider-free keyword promotion, exact
search/get, same-key replay across a front restart, and a typed semantic HOLD.
This isolated synthetic receipt does not replace an operator's project-specific
ACL, backup, rollback, or authority-write canaries.

Positive path:

1. A fresh Agent session discovers the Skill and five MCP tools.
2. It calls `list`, reads `project.handoff`, and finds one historical Case by
   keyword or exact ID when available.
3. A writer rereads the handoff and performs one CAS update.
4. Restart the front and repeat Stage/Case reads.
5. When authority writes are enabled, exercise one bounded write against a
   disposable reviewed repository, read the returned branch or StableRef from
   its owning authority, and confirm the delivery layer still refuses writes.

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
