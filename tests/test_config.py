# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from modelquad.config import Config, ProviderConfig, load_config


def test_packaged_default_config_loads():
    config = load_config()

    assert config.freshness.fresh_seconds == 300
    assert set(config.task_fit) == {
        "architecture",
        "implementation",
        "exploration",
        "debugging",
        "testing",
        "review",
        "trivial",
    }
    assert set(config.providers) == {"claude", "codex"}
    assert set(config.task_fit["implementation"]) == {"claude", "codex"}


def test_user_override_merges_with_defaults(tmp_path):
    override = tmp_path / "routing.yaml"
    override.write_text(
        "freshness:\n  fresh_seconds: 42\ntask_fit:\n  implementation:\n    codex: 0.99\n",
        encoding="utf-8",
    )

    config = load_config(override)

    assert config.freshness.fresh_seconds == 42
    assert config.freshness.stale_seconds == 3600
    assert config.task_fit["implementation"]["codex"] == 0.99
    assert config.task_fit["implementation"]["claude"] == 0.70
    # A legacy override without a providers section leaves every provider on
    # its plugin default: no provider entry is added.
    assert config.providers == {}


def test_provider_config_section_parses_enabled_and_free_form_options(tmp_path):
    override = tmp_path / "providers.yaml"
    override.write_text(
        "providers:\n  codex:\n    enabled: false\n    execution:\n      type: api\n  future:\n    endpoint: local\n",
        encoding="utf-8",
    )

    config = load_config(override)

    assert config.providers["codex"] == ProviderConfig(
        enabled=False, options={"execution": {"type": "api"}}
    )
    assert config.providers["future"] == ProviderConfig(enabled=True, options={"endpoint": "local"})
    assert config.providers["claude"].enabled is True

