# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Serializable results for client adapter installation operations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ActionKind(str, Enum):
    """The effect an adapter operation had, or would have, on one file."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    REMOVED = "removed"
    WOULD_CREATE = "would_create"
    WOULD_UPDATE = "would_update"
    WOULD_REMOVE = "would_remove"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class FileAction:
    """One file-level action performed by an adapter."""

    path: Path
    kind: ActionKind
    backup_path: Path | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "path": str(self.path),
            "kind": self.kind.value,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class InstallReport:
    """The complete result of installing or uninstalling one client adapter."""

    client: str
    dry_run: bool
    actions: tuple[FileAction, ...]

    @property
    def unchanged(self) -> bool:
        """Return True when this operation did not change files on disk."""

        non_mutating = {
            ActionKind.UNCHANGED,
            ActionKind.WOULD_CREATE,
            ActionKind.WOULD_UPDATE,
            ActionKind.WOULD_REMOVE,
        }
        return all(action.kind in non_mutating for action in self.actions)

    def to_dict(self) -> dict[str, object]:
        return {
            "client": self.client,
            "dry_run": self.dry_run,
            "actions": [action.to_dict() for action in self.actions],
            "unchanged": self.unchanged,
        }
