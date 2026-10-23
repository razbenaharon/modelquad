# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Seed retired settings as an older install would, without using current setters."""

import json

from orchestrator.service import db


def save_legacy_mode(path, mode):
    assert mode in {"ask", "manual"}
    conn = db.connect_and_init(path)
    try:
        with conn:
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('routing_mode', ?)",
                         (json.dumps(mode),))
    finally:
        conn.close()
