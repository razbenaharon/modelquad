# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Service identity rules: is the listener on the port this installation's service?

Moved with the rules from the removed desktop shell into
``orchestrator.service.control_plane``.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator import statecontext
from orchestrator.service import control_plane


def _development_context(*_args, **_kwargs):
    """A DEVELOPMENT state context, independent of how this suite was installed.

    `control_plane.classify_control_plane_payload` compares a listener's `instance_kind` against the
    running process's own context, so a test that fixes the payload must fix
    the context too -- otherwise it asserts a property of the installation
    (source checkout vs non-editable install) rather than of the probe.
    """
    return statecontext.StateContext(
        mode=statecontext.StateMode.DEVELOPMENT,
        source=statecontext.StateSource.DEV_CHECKOUT,
        state_root=Path("state-root-for-test"),
        executable="python",
    )


def _probe_with_payload(monkeypatch, payload):
    """Probe a fake /api/health body.

    A dict payload with no ``state_root`` is stamped with this process's own, so
    each test exercises the axis it is about (product, role, kind, version)
    rather than tripping the identity check that now precedes them. Stamping
    happens HERE, after every caller's monkeypatches are in place, because
    some of them relocate the state root. The unstamped case -- a listener
    that cannot say which state it serves -- is covered directly in
    test_state_identity_probe.py.
    """

    import json as _json

    if isinstance(payload, dict) and "state_root" not in payload:
        from orchestrator.statecontext import resolve_state_context

        payload = {**payload, "state_root": str(resolve_state_context().state_root)}
    # Compatibility is deliberately tested independently of authentication.
    # A successful classifier never authorizes credential release.
    if isinstance(payload, bytes):
        try:
            payload = _json.loads(payload)
        except ValueError:
            return control_plane.PROBE_MISMATCH, "listener did not speak the health protocol"
    return control_plane._classify_health_payload(payload)


def test_probe_accepts_the_real_control_plane(monkeypatch):
    """A development listener is accepted by a DEVELOPMENT installation.

    The probe compares the listener's `instance_kind` against this process's
    own state context, so which answer is correct depends on how the code
    under test was installed. Pinned here: run from a source checkout the
    context is `development` and the payload matches, but CI installs the
    package non-editable, the context becomes `installed`, and the same
    payload is correctly rejected as a mismatch. Without the pin the test
    asserts a property of the installation rather than of the probe.
    """
    monkeypatch.setattr(statecontext, "resolve_state_context", _development_context)
    state, _ = _probe_with_payload(
        monkeypatch,
        {"status": "ok", "product": "modelquad", "service_role": "control-plane",
         "instance_kind": "development", "version": "0.2.0"},
    )
    assert state == control_plane.PROBE_READY


def test_probe_rejects_a_wrong_product(monkeypatch):
    state, detail = _probe_with_payload(
        monkeypatch, {"status": "ok", "product": "someone-elses-app"}
    )
    assert state == control_plane.PROBE_MISMATCH
    assert "someone-elses-app" in detail


def test_probe_rejects_an_unknown_service_role(monkeypatch):
    state, detail = _probe_with_payload(
        monkeypatch,
        {"status": "ok", "product": "modelquad", "service_role": "unknown-service"},
    )
    assert state == control_plane.PROBE_MISMATCH
    assert "unknown-service" in detail


def test_probe_rejects_a_pre_identity_modelquad_service(monkeypatch):
    """A <=0.2.0 service sends only status/version: it cannot prove which
    instance it is, so the probe reports what it found instead of guessing."""

    state, _ = _probe_with_payload(monkeypatch, {"status": "ok", "version": "n/a"})
    assert state == control_plane.PROBE_MISMATCH


def test_probe_rejects_non_json_listener(monkeypatch):
    state, detail = _probe_with_payload(monkeypatch, b"<html>totally not modelquad</html>")
    assert state == control_plane.PROBE_MISMATCH
    assert "protocol" in detail


def test_probe_treats_http_error_as_mismatch_not_ready(monkeypatch):
    from orchestrator.service import listener
    from orchestrator.service.socket_identity import ListenerError

    def raise_http_error(port):
        raise ListenerError("service_proof_invalid")

    monkeypatch.setattr(listener, "authenticate", raise_http_error)
    verdict = control_plane.probe_control_plane(8765)
    state, detail = verdict.state, verdict.detail
    assert state == control_plane.PROBE_MISMATCH
    assert "authenticated" in detail


def test_probe_treats_no_listener_as_absent(monkeypatch):
    from orchestrator.service import listener
    from orchestrator.service.socket_identity import ListenerError

    def raise_refused(port):
        raise ListenerError("service_absent")

    monkeypatch.setattr(listener, "authenticate", raise_refused)
    state = control_plane.probe_control_plane(8765).state
    assert state == control_plane.PROBE_ABSENT


def _installed_payload(version):
    return {
        "status": "ok",
        "product": "modelquad",
        "service_role": "control-plane",
        "instance_kind": "installed",
        "version": version,
    }


def test_a_listener_of_another_version_is_not_refused_for_it(monkeypatch):
    """A source checkout reports its own version differently and must stay
    usable; state identity and schema are what separate two instances."""

    state, _ = _probe_with_payload(monkeypatch, _installed_payload("0.3.0"))
    assert state == control_plane.PROBE_READY
