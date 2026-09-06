"""Keep one TeamAI donor attempt owned after its Python caller disappears."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
from typing import Sequence


TIMEOUT_EXIT = 124
SUPERVISOR_ERROR_EXIT = 125


def _validated_arguments(arguments: Sequence[str]) -> tuple[int, float, list[str]]:
    if len(arguments) < 4:
        raise ValueError
    lock_fd = int(arguments[1])
    timeout = float(arguments[2])
    command = list(arguments[3:])
    if lock_fd < 3 or not 1 <= timeout <= 600 or not command:
        raise ValueError
    item = os.fstat(lock_fd)
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.getuid()
        or item.st_mode & 0o077
    ):
        raise ValueError
    return lock_fd, timeout, command


def _terminate(group: int) -> None:
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        return


def _kill(group: int) -> None:
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        return


def main(arguments: Sequence[str] | None = None) -> int:
    values = sys.argv if arguments is None else arguments
    try:
        lock_fd, timeout, command = _validated_arguments(values)
    except (OSError, TypeError, ValueError):
        return SUPERVISOR_ERROR_EXIT

    try:
        child = subprocess.Popen(
            command,
            close_fds=True,
            pass_fds=(lock_fd,),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return SUPERVISOR_ERROR_EXIT

    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate(child.pid)
        try:
            stdout, stderr = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _kill(child.pid)
            stdout, stderr = child.communicate()
        result = TIMEOUT_EXIT
    else:
        result = child.returncode

    try:
        sys.stdout.write(stdout)
        sys.stderr.write(stderr)
    except BrokenPipeError:
        pass
    return result


if __name__ == "__main__":
    raise SystemExit(main())
