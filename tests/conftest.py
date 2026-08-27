from pathlib import Path

import pytest

from project_continuity.config import load_config


def write_config(base: Path, *, text_suffix: str = "") -> Path:
    install_root = base / "install"
    data_root = base / "data"
    state_root = base / "state"
    path = base / "config.toml"
    base.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """schema_version = 1

[paths]
install_root = "{install_root}"
data_root = "{data_root}"
state_root = "{state_root}"

[[projects]]
id = "alpha"
repo_url = "https://github.com/example/alpha"

[[projects]]
id = "beta"
repo_url = "https://github.com/example/beta"

[[principals]]
id = "reader-client"
actor = "reader-agent"
[principals.roles]
alpha = "reader"

[[principals]]
id = "writer-client"
actor = "writer-agent"
[principals.roles]
alpha = "writer"
beta = "reader"

[[principals]]
id = "promoter-client"
actor = "promoter-agent"
[principals.roles]
alpha = "promoter"
{text_suffix}
""".format(
            install_root=install_root,
            data_root=data_root,
            state_root=state_root,
            text_suffix=text_suffix,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return write_config(tmp_path / "runtime")


@pytest.fixture
def config(config_path: Path):
    return load_config(config_path)
