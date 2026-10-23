# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The single authority on which ModelQuad product state this process owns.

Before this module, "which database am I?" was answered by two *different*
packaging predicates in two different places:

- ``paths.state_root()`` chose the dev root when the package ran from a
  source checkout;
- ``service/api.py`` reported ``instance_kind = "installed"`` only for the
  packaged executable (since removed), and ``"development"`` otherwise.

Those disagree for a plain non-editable wheel install, and -- far worse -- both
answer a question about *packaging shape* when the question that matters is
about *product identity*. On 2026-08-26 that distinction was the whole defect:
every host adapter registered the MCP server as the bare command
``modelquad-mcp``, the installed product appends its shim directory to the END
of the user PATH (``product/pathenv.add_to_user_path``), and a machine that
also had a pip/editable install therefore resolved that bare name to the
editable console script. The host's MCP was an *editable checkout* -- so
``is_dev_checkout()`` was True, so it wrote ``~/.modelquad/modelquad.db``
-- while the former installed Control Center and service wrote
``%LOCALAPPDATA%\\ModelQuad\\modelquad.db``. Both stores were
live and both were being written the same minute. The user set
``routing_mode = automatic`` in the Control Center and the host's MCP kept
reading different controls, because they were two products wearing one name.

The rule this module establishes:

    Packaging shape may supply a DEFAULT. It is never the sole authority.
    An explicit declaration of intent always wins over an inference.

So resolution is ordered by how *explicit* the evidence is, and the winning
step is recorded in :attr:`StateContext.source`. That recorded reason is what
makes the ambiguity diagnosable instead of invisible -- ``doctor`` and
``modelquad state`` report it, so a mismatch between two surfaces becomes a
reportable condition rather than something the user has to infer from a
setting that appears not to save.

Development isolation stays fully supported; it just has to be *asked for*.
``MODELQUAD_STATE_ROOT`` / ``MODELQUAD_STATE_MODE`` are how a developer says
"this instance is deliberately separate", and that is a different act from a
production integration accidentally resolving through an editable install.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


STATE_ROOT_ENV_VAR = "MODELQUAD_STATE_ROOT"
STATE_MODE_ENV_VAR = "MODELQUAD_STATE_MODE"

APP_DIR_NAME = "ModelQuad"
DATABASE_FILE_NAME = "modelquad.db"


class StateMode(str, Enum):
    """Which product a running process's state belongs to."""

    INSTALLED = "installed"
    DEVELOPMENT = "development"
    TEST = "test"


class StateSource(str, Enum):
    """Why :class:`StateContext` decided the way it did.

    Ordered most-explicit first, and that order IS the resolution order in
    :func:`resolve_state_context`. Recording the winning reason is what lets a
    diagnostic say "your host MCP is on development state *because* it is an
    editable checkout" rather than only "these two paths differ".
    """

    ENV_OVERRIDE = "env_override"
    INSTALLED_LAUNCHER = "installed_launcher"
    DEV_CHECKOUT = "dev_checkout"
    INSTALLED_DEFAULT = "installed_default"


@dataclass(frozen=True)
class StateContext:
    """The resolved product-state identity of the current process."""

    mode: StateMode
    source: StateSource
    state_root: Path
    executable: str
    #: True when ``state_root`` is not the canonical root for ``mode`` --
    #: today only the documented "installed on Windows but %LOCALAPPDATA% is
    #: unset" fallback. Kept explicit so a diagnostic never has to re-derive
    #: it, and never silently presents a fallback path as canonical.
    fallback: bool = False

    @property
    def database_path(self) -> Path:
        return self.state_root / DATABASE_FILE_NAME

    @property
    def is_installed(self) -> bool:
        return self.mode is StateMode.INSTALLED

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "source": self.source.value,
            "state_root": str(self.state_root),
            "database_path": str(self.database_path),
            "executable": self.executable,
            "fallback": self.fallback,
        }


def dev_state_root() -> Path:
    """``~/.modelquad`` -- the dev-checkout convention, unchanged since v0.4.

    Resolved fresh on every call rather than once at import time, so it
    correctly follows a HOME/USERPROFILE override made after this module was
    first imported (the import-time-constant trap that ``service/db.py``'s
    ``DEFAULT_DB_PATH`` still demonstrates and ``tests/conftest.py`` works
    around).
    """

    return Path.home() / ".modelquad"


def installed_state_root() -> tuple[Path, bool]:
    """The installed product's state root, and whether it is a fallback.

    ``%LOCALAPPDATA%\\ModelQuad`` on Windows.
    """

    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / APP_DIR_NAME, False
        # Never silently produce an unusable path; fall back, but say so.
        return dev_state_root(), True
    return Path.home() / ".local" / "share" / APP_DIR_NAME.lower(), False


def canonical_state_root(mode: StateMode) -> tuple[Path, bool]:
    """The state root a given mode owns, plus whether it is a fallback path."""

    if mode is StateMode.INSTALLED:
        return installed_state_root()
    return dev_state_root(), False


def _launched_from_installed_tree() -> bool:
    """True when this interpreter lives inside the installed product tree.

    This covers a runtime living under the product root (for example an
    embedded interpreter). It is a *positive*
    identification of the installed product, so it outranks the dev-checkout
    inference below it.
    """

    executable = sys.executable
    if not executable:
        return False
    root, fallback = installed_state_root()
    if fallback:
        return False
    try:
        Path(executable).resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _mode_from_env(raw: str) -> StateMode | None:
    try:
        return StateMode(raw.strip().lower())
    except ValueError:
        return None


def resolve_state_identity() -> tuple[StateMode, StateSource, Path | None]:
    """Decide mode and source *without* resolving a canonical root.

    Split out from :func:`resolve_state_context` so a caller that only needs
    to know "is this the installed product?" does not pay for a filesystem
    root it will not use. That is not a micro-optimisation: the adapter suite
    deliberately makes ``Path.home()`` raise to prove adapters never touch a
    real user directory, and asking "which runtime should a host launch?"
    genuinely does not need the state root.

    The third element is an explicitly declared root, when there is one, and
    ``None`` when the root should be derived from the mode.
    """

    from . import paths

    raw_root = os.environ.get(STATE_ROOT_ENV_VAR)
    raw_mode = os.environ.get(STATE_MODE_ENV_VAR)

    if raw_root:
        # An explicit root is a complete declaration of intent. The mode is
        # descriptive here: it says what this instance calls itself, and
        # defaults to development because an explicitly relocated store is by
        # definition not the shared installed one.
        mode = (_mode_from_env(raw_mode) if raw_mode else None) or StateMode.DEVELOPMENT
        return mode, StateSource.ENV_OVERRIDE, Path(raw_root).expanduser()

    if raw_mode:
        mode = _mode_from_env(raw_mode)
        if mode is not None:
            return mode, StateSource.ENV_OVERRIDE, None
        # An unparseable value is ignored rather than raising: a typo in an
        # environment variable must not make the product unstartable. It falls
        # through to inference, and the recorded source says so.

    # Called through the module so a test (and the packaging-simulation paths
    # in the existing suite) can substitute either predicate.
    if _launched_from_installed_tree():
        return StateMode.INSTALLED, StateSource.INSTALLED_LAUNCHER, None

    if paths.is_dev_checkout():
        return StateMode.DEVELOPMENT, StateSource.DEV_CHECKOUT, None

    # A plain non-editable install: not a checkout. This is a real
    # installed product and has always resolved to the installed root; the only
    # change is that it now *says* so consistently, instead of reporting
    # instance_kind="development" while writing installed state.
    return StateMode.INSTALLED, StateSource.INSTALLED_DEFAULT, None


def resolve_state_context() -> StateContext:
    """Resolve this process's product-state identity, freshly, on every call.

    Resolved per call rather than cached at import: ``service/db.py``'s
    ``DEFAULT_DB_PATH`` being an import-time constant is a documented gotcha
    that ``tests/conftest.py`` already has to work around, and this module
    exists to end that class of bug, not to add another instance of it.
    """

    mode, source, explicit_root = resolve_state_identity()
    if explicit_root is not None:
        return StateContext(
            mode=mode,
            source=source,
            state_root=explicit_root,
            executable=sys.executable or "",
        )
    root, fallback = canonical_state_root(mode)
    return StateContext(
        mode=mode,
        source=source,
        state_root=root,
        executable=sys.executable or "",
        fallback=fallback,
    )
