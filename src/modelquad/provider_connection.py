# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The four separate facts behind the word "connected", and one next step.

A normal Beta user asks one question -- *is this tool working with ModelQuad?*
-- but the product must not answer it with one boolean, because the honest
answer has four independent parts and they fail independently:

1. **Installed.** Is the provider's CLI on this machine, and how do we know?
2. **Signed in.** Has the user authenticated with the vendor? Tri-state:
   UNKNOWN is a real, valid answer here and is never dressed up as a failure.
3. **Registered.** Does ModelQuad's integration exist in that host's config?
4. **Canonical.** Does that registration name *this* installation's runtime,
   or some other copy of ModelQuad? (See :mod:`modelquad.hostruntime` for
   what conflating those two cost the product once already.)

Collapsing them loses the only information the user can act on. "Installed but
not signed in" is not "Connected". "Registered with ModelQuad but pointing at a
different installation" is not "Ready". Each state below maps to exactly one
action, which is what makes a first-run screen actionable instead of merely
colourful.

Why this module reads nothing it does not have to
-------------------------------------------------

Sign-in is established by probing for the *existence* of a provider-owned
credential artefact. The contents are never opened, parsed, logged or
persisted -- those files are named as audited refusals in
:data:`modelquad.hostobs.CODEX_SOURCES_REFUSED`, and nothing here
weakens that. Existence is the strongest signal obtainable without either
reading a secret or spawning the vendor's CLI, and this module does neither:
it performs no network I/O and starts no subprocess.

The consequence is stated rather than hidden: a present credential file proves
credentials were stored, never that the token is still valid. Expiry is not
observable to a static check, and the product does not claim otherwise.

The asymmetry is the point
--------------------------

The supported providers do **not** expose equivalent signals, and pretending
otherwise would be the most tempting mistake in this module. Verified against
the live CLIs on 2026-08-27:

===============  ============================  =====================================
Provider         Sign-in command               Why absence means what it means
===============  ============================  =====================================
``claude``       ``claude auth``               Credential file on Windows/Linux, but
                                               macOS uses the system Keychain, so on
                                               any non-Windows platform absence is
                                               UNKNOWN, not signed-out.
``codex``        ``codex login`` / ``logout``   An explicit, documented login pair, so
                                               absence really is signed-out -- unless
                                               the API-key environment path is used.
===============  ============================  =====================================

A provider with no observable signal reports UNKNOWN. That is not an omission
to be tidied up later; it is the honest answer, and ``AGENTS.md`` safety
invariant 4 requires it.

Language
--------

``headline``, ``next_step`` and :attr:`SignInEvidence.summary` are normal-user
copy and are covered by a test that bans internal vocabulary outright (MCP,
JSON, SQLite, PATH, adapter, registration, state root, file paths, ...). The
``detail`` fields are the developer half and may be technical.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import sys
from typing import Any

from . import hostruntime
from .registry import ProviderPlugin, ProviderRegistry, default_registry


class InstallState(str, Enum):
    """Whether the provider's own CLI is present, and how that was established."""

    INSTALLED_ON_PATH = "installed_on_path"
    #: On disk in the vendor's documented directory, but the vendor's installer
    #: did not wire it into PATH. Observed for all three Beta providers.
    INSTALLED_OFF_PATH = "installed_off_path"
    NOT_INSTALLED = "not_installed"
    #: The provider has no CLI concept at all (it is configured with an API
    #: key). Distinct from "not installed", which would send the user looking
    #: for an installer that does not exist.
    NOT_APPLICABLE = "not_applicable"


class SignInState(str, Enum):
    """Whether the user has authenticated with the vendor.

    ``UNKNOWN`` is a first-class answer, not a degraded one. It means no
    signal exists that a static, credential-respecting check can read.
    """

    SIGNED_IN = "signed_in"
    SIGNED_OUT = "signed_out"
    UNKNOWN = "unknown"


class IntegrationState(str, Enum):
    """Whether one host launches ModelQuad, and whether it launches *this* one."""

    CANONICAL = "canonical"
    #: Registered, but naming another installation or a bare name whose
    #: resolution this product does not control. Repairable.
    WRONG_RUNTIME = "wrong_runtime"
    NOT_REGISTERED = "not_registered"
    #: The host's configuration could not be inspected. Never an exception.
    UNKNOWN = "unknown"


class ConnectionState(str, Enum):
    """The single state a normal-user row shows, derived from the four facts."""

    READY = "ready"
    NEEDS_SIGN_IN = "needs_sign_in"
    NEEDS_PATH_FIX = "needs_path_fix"
    NEEDS_REPAIR = "needs_repair"
    NEEDS_CONNECT = "needs_connect"
    NOT_INSTALLED = "not_installed"
    UNKNOWN = "unknown"


#: Machine-readable action ids. A surface maps these to one button each; the
#: user is never shown more than one next step per provider.
ACTION_INSTALL = "install"
ACTION_FIX_PATH = "fix_path"
ACTION_SIGN_IN = "sign_in"
ACTION_CONNECT = "connect"
ACTION_REPAIR = "repair"


#: Normal-user headlines. Fixed strings so every surface says the same words,
#: and so the language guard has something to
#: assert against.
HEADLINE_READY = "Connected"
HEADLINE_NEEDS_SIGN_IN = "Needs sign-in"
HEADLINE_NEEDS_PATH_FIX = "Installed, but Windows cannot find it yet"
HEADLINE_NEEDS_REPAIR = "Needs repair"
HEADLINE_NEEDS_CONNECT = "Not connected yet"
HEADLINE_NOT_INSTALLED = "Not installed"
HEADLINE_UNKNOWN = "Status unknown"


@dataclass(frozen=True)
class SignInEvidence:
    """What we can and cannot say about the user being signed in to a provider."""

    state: SignInState
    #: Normal-user copy. No paths, no internal vocabulary.
    summary: str
    #: Developer detail. May name the *kind* of artefact, never its contents.
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "summary": self.summary,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class HostIntegration:
    """What one host is configured to launch, judged against this installation."""

    host_id: str
    display_name: str
    state: IntegrationState
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_id": self.host_id,
            "display_name": self.display_name,
            "state": self.state.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ProviderConnection:
    """The four facts for one provider, plus the one thing to do about them."""

    provider_id: str
    display_name: str
    install: InstallState
    install_path: str | None
    sign_in: SignInEvidence
    integrations: tuple[HostIntegration, ...]
    state: ConnectionState
    headline: str
    next_step: str
    action: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "install": self.install.value,
            "install_path": self.install_path,
            "sign_in": self.sign_in.to_dict(),
            "integrations": [item.to_dict() for item in self.integrations],
            "state": self.state.value,
            "headline": self.headline,
            "next_step": self.next_step,
            "action": self.action,
        }


# --------------------------------------------------------------------------
# Sign-in probes, one per provider, audited independently.
# --------------------------------------------------------------------------


def _claude_sign_in(home: Path) -> SignInEvidence:
    """Claude Code keeps an OAuth credential file -- except on macOS.

    The platform split is the whole reason this is not shared with Codex:
    macOS Claude Code stores its credentials in the system Keychain, which a
    file probe cannot see. Reporting "signed out" there would be a confident
    false statement about the user's own machine, so absence degrades to
    UNKNOWN on every platform except Windows, where the file is authoritative.
    """

    if _exists(home / ".claude" / ".credentials.json"):
        return SignInEvidence(
            SignInState.SIGNED_IN,
            "Signed in on this computer.",
            "A Claude Code credential file is present (never opened or read).",
        )
    if sys.platform == "win32":
        return SignInEvidence(
            SignInState.SIGNED_OUT,
            "You are not signed in to Claude yet.",
            "No Claude Code credential file on a platform that always uses one.",
        )
    return SignInEvidence(
        SignInState.UNKNOWN,
        "ModelQuad cannot tell whether you are signed in to Claude.",
        "No credential file, but this platform may store credentials in the system keychain.",
    )


def _codex_sign_in(home: Path) -> SignInEvidence:
    """Codex is the one provider where absence is genuinely conclusive.

    It ships an explicit ``codex login`` / ``codex logout`` pair, and logout
    removes the credential file, so "no file" really does mean "signed out" --
    unless the user is on the API-key path instead, which is a different and
    equally valid way to be authenticated. The environment variable is tested
    for truthiness only; its value is never read into any emitted string.
    """

    if _exists(home / ".codex" / "auth.json"):
        return SignInEvidence(
            SignInState.SIGNED_IN,
            "Signed in on this computer.",
            "A Codex credential file is present (never opened or read).",
        )
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return SignInEvidence(
            SignInState.SIGNED_IN,
            "Signed in using an API key.",
            "An OpenAI API key environment variable is set; its value is never read.",
        )
    return SignInEvidence(
        SignInState.SIGNED_OUT,
        "You are not signed in to Codex yet.",
        "No Codex credential file and no API key environment variable.",
    )


#: Per-provider sign-in probes, keyed by provider id. A provider with no entry
#: honestly yields UNKNOWN rather than borrowing another provider's semantics.
SIGN_IN_PROBES: dict[str, Callable[[Path], SignInEvidence]] = {
    "claude": _claude_sign_in,
    "codex": _codex_sign_in,
}


def _exists(path: Path) -> bool:
    """Existence only. The file is never opened."""

    try:
        return path.is_file()
    except OSError:
        return False


def _no_cli_evidence(plugin: ProviderPlugin) -> SignInEvidence:
    """A provider with no CLI is configured with a key, not installed and signed in."""

    if plugin.credential_env_vars:
        return SignInEvidence(
            SignInState.UNKNOWN,
            f"{plugin.display_name} is set up with an API key rather than a sign-in.",
            "Provider declares credential environment variables; values are never read.",
        )
    return SignInEvidence(
        SignInState.UNKNOWN,
        f"ModelQuad cannot tell whether {plugin.display_name} is set up.",
        "Provider declares no CLI and no credential environment variables.",
    )


def probe_sign_in(provider_id: str, *, home: Path | None = None) -> SignInEvidence:
    """Sign-in evidence for one provider, without reading any credential."""

    resolved_home = Path.home() if home is None else Path(home)
    probe = SIGN_IN_PROBES.get(provider_id)
    if probe is None:
        return SignInEvidence(
            SignInState.UNKNOWN,
            "ModelQuad cannot tell whether this tool is signed in.",
            f"No sign-in probe is defined for provider {provider_id!r}.",
        )
    return probe(resolved_home)


# --------------------------------------------------------------------------
# Integration inspection.
# --------------------------------------------------------------------------


def _inspect_integrations(
    plugin: ProviderPlugin,
    *,
    which: Callable[[str], str | None],
) -> tuple[HostIntegration, ...]:
    """Judge every host of one plugin. Never raises; failure is a state."""

    from .hostruntime import classify_registration, read_registered_command

    results: list[HostIntegration] = []
    for host in plugin.host_adapters:
        try:
            target_dir = host.default_target_dir()
            command = read_registered_command(host.id, target_dir)
        except (OSError, ValueError) as exc:
            results.append(
                HostIntegration(
                    host.id,
                    host.display_name,
                    IntegrationState.UNKNOWN,
                    f"Could not inspect this host's configuration: {exc}",
                )
            )
            continue

        if command is None:
            results.append(
                HostIntegration(
                    host.id,
                    host.display_name,
                    IntegrationState.NOT_REGISTERED,
                    "No ModelQuad server is registered with this host.",
                )
            )
            continue

        try:
            verdict = classify_registration(command, which=which)
        except (OSError, ValueError) as exc:
            results.append(
                HostIntegration(
                    host.id,
                    host.display_name,
                    IntegrationState.UNKNOWN,
                    f"Could not judge the recorded launch command: {exc}",
                )
            )
            continue

        if verdict.canonical:
            results.append(
                HostIntegration(
                    host.id,
                    host.display_name,
                    IntegrationState.CANONICAL,
                    f"Launches {verdict.resolved or verdict.command}.",
                )
            )
        else:
            results.append(
                HostIntegration(
                    host.id,
                    host.display_name,
                    IntegrationState.WRONG_RUNTIME,
                    verdict.reason,
                )
            )
    return tuple(results)


# --------------------------------------------------------------------------
# Deriving the one state and the one next step.
# --------------------------------------------------------------------------


def _derive(
    plugin: ProviderPlugin,
    install: InstallState,
    sign_in: SignInEvidence,
    integrations: tuple[HostIntegration, ...],
    install_path: str | None = None,
) -> tuple[ConnectionState, str, str, str | None]:
    """Collapse the four facts into (state, headline, next_step, action).

    Ordered most-actionable-first, so a provider that is both uninstalled and
    unregistered is told to install rather than shown two problems it cannot
    solve in that order.

    Sign-in UNKNOWN is deliberately *not* a blocker: it is absence of a
    signal, not evidence of a failure, and treating it as one would put a
    permanent false alarm next to such a provider on every machine.
    """

    name = plugin.display_name

    if install is InstallState.NOT_INSTALLED:
        return (
            ConnectionState.NOT_INSTALLED,
            HEADLINE_NOT_INSTALLED,
            f"{name} is not on this computer yet. ModelQuad can show you how to get it, "
            "then check again.",
            ACTION_INSTALL,
        )

    from .codexcli import is_desktop_version_path

    directly_usable = (
        plugin.id == "codex"
        and install_path is not None
        and is_desktop_version_path(install_path)
    )
    if install is InstallState.INSTALLED_OFF_PATH and not directly_usable:
        return (
            ConnectionState.NEEDS_PATH_FIX,
            HEADLINE_NEEDS_PATH_FIX,
            f"{name} is installed, but Windows cannot find it yet. ModelQuad can fix that "
            "for you.",
            ACTION_FIX_PATH,
        )

    if sign_in.state is SignInState.SIGNED_OUT:
        return (
            ConnectionState.NEEDS_SIGN_IN,
            HEADLINE_NEEDS_SIGN_IN,
            f"Sign in to {name}, then let ModelQuad check again.",
            ACTION_SIGN_IN,
        )

    if any(item.state is IntegrationState.WRONG_RUNTIME for item in integrations):
        return (
            ConnectionState.NEEDS_REPAIR,
            HEADLINE_NEEDS_REPAIR,
            f"{name} is set up to use a different copy of ModelQuad. ModelQuad can fix "
            "this for you.",
            ACTION_REPAIR,
        )

    if any(item.state is IntegrationState.NOT_REGISTERED for item in integrations):
        return (
            ConnectionState.NEEDS_CONNECT,
            HEADLINE_NEEDS_CONNECT,
            f"ModelQuad has not been added to {name} yet. Connecting takes a moment and "
            "changes nothing else about how you use it.",
            ACTION_CONNECT,
        )

    if integrations and all(item.state is IntegrationState.UNKNOWN for item in integrations):
        return (
            ConnectionState.UNKNOWN,
            HEADLINE_UNKNOWN,
            f"ModelQuad could not check {name} just now. Try checking again.",
            None,
        )

    if install is InstallState.NOT_APPLICABLE and not integrations:
        return (
            ConnectionState.UNKNOWN,
            HEADLINE_UNKNOWN,
            f"{name} is set up separately and ModelQuad does not manage it here.",
            None,
        )

    return (
        ConnectionState.READY,
        HEADLINE_READY,
        f"Nothing to do. Keep using {name} as you normally would.",
        None,
    )


def describe_connections(
    *,
    which: Callable[[str], str | None] | None = None,
    registry: ProviderRegistry | None = None,
    home: Path | None = None,
    selected_providers: Sequence[str] | None = None,
) -> tuple[ProviderConnection, ...]:
    """The connection truth for every registered provider.

    ``home`` is injectable so tests -- and the isolated install proof -- never
    probe the real user's home directory. ``which`` is injectable for the same
    reason ``doctor`` injects it: the real PATH is a property of the machine,
    not of the test.
    """

    from .onboarding import detect_providers

    which = which if which is not None else hostruntime.discovery_which
    active_registry = registry or default_registry()
    resolved_home = Path.home() if home is None else Path(home)
    detections = {
        detection.provider_id: detection
        for detection in detect_providers(which=which, registry=active_registry)
    }

    plugins = active_registry.all()
    known_ids = tuple(plugin.id for plugin in plugins)
    if selected_providers is not None:
        for provider_id in selected_providers:
            if provider_id not in known_ids:
                raise ValueError(
                    f"Unknown provider {provider_id!r}; valid choices are: {', '.join(known_ids)}"
                )
        wanted = set(selected_providers)
        plugins = tuple(plugin for plugin in plugins if plugin.id in wanted)

    connections: list[ProviderConnection] = []
    for plugin in plugins:
        detection = detections.get(plugin.id)

        if plugin.cli_executable is None:
            install = InstallState.NOT_APPLICABLE
            install_path = None
            sign_in = _no_cli_evidence(plugin)
        else:
            if detection is None or not detection.cli_found:
                install = InstallState.NOT_INSTALLED
            elif detection.on_path:
                install = InstallState.INSTALLED_ON_PATH
            else:
                install = InstallState.INSTALLED_OFF_PATH
            install_path = detection.cli_path if detection is not None else None
            sign_in = probe_sign_in(plugin.id, home=resolved_home)

        integrations = _inspect_integrations(plugin, which=which)
        state, headline, next_step, action = _derive(
            plugin, install, sign_in, integrations, install_path,
        )

        connections.append(
            ProviderConnection(
                provider_id=plugin.id,
                display_name=plugin.display_name,
                install=install,
                install_path=install_path,
                sign_in=sign_in,
                integrations=integrations,
                state=state,
                headline=headline,
                next_step=next_step,
                action=action,
            )
        )

    return tuple(connections)


def needs_attention(connections: Sequence[ProviderConnection]) -> tuple[ProviderConnection, ...]:
    """The providers a first-run screen should surface first.

    "Does anything need attention" outranks every
    other question *when it is true*, and a healthy system must not manufacture
    an alert to fill the space. UNKNOWN is not attention-worthy.
    """

    actionable = {
        ConnectionState.NOT_INSTALLED,
        ConnectionState.NEEDS_PATH_FIX,
        ConnectionState.NEEDS_SIGN_IN,
        ConnectionState.NEEDS_REPAIR,
        ConnectionState.NEEDS_CONNECT,
    }
    return tuple(item for item in connections if item.state in actionable)
