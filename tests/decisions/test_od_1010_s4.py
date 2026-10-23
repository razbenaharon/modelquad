# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""There is no Claude worker.

Claude is the host and a quota source; it is never launched as a worker. What
ships: the source tree, the packaged default.yaml, and the production registry
and execution manager; the expected values are literals.
"""

from pathlib import Path

import pytest

from orchestrator import registry
from orchestrator.config import load_config
from orchestrator.execution.manager import ExecutionManager
from orchestrator.service import db

SRC = Path(__file__).resolve().parents[2] / "src" / "orchestrator"


@pytest.fixture
def fresh_store(tmp_path):
    path = tmp_path / "history.db"
    db.connect_and_init(path).close()
    return path


def test_no_claude_executor_module_ships():
    for name in ("claude.py", "claude_readonly.py"):
        assert not (SRC / "execution" / name).exists()


@pytest.mark.parametrize("capability", ["delegate", "second_opinion"])
def test_packaged_config_registers_no_claude_executor(fresh_store, capability):
    config = load_config()
    assert registry.default_registry().find_runtime("claude").executor_factory is None
    manager = ExecutionManager(config, health_db_path=fresh_store)
    manager.capability, manager.launch_caller_host = capability, "codex"
    assert "claude" not in manager.executors


@pytest.mark.parametrize("capability", ["delegate", "second_opinion"])
def test_claude_is_never_a_delegation_candidate(capability):
    manager = ExecutionManager(load_config())
    manager.capability = capability
    assert manager._consultation_permitted("claude") is False
    assert manager._consultation_permitted("codex") is True
