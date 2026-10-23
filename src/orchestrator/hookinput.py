# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Reading a host hook's JSON input; shared by the light entry and the CLI."""

from __future__ import annotations

import json
import sys


def read_hook_payload() -> object:
    """Read a host's JSON from stdin as bytes, never through the ANSI code page.

    A text-mode stdin decodes with the console code page on Windows, so a
    UTF-8 payload carrying a non-ASCII path (Chinese, Japanese, Hebrew) broke
    parsing; ``json.loads`` detects UTF-8 itself from the raw bytes.
    """

    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read() if stream is not None else b""
    return json.loads(raw or b"{}")
