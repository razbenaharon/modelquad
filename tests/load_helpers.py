# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Bounded load tolerance for test-owned files and synthetic child handshakes."""

from queue import Empty, Queue
from subprocess import TimeoutExpired
from threading import Thread
from time import monotonic, sleep


def retry_temp_permission(operation, *, seconds=10.0):
    """Retry only PermissionError on a caller's temporary fixture file.

    No attributes, permissions or security settings change. A persistent lock
    raises the original error; assertions and product operations are not retried.
    Capture sleep separately because some fixtures stub time.sleep.
    """
    deadline = monotonic() + seconds
    while True:
        try:
            return operation()
        except PermissionError:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise
            sleep(min(0.05, remaining))


def read_child_line(child, stderr, *, seconds=60):
    """Read a synthetic child's handshake with a deadline and exit diagnostics.

    stderr is a test-owned capture file, so a failed import cannot fill a pipe
    while the caller waits for stdout. Kill only this owned child on failure.
    """
    result = Queue()

    def read():
        try:
            result.put(child.stdout.readline())
        except Exception as error:
            result.put(error)

    reader = Thread(target=read, daemon=True)
    reader.start()
    try:
        line = result.get(timeout=seconds)
    except Empty:
        line = None
    if isinstance(line, str) and line:
        return line
    exit_before_cleanup = child.poll()
    if line is None and exit_before_cleanup is None:
        child.kill()
    try:
        # EOF may precede process exit; retain its real failing exit code.
        child.wait(timeout=seconds)
    except TimeoutExpired:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=seconds)
    reader.join(timeout=seconds)
    stderr.seek(0)
    raise AssertionError(
        f'child handshake {"timed out" if line is None else "hit EOF or failed"}; '
        f'pid={child.pid}, returncode={child.returncode}, '
        f'exit_before_cleanup={exit_before_cleanup}, '
        f'read={line!r}, stderr={stderr.read().decode("utf-8", "replace")}'
    )
