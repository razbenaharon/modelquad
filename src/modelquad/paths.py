# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Installed-mode vs dev-checkout state paths for the local control plane.

Distinct from ``adapters/paths.py`` (which resolves the *dev repo root* for
adapter template packaging, an orthogonal concern). This module resolves
where this installation's *mutable state* -- the SQLite history DB, a user
config override, future log files -- should live, which differs between a
dev/editable checkout and a real installed product.

**The decision now lives in ``statecontext``**, which this module is a thin,
stable accessor over. It kept its name and signature because a great deal of
the tree consumes ``paths.state_root()`` (``hostobs``, ``netpolicy``,
``service/lifecycle``), and moving that call site would have been a bigger change than the
defect warranted. What changed is upstream: the resolution is one ordered,
explicit decision that records *why* it decided, instead of a packaging-shape
inference duplicated wherever someone needed the answer.

``service/db.py``'s ``DEFAULT_DB_PATH`` and ``service/lifecycle.py``'s
``ServiceProcessManager`` state root now resolve through this module (via
``db_path()`` / ``state_root()``), so an installed copy keeps its DB,
pidfile and logs together under ``~/.local/share/modelquad``. ``config.py``'s
``DEFAULT_SESSIONS_PATH`` is intentionally NOT wired here: ``sessions_path`` is
config-file-driven (the packaged ``default.yaml`` sets an explicit
``~/.modelquad/sessions.json`` that wins the config merge), and
``sessions.json`` is a legacy artifact, so redirecting it would be inert
without a config-layer change. Resolution stays dev-checkout-identical
(``~/.modelquad``) so it never silently redirects state out from under a
running dev checkout.
"""

from __future__ import annotations

from pathlib import Path

from .statecontext import APP_DIR_NAME, resolve_state_context

__all__ = [
    "APP_DIR_NAME",
    "db_path",
    "is_dev_checkout",
    "logs_dir",
    "pending_output_dir",
    "sessions_path",
    "state_root",
]


def is_dev_checkout() -> bool:
    """True when this package is running from a source checkout of this exact repo.

    Detected the same way ``adapters/paths.py``'s ``repo_root()`` locates the
    checkout root: by walking up from this file's own location and checking
    for the ``src/modelquad/`` layout with a sibling ``pyproject.toml``.
    A real installed copy (editable or not) never has this
    shape -- an editable install's ``__file__`` does point back into a
    checkout, which is exactly the case this function is meant to catch, so
    an editable dev install correctly keeps dev-mode paths; a *non-editable*
    install (even one built from this same source tree) does not, since its
    ``__file__`` resolves into ``site-packages`` instead.
    """

    here = Path(__file__).resolve()
    src_dir = here.parent.parent
    repo_root = src_dir.parent
    return src_dir.name == "src" and (repo_root / "pyproject.toml").is_file()


def state_root() -> Path:
    """Resolve where this installation's mutable state lives.

    Dev checkout: unchanged from pre-v0.8 behavior, ``~/.modelquad/``.

    Installed (a plain non-editable install): ``~/.local/share/modelquad/``,
    outside ``AppData``, where Windows redirects the writes of a packaged app
    such as the Claude desktop app (see ``statecontext.installed_state_root``).

    The decision itself now lives in ``statecontext.resolve_state_context()``,
    which resolves the same paths from the same predicates but also records
    *why* -- and honours an explicit ``MODELQUAD_STATE_ROOT`` /
    ``MODELQUAD_STATE_MODE`` declaration ahead of any packaging inference.
    This function is kept as the stable, widely-consumed accessor (notably
    ``hostobs.read_host_push()``); callers that need the reason, the mode, or
    the executable identity should ask ``statecontext`` directly rather than
    re-deriving them from the path.
    """

    return resolve_state_context().state_root


def db_path() -> Path:
    """The SQLite history DB path for this installation mode."""

    return state_root() / "modelquad.db"


def pending_output_dir() -> Path:
    """Local pending answers, separate from history."""

    return state_root() / "pending-output"


def sessions_path() -> Path:
    """The sessions.json path for this installation mode."""

    return state_root() / "sessions.json"


def logs_dir() -> Path:
    """Where future log files (e.g. the auto-start service log) should live."""

    return state_root() / "logs"
