# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The one thing a bare `python -m pytest` needs from this repository's harness.

`scripts/devenv.py` states where a development or test child process writes its
temporary files, and `check.py` and the release
pipeline each apply it to the environment they hand a subprocess. None of them
is involved when someone -- a developer, or an agent following `AGENTS.md` --
types the command `AGENTS.md` actually documents as the primary Python gate:

    python -m pytest -q

That command was the largest remaining producer of `pytest-of-<user>` trees on
the ambient volume, and no amount of care in the two callers above could
reach it: they build environments for children, and this process has no such
parent. A conftest at the repository root is the only hook pytest loads for it
without an installed plugin or an entry point, because pytest collects conftest
files from `rootdir` -- which `pyproject.toml` pins here -- down to whatever was
collected. So `pytest`, `python -m pytest`, `pytest tests/test_x.py` and a run
started from inside `tests/` all pass through this file.

**It is opt-in and it is a no-op by default.** With `MODELQUAD_TEMP_ROOT`
unset -- every machine that has not chosen a root -- nothing is read, nothing is
written, and the session keeps the ambient temporary directory exactly. There is
no default root and no path in this repository; the location is a property of
the machine.

**It cannot reach the product.** This file lives at the repository root, and
`pyproject.toml` builds from `src/` (`[tool.setuptools.packages.find] where =
["src"]`), so it is in no wheel, no sdist and no frozen bundle. `orchestrator`'s
own validation runner -- which runs pytest inside a *user's* project, with that
project as rootdir -- never collects this file and is unaffected by design.

`devenv.adopt_temp_root` explains why all three of `TEMP`, `TMP` and
`tempfile.tempdir` have to be set; `tests/test_devenv.py` proves the redirection
end to end through a real subprocess rather than asserting this comment.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import time

_DEVENV = Path(__file__).resolve().parent / "scripts" / "devenv.py"


def _load_devenv():
    """Import `scripts/devenv.py` by path, or `None` when it is not there.

    By path because this file is not in a package and `scripts/` is not on
    `sys.path`; `None` rather than an error for the reason `check.py` tolerates
    the same absence -- the redirection is an optimisation, and a suite that
    refuses to start because it could not find it would be a worse failure than
    the one it prevents.
    """
    if not _DEVENV.is_file():  # pragma: no cover - the file is tracked beside this one
        return None
    spec = importlib.util.spec_from_file_location("_modelquad_root_conftest_devenv", _DEVENV)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Applied at import, which is before pytest's `tmp_path_factory` resolves its
#: base directory, and empty on every machine that set no root.
_devenv = _load_devenv()
TEMP_ROOT = _devenv.adopt_temp_root() if _devenv is not None else ""


def pytest_sessionfinish(session, exitstatus):
    """Best-effort cleanup of opted-in leak prefixes, once per suite."""
    if (
        not TEMP_ROOT
        or hasattr(session.config, "workerinput")
        or os.environ.get("MODELQUAD_TEMP_PRUNE") == "0"
    ):
        return
    try:
        path = Path(__file__).resolve().parent / "scripts" / "prune_temp.py"
        spec = importlib.util.spec_from_file_location("_modelquad_temp_pruner", path)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.prune_temp_root(
            TEMP_ROOT, now=time.time(), min_age_seconds=module.TWO_DAYS,
            apply=True, time_budget_seconds=3,
        )
    except Exception:
        # Cleanup can never turn a passing suite into a failure.
        pass
