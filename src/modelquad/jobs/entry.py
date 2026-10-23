# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Direct-base runner bootstrap preserving an admitted Windows venv.

Launched with -I -S: neither cwd nor PYTHONPATH supplies an import root, and
base .pth files do not run before the owning venv has been verified. Actual
runner containment/startup/admission remain in run_job, without exemptions.
"""
from __future__ import annotations

from pathlib import Path
import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] != ['--installation-python'] or len(arguments) < 2:
        return 1
    installation, arguments = arguments[1], arguments[2:]
    sys.path.insert(0, str(Path(__file__).resolve(strict=True).parents[2]))
    from modelquad.service.entry import _restore_installation
    try:
        _restore_installation(installation)
    except Exception:
        # No dependency bypass or raw error. An unreported runner remains
        # subject to the existing bounded startup-expiry closure.
        return 1
    from modelquad.jobs.runner import main as runner_main
    return runner_main(arguments)


if __name__ == '__main__':
    raise SystemExit(main())
