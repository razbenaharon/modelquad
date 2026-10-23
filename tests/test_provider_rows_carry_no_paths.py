# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Provider usage and health rows keep no absolute path.

The history database promises a project folder's name and never a full path.
Two writers broke it: a usage reading carried the full path of the quota file
it came from (``source_file``), and the provider health row stored the doctor's
"Found at <path>" lines. Both paths include the Windows user name.
"""

from __future__ import annotations

import json
import sqlite3

from orchestrator.doctor import CheckStatus, DoctorCheck, DoctorReport
from orchestrator.models import Freshness, ProviderStatus, ResourcePool
from orchestrator.service import history

QUOTA_FILE = "C:\\Users\\example\\.claude\\usage-codex.json"
CODEX_EXE = "C:\\Users\\example\\AppData\\Roaming\\npm\\codex.cmd"


def _rows(db_path, sql):
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[0] for row in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def test_a_usage_reading_stores_no_quota_file_path(tmp_path):
    db_path = tmp_path / "orchestrator.db"
    # Sub-pool rows are written only when a reading has more than one pool.
    pools = tuple(
        ResourcePool(
            id=f"codex-{window}",
            provider="codex",
            remaining_percentage=percent,
            freshness=Freshness.FRESH,
            raw={"source_file": QUOTA_FILE, "window": window},
        )
        for window, percent in (("5h", 70.0), ("weekly", 40.0))
    )
    status = ProviderStatus(
        name="codex",
        remaining_percentage=40.0,
        freshness=Freshness.FRESH,
        metadata={"source_file": QUOTA_FILE, "plan": "pro"},
        pools=pools,
    )

    history.record_provider_usage(db_path, (status,))

    stored = _rows(db_path, "SELECT metadata_json FROM provider_usage_history")
    assert len(stored) == 3, stored  # the headline row and two pool rows
    for text in stored:
        assert "Users" not in text and "example" not in text, text
        json.loads(text)  # still valid JSON after redaction
    assert any(json.loads(text).get("plan") == "pro" for text in stored)


def test_a_health_row_stores_no_found_at_path(tmp_path):
    db_path = tmp_path / "orchestrator.db"
    report = DoctorReport(checks=(
        DoctorCheck("codex CLI", CheckStatus.OK, "Found at " + CODEX_EXE),
        DoctorCheck("codex adapter installed", CheckStatus.OK, "Installed at C:\\Users\\example\\.codex"),
    ))

    history.record_provider_health(db_path, report, provider_names=("codex",))

    details = _rows(db_path, "SELECT detail FROM provider_health")
    assert details == ["Found at <path> | Installed at <path>"]
