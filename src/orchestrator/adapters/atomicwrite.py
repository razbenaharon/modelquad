# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Atomic, symlink-safe writes for adapter-owned file content."""

from __future__ import annotations

from contextlib import ExitStack, suppress
import os
from pathlib import Path
import tempfile

from . import file_security


def atomic_write(
    path: Path, content: str | bytes, *, encoding: str = "utf-8", private: bool = False
) -> None:
    """Replace ``path`` through a freshly claimed sibling temp file.

    Each writer exclusively claims its sibling, rejecting occupied names and
    symlinks. Windows preserves the destination's DACL before writing, or uses
    folder inheritance for a new file, falling back to private protection if
    inheritance supplies no direct user grant. ``private=True`` grants only
    the current user and SYSTEM. A destination symlink is replaced as a new
    file, never followed. POSIX retains the existing 0600 creation behavior.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        for name in tempfile._get_candidate_names():
            temporary = path.with_name(f"{path.name}.{name}.tmp")
            try:
                descriptor = file_security.create_exclusive(temporary)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError("No unoccupied temporary filename is available.")
    else:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
    try:
        with ExitStack() as cleanup:
            try:
                if os.name == "nt":
                    cleanup.enter_context(file_security.cleanup_access(temporary))
                    file_security.prepare_write(descriptor, path, private=private)
                if isinstance(content, bytes):
                    handle = os.fdopen(descriptor, "wb", closefd=False)
                else:
                    handle = os.fdopen(descriptor, "w", encoding=encoding, closefd=False)
                with handle:
                    handle.write(content)
            except BaseException:
                # We retain ownership even if fdopen fails while wrapping the fd.
                with suppress(OSError):
                    os.close(descriptor)
                raise
            os.close(descriptor)  # The metadata-only cleanup handle stays open.
            temporary.replace(path)
    except BaseException:
        # Only this call's file is ours to remove; preserve the original error.
        with suppress(OSError):
            temporary.unlink()
        raise
