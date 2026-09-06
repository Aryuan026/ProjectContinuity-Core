"""Thin policy and identity seam around donor-owned TeamAI collaboration."""

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .auth import authenticate
from .config import Config, _repository_url as _validated_repository_url


TEAMAI_VERSION = "0.20.0"
TEAMAI_PRODUCER = "teamai-cli@" + TEAMAI_VERSION
TEAMAI_PACKAGE_INTEGRITY = (
    "sha512-aEpaGgWsD/EPqtZrB+4maP7FXqaIy3RXk9w4B3MT7RiF7f88Kvgp7whQFYAr9jvD7m9NoxeGJsn+p2yh66s4EA=="
)
TEAMAI_LOCK_DIGEST = (
    "sha256:5e2c5af06f7025dbe68f2191427ddfe99213f51ed098ae9399b48dacc49dddf7"
)
SIMPLE_GIT_VERSION = "3.36.0"
JS_YAML_VERSION = "3.15.1"
ROLE_MAP = {
    "reader": "reader",
    "writer": "contributor",
    "promoter": "reviewer",
}
DISABLED_BUILTIN_HOOKS = (
    "Hook dispatch session-start",
    "Hook dispatch stop",
    "Hook dispatch post-tool-use wildcard",
    "Hook dispatch post-tool-use Skill",
    "Hook dispatch post-tool-use TodoWrite",
    "Hook dispatch prompt-submit",
)
IMPLICIT_INPUTS = (
    Path(".teamai/usage.jsonl"),
    Path(".teamai/dashboard/events.jsonl"),
    Path(".teamai/sessions"),
    Path(".teamai/votes"),
)
EXPLICIT_ENVIRONMENT = {
    "TEAMAI_HOOKS_DISABLED": "1",
    "TEAMAI_RECALL_DISABLED": "1",
}
TEAMAI_SELF_MODE_GITIGNORE = """\
# teamai single-repo mode — machine-local state (never commit)
config.yaml
state.json
token
.update-lock
.reports-lock
.bootstrap-lock
env.sh
env.local
usage.jsonl
known-skills.json
search-index.json
dashboard/
# git worktrees for reports (orphan branch) and knowledge PRs
reports-wt/
knowledge-wt/
# report data lives on the teamai-reports orphan branch, not on main
members/
sessions/
votes/
stats/
pending-review.jsonl

# Knowledge (skills/, rules/, docs/, learnings/) is intentionally committed to main.
"""
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PUSHED_BRANCH = re.compile(r"\bBranch ([A-Za-z0-9._/-]+) has been pushed\b")


class TeamAIContractError(ValueError):
    """TeamAI consumer state does not satisfy the frozen integration contract."""


@dataclass(frozen=True)
class TeamAIIdentity:
    """Operator-derived TeamAI identity; clients cannot self-report these fields."""

    principal_id: str
    project_id: str
    actor_id: str
    endpoint_id: str
    username: str
    role: str


@dataclass(frozen=True)
class TeamAIPublishReceipt:
    """Fail-closed interpretation of the exact donor CLI's publish output."""

    state: str
    branch: str
    pull_request: Optional[int]
    pull_request_url: Optional[str]


def resolve_teamai_identity(
    config: Config, principal_id: str, project_id: str
) -> TeamAIIdentity:
    """Map one authenticated principal to TeamAI without a second identity store."""

    config.project(project_id)
    context = authenticate(config, principal_id)
    continuity_role = context.role_for(project_id)
    if continuity_role is None:
        raise TeamAIContractError(
            "principal has no TeamAI mapping for project: %s" % project_id
        )
    return TeamAIIdentity(
        principal_id=context.principal_id,
        project_id=project_id,
        actor_id=context.actor,
        endpoint_id=context.principal_id,
        username=context.actor,
        role=ROLE_MAP[continuity_role],
    )


def render_teamai_guard_documents(
    *, team_id: str, repo_url: str, reviewers: Sequence[str] = ()
) -> Dict[str, str]:
    """Render donor-native JSON-as-YAML files that disable implicit writers."""

    team = _identifier(team_id, "team_id")
    remote = _repository_url(repo_url, "repo_url")
    reviewer_ids = tuple(_identifier(value, "reviewer") for value in reviewers)
    if len(set(reviewer_ids)) != len(reviewer_ids):
        raise TeamAIContractError("reviewers must be unique")
    team_config = {
        "description": "ProjectContinuity reviewed collaboration",
        "mode": "self",
        "provider": "github",
        "repo": remote,
        "reviewers": list(reviewer_ids),
        "sharing": {
            "hooks": {"autoApply": False, "requireTeamScripts": True},
            "mcp": {
                "allowedCommands": [],
                "allowedHosts": [],
                "autoApply": False,
            },
            "recall": {"enabled": False},
        },
        "team": team,
    }
    hooks_config = {
        "builtin": {
            "disabled": list(DISABLED_BUILTIN_HOOKS),
            "overrides": {},
        },
        "hooks": [],
    }
    return {
        ".teamai/.gitignore": TEAMAI_SELF_MODE_GITIGNORE,
        ".teamai/teamai.yaml": _json_document(team_config),
        ".teamai/hooks/hooks.yaml": _json_document(hooks_config),
    }


def verify_teamai_guard_documents(
    repo_root: Path,
    *,
    expected_team_id: str,
    expected_repo_url: str,
    expected_reviewers: Sequence[str],
) -> Dict[str, Any]:
    """Fail closed unless TeamAI matches its approved binding and guard policy."""

    root = Path(repo_root)
    approved_team = _identifier(expected_team_id, "expected_team_id")
    approved_repo = _repository_url(expected_repo_url, "expected_repo_url")
    if isinstance(expected_reviewers, (str, bytes)):
        raise TeamAIContractError("expected_reviewers must be a sequence")
    approved_reviewers = tuple(
        _identifier(value, "expected_reviewer") for value in expected_reviewers
    )
    if len(set(approved_reviewers)) != len(approved_reviewers):
        raise TeamAIContractError("expected_reviewers must be unique")
    team = _load_json(root / ".teamai/teamai.yaml")
    hooks = _load_json(root / ".teamai/hooks/hooks.yaml")
    if _load_bytes(root / ".teamai/.gitignore") != TEAMAI_SELF_MODE_GITIGNORE.encode(
        "utf-8"
    ):
        raise TeamAIContractError("TeamAI self-mode gitignore changed")
    try:
        repo_url = _repository_url(team["repo"], "teamai.repo")
        sharing = team["sharing"]
        hook_policy = sharing["hooks"]
        mcp_policy = sharing["mcp"]
        recall_policy = sharing["recall"]
        disabled = hooks["builtin"]["disabled"]
    except (KeyError, TypeError, ValueError) as exc:
        raise TeamAIContractError("TeamAI guard documents are incomplete") from exc
    expected_team_keys = {
        "description",
        "mode",
        "provider",
        "repo",
        "reviewers",
        "sharing",
        "team",
    }
    if set(team) != expected_team_keys:
        raise TeamAIContractError("TeamAI team config has unknown or missing keys")
    expected_sharing_keys = {"hooks", "mcp", "recall"}
    if set(sharing) != expected_sharing_keys:
        raise TeamAIContractError("TeamAI sharing config has unknown or missing keys")
    if team.get("mode") != "self" or team.get("provider") != "github":
        raise TeamAIContractError("TeamAI must use reviewed Git self mode")
    if team.get("description") != "ProjectContinuity reviewed collaboration":
        raise TeamAIContractError("TeamAI managed description changed")
    if hook_policy != {"autoApply": False, "requireTeamScripts": True}:
        raise TeamAIContractError("TeamAI team hooks must remain explicit and guarded")
    if mcp_policy != {
        "allowedCommands": [],
        "allowedHosts": [],
        "autoApply": False,
    }:
        raise TeamAIContractError("TeamAI MCP auto-apply must remain disabled")
    if recall_policy != {"enabled": False}:
        raise TeamAIContractError("TeamAI automatic recall must remain disabled")
    if set(hooks) != {"builtin", "hooks"} or set(hooks["builtin"]) != {
        "disabled",
        "overrides",
    }:
        raise TeamAIContractError("TeamAI hook config has unknown or missing keys")
    if (
        hooks.get("hooks") != []
        or hooks["builtin"].get("overrides") != {}
        or tuple(disabled) != DISABLED_BUILTIN_HOOKS
    ):
        raise TeamAIContractError("all TeamAI session hooks must remain disabled")
    reviewers = team.get("reviewers")
    if not isinstance(reviewers, list):
        raise TeamAIContractError("TeamAI reviewers must be a list")
    reviewer_ids = tuple(_identifier(value, "reviewer") for value in reviewers)
    if len(set(reviewer_ids)) != len(reviewer_ids):
        raise TeamAIContractError("TeamAI reviewers must be unique")
    team_id = _identifier(team.get("team"), "team")
    if (
        team_id != approved_team
        or repo_url != approved_repo
        or reviewer_ids != approved_reviewers
    ):
        raise TeamAIContractError(
            "TeamAI binding does not match operator-approved configuration"
        )
    return {
        "hook_contribution_disabled": True,
        "hook_learning_disabled": True,
        "mcp_auto_apply_disabled": True,
        "ok": True,
        "pull_report_preflight_required": True,
        "recall_injection_disabled": True,
        "repo_url": repo_url,
        "team": team_id,
    }


def verify_teamai_runtime_lock(runtime_root: Path) -> Dict[str, Any]:
    """Verify the exact isolated npm consumer and its two security overrides."""

    root = Path(runtime_root)
    package = _load_json(root / "package.json")
    lock_path = root / "package-lock.json"
    try:
        lock_bytes = lock_path.read_bytes()
    except OSError as exc:
        raise TeamAIContractError("cannot read TeamAI package lock") from exc
    if "sha256:" + sha256(lock_bytes).hexdigest() != TEAMAI_LOCK_DIGEST:
        raise TeamAIContractError("TeamAI consumer lock digest changed")
    lock = _load_json(lock_path)
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise TeamAIContractError("TeamAI package lock has no packages map")
    expected_overrides = {
        "js-yaml": JS_YAML_VERSION,
        "simple-git": SIMPLE_GIT_VERSION,
    }
    expected_package_keys = {
        "dependencies",
        "description",
        "engines",
        "license",
        "name",
        "overrides",
        "private",
        "version",
    }
    if set(package) != expected_package_keys:
        raise TeamAIContractError("TeamAI consumer package has unknown or missing keys")
    if package.get("private") is not True:
        raise TeamAIContractError("TeamAI consumer must be private")
    if package.get("dependencies") != {"teamai-cli": TEAMAI_VERSION}:
        raise TeamAIContractError("TeamAI consumer dependency is not exact")
    if package.get("overrides") != expected_overrides:
        raise TeamAIContractError("TeamAI consumer security overrides changed")
    expected_engines = {"node": "24.20.0"}
    if package.get("engines") != expected_engines:
        raise TeamAIContractError("TeamAI consumer Node requirement changed")
    if lock.get("lockfileVersion") != 3:
        raise TeamAIContractError("TeamAI package lock must use lockfileVersion 3")
    root_entry = packages.get("")
    if not isinstance(root_entry, dict) or root_entry.get("dependencies") != {
        "teamai-cli": TEAMAI_VERSION
    }:
        raise TeamAIContractError("TeamAI lock root does not match package.json")
    if root_entry.get("engines") != expected_engines:
        raise TeamAIContractError("TeamAI lock root Node requirement changed")
    versions = {
        "teamai-cli": _locked_version(packages, "teamai-cli"),
        "simple-git": _locked_version(packages, "simple-git"),
        "js-yaml": _locked_version(packages, "js-yaml"),
    }
    expected = {
        "teamai-cli": TEAMAI_VERSION,
        "simple-git": SIMPLE_GIT_VERSION,
        "js-yaml": JS_YAML_VERSION,
    }
    if versions != expected:
        raise TeamAIContractError("TeamAI lock resolved unexpected versions")
    teamai = packages["node_modules/teamai-cli"]
    if teamai.get("integrity") != TEAMAI_PACKAGE_INTEGRITY:
        raise TeamAIContractError("TeamAI package integrity changed")
    for name in expected:
        _verify_registry_package(packages["node_modules/" + name], name)
    return {"ok": True, "producer": TEAMAI_PRODUCER, "versions": versions}


def assert_no_teamai_implicit_inputs(runtime_home: Path) -> None:
    """Require the explicit-command home to contain no auto-report inputs."""

    home = Path(runtime_home)
    present = []
    for relative in IMPLICIT_INPUTS:
        path = home / relative
        current = home
        has_symlink = False
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                has_symlink = True
                break
        if has_symlink:
            present.append(str(relative))
        elif path.is_dir():
            try:
                if any(path.iterdir()):
                    present.append(str(relative))
            except OSError as exc:
                raise TeamAIContractError("cannot inspect TeamAI implicit input") from exc
        elif path.exists():
            present.append(str(relative))
    if present:
        raise TeamAIContractError(
            "TeamAI implicit write inputs are present: %s" % ", ".join(present)
        )


def teamai_explicit_environment() -> Dict[str, str]:
    """Return a detached environment overlay for explicit-command sessions."""

    return dict(EXPLICIT_ENVIRONMENT)


def teamai_readonly_recall_request(query: str) -> str:
    """Encode one literal donor recall query for the release-owned wrapper."""

    if (
        not isinstance(query, str)
        or not query
        or query != query.strip()
        or len(query) > 500
        or any(ord(character) < 32 or ord(character) == 127 for character in query)
    ):
        raise TeamAIContractError("recall query must be a bounded trimmed string")
    return json.dumps({"query": query}, ensure_ascii=False, separators=(",", ":"))


def classify_teamai_publish(
    exit_code: int, output: str, *, expected_repo_url: str
) -> TeamAIPublishReceipt:
    """Reject contradictory success text unless the exact CLI emitted PR evidence."""

    if type(exit_code) is not int or not isinstance(output, str):
        raise TeamAIContractError("TeamAI publish result has invalid types")
    approved_repo = _repository_url(expected_repo_url, "expected_repo_url").rstrip("/")
    pr_pattern = re.compile(
        r"(?<!\S)" + re.escape(approved_repo) + r"/pull/([1-9][0-9]*)(?=$|\s)"
    )
    branch_match = _PUSHED_BRANCH.search(output)
    branch = branch_match.group(1) if branch_match else ""
    pr_match = pr_pattern.search(output)
    failed = "Failed to create PR:" in output or "create a PR manually" in output
    if exit_code != 0:
        raise TeamAIContractError("TeamAI publish command failed")
    if failed:
        if not _safe_branch(branch):
            raise TeamAIContractError("TeamAI PR failure did not preserve a branch")
        return TeamAIPublishReceipt(
            state="branch_only",
            branch=branch,
            pull_request=None,
            pull_request_url=None,
        )
    if pr_match:
        url = pr_match.group(0)
        return TeamAIPublishReceipt(
            state="pr_opened",
            branch=branch,
            pull_request=int(pr_match.group(1)),
            pull_request_url=url,
        )
    raise TeamAIContractError("TeamAI publish produced no verifiable PR receipt")


def _safe_branch(value: str) -> bool:
    return bool(
        value
        and len(value) <= 240
        and not value.startswith(("/", "-"))
        and not value.endswith(("/", "."))
        and ".." not in value
        and "//" not in value
    )


def _locked_version(packages: Mapping[str, Any], name: str) -> str:
    entry = packages.get("node_modules/" + name)
    if not isinstance(entry, dict) or not isinstance(entry.get("version"), str):
        raise TeamAIContractError("TeamAI lock is missing package: %s" % name)
    return entry["version"]


def _verify_registry_package(entry: Mapping[str, Any], name: str) -> None:
    resolved = entry.get("resolved")
    integrity = entry.get("integrity")
    if (
        not isinstance(resolved, str)
        or not resolved.startswith("https://registry.npmjs.org/")
        or not isinstance(integrity, str)
        or not integrity.startswith("sha512-")
    ):
        raise TeamAIContractError("TeamAI lock has unsafe package metadata: %s" % name)


def _load_json(path: Path) -> Mapping[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise TeamAIContractError("managed JSON document is absent or unsafe: %s" % path)
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TeamAIContractError("cannot read managed JSON document: %s" % path) from exc
    if not isinstance(value, dict):
        raise TeamAIContractError("managed JSON document must be an object: %s" % path)
    return value


def _load_bytes(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise TeamAIContractError(
            "managed byte document is absent or unsafe: %s" % path
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise TeamAIContractError(
            "cannot read managed byte document: %s" % path
        ) from exc


def _reject_duplicate_json_keys(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TeamAIContractError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise TeamAIContractError("%s must be a stable identifier" % field)
    return value


def _json_document(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _repository_url(value: Any, field: str) -> str:
    try:
        return _validated_repository_url(value, field)
    except ValueError as exc:
        raise TeamAIContractError(str(exc)) from exc
