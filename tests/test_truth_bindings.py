import json
from pathlib import Path

import pytest

from project_continuity.truth_bindings import (
    TruthBindingError,
    load_truth_bindings,
    truth_bindings_path,
)


def _write(config, value) -> Path:
    path = truth_bindings_path(config)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_missing_bindings_are_an_explicit_empty_projection(config) -> None:
    bindings = load_truth_bindings(config)

    assert bindings.project_ids() == ()
    assert bindings.project("alpha").openspec is None
    assert bindings.project("alpha").teamai is None


def test_exact_donor_bindings_are_project_scoped(config) -> None:
    _write(
        config,
        {
            "schema_version": 1,
            "projects": {
                "alpha": {
                    "openspec": {
                        "store_id": "alpha-specs",
                        "repo_url": "https://github.com/example/alpha-specs",
                    },
                    "teamai": {
                        "team_id": "alpha-team",
                        "repo_url": "https://github.com/example/alpha-team",
                        "reviewers": ["reviewer-agent"],
                    },
                }
            },
        },
    )

    bindings = load_truth_bindings(config)

    assert bindings.project_ids() == ("alpha",)
    assert bindings.project("alpha").openspec.store_id == "alpha-specs"
    assert bindings.project("alpha").teamai.reviewers == ("reviewer-agent",)
    assert bindings.project("beta").openspec is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["projects"].update(gamma={"openspec": {}}),
        lambda value: value["projects"]["alpha"]["openspec"].update(
            repo_url="https://token@github.com/example/alpha"
        ),
        lambda value: value["projects"]["alpha"]["teamai"].update(
            reviewers=["same", "same"]
        ),
        lambda value: value.update(surprise=True),
    ],
)
def test_malformed_or_foreign_bindings_fail_closed(config, mutate) -> None:
    value = {
        "schema_version": 1,
        "projects": {
            "alpha": {
                "openspec": {
                    "store_id": "alpha-specs",
                    "repo_url": "https://github.com/example/alpha-specs",
                },
                "teamai": {
                    "team_id": "alpha-team",
                    "repo_url": "https://github.com/example/alpha-team",
                    "reviewers": [],
                },
            }
        },
    }
    mutate(value)
    _write(config, value)

    with pytest.raises(TruthBindingError):
        load_truth_bindings(config)


def test_binding_symlink_and_public_mode_fail_closed(config, tmp_path: Path) -> None:
    value = {"schema_version": 1, "projects": {}}
    path = _write(config, value)
    path.chmod(0o644)
    with pytest.raises(TruthBindingError, match="owner-private"):
        load_truth_bindings(config)

    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(value), encoding="utf-8")
    path.symlink_to(outside)
    with pytest.raises(TruthBindingError, match="symlink"):
        load_truth_bindings(config)
