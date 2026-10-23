# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Pseudonymous installation identity.

A random, locally generated identifier for this ModelQuad installation. It
keys local, pseudonymous fingerprints (for example host-session ones) so a
reset can sever them. ModelQuad has no telemetry and sends it nowhere.

Deliberate properties:

- Random ``uuid4``. Derived from nothing: no hardware serials, MAC addresses,
  usernames, or machine names are read, hashed, or mixed in.
- Lazy: the file is created on first request, not at install time.
- User-resettable: :func:`reset_installation_id` mints a fresh identity,
  severing any future link to previously recorded data.
- Stays on this machine: nothing sends it anywhere.
- Removed with the state folder.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .adapters.atomicwrite import atomic_write

IDENTITY_FILE_NAME = "identity.json"
IDENTITY_VERSION = 1


def _identity_path(state_dir: Path | None = None) -> Path:
    if state_dir is None:
        from . import paths

        state_dir = paths.state_root()
    return Path(state_dir) / IDENTITY_FILE_NAME


def _read_identity(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    installation_id = data.get("installation_id")
    if not isinstance(installation_id, str) or len(installation_id) != 32:
        return None
    return data


def _write_identity(path: Path) -> dict:
    record = {
        "identity_version": IDENTITY_VERSION,
        "installation_id": uuid.uuid4().hex,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    # Write-then-rename so a crash mid-write never leaves a half identity
    # that would silently mint a second id on the next read. The identity is
    # minted by whichever process reads first -- a host hook, the CLI or the
    # service -- and two first runs can race here, so each writer stages in
    # its own exclusively created temp file rather than a shared ``.json.tmp``.
    # Whichever record lands last wins; the losing process used its own
    # id for that one run only, and a regenerated id only ever loosens linkage.
    atomic_write(path, json.dumps(record, indent=2) + "\n")
    return record


#: How many times a mint retries a replace that Windows refused.
_WRITE_ATTEMPTS = 5
_WRITE_RETRY_SECONDS = 0.02


def _mint_identity(path: Path, *, adopt_concurrent: bool) -> dict:
    """Write a fresh identity, surviving a concurrent first run.

    On Windows, replacing a file that another process is replacing or reading
    at that same instant is refused with ``PermissionError``. Two first runs
    racing to mint the identity hit exactly that, and a lazily minted identity
    must never be able to break the product. With ``adopt_concurrent``
    the loser adopts the record the winner just wrote -- the two are
    interchangeable, and adopting is what keeps every later read agreeing --
    and otherwise retries briefly. A refusal that persists with nothing valid
    on disk is a real filesystem problem and is raised as before.
    """

    attempt = 0
    while True:
        attempt += 1
        try:
            return _write_identity(path)
        except PermissionError:
            if adopt_concurrent:
                settled = _read_identity(path)
                if settled is not None:
                    return settled
            if attempt >= _WRITE_ATTEMPTS:
                raise
            time.sleep(_WRITE_RETRY_SECONDS)


def installation_id(state_dir: Path | None = None) -> str:
    """Return this installation's pseudonymous id, creating it if absent.

    A corrupt or unreadable identity file is replaced with a fresh identity
    rather than raising: an identity must never be able to break the product,
    and a regenerated id is always safe (it only ever loosens linkage).
    """
    path = _identity_path(state_dir)
    existing = _read_identity(path)
    if existing is not None:
        return existing["installation_id"]
    return _mint_identity(path, adopt_concurrent=True)["installation_id"]


def peek_installation_id(state_dir: Path | None = None) -> str | None:
    """Return the installation id if one exists, without creating one."""
    existing = _read_identity(_identity_path(state_dir))
    return existing["installation_id"] if existing is not None else None


def reset_installation_id(state_dir: Path | None = None) -> str:
    """Mint a fresh installation id, replacing any existing one.

    This is the user's unlinkability lever: after a reset, nothing recorded
    under the old id can be associated with this installation again.
    """
    # A reset must mint, never adopt: adopting a concurrent writer's record
    # would hand back an id that predates the user's request.
    return _mint_identity(_identity_path(state_dir), adopt_concurrent=False)["installation_id"]
