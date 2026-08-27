"""Static project, role, and Stage-scope checks for the cognition front."""

from dataclasses import dataclass
import re
from typing import Optional

from .auth import AuthContext, AuthorizationError, authenticate, authorize
from .config import Config, ConfigError


_STAGE_ID = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,160}$")


class StageAccessError(AuthorizationError):
    """A Stage identifier is invalid or outside the selected project Store."""


@dataclass(frozen=True)
class StaticACL:
    """Apply the frozen reader/writer/promoter matrix without a policy language."""

    config: Config

    def grant(
        self,
        principal_id: str,
        project_id: str,
        tool: str,
        *,
        claimed_actor: Optional[str] = None,
        stage_id: Optional[str] = None,
    ) -> AuthContext:
        try:
            self.config.project(project_id)
        except ConfigError as exc:
            raise AuthorizationError(str(exc)) from exc
        context = authenticate(
            self.config, principal_id, claimed_actor=claimed_actor
        )
        authorize(context, project_id, tool)
        if stage_id is not None:
            self.validate_stage_id(stage_id)
        return context

    @staticmethod
    def validate_stage_id(stage_id: str) -> str:
        if (
            not isinstance(stage_id, str)
            or stage_id != stage_id.strip()
            or not _STAGE_ID.fullmatch(stage_id)
        ):
            raise StageAccessError("stage_id must be a bounded opaque identifier")
        return stage_id

    @staticmethod
    def unavailable_stage(project_id: str) -> StageAccessError:
        return StageAccessError(
            "stage is not available in project: %s" % project_id
        )
