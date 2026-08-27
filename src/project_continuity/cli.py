"""Small operator CLI for validation and offline Case relocation."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any, Dict, Optional, Sequence

from .cognee_relocation import CogneeRelocationError, relocate_cognee_case_storage
from .config import ConfigError, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="project-continuity")
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate", help="validate strict operator config")
    relocate = commands.add_parser(
        "relocate-cognee",
        help="offline-rebase restored Case files to the configured data root",
    )
    relocate.add_argument("--previous-data-root", required=True, type=Path)
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "validate":
            result = _config_receipt(config)
        else:
            from .server import bind_cognee_environment, load_private_service_config

            config = load_private_service_config(args.config)
            bind_cognee_environment(config)
            result = asyncio.run(
                relocate_cognee_case_storage(config, args.previous_data_root)
            )
    except (ConfigError, CogneeRelocationError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


def main() -> None:
    raise SystemExit(run())


def _config_receipt(config: Any) -> Dict[str, Any]:
    return {
        "action": "validate",
        "ok": True,
        "paths": {
            "install_root": str(config.paths.install_root),
            "data_root": str(config.paths.data_root),
            "state_root": str(config.paths.state_root),
        },
        "projects": [project.project_id for project in config.projects],
        "principals": [principal.principal_id for principal in config.principals],
    }


if __name__ == "__main__":
    main()
