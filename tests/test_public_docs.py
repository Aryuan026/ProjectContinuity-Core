from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_agent_entry_routes_first_install_and_maintenance() -> None:
    entry = (ROOT / "AI_START_HERE.md").read_text(encoding="utf-8")
    assert "First installation: read `INSTALL.md` completely" in entry
    assert "Runtime maintenance, backup, restore, or upgrade" in entry
    assert "read `THIRD_PARTY_NOTICES.md`" in entry


def test_first_install_names_every_required_runtime_piece() -> None:
    install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    for required in (
        "immutable source checkout",
        "one Turritopsis Store per configured project",
        "one private token for each principal",
        "one loopback front",
        "positive and negative canary",
        "<data_root>/projects/<project_id>/turritopsis/stages.json",
        "turritopsis add project project.handoff",
    ):
        assert required in install


def test_maintenance_keeps_upstream_relationships_explicit() -> None:
    operations = (ROOT / "OPERATIONS.md").read_text(encoding="utf-8")
    assert "runtime dependencies: Turritopsis, Cognee" in operations
    assert "authority integrations: OpenSpec and Graphify" in operations
    assert "one canonical writer" in operations


def test_project_and_cognee_license_notices_remain_separate() -> None:
    project_license = (ROOT / "LICENSE").read_text(encoding="utf-8")
    project_notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    cognee_license = (
        ROOT / "third_party" / "licenses" / "COGNEE-LICENSE"
    ).read_text(encoding="utf-8")

    assert "Apache License\n                           Version 2.0" in project_license
    assert "Copyright [yyyy] [name of copyright owner]" in project_license
    assert "Topoteretes UG" not in project_license
    assert (
        "Copyright 2026 Aryuan026 and ProjectContinuity contributors"
        in project_notice
    )
    assert "Copyright 2024 Topoteretes UG" in cognee_license


def test_private_operator_paths_are_ignored() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (
        "credentials/",
        ".secrets/",
        "config/*.private.toml",
        "config/*.local.toml",
    ):
        assert pattern in ignored


def test_client_install_is_agent_neutral_with_legacy_alias() -> None:
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = package["project"]["optional-dependencies"]
    assert extras["mcp-client"] == ["mcp==1.29.1"]
    assert extras["codex-mcp"] == extras["mcp-client"]

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    skill = (ROOT / "skills/project-continuity/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "--extra mcp-client" in readme
    assert "--extra mcp-client" in install
    assert "coding Agent or MCP-capable client" in skill
    assert "Use when Codex joins" not in skill
    assert 'search(scope="auto")' in skill
    assert "get(resource_ref=...)" in skill
    assert "not a hierarchy between Agent products" in readme
    assert "no client product is the permission authority" in (
        ROOT / "AI_START_HERE.md"
    ).read_text(encoding="utf-8")


def test_agent_tool_timeout_outlives_mcp_and_front_deadlines() -> None:
    install = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    assert "tool_timeout_sec = 120" in install
    assert "MCP adapter's 90-second front timeout" in install
    assert "bounded 60-second archive timeout" in install
    assert "operation_state=in_progress" in install


def test_literal_teamai_wrapper_is_release_owned_and_in_the_source_artifact() -> None:
    wrapper = ROOT / "vendor/teamai-runtime/project-continuity-literal-recall.mjs"
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    runtime_readme = (ROOT / "vendor/teamai-runtime/README.md").read_text(
        encoding="utf-8"
    )

    assert wrapper.is_file()
    assert "recursive-include vendor/teamai-runtime *.json *.md *.mjs" in manifest
    assert "native `recall` action" in runtime_readme
