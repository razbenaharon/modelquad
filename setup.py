# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The build's one addition to pyproject.toml: start from an empty build/lib.

setuptools copies the package into ``build/lib`` and makes the wheel from
whatever is there. It never empties that folder, so a checkout that built an
earlier version puts modules that no longer exist in ``src/`` into the new
wheel (``tests/test_wheel_packaging.py``). Everything else about the package
is declared in pyproject.toml.
"""

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class build_py_from_empty(build_py):
    def run(self):
        # Not ignore_errors: a leftover that cannot be removed must stop the
        # build, not ship.
        if Path(self.build_lib).is_dir():
            shutil.rmtree(self.build_lib)
        super().run()


setup(cmdclass={"build_py": build_py_from_empty})
