# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Safe sibling backups for fully-owned adapter files."""

from __future__ import annotations

from contextlib import ExitStack, suppress
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil

from ..branding import PRODUCT_SLUG
from . import file_security


def _copy_windows(path: Path, candidate: Path, descriptor: int) -> None:
    source_descriptor = file_security.open_source(path)
    try:
        info = file_security.source_info(source_descriptor)
        expected_length = (info.size_high << 32) | info.size_low
        file_security.prepare_write(descriptor, candidate, source_descriptor=source_descriptor)
        # Retain fd ownership even if fdopen fails while constructing a wrapper.
        with os.fdopen(source_descriptor, "rb", closefd=False) as source:
            with os.fdopen(descriptor, "wb", closefd=False) as destination:
                shutil.copyfileobj(source, destination)
                destination.flush()
        if (
            os.fstat(descriptor).st_size != expected_length
            or os.fstat(source_descriptor).st_size != expected_length
        ):
            raise OSError("The backup length does not match its source.")
        file_security.copy_times(descriptor, info)
    finally:
        os.close(source_descriptor)


def backup_if_exists(path: Path, *, dry_run: bool) -> Path | None:
    """Back up an existing file beside itself and return the backup path.

    The backup filename is claimed atomically with exclusive creation
    rather than probed with a separate ``.exists()`` check: probing first and
    writing second leaves a window where a locally-planted symlink at the
    candidate path could redirect the backup write at an unrelated file.
    """

    if not path.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.{PRODUCT_SLUG}-backup-{timestamp}")
    if not dry_run:
        suffix = 2
        while True:
            try:
                descriptor = (
                    file_security.create_exclusive(candidate) if os.name == "nt"
                    else os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                )
                break
            except FileExistsError:
                candidate = path.with_name(
                    f"{path.name}.{PRODUCT_SLUG}-backup-{timestamp}-{suffix}"
                )
                suffix += 1
        if os.name == "nt":
            try:
                with ExitStack() as cleanup:
                    try:
                        cleanup.enter_context(file_security.cleanup_access(candidate))
                        _copy_windows(path, candidate, descriptor)
                    finally:
                        os.close(descriptor)
            except BaseException:
                with suppress(OSError):
                    candidate.unlink()
                raise
        else:
            os.close(descriptor)
            shutil.copy2(path, candidate)
    return candidate
