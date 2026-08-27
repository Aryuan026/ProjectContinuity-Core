from pathlib import Path


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
