import pytest

from project_continuity.evidence import REDACTED, StableRef, is_excluded_path, sanitize_evidence


def test_scan_and_maintain_share_identical_bounded_sanitization() -> None:
    fixture = {
        "title": "正常中文证据",
        "authorization": "Bearer live-token",
        "nested": {
            "url": "https://example.test/item?token=abc&view=1",
            "notes": "password=hunter2 keep this explanation",
            "items": list(range(8)),
        },
    }

    def scan_preview(value):
        return sanitize_evidence(value, max_items=3, max_string=80)

    def maintain_preview(value):
        return sanitize_evidence(value, max_items=3, max_string=80)

    scan = scan_preview(fixture)
    maintain = maintain_preview(fixture)
    assert scan == maintain
    assert scan["authorization"] == REDACTED
    assert "abc" not in str(scan)
    assert "hunter2" not in str(scan)
    assert scan["nested"]["items"][-1] == "[5 items truncated]"


def test_preview_bounds_depth_strings_and_mapping_size() -> None:
    value = {
        "one": "x" * 30,
        "two": {"deeper": {"hidden": True}},
        "three": 3,
    }
    result = sanitize_evidence(value, max_depth=1, max_items=2, max_string=10)
    assert result["one"].endswith("...[truncated]")
    assert result["two"]["deeper"] == "[MAX_DEPTH]"
    assert result["__truncated_items__"] == 1


def test_shared_exclude_policy_covers_runtime_and_secret_files() -> None:
    for path in (
        ".git/config",
        "node_modules/pkg/index.js",
        ".venv/bin/python",
        "service/.env.production",
        "keys/client.pem",
        "graphify-out/graph.json",
    ):
        assert is_excluded_path(path)
    assert not is_excluded_path("src/project_continuity/config.py")


def test_stable_ref_keeps_authority_version_digest_and_provenance() -> None:
    reference = StableRef(
        authority="github",
        object_id="commit:abc123",
        version="abc123",
        digest="sha256:" + "d" * 64,
        producer="github",
        provenance=(("repo", "example/project"),),
        projection="current-delivery",
    )
    assert reference.as_dict() == {
        "authority": "github",
        "object_id": "commit:abc123",
        "version": "abc123",
        "digest": "sha256:" + "d" * 64,
        "producer": "github",
        "provenance": {"repo": "example/project"},
        "projection": "current-delivery",
    }


@pytest.mark.parametrize(
    "raw,leaked",
    [
        ("Authorization: Basic dXNlcjpwYXNz", ("dXNlcjpwYXNz",)),
        (
            "Proxy-Authorization: Digest username=agent, response=deadbeef",
            ("username=agent", "deadbeef"),
        ),
        ("password: correct horse battery staple", ("correct", "horse", "staple")),
        ('{"authorization": "Basic dXNlcjpwYXNz"}', ("dXNlcjpwYXNz",)),
        ("https://reader:swordfish@example.test/repo", ("reader", "swordfish")),
        ("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", ("AKIAIOSFODNN7EXAMPLE",)),
        ("OPENAI_API_KEY=sk-live-example", ("sk-live-example",)),
        ("GITHUB_TOKEN=ghp_example", ("ghp_example",)),
        ("DATABASE_PASSWORD=correct-horse", ("correct-horse",)),
        ("AWS_SECRET_ACCESS_KEY=wJalr-example", ("wJalr-example",)),
        ('{"OPENAI_API_KEY": "sk-json-example"}', ("sk-json-example",)),
        (
            '{"DATABASE_URL":"postgres://alice:swordfish@db.internal/app"}',
            ("alice", "swordfish"),
        ),
        (
            "FOO=bar OPENAI_API_KEY=sk-live-example",
            ("sk-live-example",),
        ),
        ("mode=dev GITHUB_TOKEN=ghp_example", ("ghp_example",)),
        (
            "export FOO=bar DATABASE_PASSWORD=correct-horse",
            ("correct-horse",),
        ),
        (
            "FOO=bar OPENAI_API_KEY=sk-live-example GITHUB_TOKEN=ghp_example",
            ("sk-live-example", "ghp_example"),
        ),
        (
            "FOO= OPENAI_API_KEY=sk-live-example",
            ("sk-live-example",),
        ),
        ("mode:\tGITHUB_TOKEN=ghp_example", ("ghp_example",)),
        (
            "EMPTY= DATABASE_PASSWORD=correct horse battery staple",
            ("correct", "horse", "staple"),
        ),
        (
            "FOO= OPENAI_API_KEY=sk-live-example GITHUB_TOKEN=ghp_example",
            ("sk-live-example", "ghp_example"),
        ),
    ],
)
def test_string_credentials_are_fully_redacted(raw, leaked) -> None:
    result = sanitize_evidence(raw, max_string=100)
    assert REDACTED in result
    for fragment in leaked:
        assert fragment not in result
    assert len(result) <= 114


@pytest.mark.parametrize(
    "field_name",
    ("authority", "object_id", "version", "producer"),
)
def test_stable_ref_rejects_edge_whitespace_in_identity_fields(field_name) -> None:
    fields = {
        "authority": "github",
        "object_id": "commit:abc123",
        "version": "abc123",
        "digest": "sha256:" + "a" * 64,
        "producer": "github",
    }
    fields[field_name] += " "

    with pytest.raises(ValueError, match=field_name):
        StableRef(**fields)


def test_stable_ref_rejects_duplicate_mutable_and_malformed_provenance() -> None:
    fields = {
        "authority": "github",
        "object_id": "commit:abc123",
        "version": "abc123",
        "digest": "sha256:" + "a" * 64,
        "producer": "github",
    }
    with pytest.raises(ValueError, match="duplicate provenance key"):
        StableRef(**fields, provenance=(("repo", "first"), ("repo", "second")))
    with pytest.raises(ValueError, match="tuple"):
        StableRef(**fields, provenance=[["repo", "mutable"]])
    with pytest.raises(ValueError, match="two-string tuple"):
        StableRef(**fields, provenance=(("repo",),))
    with pytest.raises(ValueError, match="two-string tuple"):
        StableRef(**fields, provenance=(("repo", 123),))
    with pytest.raises(ValueError, match="whitespace"):
        StableRef(**fields, provenance=((" repo", "example/project"),))
    with pytest.raises(ValueError, match="projection"):
        StableRef(**fields, projection=123)
    with pytest.raises(ValueError, match="projection"):
        StableRef(**fields, projection="x" * 501)


def test_stable_ref_round_trip_is_lossless_and_deterministic() -> None:
    reference = StableRef(
        authority="graphify",
        object_id="snapshot:one",
        version="abc123",
        digest="sha256:" + "b" * 64,
        producer="graphify",
        provenance=(("repo", "example/project"), ("commit", "abc123")),
        projection="current-canonical",
    )
    assert reference.provenance == (
        ("commit", "abc123"),
        ("repo", "example/project"),
    )
    serialized = reference.as_dict()
    assert StableRef.from_dict(serialized) == reference
    serialized["provenance"]["repo"] = "mutated copy"
    assert reference.provenance[-1] == ("repo", "example/project")
