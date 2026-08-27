from __future__ import annotations

import os
from pathlib import Path

import pytest

from project_continuity.runtime_lock import RuntimeLockError, runtime_lifetime_lock


def test_runtime_lock_is_private_nonblocking_and_reusable(tmp_path: Path) -> None:
    state_root = tmp_path / "state"

    with runtime_lifetime_lock(state_root):
        lock_path = state_root / "runtime/front-or-relocation.lock"
        assert lock_path.is_file()
        assert lock_path.stat().st_mode & 0o077 == 0
        with pytest.raises(RuntimeLockError, match="already active"):
            with runtime_lifetime_lock(state_root):
                pass

    with runtime_lifetime_lock(state_root):
        pass


@pytest.mark.parametrize("broken", [False, True])
def test_runtime_lock_rejects_parent_symlink(tmp_path: Path, broken: bool) -> None:
    outside = tmp_path / "outside"
    if not broken:
        outside.mkdir(mode=0o700)
    state_root = tmp_path / "state"
    state_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeLockError, match="absolute real private directory"):
        with runtime_lifetime_lock(state_root):
            pass


def test_runtime_lock_rejects_non_private_existing_state_root(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    os.chmod(state_root, 0o755)

    with pytest.raises(RuntimeLockError, match="owner-private"):
        with runtime_lifetime_lock(state_root):
            pass
