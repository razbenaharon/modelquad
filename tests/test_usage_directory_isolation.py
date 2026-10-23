# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Import-time usage defaults stay inside the suite's temporary directories."""

from orchestrator import cli, doctor
from orchestrator.mcp import server
from orchestrator.providers import ClaudeProvider, CodexProvider, base



def test_usage_default_aliases_and_provider_reads_are_isolated(isolate_usage_directory):
    directory = isolate_usage_directory
    for module in (base, cli, doctor, server):
        assert module.DEFAULT_USAGE_DIRECTORY == directory

    for provider_type in (ClaudeProvider, CodexProvider):
        provider = provider_type()
        assert provider.usage_file == directory / provider.filename
        status = provider.get_status()
        assert status.remaining_percentage is None
        assert status.metadata["source_file"] == str(provider.usage_file)
    assert not directory.exists()
