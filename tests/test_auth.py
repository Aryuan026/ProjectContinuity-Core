import pytest

from project_continuity.auth import AuthorizationError, authenticate, authorize


def test_actor_is_derived_and_caller_claim_is_rejected(config) -> None:
    context = authenticate(config, "writer-client")
    assert context.actor == "writer-agent"
    with pytest.raises(AuthorizationError, match="derived"):
        authenticate(config, "writer-client", claimed_actor="someone-else")


@pytest.mark.parametrize(
    "principal,allowed,denied",
    [
        ("reader-client", {"list", "search", "get"}, {"update", "promote"}),
        ("writer-client", {"list", "search", "get", "update"}, {"promote"}),
        (
            "promoter-client",
            {"list", "search", "get", "update", "promote"},
            set(),
        ),
    ],
)
def test_static_three_role_five_tool_matrix(config, principal, allowed, denied) -> None:
    context = authenticate(config, principal)
    for tool in allowed:
        authorize(context, "alpha", tool)
    for tool in denied:
        with pytest.raises(AuthorizationError):
            authorize(context, "alpha", tool)


def test_unknown_principal_project_and_tool_fail_closed(config) -> None:
    with pytest.raises(AuthorizationError, match="unknown principal"):
        authenticate(config, "ghost")
    context = authenticate(config, "reader-client")
    with pytest.raises(AuthorizationError, match="no role"):
        authorize(context, "beta", "get")
    with pytest.raises(AuthorizationError, match="unknown tool"):
        authorize(context, "alpha", "delete")
