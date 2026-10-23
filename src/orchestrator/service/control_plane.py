# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Is the listener on ModelQuad's service port this installation's service?

The loopback control-plane service answers ``/api/health`` with identity
fields. Anything else on the port -- another program, a development instance
from another checkout, an older version left running after an update -- must
never be mistaken for it. These rules are shared by the authenticated
listener, the job runner's service client and ``doctor``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..branding import CLI_COMMAND

# The identity required before treating a listener as OUR service.
# "something answered on 127.0.0.1:8765" is not that: a random local app, an
# HTTP error page, or an incompatible instance must never masquerade as the
# ModelQuad service.
_EXPECTED_PRODUCT = "modelquad"
_EXPECTED_ROLE = "control-plane"

#: Probe outcomes: READY (verified ModelQuad control plane), ABSENT (nothing
#: listening), MISMATCH (a listener exists but is NOT the service this
#: installation should attach to).
PROBE_READY = "ready"
PROBE_ABSENT = "absent"
PROBE_MISMATCH = "mismatch"


#: Why a listener on the service port is not this installation's service.
#: Codes, not sentences: callers and ``doctor`` both have to act on the
#: distinction, and both reading it out of the human-readable detail
#: string would be two parsers of one prose format waiting to disagree.
MISMATCH_FOREIGN = "foreign_listener"
MISMATCH_PROTOCOL = "protocol"
MISMATCH_ROLE = "wrong_role"
MISMATCH_DEVELOPMENT_INSTANCE = "development_instance"
MISMATCH_STATE_ROOT = "state_root"
MISMATCH_UNIDENTIFIED = "unidentified"
MISMATCH_STALE_VERSION = "stale_version"
MISMATCH_SCHEMA = "schema"


@dataclass(frozen=True)
class ControlPlaneVerdict:
    """What is on the service port, and why that is or is not us.

    The old window shell worked this out correctly and then threw all but one
    sentence of it away, which is why the product could tell a user "listener
    is a ModelQuad development instance" and, in the same breath, tell them to
    run a ``doctor`` that reported ``Overall: HEALTHY``. Keeping the verdict
    structured lets every caller and the diagnostic derive their answers from
    one classification instead of two.
    """

    state: str
    detail: str
    reason: str | None = None
    payload: dict | None = None

    @property
    def ready(self) -> bool:
        return self.state == PROBE_READY

    @property
    def is_modelquad(self) -> bool:
        """True when the listener at least identifies itself as this product."""

        return bool(self.payload) and self.payload.get("product") == _EXPECTED_PRODUCT

    def identity(self) -> dict[str, str]:
        """The safe identity fields, for a diagnostic to print.

        Deliberately a fixed allow-list rather than the whole payload: this is
        rendered into user-visible output, and a future health field must not
        become a leak by being added upstream.
        """

        payload = self.payload or {}
        fields = ("instance_kind", "state_root", "state_root_id", "version", "service_role")
        return {k: str(payload[k]) for k in fields if payload.get(k) is not None}


def _classify_health_payload(payload: dict) -> tuple[str, str]:
    """The (state, detail) pair of :func:`classify_control_plane_payload`."""

    verdict = classify_control_plane_payload(payload)
    return verdict.state, verdict.detail


def probe_control_plane(port: int) -> ControlPlaneVerdict:
    """Authenticate the connection, then expose bounded compatibility facts."""
    from .listener import authenticate
    from .socket_identity import ListenerError
    try:
        with authenticate(port) as connection:
            verdict = classify_control_plane_payload(connection.health)
            return verdict
    except ListenerError as exc:
        state = PROBE_ABSENT if exc.code == "service_absent" else PROBE_MISMATCH
        return ControlPlaneVerdict(state, exc.detail, exc.code)



def classify_control_plane_payload(payload: dict) -> ControlPlaneVerdict:
    """Classify compatibility fields; this function never authenticates a peer.

    Split out of the transport so the identity rules can be exercised
    directly. The incident these rules exist for was not a network failure:
    every field the shell checked matched, and the one field that would have
    exposed the split was simply absent.
    """

    product = payload.get("product")
    role = payload.get("service_role")
    if product != _EXPECTED_PRODUCT:
        # Pre-identity ModelQuad services (<= 0.2.0) sent only status/version.
        # They ARE ModelQuad, but cannot prove which instance -- treat as a
        # mismatch rather than guessing; the user is told what was found.
        return ControlPlaneVerdict(PROBE_MISMATCH, f"listener identifies as product={product!r}", MISMATCH_FOREIGN, payload)
    if role != _EXPECTED_ROLE:
        return ControlPlaneVerdict(PROBE_MISMATCH, f"listener is a ModelQuad {role or 'unknown'} instance, not the control plane", MISMATCH_ROLE, payload)
    from pathlib import Path

    from ..statecontext import resolve_state_context
    from . import db as _service_db

    own_state = resolve_state_context()
    if own_state.is_installed and payload.get("instance_kind") != "installed":
        return ControlPlaneVerdict(PROBE_MISMATCH, (
            "listener is a ModelQuad development instance; the installed "
            "ModelQuad will not attach to it"
        ), MISMATCH_DEVELOPMENT_INSTANCE, payload)
    # The identity checks above ask "same product, same role, same kind". None
    # of them catches two INSTALLED instances resolving different state roots,
    # which is exactly the split this workstream exists to end. A service
    # old enough not to publish the field is not penalised -- absent evidence
    # reads unknown, never a confident mismatch (AGENTS.md, invariant 4).
    listener_root = payload.get("state_root")
    listener_root_id = payload.get("state_root_id")
    if isinstance(listener_root, str) and listener_root:
        if Path(listener_root) != own_state.state_root:
            return ControlPlaneVerdict(PROBE_MISMATCH, (
                f"listener serves ModelQuad state at {listener_root}, but this "
                f"ModelQuad owns {own_state.state_root}"
            ), MISMATCH_STATE_ROOT, payload)
    elif isinstance(listener_root_id, str) and listener_root_id:
        from .api import state_identity_tag

        if listener_root_id != state_identity_tag(own_state.state_root):
            return ControlPlaneVerdict(PROBE_MISMATCH, (
                "listener serves a different ModelQuad state store "
                f"(id {listener_root_id}) than this ModelQuad owns"
            ), MISMATCH_STATE_ROOT, payload)
    else:
        # A listener that cannot say WHICH state it serves is exactly the
        # shape that let a stale instance pass for the current one: every
        # cheap field matched, so days-old data was shown as though it
        # were live. Absent identity is treated as unproven, not
        # as agreement -- the one place "unknown" must not read as "fine",
        # because the whole purpose of this probe is to establish identity.
        return ControlPlaneVerdict(PROBE_MISMATCH, (
            "listener does not publish which ModelQuad state it serves "
            "(pre-identity instance); it cannot be proven to be this "
            "ModelQuad's service"
        ), MISMATCH_UNIDENTIFIED, payload)
    listener_schema = payload.get("db_schema_version")
    own_schema = _service_db.SCHEMA_VERSION
    if isinstance(listener_schema, int) and listener_schema != own_schema:
        # Two builds can carry one version string mid-development while
        # already disagreeing about the store's shape. The schema is the
        # blunter, harder-to-fake answer to "same product state?".
        return ControlPlaneVerdict(PROBE_MISMATCH, (
            f"listener serves database schema v{listener_schema}, but this "
            f"ModelQuad expects v{own_schema}"
        ), MISMATCH_SCHEMA, payload)
    return ControlPlaneVerdict(PROBE_READY, "verified", None, payload)


def recovery_advice(verdict: ControlPlaneVerdict, port: int) -> str:
    """The safest next action for this exact failure, in one or two sentences.

    The gap this closes: the old window diagnosed the collision correctly and
    then stopped. "listener is a ModelQuad development instance" is true, precise,
    and leaves a non-technical user with nowhere to go -- and the one
    instruction that WAS offered, ``modelquad doctor``, could not see the
    condition at all.

    What it deliberately never says: kill PID N, or end a process by name.
    The listener may be another developer checkout mid-run, and a product that
    answers a port conflict by telling users to terminate processes is one
    typo away from destroying work it does not own. Every branch below either
    names a ModelQuad-owned command that can actually resolve THAT situation,
    or asks the user to act in the environment that owns the other instance --
    and never offers a command that would report success having done nothing.
    """

    if verdict.reason and verdict.reason.startswith("service_") and verdict.reason != "service_absent":
        from .socket_identity import RESTART_MESSAGE
        return RESTART_MESSAGE

    # Deliberately NOT "run 'modelquad service stop'" for the foreign-instance
    # cases below. ``ServiceProcessManager`` stops the service named by THIS
    # installation's own pidfile; a development instance from another checkout
    # has its own state root and its own pidfile, so the command would report
    # success having stopped nothing and the port would still be busy.
    # Recommending it is the same defect as pointing at a doctor that cannot
    # see the fault. No product-owned command can safely stop another
    # installation's service, so the advice says so plainly and hands the
    # action to whoever owns that environment.
    foreign_hint = (
        "Stop it from the ModelQuad that started it -- run "
        "'modelquad service stop' in that checkout -- then try "
        "again. This copy cannot stop another ModelQuad's service for you."
    )
    if verdict.reason == MISMATCH_DEVELOPMENT_INSTANCE:
        return (
            f"A ModelQuad development instance from another checkout is using "
            f"port {port}. {foreign_hint}"
        )
    if verdict.reason == MISMATCH_STATE_ROOT:
        return (
            f"Another ModelQuad is using port {port} with a different data "
            f"store, so this one will not attach to it. {foreign_hint}"
        )
    if verdict.reason == MISMATCH_STALE_VERSION:
        # This one IS ours: same installation, same pidfile, an older process
        # left behind by an in-place update -- so our own command can stop it.
        return (
            f"A previous version of ModelQuad is still running on port {port} "
            f"after an update. Stop it with '{CLI_COMMAND} service stop', then "
            f"try again."
        )
    if verdict.reason == MISMATCH_SCHEMA:
        return (
            f"The ModelQuad running on port {port} expects a different data "
            f"format than this one. If it belongs to this installation, stop it "
            f"with '{CLI_COMMAND} service stop'; otherwise stop it from the "
            f"ModelQuad that started it. Then try again."
        )
    if verdict.reason == MISMATCH_UNIDENTIFIED:
        return (
            f"An older ModelQuad on port {port} cannot prove which data store "
            f"it serves, so this one will not attach to it. {foreign_hint}"
        )
    if verdict.reason in (MISMATCH_FOREIGN, MISMATCH_PROTOCOL, MISMATCH_ROLE):
        return (
            f"Another program on this computer is already using port {port}. "
            f"Close it and try again."
        )
    if verdict.state == PROBE_ABSENT:
        return (
            f"ModelQuad could not start its background service. Run "
            f"'{CLI_COMMAND} service start' to see why it did not start."
        )
    return f"Run '{CLI_COMMAND} doctor' to see what is wrong."


