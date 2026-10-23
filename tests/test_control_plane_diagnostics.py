# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""``doctor`` must be able to see the fault the product told the user to diagnose.

The observed sequence on a real Windows install, 2026-08-29:

1. The former installed Control Center refused to open and said, correctly, that the
   listener on its port was a ModelQuad *development* instance.
2. It then said "Run 'modelquad doctor' for diagnostics."
3. ``modelquad doctor`` said ``Overall: HEALTHY``.

Both statements were produced by the same product about the same machine in
the same minute. The identity probe was right; ``doctor`` simply had no
control-plane check at all, so the one condition the user had just been told
to investigate was the one condition the diagnostic could not observe.

The dialog had a second, separate defect: it stopped at the diagnosis. "Your
AI tools are unaffected. Run 'modelquad doctor'" is not a next step when
``doctor`` is blind, and even when it is not, it does not say what to *do*.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.config import Config
from orchestrator.service import control_plane as shell
from orchestrator.doctor import CheckStatus, _control_plane_check, run_doctor


CONTROL_PLANE = "Background service"


@pytest.mark.parametrize("code", ["service_owner_mismatch", "service_image_mismatch",
                                  "service_proof_invalid", "service_auth_unsupported"],
                         ids=["owner", "image", "proof", "legacy"])
def test_sec552_authentication_refusals_are_diagnostic_faults(code):
    from orchestrator.service.socket_identity import ListenerError, RESTART_MESSAGE
    exc = ListenerError(code)
    verdict = shell.ControlPlaneVerdict(shell.PROBE_MISMATCH, exc.detail, code)
    check = _control_plane_check(8765, probe=lambda port: verdict)
    assert check.status is CheckStatus.MISSING
    assert RESTART_MESSAGE in check.detail


def _verdict_for(payload: dict) -> shell.ControlPlaneVerdict:
    return shell.classify_control_plane_payload(payload)


def _healthy_installed_payload() -> dict:
    from orchestrator import statecontext

    state = statecontext.resolve_state_context()
    from orchestrator.service import db as service_db

    return {
        "status": "ok",
        "product": "modelquad",
        "service_role": "control-plane",
        "instance_kind": state.mode.value,
        "state_root": str(state.state_root),
        "db_schema_version": service_db.SCHEMA_VERSION,
        "version": "0.2.0",
    }


# ---------------------------------------------------------------------------
# doctor can now observe every listener case
# ---------------------------------------------------------------------------


def test_a_verified_control_plane_is_reported_healthy():
    check = _control_plane_check(8765, probe=lambda port: _verdict_for(_healthy_installed_payload()))

    assert check.status is CheckStatus.OK
    assert "8765" in check.detail


def test_no_listener_is_truthful_and_not_a_fault():
    """The service starts on demand, so "not running" must not read as damage --
    a check that cries wolf on every healthy machine is one users learn to skip."""

    check = _control_plane_check(
        8765, probe=lambda port: shell.ControlPlaneVerdict(shell.PROBE_ABSENT, "no listener")
    )

    assert check.status is CheckStatus.OK
    assert "not running" in check.detail.lower()


def test_a_development_instance_on_the_installed_port_is_reported_as_a_fault():
    """The exact observed condition. This is the assertion that would have
    failed before the fix, because there was no check to run."""

    payload = _healthy_installed_payload()
    payload["instance_kind"] = "development"
    payload["state_root"] = "C:\\Users\\someone\\.orchestrator"
    verdict = _verdict_for(payload)
    if verdict.state != shell.PROBE_MISMATCH:
        pytest.skip("this process resolves as development, so the payload is a legitimate match")

    check = _control_plane_check(8765, probe=lambda port: verdict)

    assert check.status is CheckStatus.MISSING


def test_a_state_root_mismatch_is_reported_as_a_fault():
    payload = _healthy_installed_payload()
    payload["state_root"] = "C:\\somewhere\\else\\entirely"
    verdict = _verdict_for(payload)
    if verdict.state != shell.PROBE_MISMATCH:
        pytest.skip("payload matched this process's own state root")

    check = _control_plane_check(8765, probe=lambda port: verdict)

    assert check.status is CheckStatus.MISSING
    assert "somewhere" in check.detail


def test_an_unrelated_listener_on_the_port_is_reported():
    verdict = _verdict_for({"status": "ok", "product": "someone-elses-app"})
    check = _control_plane_check(8765, probe=lambda port: verdict)

    assert check.status is CheckStatus.MISSING
    assert "someone-elses-app" in check.detail


def test_a_listener_that_does_not_speak_the_protocol_is_reported():
    check = _control_plane_check(
        8765,
        probe=lambda port: shell.ControlPlaneVerdict(
            shell.PROBE_MISMATCH,
            "listener did not speak the health protocol",
            shell.MISMATCH_PROTOCOL,
        ),
    )

    assert check.status is CheckStatus.MISSING


def test_a_probe_that_explodes_never_crashes_the_diagnostic():
    def boom(_port):
        raise RuntimeError("socket layer went away")

    check = _control_plane_check(8765, probe=boom)

    assert check.status is CheckStatus.WARN
    assert "socket layer went away" not in check.detail  # no raw exception text


# ---------------------------------------------------------------------------
# overall-health semantics
# ---------------------------------------------------------------------------


def test_doctor_is_not_healthy_overall_while_the_control_center_is_unusable(tmp_path):
    """The headline defect: ``Overall: HEALTHY`` about a product that will not open."""

    verdict = shell.ControlPlaneVerdict(
        shell.PROBE_MISMATCH,
        "listener is a ModelQuad development instance; the installed "
        "ModelQuad will not attach to it",
        shell.MISMATCH_DEVELOPMENT_INSTANCE,
    )
    report = run_doctor(
        Config(),
        usage_directory=tmp_path,
        which=lambda _: "/fake/cli",
        control_plane_probe=lambda port: verdict,
    )

    assert report.ok is False
    check = next(c for c in report.checks if c.name == CONTROL_PLANE)
    assert check.status is CheckStatus.MISSING


def test_doctor_reports_a_control_plane_check_at_all(tmp_path):
    """Guards the regression directly: the check must be wired into run_doctor,
    not merely exist."""

    report = run_doctor(
        Config(),
        usage_directory=tmp_path,
        which=lambda _: "/fake/cli",
        control_plane_probe=lambda port: shell.ControlPlaneVerdict(
            shell.PROBE_ABSENT, "no listener"
        ),
    )

    assert any(c.name == CONTROL_PLANE for c in report.checks)


def test_the_diagnostic_never_prints_the_whole_health_payload():
    """Only an allow-list of identity fields may reach user-visible output."""

    payload = _healthy_installed_payload()
    payload["instance_kind"] = "development"
    payload["session_token"] = "super-secret-value"
    # Built directly rather than classified, so the assertion holds regardless
    # of how the process running the test happens to resolve its own identity.
    verdict = shell.ControlPlaneVerdict(
        shell.PROBE_MISMATCH,
        "listener is a ModelQuad development instance",
        shell.MISMATCH_DEVELOPMENT_INSTANCE,
        payload,
    )

    check = _control_plane_check(8765, probe=lambda port: verdict)

    assert "instance_kind=development" in check.detail  # the useful field survives
    assert "super-secret-value" not in check.detail
    assert "session_token" not in check.detail


# ---------------------------------------------------------------------------
# the recovery is actionable, and safe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        shell.MISMATCH_DEVELOPMENT_INSTANCE,
        shell.MISMATCH_STATE_ROOT,
        shell.MISMATCH_STALE_VERSION,
        shell.MISMATCH_SCHEMA,
        shell.MISMATCH_UNIDENTIFIED,
        shell.MISMATCH_FOREIGN,
        shell.MISMATCH_PROTOCOL,
        shell.MISMATCH_ROLE,
    ],
)
def test_every_mismatch_reason_has_a_next_step(reason):
    """A correct diagnosis with no next step is the defect; there must be no
    branch that only names the problem."""

    advice = shell.recovery_advice(
        shell.ControlPlaneVerdict(shell.PROBE_MISMATCH, "detail", reason), 8765
    )

    assert advice.strip()
    assert advice.rstrip().endswith((".", "!"))
    assert "8765" in advice or "try again" in advice


def test_the_recovery_never_tells_the_user_to_kill_a_process():
    """The listener may be another developer's checkout mid-run. A product that
    answers a port conflict with 'end this process' is one typo from destroying
    work it does not own."""

    banned = ("kill", "taskkill", "end task", "task manager", "terminate", "pid ")
    for reason in (
        shell.MISMATCH_DEVELOPMENT_INSTANCE,
        shell.MISMATCH_STATE_ROOT,
        shell.MISMATCH_FOREIGN,
        shell.MISMATCH_STALE_VERSION,
        shell.MISMATCH_SCHEMA,
        shell.MISMATCH_UNIDENTIFIED,
        None,
    ):
        advice = shell.recovery_advice(
            shell.ControlPlaneVerdict(shell.PROBE_MISMATCH, "d", reason), 8765
        ).lower()
        assert not any(word in advice for word in banned), (reason, advice)


def test_the_recovery_only_names_commands_the_product_actually_has():
    """Naming a command that does not exist is the same misleading-instruction
    defect as pointing at a blind doctor."""

    from orchestrator.cli import build_parser

    parser = build_parser()
    for reason in (
        shell.MISMATCH_DEVELOPMENT_INSTANCE,
        shell.MISMATCH_STALE_VERSION,
        shell.MISMATCH_SCHEMA,
        None,
    ):
        advice = shell.recovery_advice(
            shell.ControlPlaneVerdict(shell.PROBE_MISMATCH, "d", reason), 8765
        )
        for fragment in ("service stop", "service start", "doctor"):
            if fragment in advice:
                parser.parse_args(fragment.split())  # raises SystemExit if unknown


#: The collisions where the listener belongs to ANOTHER installation, so this
#: installation's own service commands cannot resolve them.
_FOREIGN_INSTANCE_REASONS = (
    shell.MISMATCH_DEVELOPMENT_INSTANCE,
    shell.MISMATCH_STATE_ROOT,
    shell.MISMATCH_UNIDENTIFIED,
)


@pytest.mark.parametrize("reason", _FOREIGN_INSTANCE_REASONS)
def test_a_foreign_instance_is_not_answered_with_our_own_service_stop(reason):
    """``modelquad service stop`` stops the service named by THIS installation's
    pidfile. A development instance from another checkout has its own state
    root and its own pidfile, so the command would report success having
    stopped nothing and the port would still be busy.

    Recommending it is the same defect as pointing at a doctor that cannot see
    the fault: an instruction that cannot resolve the situation it is offered
    for. This test fails if that advice comes back.
    """

    advice = shell.recovery_advice(
        shell.ControlPlaneVerdict(shell.PROBE_MISMATCH, "d", reason), 8765
    )

    # It may TELL the user to run that command in the other checkout, but it
    # must never offer it as this copy's own remedy.
    assert "cannot stop another ModelQuad" in advice
    assert "from the ModelQuad that started it" in advice


@pytest.mark.parametrize("reason", _FOREIGN_INSTANCE_REASONS)
def test_a_foreign_instance_tells_the_user_where_to_act(reason):
    advice = shell.recovery_advice(
        shell.ControlPlaneVerdict(shell.PROBE_MISMATCH, "d", reason), 8765
    )

    assert "8765" in advice
    assert "try again" in advice.lower()


def test_a_stale_version_of_our_own_service_may_still_be_stopped_by_us():
    """The one collision that IS ours: same installation, same pidfile, an
    older process left behind by an in-place update."""

    advice = shell.recovery_advice(
        shell.ControlPlaneVerdict(shell.PROBE_MISMATCH, "d", shell.MISMATCH_STALE_VERSION),
        8765,
    )

    assert "service stop" in advice
    assert "cannot stop another ModelQuad" not in advice


def test_the_development_instance_case_names_the_actual_situation():
    advice = shell.recovery_advice(
        shell.ControlPlaneVerdict(
            shell.PROBE_MISMATCH, "d", shell.MISMATCH_DEVELOPMENT_INSTANCE
        ),
        8765,
    )

    assert "development" in advice.lower()
    assert "8765" in advice


def test_the_advice_and_doctor_agree_about_the_same_listener():
    """Two surfaces, one classification -- they cannot drift apart because they
    no longer each decide for themselves."""

    verdict = shell.ControlPlaneVerdict(
        shell.PROBE_MISMATCH, "listener detail", shell.MISMATCH_DEVELOPMENT_INSTANCE
    )
    check = _control_plane_check(8765, probe=lambda port: verdict)

    assert shell.recovery_advice(verdict, 8765) in check.detail
