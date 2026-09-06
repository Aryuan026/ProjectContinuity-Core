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
from .truth_setup import TruthSetupError, install_truth_plane, refresh_truth_plane


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
    truth_setup = commands.add_parser(
        "truth-setup",
        help="install one project's managed truth-plane checkouts and bindings",
    )
    truth_setup.add_argument("--declaration", required=True, type=Path)
    truth_refresh = commands.add_parser(
        "truth-refresh",
        help="fast-forward reviewed managed truth-plane Git projections",
    )
    truth_refresh.add_argument("--project-id", required=True)
    truth_refresh.add_argument(
        "--layer",
        action="append",
        choices=("delivery", "openspec", "teamai"),
        dest="layers",
        required=True,
    )
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            config = load_config(args.config)
            result = _config_receipt(config)
        else:
            from .server import load_private_service_config

            config = load_private_service_config(args.config)
            if args.command == "relocate-cognee":
                from .server import bind_cognee_environment

                bind_cognee_environment(config)
                result = asyncio.run(
                    relocate_cognee_case_storage(config, args.previous_data_root)
                )
            elif args.command == "truth-setup":
                result = install_truth_plane(config, args.declaration)
            else:
                result = refresh_truth_plane(config, args.project_id, args.layers)
    except TruthSetupError as exc:
        failure: Dict[str, Any] = {"ok": False, "error": str(exc)}
        if exc.receipt is not None:
            failure["receipt"] = exc.receipt
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 2
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
