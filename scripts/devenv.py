#!/usr/bin/env python
# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Where a development or test child process puts its temporary files.

Stdlib-only and dependency-free, for the same reason `check.py` is: every
path that uses it must work in a fresh worktree with nothing installed. It is
development tooling, not the product, so nothing here may import
`modelquad`.

**The problem.** Windows Defender's real-time scanner is a measurable cost on
this repository's test runs, and profiling after the Dev Drive migration said
where what is left of it comes from: the source tree moved off `C:`, but
pytest's temporary trees did not. `tmp_path`, `tmp_path_factory` and every
`tempfile` call resolve through `tempfile.gettempdir()`, which on Windows reads
`TEMP`/`TMP` -- so a suite launched from a Dev Drive checkout still writes
`pytest-of-<user>` into the user's `%LOCALAPPDATA%` temporary directory -- on
the scanned system volume, thousands
of files per run.

**The mechanism, and its three deliberate limits.**

1. **Opt-in, and only ever opt-in.** With `MODELQUAD_TEMP_ROOT` unset -- the
   state of every machine that has not chosen this -- `apply_temp_root` is a
   no-op and the child inherits the ambient `TEMP`/`TMP` exactly. There is no
   default root, and no path constant: the location is a property of the
   machine, so the machine states it and this repository contains no drive
   letter.

2. **Development children only, never a process that did not ask.**
   `apply_temp_root` writes only the mapping it is handed. A developer's shell,
   the interactive session, and the product runtime all keep the temporary
   directory Windows gave them; what changes is the environment *handed to* a
   subprocess this repository's development tooling launches. Redirecting a
   process that never opted in would make the change outlive the harness that
   asked for it.

   `adopt_temp_root` is the deliberate second case, and the only place this
   module writes `os.environ`: a process that *is* the development child and
   was handed an environment by nothing -- a bare `python -m pytest`, typed by
   hand or launched by an agent, with no `check.py` above it to build one. The
   repository-root `conftest.py` is its one caller, so the write lands inside a
   pytest session, on that session's own copy of the environment, and dies with
   it. Nothing persistent is touched: `os.environ` is process-local, and no
   Windows user or machine environment variable is written by this module or by
   anything that calls it.

3. **Not `--basetemp`.** pytest's `--basetemp` moves `tmp_path` and *empties
   the directory it is given* at the start of each run, which makes it unsafe
   to point at anything shared and covers only the fixtures. `TEMP`/`TMP` cover
   every `tempfile` consumer in the child process -- pytest's fixtures, the
   suite's own `mkdtemp` calls, and whatever a tool underneath does -- and
   preserve pytest's own retention policy (it keeps failed-test diagnostics
   from the last three runs and garbage-collects older ones itself). Reuse the
   machine's root across runs: a fresh per-run root defeats that retention.
   If isolation requires a separate root, the caller removes it at run end.
   `scripts/prune_temp.py` previews or removes old leftovers; the root pytest
   conftest also prunes known leak prefixes at session finish, with a two-day
   age floor and a three-second budget. `MODELQUAD_TEMP_PRUNE=0` disables that
   automatic pass. Per-run roots are pruned only by the manual command.

The variable is read from the environment being *built*, not from `os.environ`,
so a caller that passes an explicit base gets a decision derived from that base
and the function stays a pure mapping-to-mapping transform under test.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

#: The machine states this; the repository never defaults it. Set it to a
#: directory on the volume you want development and test scratch files on --
#: on Windows that means one excluded from Defender's real-time scan, which a
#: Dev Drive is by default.
TEMP_ROOT_VAR = "MODELQUAD_TEMP_ROOT"

#: Every name `tempfile.gettempdir()` consults, in the order it consults them.
#: All three are written, and `TMPDIR` is not optional: it is checked *first*,
#: on every platform including Windows. Leaving it alone was the original
#: reasoning -- "Windows does not set it" -- and that is true of Windows itself
#: and false of the shells run on top of it. A Git Bash or MSYS environment that
#: exports `TMPDIR` would silently win over both names below, and the
#: redirection would appear to be applied while every temporary file kept
#: landing on the ambient volume. A silent no-op is the exact failure this
#: module exists to remove, so the variable that could cause one is set rather
#: than trusted to be absent.
TEMP_VARS = ("TMPDIR", "TEMP", "TMP")


def resolved_temp_root(env: dict | None = None) -> str:
    """The absolute temp root `env` asks for, or `""` when it asks for none.

    Absolute, because `TEMP` is resolved by each child against *its own*
    working directory, and the development tooling launches children in several
    (`REPO_ROOT`, a worktree, `dashboard/`). A relative root would scatter.
    """
    source = os.environ if env is None else env
    raw = str(source.get(TEMP_ROOT_VAR) or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).expanduser().resolve())
    except (OSError, ValueError, RuntimeError):  # pragma: no cover - defensive
        # `RuntimeError` is what `expanduser()` raises when a `~` path has no
        # home directory to resolve against. It is the one failure here that
        # is not an `OSError`, and letting it out would abort a conftest
        # import, a `check.py` run or a launched session -- crashing on a
        # misconfigured root instead of degrading to the ambient directory,
        # which is the single promise every failure path in this module keeps.
        return ""


def usable_temp_root(root: str) -> bool:
    """True when `root` exists *and* this process can create a file in it.

    `mkdir(parents=True, exist_ok=True)` proves the directory is there, which is
    a different fact and not the one that matters. An existing directory with a
    denying ACL, a read-only mount, or a full volume all satisfy it and then
    fail on the first `mkdtemp` -- and because `adopt_temp_root` assigns
    `tempfile.tempdir` directly, it also bypasses `tempfile`'s own search for a
    working candidate. A present-but-unwritable root would therefore abort the
    run instead of degrading, which is exactly what `apply_temp_root` promises
    not to do.

    So the check is the operation itself. It creates a *directory* rather than a
    file, because that is what the consumers actually need: pytest builds
    `pytest-of-<user>/pytest-N/` under this root, and a Windows ACL can grant
    file creation while denying it for subdirectories. `mkdtemp` proves both --
    it can only succeed if the root is writable -- so it is the stronger probe
    and the shorter code.

    Cleanup runs in `finally` and is swallowed, for two different reasons that
    both matter: a removal failure does not change the answer (the create
    already succeeded, which was the question), and putting it anywhere else
    leaks the probe directory when the removal path is the one that raises.
    """
    probe = ""
    try:
        Path(root).mkdir(parents=True, exist_ok=True)
        probe = tempfile.mkdtemp(dir=root, prefix=".devenv-probe-")
    except (OSError, ValueError):
        return False
    finally:
        if probe:
            try:
                os.rmdir(probe)
            except OSError:  # pragma: no cover - the create succeeded; cleanup is best effort
                pass
    return True


def apply_temp_root(env: dict) -> str:
    """Point `env`'s `TEMP`/`TMP` at `MODELQUAD_TEMP_ROOT`, in place.

    Returns the root applied, or `""` when nothing was applied -- so a caller
    that reports its environment (`check.py python` does) can say which root a
    run actually used rather than which one it intended.

    Failure to *use* the root is not fatal. The redirection is an
    optimisation; an unmounted drive or a revoked permission must degrade to
    the ambient temporary directory and let the tests run, not take the harness
    down with it. The caller learns nothing was applied from the return value.
    """
    root = resolved_temp_root(env)
    if not root or not usable_temp_root(root):
        return ""
    for name in TEMP_VARS:
        env[name] = root
    return root


def adopt_temp_root(env: dict | None = None, *, tempfile_module=None) -> str:
    """Point the *running* process at the root, and return it (or `""`).

    The in-process counterpart to `apply_temp_root`, for the one caller that is
    itself the child: a bare `python -m pytest`. `check.py` and the release
    pipeline each build an environment and hand it to a subprocess;
    a pytest run started by hand has no such parent, and until this existed it
    was the single largest producer of temporary files still landing on the
    ambient volume.

    Three names are set, and all three are needed:

    * `TEMP` and `TMP`, so every *subprocess* the suite spawns -- an xdist
      worker, a `git`, a throwaway interpreter -- inherits the same root.
    * `tempfile.tempdir`, because `tempfile.gettempdir()` memoises its answer
      on first use and consults `TEMP`/`TMP` only to compute it. A session that
      touched a temporary file before this ran would otherwise keep the cached
      ambient directory no matter what the environment now says, and pytest's
      `tmp_path_factory` resolves its base through exactly that call.

    Absent or unusable, nothing at all is written -- not the environment, not
    the cache -- and the process keeps the ambient directory byte for byte.
    `tempfile_module` is an injection point for the tests, which must assert
    the cache is written without redirecting the suite running them.
    """
    target = os.environ if env is None else env
    root = apply_temp_root(target)
    if not root:
        return ""
    (tempfile if tempfile_module is None else tempfile_module).tempdir = root
    return root
