#!/usr/bin/env python3
"""Verify the public wheel and source archive arrival contract."""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import tarfile
import zipfile


VERSION = "0.1.3"
ENTRY_POINTS = {
    "project-continuity = project_continuity.cli:main",
    "project-continuity-front = project_continuity.server:main",
    "project-continuity-mcp = project_continuity.mcp_server:main",
}
ARRIVAL_SUFFIXES = {
    "share/project-continuity/AI_START_HERE.md",
    "share/project-continuity/CHANGELOG.md",
    "share/project-continuity/INSTALL.md",
    "share/project-continuity/LICENSE",
    "share/project-continuity/NOTICE",
    "share/project-continuity/OPERATIONS.md",
    "share/project-continuity/THIRD_PARTY_NOTICES.md",
    "share/project-continuity/uv.lock",
    "share/project-continuity/config/project-continuity.example.toml",
    "share/project-continuity/skills/project-continuity/SKILL.md",
    "share/project-continuity/skills/project-continuity/agents/openai.yaml",
    "share/project-continuity/third_party/licenses/COGNEE-LICENSE",
    "share/project-continuity/third_party/licenses/GRAPHIFY-LICENSE",
    "share/project-continuity/third_party/licenses/OPENSPEC-LICENSE",
    "share/project-continuity/third_party/licenses/TEAMAI-CLI-LICENSE",
    "share/project-continuity/third_party/licenses/TURRITOPSIS-LICENSE",
    "share/project-continuity/vendor/openspec-runtime/package-lock.json",
    "share/project-continuity/vendor/teamai-runtime/package-lock.json",
    "share/project-continuity/vendor/teamai-runtime/project-continuity-literal-recall.mjs",
}
SOURCE_SUFFIXES = {
    "scripts/r4c_linux_cold_start.py",
    "scripts/verify_distribution.py",
    "src/project_continuity/__init__.py",
    "skills/project-continuity/SKILL.md",
    "skills/project-continuity/agents/openai.yaml",
    "vendor/openspec-runtime/package.json",
    "vendor/openspec-runtime/package-lock.json",
    "vendor/teamai-runtime/package.json",
    "vendor/teamai-runtime/package-lock.json",
    "vendor/teamai-runtime/project-continuity-literal-recall.mjs",
    "third_party/licenses/COGNEE-LICENSE",
    "uv.lock",
}
FORBIDDEN_PARTS = {".venv", "node_modules", "credentials", ".secrets"}


def _require_suffixes(names: set[str], suffixes: set[str], kind: str) -> None:
    missing = [suffix for suffix in sorted(suffixes) if not any(
        name == suffix or name.endswith("/" + suffix) for name in names
    )]
    if missing:
        raise SystemExit(f"{kind} missing required arrival files: {missing}")


def _refuse_runtime_state(names: set[str], kind: str) -> None:
    bad = sorted(
        name
        for name in names
        if FORBIDDEN_PARTS.intersection(PurePosixPath(name).parts)
        or name.endswith((".db", ".sqlite", ".sqlite3", ".token"))
    )
    if bad:
        raise SystemExit(f"{kind} contains runtime/private state: {bad}")


def verify_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        _require_suffixes(names, ARRIVAL_SUFFIXES, "wheel")
        _refuse_runtime_state(names, "wheel")
        entry_name = next(
            (name for name in names if name.endswith(".dist-info/entry_points.txt")),
            None,
        )
        metadata_name = next(
            (name for name in names if name.endswith(".dist-info/METADATA")), None
        )
        if entry_name is None or metadata_name is None:
            raise SystemExit("wheel metadata is incomplete")
        entries = archive.read(entry_name).decode("utf-8").splitlines()
        missing_entries = sorted(ENTRY_POINTS.difference(entries))
        if missing_entries:
            raise SystemExit(f"wheel entry points are incomplete: {missing_entries}")
        metadata = archive.read(metadata_name).decode("utf-8")
        if f"Version: {VERSION}\n" not in metadata:
            raise SystemExit("wheel version does not match the arrival contract")


def verify_sdist(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = {member.name for member in archive.getmembers() if member.isfile()}
    _require_suffixes(names, SOURCE_SUFFIXES, "sdist")
    _refuse_runtime_state(names, "sdist")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("sdist", type=Path)
    args = parser.parse_args()
    verify_wheel(args.wheel)
    verify_sdist(args.sdist)
    print("distribution arrival valid")


if __name__ == "__main__":
    main()
