from pathlib import Path

import pytest

from project_continuity.config import ConfigError, load_config

from conftest import write_config


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_loads_strict_positive_config(config_path: Path) -> None:
    config = load_config(config_path)
    assert [project.project_id for project in config.projects] == ["alpha", "beta"]
    assert config.principal("writer-client").role_for("beta") == "reader"
    assert config.paths.install_root.is_absolute()


@pytest.mark.parametrize(
    "needle,replacement,expected",
    [
        ("schema_version = 1", "schema_version = 1\nsurprise = true", "unknown"),
        ("id = \"beta\"", "id = \"alpha\"", "duplicate project"),
        ("id = \"writer-client\"", "id = \"reader-client\"", "duplicate principal"),
        ("alpha = \"writer\"", "alpha = \"owner\"", "invalid role"),
        ("alpha = \"reader\"", "missing = \"reader\"", "unknown project"),
        (
            "https://github.com/example/alpha",
            "https://token@github.com/example/alpha",
            "must not contain credentials",
        ),
    ],
)
def test_rejects_invalid_identity_and_unknown_keys(
    config_path: Path, needle: str, replacement: str, expected: str
) -> None:
    text = config_path.read_text(encoding="utf-8").replace(needle, replacement, 1)
    config_path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=expected):
        load_config(config_path)


def test_rejects_relative_and_overlapping_runtime_roots(tmp_path: Path) -> None:
    relative = write_config(tmp_path / "relative")
    text = relative.read_text(encoding="utf-8")
    relative.write_text(
        text.replace(str(tmp_path / "relative" / "install"), "relative/install"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="absolute"):
        load_config(relative)

    nested = write_config(tmp_path / "nested")
    text = nested.read_text(encoding="utf-8")
    nested.write_text(
        text.replace(
            str(tmp_path / "nested" / "data"),
            str(tmp_path / "nested" / "install" / "data"),
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="must not contain"):
        load_config(nested)


def test_rejects_plaintext_secret_extension(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8").replace(
        "schema_version = 1", 'schema_version = 1\napi_key = "do-not-store"'
    )
    config_path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config keys: api_key"):
        load_config(config_path)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com:bad/example/repo",
        "https://[::1/example/repo",
        "https://github.com\\n/example/repo",
        "https://github.com\\\\example/repo",
        "https://github.com",
    ],
)
def test_rejects_malformed_https_repository_urls(config_path: Path, url: str) -> None:
    text = config_path.read_text(encoding="utf-8").replace(
        "https://github.com/example/alpha", url, 1
    )
    config_path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="repository URL"):
        load_config(config_path)


def test_notices_freeze_exact_donor_coordinates_and_relationships() -> None:
    plan = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for coordinate in (
        "6abfc69f454a2b84762cb84a6efcd9dc82f25d88",
        "f1b521dffac38ed6638689cd28b0c204b1eef0f1",
        "b2cd36267456c166788c95be6e68574064a92a42",
        "a8f9760bb6da90a9956b3be77c0d0534134f533a",
        "fd94c75f362260abb81ddd02296f14dc22350e73",
    ):
        assert coordinate in plan
    assert "Built with / integrated" in plan
    assert "Architecture references / authority integrations" in plan
    assert plan.count("Copied source: none") == 6


def test_ai_entry_keeps_authority_map_and_five_tool_route_thin() -> None:
    entry = (REPO_ROOT / "AI_START_HERE.md").read_text(encoding="utf-8")
    for owner in (
        "Turritopsis",
        "Cognee",
        "OpenSpec",
        "Graphify",
        "TeamAI",
        "GitHub",
        "External event system",
        "Personal memory system",
    ):
        assert owner in entry
    assert "`list / search / get / update / promote`" in entry
    assert "get(project.handoff) -> update(expected_revision" in entry
