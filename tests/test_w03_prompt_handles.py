# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""W03a: status reads cannot add provider I/O before the bounded handoff.

Synthetic service reservation and collectors only; no installed provider runs.
U1/U8 input parity and recording belong to W03b.
"""

from datetime import datetime, timedelta, timezone
import os
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from conftest import no_window_kwargs
from test_mcp_jobs import reserve_only, synthetic  # shared isolated DB/service seam
from orchestrator.config import Config
from orchestrator.mcp import server as mcp
from orchestrator.models import Freshness, ProviderStatus
from orchestrator.providers import CodexProvider
from orchestrator.providers import base
from orchestrator.router import Router
from orchestrator.registry import default_registry
from orchestrator.service import health
from orchestrator.service import history
from orchestrator.usage import codex_appserver, refresh
from orchestrator import usage
from orchestrator.usage.base import write_usage_snapshot


NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
OLD = NOW - timedelta(minutes=20)
REAL_REFRESH_PROVIDER = refresh.refresh_provider


class FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is not None else NOW.replace(tzinfo=None)


@pytest.fixture
def sources(tmp_path, monkeypatch):
    directory = tmp_path / "usage $ Hebrew ×¢×‘×¨×™×ª"
    directory.mkdir()
    monkeypatch.setattr(base, "DEFAULT_USAGE_DIRECTORY", directory)
    providers = (CodexProvider(now=lambda: NOW),)
    monkeypatch.setattr(mcp, "load_config", lambda *a: Config())
    monkeypatch.setattr(mcp, "build_providers", lambda *a, **kw: providers)
    monkeypatch.setattr(mcp, "cached_catalog", lambda *a: None)
    monkeypatch.setattr(mcp, "datetime", FrozenClock)
    monkeypatch.setattr(health, "_now", lambda: NOW)
    return directory, providers


def stale_snapshots(directory):
    target = directory / CodexProvider.filename
    write_usage_snapshot(target, {
        "available": True, "observed_at": OLD.isoformat(),
        "collected_at": OLD.isoformat(), "effective_remaining_percentage": 2,
        "five_hour_remaining_percentage": 2,
        "seven_day_remaining_percentage": 2,
        "source": codex_appserver.CODEX_APPSERVER_SOURCE,
    })
    os.utime(target, (OLD.timestamp(), OLD.timestamp()))


@pytest.fixture
def probes(monkeypatch):
    calls = []

    def record(kind):
        calls.append((kind, refresh._refresh_deadline.get() is not None))

    def decision_refresh(name, directory, *, now):
        record("refresh_provider:" + name)
        return refresh.RefreshResult(
            provider=name, refresh_kind=refresh.UNAVAILABLE, available=True,
            collected_at=now().isoformat(), error="synthetic unavailable probe",
        )

    def run(*a, **kw):
        record("subprocess.run")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def popen(*a, **kw):
        record("subprocess.Popen")
        raise AssertionError("a synthetic test must never start a provider process")

    monkeypatch.setattr(refresh, "refresh_provider", decision_refresh)
    # The collector references the standard-library subprocess module, so
    # these patches are process-wide. They fail closed on any subprocess call
    # on the tested path, even a future non-provider call such as git.
    monkeypatch.setattr(codex_appserver.subprocess, "run", run)
    monkeypatch.setattr(codex_appserver.subprocess, "Popen", popen)
    return calls


@pytest.mark.parametrize("snapshot", ["missing_snapshot", "stale_snapshot"],
                         ids=["missing_snapshot", "stale_snapshot"])
def test_route_and_initial_status_never_probe_provider(
    synthetic, sources, probes, tmp_path, monkeypatch, snapshot,
):
    directory, providers = sources
    if snapshot == "stale_snapshot":
        stale_snapshots(directory)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    statuses = tuple(provider.get_status() for provider in providers)
    assert {status.freshness for status in statuses} == {
        Freshness.UNKNOWN if snapshot == "missing_snapshot" else Freshness.STALE,
    }
    assert probes == []
    Router(Config()).route("implementation", statuses, now=NOW)
    assert probes == []
    captured = []
    reserve_only(monkeypatch, captured)
    try:
        payload = mcp._delegate_payload(
            "review", "quick", str(tmp_path), provider="codex", timeout=30,
            usage_directory=str(directory), invocation_mode="explicit_user_request",
            user_approved=True,
        )
        assert payload["job_id"] == captured[0][1].job_id
        # Quota is display-only: a delegation never starts or waits for a
        # quota read, whether the reading is missing or stale.
        assert probes == []
        assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    finally:
        for _, ref, _ in captured:
            history.fail_job_attempt(synthetic, ref.job_id, detail="synthetic_cleanup")


@pytest.mark.parametrize("snapshot", ["missing_snapshot", "stale_snapshot"],
                         ids=["missing_snapshot", "stale_snapshot"])
def test_cli_route_never_refreshes(synthetic, sources, probes, monkeypatch, snapshot, capsys):
    import json
    from orchestrator import cli

    directory, providers = sources
    if snapshot == "stale_snapshot":
        stale_snapshots(directory)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    monkeypatch.setattr(cli, "build_providers", lambda *a, **kw: providers)
    monkeypatch.setattr(cli, "load_config", lambda *a: Config())
    assert cli.main([
        "--history-db", str(synthetic), "--no-history", "route", "implementation", "--json",
    ]) == 0
    assert "recommendation" in json.loads(capsys.readouterr().out)
    assert probes == []
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


@pytest.mark.parametrize("refresh_status", [False, True], ids=["snapshot", "refresh"])
def test_status_helper_accepts_plugin_without_refresh(refresh_status):
    class PluginProvider(base.Provider):
        name = "plugin"

        def get_status(self):
            return ProviderStatus(name=self.name, available=True)

    assert base.read_status(PluginProvider(), refresh=refresh_status).name == "plugin"


@pytest.mark.parametrize("caller", [
    "poll", "status", "cli",
], ids=["poll", "status", "cli"])
def test_status_callers_read_without_probing(synthetic, sources, probes, monkeypatch, caller):
    from orchestrator import cli
    from orchestrator.service import collector

    directory, providers = sources
    if caller == "poll":
        monkeypatch.setattr(collector, "build_providers", lambda *a, **kw: providers)
        monkeypatch.setattr(collector, "run_doctor", lambda *a, **kw: object())
        monkeypatch.setattr(collector.history, "record_provider_health", lambda *a: None)
        collector.poll_once(Config(), directory, synthetic)
    elif caller == "status":
        mcp.modelquad_status()
    else:
        monkeypatch.setattr(cli, "build_providers", lambda *a, **kw: providers)
        monkeypatch.setattr(cli, "load_config", lambda *a: Config())
        assert cli.main(["--history-db", str(synthetic), "--no-history", "status", "--json"]) == 0
    assert probes == []
