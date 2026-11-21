# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``python -m modelquad``: the ``modelquad`` command without modelquad.exe.

``python -m modelquad update`` is how the update runs: pip cannot replace
modelquad.exe while it is the running program.
"""

from .cli_entry import main

if __name__ == "__main__":
    raise SystemExit(main())
