"""One non-blocking lifetime lock for the front and offline custody work."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import stat
from typing import Iterator

from .client import path_has_symlink

LOCK_DIRECTORY = "runtime"
LOCK_NAME = "front-or-relocation.lock"


class RuntimeLockError(RuntimeError):
    """The front/relocation lifetime boundary is unsafe or already occupied."""


@contextmanager
def runtime_lifetime_lock(state_root: Path) -> Iterator[None]:
    """Hold the single-writer lifetime lock without waiting for another process."""

    root = _private_directory(Path(state_root), "state root")
    lock_root = _private_directory(root / LOCK_DIRECTORY, "runtime lock directory")
    lock_path = lock_root / LOCK_NAME
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        descriptor = os.open(str(lock_path), flags, 0o600)
    except OSError as exc:
        raise RuntimeLockError("cannot open ProjectContinuity runtime lock") from exc
    locked = False
    try:
        lock_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_mode & 0o077
            or (hasattr(os, "geteuid") and lock_stat.st_uid != os.geteuid())
        ):
            raise RuntimeLockError(
                "ProjectContinuity runtime lock is not owner-private"
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise RuntimeLockError(
                    "ProjectContinuity front or relocation is already active"
                ) from exc
            raise RuntimeLockError(
                "cannot acquire ProjectContinuity runtime lock"
            ) from exc
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _private_directory(path: Path, where: str) -> Path:
    if not path.is_absolute() or path_has_symlink(path):
        raise RuntimeLockError("%s must be an absolute real private directory" % where)
    if not path.exists():
        try:
            path.mkdir(parents=True, mode=0o700)
            os.chmod(path, 0o700)
        except OSError as exc:
            raise RuntimeLockError("cannot create %s" % where) from exc
    if path_has_symlink(path):
        raise RuntimeLockError("%s must be an absolute real private directory" % where)
    try:
        value = path.stat()
    except OSError as exc:
        raise RuntimeLockError("cannot inspect %s" % where) from exc
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_mode & 0o077
        or (hasattr(os, "geteuid") and value.st_uid != os.geteuid())
    ):
        raise RuntimeLockError("%s must be owner-private" % where)
    return path
