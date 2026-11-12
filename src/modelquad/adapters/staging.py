# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""One read, one backup and one write per host config file per run.

Install and uninstall change several things inside the same user file
(``settings.json`` carries the status line, the gate hook, the tool timeout
and the startup reminder). Writing each change separately meant the backup was
taken after the first change had already landed, and a failure half-way left
the file half-changed. Staging reads every file before anything is written,
applies all changes in memory, and then writes each file once -- backing up
the original bytes immediately before that single write, and not at all when
nothing changed.

A file that cannot be read safely (a link, not UTF-8, unreadable, malformed
JSON) is refused up front with a plain reason, and every change aimed at it is
reported as skipped; the other files still proceed.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable

from .atomicwrite import atomic_write
from .backup import backup_if_exists
from .report import ActionKind, FileAction

#: Windows reparse-tag bit shared by symbolic links and junctions.
_NAME_SURROGATE = 0x20000000


def is_link(path: Path) -> bool:
    """True when ``path`` itself is a symbolic link or junction.

    Only the file is judged; a linked parent folder is an ordinary layout
    choice and the file inside it is still the user's real file.
    """

    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(
        attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        and getattr(info, "st_reparse_tag", 0) & _NAME_SURROGATE
    )


def link_refusal(path: Path) -> str:
    return (
        f"{path} is a link to another file, and ModelQuad does not replace links "
        "with regular files. Make this change by hand, or replace the link with a "
        "regular file and run the command again."
    )


@dataclass(frozen=True)
class Change:
    """One concern's planned effect on a staged file."""

    #: "set", "remove", "unchanged" or "skipped".
    kind: str
    detail: str | None = None

    @property
    def changes_file(self) -> bool:
        return self.kind in ("set", "remove")


UNCHANGED = Change("unchanged")


@dataclass
class CommitResult:
    written: bool = False
    backup_path: Path | None = None
    error: str | None = None


@dataclass
class StagedText:
    """A user file read once, changed in memory, written at most once."""

    path: Path
    existed: bool = False
    original: bytes | None = None
    text: str = ""
    refusal: str | None = None
    #: False only for a second write in one run, after the first saved a copy.
    backup: bool = True
    _undo: list[Callable[[], None]] = field(default_factory=list, repr=False)
    result: CommitResult | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path: Path, *, read_only: bool = False):
        """Read ``path`` once. ``read_only`` follows a link, for inspection only."""

        staged = cls(Path(path))
        staged._read(read_only=read_only)
        return staged

    def _read(self, *, read_only: bool = False) -> None:
        path = self.path
        if not read_only and is_link(path):
            self.existed = True
            self.refusal = link_refusal(path)
            return
        if not path.exists():
            return
        self.existed = True
        if not path.is_file():
            self.refusal = f"{path} exists but is not a regular file, so ModelQuad left it unchanged."
            return
        try:
            self.original = path.read_bytes()
        except OSError as exc:
            self.refusal = f"Could not read {path}: {exc.strerror or exc}. It was left unchanged."
            return
        try:
            self.text = self.original.decode("utf-8")
        except UnicodeDecodeError:
            self.refusal = (
                f"{path} is not saved as UTF-8 text, so ModelQuad left it unchanged. "
                "Save it as UTF-8 and run the command again."
            )

    # -- transaction hooks --------------------------------------------------

    def on_failure(self, undo: Callable[[], None]) -> None:
        """Run ``undo`` if the write does not land (latest registered first)."""

        self._undo.append(undo)

    def _rollback(self) -> None:
        while self._undo:
            undo = self._undo.pop()
            try:
                undo()
            except Exception:  # noqa: BLE001 -- the write failure is the news
                pass

    # -- writing ------------------------------------------------------------

    def render(self) -> bytes | None:
        """The new content, or None when nothing changed."""

        if self.refusal is not None or (self.original is None and not self.text):
            return None
        rendered = self.text.encode("utf-8")
        return None if rendered == self.original else rendered

    def commit(self, *, dry_run: bool) -> CommitResult:
        """Back up the original bytes and write the new content, once."""

        if self.result is not None:
            return self.result
        content = self.render()
        if content is None:
            self._undo.clear()
            self.result = CommitResult()
            return self.result
        if dry_run:
            backup = (
                backup_if_exists(self.path, dry_run=True) if self.existed and self.backup else None
            )
            self.result = CommitResult(written=True, backup_path=backup)
            return self.result
        try:
            if is_link(self.path):
                raise _Refused(link_refusal(self.path))
            current = self.path.read_bytes() if self.path.exists() else None
            if current != self.original:
                raise _Refused(
                    f"{self.path} was changed by another program while ModelQuad was "
                    "updating it, so it was left as it is now. Run the command again."
                )
            backup = (
                backup_if_exists(self.path, dry_run=False) if self.existed and self.backup else None
            )
            atomic_write(self.path, content)
        except _Refused as exc:
            self._rollback()
            self.result = CommitResult(error=str(exc))
            return self.result
        except (OSError, ValueError) as exc:
            self._rollback()
            reason = getattr(exc, "strerror", None) or str(exc) or type(exc).__name__
            self.result = CommitResult(
                error=f"Could not write {self.path}: {reason}. It was left unchanged."
            )
            return self.result
        self._undo.clear()
        self.result = CommitResult(written=True, backup_path=backup)
        return self.result

    def action(self, change: Change, *, dry_run: bool) -> FileAction:
        """Report one concern's outcome once the file has been committed."""

        if self.refusal is not None:
            return FileAction(self.path, ActionKind.SKIPPED, detail=self.refusal)
        if change.kind == "skipped":
            return FileAction(self.path, ActionKind.SKIPPED, detail=change.detail)
        if not change.changes_file:
            return FileAction(self.path, ActionKind.UNCHANGED, detail=change.detail)
        result = self.result or self.commit(dry_run=dry_run)
        if result.error is not None:
            return FileAction(self.path, ActionKind.SKIPPED, detail=result.error)
        if change.kind == "remove":
            kind = ActionKind.WOULD_REMOVE if dry_run else ActionKind.REMOVED
        elif self.existed:
            kind = ActionKind.WOULD_UPDATE if dry_run else ActionKind.UPDATED
        else:
            kind = ActionKind.WOULD_CREATE if dry_run else ActionKind.CREATED
        return FileAction(self.path, kind, backup_path=result.backup_path, detail=change.detail)


class _Refused(Exception):
    pass


@dataclass
class StagedJson(StagedText):
    """A JSON object file staged like :class:`StagedText`.

    Written back with the user's own key order and characters
    (``ensure_ascii=False``, no key sorting): these files belong to the user
    and the host, and a rewrite should differ only where ModelQuad changed it.
    The one exception is a lone surrogate, which is written as its escape.
    """

    payload: dict[str, Any] | None = None
    _baseline: Any = field(default=None, repr=False)

    def _read(self, *, read_only: bool = False) -> None:
        super()._read(read_only=read_only)
        if self.refusal is not None:
            return
        if not self.text.strip():
            # An absent or empty file has nothing to lose by being treated as
            # an empty object; the host creates these files lazily.
            self.payload = {}
        else:
            try:
                loaded = json.loads(self.text)
            except json.JSONDecodeError as exc:
                self.refusal = f"Could not safely parse JSON config: {exc}"
                return
            if not isinstance(loaded, dict):
                self.refusal = "Could not safely parse JSON config: top-level value is not an object."
                return
            self.payload = loaded
        self._baseline = copy.deepcopy(self.payload)

    def render(self) -> bytes | None:
        if self.refusal is not None or self.payload is None or self.payload == self._baseline:
            return None
        text = json.dumps(self.payload, indent=2, ensure_ascii=False) + "\n"
        # Half of a surrogate pair ("\ud83d", what a JavaScript host writes for
        # an emoji it cut in two) is valid JSON as an escape, and UTF-8 has no
        # bytes for it. Once parsed, a surrogate code point is always a lone
        # one and sits inside a JSON string, so it goes back as that escape.
        return _LONE_SURROGATE.sub(
            lambda match: f"\\u{ord(match.group()):04x}", text
        ).encode("utf-8")


_LONE_SURROGATE = re.compile("[\\ud800-\\udfff]")
