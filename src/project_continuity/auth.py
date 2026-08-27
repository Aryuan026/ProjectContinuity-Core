"""Static principal-to-actor and project-role authorization."""

from dataclasses import dataclass
from typing import Optional, Tuple

from .config import Config, ConfigError


READ_TOOLS = frozenset({"list", "search", "get"})
ROLE_TOOLS = {
    "reader": READ_TOOLS,
    "writer": READ_TOOLS | {"update"},
    "promoter": READ_TOOLS | {"update", "promote"},
}
TOOLS = frozenset().union(*ROLE_TOOLS.values())


class AuthorizationError(PermissionError):
    """Authentication or the frozen role matrix rejected the request."""


@dataclass(frozen=True)
class AuthContext:
    principal_id: str
    actor: str
    roles: Tuple[Tuple[str, str], ...]

    def role_for(self, project_id: str) -> Optional[str]:
        return dict(self.roles).get(project_id)


def authenticate(
    config: Config, principal_id: str, claimed_actor: Optional[str] = None
) -> AuthContext:
    """Resolve actor and roles from operator config, never from caller claims."""

    if claimed_actor is not None:
        raise AuthorizationError("actor is derived from the authenticated principal")
    try:
        principal = config.principal(principal_id)
    except ConfigError as exc:
        raise AuthorizationError(str(exc)) from exc
    return AuthContext(
        principal_id=principal.principal_id,
        actor=principal.actor,
        roles=principal.roles,
    )


def authorize(context: AuthContext, project_id: str, tool: str) -> None:
    """Require one of the five frozen tools under the configured static role."""

    if tool not in TOOLS:
        raise AuthorizationError("unknown tool: %s" % tool)
    role = context.role_for(project_id)
    if role is None:
        raise AuthorizationError("principal has no role for project: %s" % project_id)
    if tool not in ROLE_TOOLS[role]:
        raise AuthorizationError("role %s cannot use %s" % (role, tool))
