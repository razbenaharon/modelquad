# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Helping the user obtain and wire up the provider CLIs themselves.

Two problems, both observed on a genuinely clean Windows machine during a
2026-08-22 test run:

1. The former setup picker said only "Install or sign in to one of these tools",
   with no indication of how. For the audience this product targets, that is
   a dead end.
2. Claude Code's official Windows installer completed successfully and did
   **not** add itself to PATH, instructing the user to edit System Properties
   by hand. ModelQuad's PATH-only detection then reported a working install
   as absent -- a false statement about the user's own machine.

This module owns the answer to both, and deliberately owns nothing else:

* It never installs anything. The install command is *shown* for the user to
  run; the product does not execute code fetched from a vendor's URL on the
  user's behalf, and every provider stays installed through its own official
  mechanism (the native-agent principle).
* It repairs PATH only for a directory that actually contains the provider's
  executable, through one audited user-PATH writer (``pathenv``), so there
  is one place where "modify the user's PATH" is reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .onboarding import ProviderDetection, detect_providers
from .pathenv import EnvironmentBackend, add_to_user_path


@dataclass(frozen=True)
class PathFixResult:
    """Outcome of repairing PATH for one provider."""

    provider_id: str
    ok: bool
    detail: str
    directory: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "ok": self.ok,
            "detail": self.detail,
            "directory": self.directory,
        }


def providers_needing_path_fix(
    detections: "tuple[ProviderDetection, ...] | None" = None,
) -> tuple[ProviderDetection, ...]:
    """Providers whose off-PATH CLI needs a stable PATH entry."""

    from .codexcli import is_desktop_version_path

    resolved = detect_providers() if detections is None else detections
    return tuple(
        d for d in resolved if d.cli_found and not d.on_path
        and not (d.provider_id == "codex" and d.cli_path and is_desktop_version_path(d.cli_path))
    )


def fix_provider_path(
    detection: ProviderDetection,
    *,
    backend: EnvironmentBackend | None = None,
    dry_run: bool = False,
) -> PathFixResult:
    """Add the directory holding this provider's CLI to the user PATH.

    Refuses anything it cannot justify: a provider that is already reachable
    needs no change, and a recorded path that no longer exists is not written
    to PATH just because detection once saw it. ``dry_run`` reads PATH and
    reports what would be added, without writing it.
    """

    if detection.on_path:
        return PathFixResult(detection.provider_id, True, "Already on PATH.")
    if not detection.cli_path:
        return PathFixResult(
            detection.provider_id, False, "No executable path was detected for this provider."
        )

    executable = Path(detection.cli_path)
    if detection.provider_id == "codex":
        from .codexcli import is_desktop_version_path

        if is_desktop_version_path(executable):
            return PathFixResult(
                detection.provider_id,
                False,
                "ModelQuad can use this Codex install directly. Its desktop version "
                "folder is temporary and must not be added to PATH.",
            )
    if not executable.is_file():
        return PathFixResult(
            detection.provider_id,
            False,
            f"Executable no longer exists at {executable}.",
            str(executable.parent),
        )

    directory = executable.parent
    if backend is None:
        from .pathenv import WindowsRegistryBackend

        backend = WindowsRegistryBackend()

    try:
        changed = add_to_user_path(directory, backend, dry_run=dry_run)
    except OSError as exc:
        return PathFixResult(
            detection.provider_id, False, f"Could not update PATH: {exc}", str(directory)
        )

    if dry_run:
        detail = (
            f"Would add {directory} to your PATH." if changed
            else f"{directory} is already on your PATH."
        )
    else:
        detail = (
            f"Added {directory} to your PATH. Open a new terminal for it to take effect."
            if changed
            else f"{directory} was already on your PATH."
        )
    return PathFixResult(detection.provider_id, True, detail, str(directory))
