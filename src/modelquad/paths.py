# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Installed-mode vs dev-checkout state root.

Distinct from ``adapters/paths.py`` (which resolves the *dev repo root* for
adapter template packaging, an orthogonal concern). This module resolves
where this installation's *mutable state* -- the review ledger, the
preferences, the installation id, the review hook's records -- lives, which
differs between a dev/editable checkout and a real installed product.

**The decision lives in ``statecontext``**, which this module is a thin,
stable accessor over: the resolution is one ordered, explicit decision that
records *why* it decided. Resolution stays dev-checkout-identical
(``~/.modelquad``) so it never silently redirects state out from under a
running dev checkout.
"""

from __future__ import annotations

from pathlib import Path

from .statecontext import APP_DIR_NAME, resolve_state_context

__all__ = [
    "APP_DIR_NAME",
    "is_dev_checkout",
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
    This function is kept as the stable, widely-consumed accessor; callers
    that need the reason, the mode, or
    the executable identity should ask ``statecontext`` directly rather than
    re-deriving them from the path.
    """

    return resolve_state_context().state_root
