# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Task-scoped declarations, not runtime grants or OS enforcement evidence.

This is the foundation. Runtime mapping and shared-path enablement are
deliberately absent. No caller-supplied value can activate a worker path.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any


class AuthorityMode(str, Enum):
    READ_ONLY = "read_only"
    PROPOSE_ONLY = "propose_only"
    SHARED_EDIT = "shared_edit"
    ISOLATED_EDIT = "isolated_edit"


class TaskAction(str, Enum):
    READ = "read"
    PROPOSE = "propose"
    EDIT = "edit"
    COMMAND = "command"
    TEST = "test"


def _object(data: Any, keys: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict) or any(not isinstance(k, str) for k in data):
        raise ValueError("authority payload must be an object with string keys")
    if set(data) - keys:
        raise ValueError("unknown authority fields")
    if required - set(data):
        raise ValueError("missing required authority fields")
    return data


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(v, str) or not v.strip() or "\x00" in v for v in value
    ):
        raise ValueError(f"{name} must be a sequence of non-empty strings")
    return tuple(value)


def _reference(value: Any, name: str) -> None:
    if value is not None and (
        not isinstance(value, str) or not value.strip() or "\x00" in value
    ):
        raise ValueError(f"{name} must be a non-empty reference or null")


def _path(value: str, *, directory: bool = False) -> None:
    # Lexically reject globs, drive/UNC paths, traversal, NT streams, empty or
    # whitespace/dot-suffixed components, controls and the device names below.
    # This is NOT complete alias, symlink/junction or OS enforcement checking.
    if directory and value == ".":
        return
    if any(c in value for c in '\\:*?[]<>|"') or value.startswith("/"):
        raise ValueError("authority paths must be exact workspace-relative paths")
    for part in value.split("/"):
        if not part or part in {".", ".."} or part != part.strip() or part.endswith("."):
            raise ValueError("authority paths must be canonical and bounded")
        if any(ord(c) < 32 for c in part) or re.fullmatch(
            r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part
        ):
            raise ValueError("authority path contains a reserved component")


@dataclass(frozen=True)
class CommandEffect:
    """One requested task command/test and its explicitly declared write set.

    argv is data, never executed by this module. An empty write set is a claim
    of no writes, not proof that a test or arbitrary command is read-only.
    """

    action: TaskAction
    argv: tuple[str, ...]
    working_directory: str
    write_files: tuple[str, ...]

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "action", TaskAction(self.action))
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid command action") from exc
        if self.action not in {TaskAction.COMMAND, TaskAction.TEST}:
            raise ValueError("command effect must declare command or test")
        object.__setattr__(self, "argv", _strings(self.argv, "argv"))
        if not self.argv:
            raise ValueError("argv must not be empty")
        object.__setattr__(self, "write_files", _strings(self.write_files, "write_files"))
        if not isinstance(self.working_directory, str):
            raise ValueError("command working_directory must be a relative path")
        _path(self.working_directory, directory=True)
        for path in self.write_files:
            _path(path)

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action.value, "argv": list(self.argv),
                "working_directory": self.working_directory, "write_files": list(self.write_files)}

    @classmethod
    def from_dict(cls, data: Any) -> CommandEffect:
        keys = {"action", "argv", "working_directory", "write_files"}
        return cls(**_object(data, keys, keys))


@dataclass(frozen=True)
class TaskAuthority:
    """Requested task authority, separate from capability and context ownership.

    Authorization/confirmation/isolation references preserve the caller's
    declarations only. Verified isolated editing still needs independent
    runtime/environment evidence before execution; a reference proves nothing.
    """

    mode: AuthorityMode
    actions: tuple[TaskAction, ...]
    assigned_files: tuple[str, ...] = ()
    command_effects: tuple[CommandEffect, ...] = ()
    host_authorization: str | None = None
    confirmations: tuple[str, ...] = ()
    isolation_reference: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "mode", AuthorityMode(self.mode))
            if not isinstance(self.actions, (tuple, list)):
                raise ValueError("actions must be a sequence")
            object.__setattr__(self, "actions", tuple(TaskAction(a) for a in self.actions))
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid authority mode/actions") from exc
        if not self.actions or len(set(self.actions)) != len(self.actions):
            raise ValueError("actions must be non-empty and unique")
        for name in ("assigned_files", "confirmations"):
            values = _strings(getattr(self, name), name)
            if len(set(v.casefold() for v in values)) != len(values):
                raise ValueError(f"{name} must be unique")
            object.__setattr__(self, name, values)
        for path in self.assigned_files:
            _path(path)
        for name in ("host_authorization", "isolation_reference"):
            _reference(getattr(self, name), name)
        if not isinstance(self.command_effects, (tuple, list)) or any(
            not isinstance(e, CommandEffect) for e in self.command_effects
        ):
            raise ValueError("command_effects must contain typed CommandEffect values")
        object.__setattr__(self, "command_effects", tuple(self.command_effects))
        declared = set(self.actions) & {TaskAction.COMMAND, TaskAction.TEST}
        if declared != {e.action for e in self.command_effects}:
            raise ValueError("command/test actions require matching effect declarations")
        editing = self.mode in {AuthorityMode.SHARED_EDIT, AuthorityMode.ISOLATED_EDIT}
        if editing:
            if TaskAction.EDIT not in self.actions or not self.assigned_files or not self.host_authorization:
                raise ValueError("editing requires edit action, assigned files and Host authorization")
        elif TaskAction.EDIT in self.actions or self.assigned_files or any(e.write_files for e in self.command_effects):
            raise ValueError("read/propose modes cannot declare writable authority")
        if self.mode is AuthorityMode.READ_ONLY and TaskAction.PROPOSE in self.actions:
            raise ValueError("propose action requires propose_only or editing mode")
        if self.mode is AuthorityMode.ISOLATED_EDIT:
            if not self.isolation_reference:
                raise ValueError("isolated editing requires an isolation reference, still unverified")
        elif self.isolation_reference is not None:
            raise ValueError("isolation reference requires isolated_edit mode")
        owned = {p.casefold() for p in self.assigned_files}
        if any(p.casefold() not in owned for e in self.command_effects for p in e.write_files):
            raise ValueError("command/test writes must stay within assigned files")

    def validate_consultation(self, consultation: bool) -> None:
        if consultation and self.mode not in {AuthorityMode.READ_ONLY, AuthorityMode.PROPOSE_ONLY}:
            raise ValueError("consultation authority must remain read/propose-only")

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "actions": [a.value for a in self.actions],
                "assigned_files": list(self.assigned_files),
                "command_effects": [e.to_dict() for e in self.command_effects],
                "host_authorization": self.host_authorization,
                "confirmations": list(self.confirmations), "isolation_reference": self.isolation_reference}

    @classmethod
    def from_dict(cls, data: Any) -> TaskAuthority:
        keys = {"mode", "actions", "assigned_files", "command_effects",
                "host_authorization", "confirmations", "isolation_reference"}
        values = dict(_object(data, keys, {"mode", "actions"}))
        effects = values.get("command_effects", ())
        if not isinstance(effects, (tuple, list)):
            raise ValueError("command_effects must be a sequence")
        values["command_effects"] = tuple(CommandEffect.from_dict(e) for e in effects)
        return cls(**values)


@dataclass(frozen=True)
class PermissionObservation:
    """Separate runtime facts for a requested action, never parsed from a capsule.

    Even this record does not enable execution. Later mapping must bind effect
    evidence to this task's exact scope and its fresh/resumed runtime session.
    A caller or worker claim cannot establish effective OS permission.
    """

    supported: bool | None = None
    allowed: bool | None = None
    effective: bool | None = None
    source: str = "unknown"
    evidence_reference: str | None = None
    restriction_reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("supported", "allowed", "effective"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise ValueError(f"{name} must be a boolean or unknown")
        if self.source not in {"unknown", "caller_declared", "worker_reported", "runtime_observed"}:
            raise ValueError("invalid permission observation source")
        _reference(self.evidence_reference, "evidence_reference")
        _reference(self.restriction_reason, "restriction_reason")
        if self.allowed is False and not self.restriction_reason:
            raise ValueError("restricted permission must name its restriction")
        if self.effective is True and (
            self.supported is not True or self.allowed is not True
            or self.source != "runtime_observed" or not self.evidence_reference
        ):
            raise ValueError("effective permission requires independent runtime evidence")

    @property
    def required_gap(self) -> str | None:
        if self.supported is False:
            return "unsupported"
        if self.allowed is False:
            return "restricted"
        if self.effective is False and self.source == "runtime_observed":
            return "unavailable"
        if self.effective is not True:
            return "unknown"
        return None
