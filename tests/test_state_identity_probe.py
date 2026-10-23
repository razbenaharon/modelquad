# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Regression cover for the service's state identity (health and its probe).

These tests exist because of a real incident: a frozen 0.2.0 Control Center
was serving its own state store on the default port while development ran
against a different one. Every cheap identity field matched, so the window
rendered three-day-old rows and a stale routing mode as though they were
live. Nothing in the product said otherwise.

The lesson shapes what is tested here. Agreement between the native surface
and the MCP surface is asserted by making them share one derivation, not by
writing the same expected value on both sides -- a mock-only test would have
passed happily throughout the incident.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from modelquad.service import db as service_db
from modelquad.service.api import state_identity_tag
from modelquad.service.app import create_app



_OPEN_CLIENTS: list[TestClient] = []


@pytest.fixture(autouse=True)
def _close_clients():
    """Shut each client's lifespan down after its test."""

    yield
    while _OPEN_CLIENTS:
        try:
            _OPEN_CLIENTS.pop().__exit__(None, None, None)
        except Exception:  # noqa: BLE001 -- teardown must not mask a failure
            pass


def _client(tmp_path, **kwargs):
    """An entered TestClient: ``app.state.db_path`` is set by the lifespan."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    usage_dir = tmp_path / "usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    db_path = kwargs.pop("db_path", tmp_path / "modelquad.db")
    kwargs.setdefault("poll_providers", False)
    app = create_app(db_path=db_path, usage_directory=usage_dir, poll_interval=3600.0, **kwargs)
    client = TestClient(app, base_url="http://127.0.0.1")
    client.__enter__()
    _OPEN_CLIENTS.append(client)
    return client, db_path


def test_state_identity_tag_is_stable_and_leaks_no_path(tmp_path):
    """The tag identifies a store without carrying the path that names it."""

    root = tmp_path / "StateRoot"
    root.mkdir()
    tag = state_identity_tag(root)

    assert tag == state_identity_tag(root), "identity must be stable across calls"
    assert len(tag) == 12 and all(c in "0123456789abcdef" for c in tag)
    # Redaction: the username and the absolute path must not survive.
    assert "StateRoot" not in tag
    assert str(root) not in tag
    assert tag != state_identity_tag(tmp_path / "Other")


def test_health_publishes_comparable_runtime_identity(tmp_path):
    client, _ = _client(tmp_path, write_secret="health-secret")
    payload = client.get("/api/health", headers={"X-ModelQuad-Token": "health-secret"}).json()

    for field in (
        "product",
        "service_role",
        "instance_kind",
        "state_root_id",
        "state_source",
        "version",
        "db_schema_version",
    ):
        assert payload.get(field), f"health must publish {field} for identity comparison"


def test_foreign_state_identity_is_detected_not_silently_accepted():
    """A listener that cannot prove which state it serves is a mismatch.

    This is the exact 0.2.0 shape: right product, right role, right kind,
    no state identity. It previously probed READY.
    """

    from modelquad.service import control_plane as shell

    pre_identity = {
        "status": "ok",
        "product": "modelquad",
        "service_role": "control-plane",
        "instance_kind": "installed",
        "version": "0.2.0",
        "db_schema_version": 15,
    }
    verdict, detail = shell._classify_health_payload(pre_identity)
    assert verdict == shell.PROBE_MISMATCH
    assert "state" in detail.lower()


def _pin_context(monkeypatch, state_root):
    """Pin the shell's own state context, independent of how this suite ran.

    Without the pin these tests assert a property of the developer's
    installation (source checkout vs non-editable install) rather than of
    the probe -- the same trap documented in test_desktop_shell.py.
    """

    from modelquad import statecontext

    context = statecontext.StateContext(
        mode=statecontext.StateMode.DEVELOPMENT,
        source=statecontext.StateSource.DEV_CHECKOUT,
        state_root=Path(state_root),
        executable="python",
    )
    monkeypatch.setattr(statecontext, "resolve_state_context", lambda *a, **k: context)
    return context


def _healthy_payload(**overrides):
    from modelquad.branding import installed_version

    payload = {
        "status": "ok",
        "product": "modelquad",
        "service_role": "control-plane",
        "instance_kind": "development",
        "state_root": "shared-state-root",
        "version": installed_version("n/a"),
        "db_schema_version": service_db.SCHEMA_VERSION,
    }
    payload.update(overrides)
    return payload


def test_matching_identity_still_probes_ready(monkeypatch):
    """The guard must not make the normal, matching case noisy."""

    from modelquad.service import control_plane as shell

    _pin_context(monkeypatch, "shared-state-root")
    verdict, _ = shell._classify_health_payload(_healthy_payload())
    assert verdict == shell.PROBE_READY


def test_a_different_state_root_is_the_incident_shape_and_is_rejected(monkeypatch):
    """Published identity that DISAGREES is the split, caught explicitly."""

    from modelquad.service import control_plane as shell

    _pin_context(monkeypatch, "development-state-root")
    verdict, detail = shell._classify_health_payload(
        _healthy_payload(state_root="installed-state-root")
    )
    assert verdict == shell.PROBE_MISMATCH
    assert "installed-state-root" in detail


def test_a_different_schema_version_is_rejected(monkeypatch):
    """Two builds can share a version string while disagreeing about the store."""

    from modelquad.service import control_plane as shell

    _pin_context(monkeypatch, "shared-state-root")
    verdict, detail = shell._classify_health_payload(
        _healthy_payload(db_schema_version=service_db.SCHEMA_VERSION - 1)
    )
    assert verdict == shell.PROBE_MISMATCH
    assert "schema" in detail.lower()
