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
