# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""User PATH environment management with pluggable registry backends.

Adds a provider CLI's own install directory to the user PATH
(``provider_setup``). Allows injecting a mock or in-memory backend during
tests to prevent modifying the real Windows registry.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


class EnvironmentBackend(Protocol):
    """Abstract interface for reading and writing user environment variables."""

    def get_path(self) -> tuple[str | None, bool]:
        """Return (path_value, is_expandable). path_value is None if unset."""
        ...

    def set_path(self, value: str, expandable: bool) -> None:
        """Write the user Path environment variable."""
        ...

    def broadcast_change(self) -> None:
        """Broadcast WM_SETTINGCHANGE to top-level windows (best-effort)."""
        ...


class WindowsRegistryBackend:
    """Reads and writes the user Path environment variable in HKCU\\Environment."""

    def get_path(self) -> tuple[str | None, bool]:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ) as key:
                value, value_type = winreg.QueryValueEx(key, "Path")
                is_expandable = (value_type == winreg.REG_EXPAND_SZ)
                return str(value), is_expandable
        except FileNotFoundError:
            return None, True
        except OSError:
            # A read failure must abort the PATH edit, never masquerade as an empty PATH
            raise

    def set_path(self, value: str, expandable: bool) -> None:
        import winreg

        value_type = winreg.REG_EXPAND_SZ if expandable else winreg.REG_SZ
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            winreg.SetValueEx(key, "Path", 0, value_type, value)

    def broadcast_change(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            HWND_BROADCAST = 0xFFFF
            WM_SETTINGCHANGE = 0x001A
            SMTO_ABORTIFHUNG = 0x0002
            result = wintypes.DWORD()
            ctypes.windll.user32.SendMessageTimeoutW(
                HWND_BROADCAST,
                WM_SETTINGCHANGE,
                0,
                "Environment",
                SMTO_ABORTIFHUNG,
                5000,
                ctypes.byref(result),
            )
        except Exception:
            # Broadcast notification is strictly best-effort
            pass


def _normalize_entry(entry: str) -> str:
    """Normalize a PATH entry for case-insensitive comparison.

    Variables are expanded first: the user PATH is usually an expandable
    string, so ``%LOCALAPPDATA%`` followed by a folder and the literal folder
    it names are the same entry, and adding the second would duplicate it.
    """
    return os.path.expandvars(entry.strip()).replace("/", "\\").rstrip("\\").casefold()


def add_to_user_path(directory: Path, backend: EnvironmentBackend, *, dry_run: bool = False) -> bool:
    """Append directory to user PATH if not already present.

    Returns True if the PATH was updated (or, with ``dry_run``, would be),
    False if it was already present. Preserves existing order, entries, and
    expandable string kind.
    """
    current_path, is_expandable = backend.get_path()
    norm_target = _normalize_entry(str(directory))

    if current_path is None:
        if not dry_run:
            backend.set_path(str(directory), is_expandable)
            backend.broadcast_change()
        return True

    parts = current_path.split(";")
    for part in parts:
        if part.strip() and _normalize_entry(part) == norm_target:
            return False
    if dry_run:
        return True

    if not current_path.strip():
        new_path = str(directory)
    elif current_path.endswith(";"):
        new_path = f"{current_path}{directory}"
    else:
        new_path = f"{current_path};{directory}"

    backend.set_path(new_path, is_expandable)
    backend.broadcast_change()
    return True
