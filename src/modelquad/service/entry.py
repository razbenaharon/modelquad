# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Isolated source-mode service bootstrap, launched by absolute filename."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def _restore_installation(image: str) -> None:
    """Restore only the venv associated with the owning interpreter image.

    The product source root still comes solely from this bootstrap's file.
    Isolated mode keeps user-site and inherited PYTHONPATH out of the import
    environment; site handles the installation's own .pth files and policy.
    """
    import site
    from modelquad.executable_resolution import admit_executable

    admitted = admit_executable(image, allow_batch=False)
    if admitted.path is None:
        raise ValueError
    executable = Path(admitted.path)
    if executable.name.lower() not in ("python.exe", "pythonw.exe"):
        raise ValueError
    if executable.parent.name.lower() != "scripts" or not executable.is_file():
        raise ValueError
    root = executable.parent.parent
    # Match Python site's precedence so a nearer config cannot bypass the
    # base-image binding checked here.
    config_path = executable.parent / "pyvenv.cfg"
    if not config_path.is_file():
        config_path = root / "pyvenv.cfg"
    config = config_path.read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in config.splitlines() if "=" in line)
    values = {key.strip().lower(): value.strip() for key, value in values.items()}
    home = Path(values.get("home", ""))
    if not home.is_absolute() or not home.samefile(Path(sys._base_executable).parent):
        raise ValueError
    # -S defers site initialization until the owning image is known. Removing
    # old base sites also keeps direct in-process callers faithful to the
    # installation's include-system-site-packages policy.
    base_sites = {os.path.normcase(os.path.realpath(path)) for path in site.getsitepackages()}
    sys.path[:] = [path for path in sys.path if os.path.normcase(os.path.realpath(path)) not in base_sites]
    sys.executable = str(executable)
    sys.prefix = sys.exec_prefix = str(root)
    site.PREFIXES[:] = [sys.base_prefix, sys.base_exec_prefix]
    site.main()


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    installation = None
    if arguments[:1] == ["--installation-python"]:
        if len(arguments) < 2:
            return 1
        installation = arguments[1]
        arguments = arguments[2:]
    if not (arguments[:2] == ["service", "start"] or
            (arguments[:1] == ["serve"] and "--managed-child" in arguments)):
        return 1
    # Declare state before any product import computes defaults. No import
    # root comes from argv, cwd, environment or an editable-install redirect.
    for flag, variable in (("--state-root", "MODELQUAD_STATE_ROOT"),
                           ("--state-mode", "MODELQUAD_STATE_MODE")):
        if flag in arguments:
            index = arguments.index(flag)
            if arguments.count(flag) != 1 or index + 1 >= len(arguments):
                return 1
            value = arguments[index + 1]
            if flag == "--state-root" and not Path(value).is_absolute():
                return 1
            if flag == "--state-mode" and value not in ("installed", "development", "test"):
                return 1
            os.environ[variable] = value
    package_root = Path(__file__).resolve(strict=True).parents[2]
    sys.path.insert(0, str(package_root))
    from modelquad.windowless_diagnostics import bootstrap, bind_selected_state, emit, select_state
    bootstrap(force=True)
    # Includes argparse's --flag=value spelling before dependency restoration
    # can fail; an explicit state must never leave a log in the default state.
    if not select_state(arguments):
        return 1
    if installation is not None:
        try:
            _restore_installation(installation)
        except Exception:
            bind_selected_state("service")
            emit("entry")
            emit("startup_failed")
            emit("exit")
            return 1  # Dependency admission never echoes paths or exception prose.
    from modelquad.cli_entry import main as cli_main
    return cli_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
