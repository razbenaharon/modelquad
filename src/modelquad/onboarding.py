# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""First-run onboarding wizard for the multi-agent modelquad.

Provides UI-agnostic detection, host adapter installation, routing mode
configuration, and post-install diagnostics verification.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from . import hostruntime
from .host_integrations import OFF_DETAIL, global_integrations_enabled
from .executable_resolution import admit_executable
from .adapters.registration import HostRegistrationOutcome, RegistrationStatus
from .adapters.registry import CLIENT_NAMES, get_host_descriptor
from .branding import CLI_COMMAND, PRODUCT_NAME
from .config import load_config
from .doctor import CheckStatus, DoctorReport, run_doctor
from .registry import (
    HostAdapterDescriptor,
    ProviderInstallInfo,
    ProviderPlugin,
    ProviderRegistry,
    default_registry,
)
from .service import db as service_db
from .service.history import (
    DEFAULT_ROUTING_MODE,
    VALID_ROUTING_MODES,
    put_routing_mode,
    routing_mode_if_set,
)


@dataclass(frozen=True)
class ProviderDetection:
    """Detection state for one provider plugin and its host integrations."""

    provider_id: str
    display_name: str
    cli_found: bool
    cli_path: str | None
    hosts: tuple[str, ...]
    #: Every *detected* host adapter for this provider reports itself
    #: installed. This is an adapter-installation fact and nothing more --
    #: it is NOT "connected". A Codex AGENTS.md managed block satisfies it on
    #: a machine with no Codex CLI at all, which is exactly how this field,
    #: under its old name ``already_connected``, came to print "Connected"
    #: next to a tool that was not installed. Whether a provider is connected
    #: is `provider_connection.describe_connections`' answer, never this one.
    adapter_installed: bool
    # False when the CLI was found only by looking in the vendor's documented
    # install directory, i.e. it is installed but the vendor's installer did
    # not wire PATH. The product can then offer to fix PATH instead of
    # claiming -- falsely -- that the tool is not installed.
    on_path: bool = True
    install: "ProviderInstallInfo | None" = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "cli_found": self.cli_found,
            "cli_path": self.cli_path,
            "hosts": list(self.hosts),
            "adapter_installed": self.adapter_installed,
            "on_path": self.on_path,
            "install": self.install.to_dict() if self.install is not None else None,
        }


def _find_in_install_dirs(plugin: "ProviderPlugin") -> Path | None:
    """Look for a provider's CLI in the vendor's documented install directories.

    Only consulted after PATH discovery has already failed. Returns the
    executable's path, or None. Never raises: a probe of a directory that does
    not exist, or that the process cannot read, simply means "not there".
    """

    info = getattr(plugin, "install", None)
    if info is None or plugin.cli_executable is None:
        return None

    names = [plugin.cli_executable]
    if os.name == "nt":
        names = [f"{plugin.cli_executable}{ext}" for ext in (".exe", ".cmd", ".bat")] + names

    for directory_factory in info.extra_search_dirs:
        try:
            directory = Path(directory_factory())
        except (OSError, ValueError):
            continue
        if not directory.is_absolute():
            continue
        for name in names:
            try:
                candidate = directory / name
                # Admission is a gate, preserving vendor directory spelling.
                # Presence admits batch filenames; discovery never launches them.
                if admit_executable(candidate, allow_batch=True).path is not None:
                    return candidate
            except OSError:
                continue
    return None


def detect_providers(
    *,
    which: Callable[[str], str | None] | None = None,
    registry: ProviderRegistry | None = None,
) -> tuple[ProviderDetection, ...]:
    """Inspect all registered providers and their host adapters."""

    which = which if which is not None else hostruntime.discovery_which
    active_registry = registry or default_registry()
    detections: list[ProviderDetection] = []

    for plugin in active_registry.all():
        cli_found = False
        cli_path = None
        on_path = True
        if plugin.cli_executable is not None:
            try:
                resolved = which(plugin.cli_executable)
                if resolved:
                    cli_found = True
                    cli_path = str(resolved)
            except OSError:
                cli_found = False
                cli_path = None

            if not cli_found:
                # PATH is not the whole truth. A vendor installer can finish
                # successfully without adding itself to PATH (Claude Code's
                # official Windows installer does exactly that, and tells the
                # user to edit System Properties by hand). Reporting "not
                # installed" there is a false statement about the user's own
                # machine, and it disables the setup checkbox for a tool they
                # do have -- so fall back to the vendor's documented install
                # directories before concluding anything.
                if plugin.id == "codex" and plugin.cli_executable == "codex":
                    from .codexcli import is_desktop_version_path, official_candidates, resolve_codex_cli

                    # Use execution's version choice and stat-cached probe,
                    # rather than pinning the first desktop version directory.
                    chosen = resolve_codex_cli(which=lambda _: None)
                    found = Path(chosen.path) if chosen is not None else None
                    if found is None:
                        # An unreadable version does not mean the install is
                        # absent. Prefer a stable folder for this install fact.
                        # Official candidates are .exe files, with no batch need.
                        # Keep the original spelling for reports and PATH repair.
                        candidates = [
                            path for path in official_candidates()
                            if admit_executable(path, allow_batch=False).path is not None
                        ]
                        found = next(
                            (path for path in candidates if not is_desktop_version_path(path)),
                            next(iter(candidates), None),
                        )
                else:
                    found = _find_in_install_dirs(plugin)
                if found is not None:
                    cli_found = True
                    cli_path = str(found)
                    on_path = False

        host_ids = tuple(host.id for host in plugin.host_adapters)
        detected_installed: list[bool] = []

        for host in plugin.host_adapters:
            try:
                detected = True if host.detect is None else bool(host.detect())
            except (OSError, ValueError):
                detected = False

            if detected:
                try:
                    target_dir = host.default_target_dir()
                    installed = bool(host.adapter_cls().is_installed(target_dir))
                except (OSError, ValueError):
                    installed = False
                detected_installed.append(installed)

        adapter_installed = bool(detected_installed) and all(detected_installed)

        detections.append(
            ProviderDetection(
                provider_id=plugin.id,
                display_name=plugin.display_name,
                cli_found=cli_found,
                cli_path=cli_path,
                hosts=host_ids,
                adapter_installed=adapter_installed,
                on_path=on_path,
                install=plugin.install,
            )
        )

    return tuple(detections)


def _find_host_descriptor(client: str, registry: ProviderRegistry | None = None) -> HostAdapterDescriptor:
    active_reg = registry or default_registry()
    for plugin in active_reg.all():
        for host in plugin.host_adapters:
            if host.id == client:
                return host
    choices = ", ".join(CLIENT_NAMES)
    raise ValueError(f"Unknown adapter client {client!r}; choose one of: {choices}")


def install_detected_hosts(
    client_ids: Sequence[str],
    *,
    dry_run: bool = False,
    registry: ProviderRegistry | None = None,
) -> list[HostRegistrationOutcome]:
    """Install adapters for the specified clients if detected on this machine.

    Returns one :class:`HostRegistrationOutcome` per client rather than the
    ``{"detected": ..., "error": ...}`` dict it used to. That dict could say
    only "it worked", so onboarding rendered a fresh registration, an already
    identical one, and the *replacement of a different pre-existing ModelQuad
    registration* with one identical sentence. The registration was read
    before and after the adapter ran on a machine where the third case was
    live, and nothing in the result could tell the three apart.

    The before/after reads go through ``hostruntime``, which already owns the
    single interpretation of where each host records ModelQuad, instead of
    each adapter inventing its own notion of "what was there before".

    A dry run writes nothing, so its before/after readings are always equal;
    its outcome comes from the adapter's own report of what it WOULD do to
    the registration file instead. Otherwise a dry run that would replace a
    different registration said "already connected".
    """

    from .adapters.registration import (
        BackupPersistenceError,
        classify_registration_change,
        install_with_registration,
        restore_supported,
    )
    from .hostruntime import (
        read_registered_command,
        read_registered_entry,
        registration_is_readable,
    )

    if not global_integrations_enabled():
        return [HostRegistrationOutcome(
            host=client, status=RegistrationStatus.REGISTRATION_SKIPPED,
            error=OFF_DETAIL, dry_run=dry_run,
        ) for client in client_ids]

    results: list[HostRegistrationOutcome] = []
    for client in client_ids:
        try:
            descriptor = _find_host_descriptor(client, registry=registry)
            detected = True if descriptor.detect is None else bool(descriptor.detect())
        except (OSError, ValueError) as exc:
            results.append(
                HostRegistrationOutcome(
                    host=client,
                    status=RegistrationStatus.NOT_DETECTED,
                    error=str(exc),
                    dry_run=dry_run,
                )
            )
            continue

        if not detected:
            results.append(
                HostRegistrationOutcome(
                    host=client,
                    status=RegistrationStatus.NOT_DETECTED,
                    dry_run=dry_run,
                )
            )
            continue

        target_dir = descriptor.default_target_dir()
        before_entry = read_registered_entry(client, target_dir)
        before_command = read_registered_command(client, target_dir)
        had_prior = before_entry is not None or before_command is not None
        can_restore = restore_supported(client, target_dir)

        def _failed(error: str) -> HostRegistrationOutcome:
            return HostRegistrationOutcome(
                host=client,
                status=RegistrationStatus.FAILED,
                previous_registration_present=had_prior,
                previous_command=before_command,
                restore_supported=can_restore,
                error=error,
                dry_run=dry_run,
            )

        try:
            report = install_with_registration(
                client, target_dir, descriptor.adapter_cls(), dry_run=dry_run
            )
        except (OSError, ValueError, BackupPersistenceError) as exc:
            results.append(_failed(str(exc)))
            continue

        after_entry = read_registered_entry(client, target_dir)
        after_command = read_registered_command(client, target_dir)
        status = classify_registration_change(
            before_entry=before_entry,
            before_command=before_command,
            after_entry=after_entry,
            after_command=after_command,
        )
        if dry_run and registration_is_readable(client):
            status, after_command = _dry_run_registration_status(
                client, target_dir, report, had_prior=had_prior, before_command=before_command
            )
        elif status is None and (dry_run or not registration_is_readable(client)):
            # Two cases where "nothing is registered afterwards" is not
            # evidence of a failed registration, and must not be reported as
            # one:
            #
            # * a dry run wrote nothing ON PURPOSE, so an empty result is the
            #   successful rehearsal, not a failure;
            # * a host this module has no reader for -- absence of a reading
            #   is not a reading of absence (``AGENTS.md`` invariant 4), and
            #   only such a host's own adapter report can speak for it.
            #
            # Both fall back to the historical before/after comparison, which
            # is what these two cases have always been judged by.
            status = (
                RegistrationStatus.CONNECTED_UNCHANGED
                if had_prior
                else RegistrationStatus.CONNECTED_NEW
            )
        if status is None:
            # The adapter returned without raising and there is still no
            # ModelQuad registration on disk. That is NOT a connection, and
            # reporting it as one is the defect this branch exists to close:
            # a malformed Codex config.toml produced "Codex CLI can reach
            # ModelQuad." directly above a detail saying it was not
            # registered, and the overall onboarding result said ok.
            #
            # Which of the two honest answers it is comes from the host's
            # config parsing structurally, not from matching the adapter's
            # prose -- and the *wording* the user sees comes from the
            # adapter's own report, which already says exactly what to do.
            results.append(
                _not_registered(
                    client,
                    target_dir,
                    report=report,
                    had_prior=had_prior,
                    before_command=before_command,
                    can_restore=can_restore,
                    dry_run=dry_run,
                )
            )
            continue
        replaced = status is RegistrationStatus.CONNECTED_REPLACED
        results.append(
            HostRegistrationOutcome(
                host=client,
                status=status,
                previous_registration_present=had_prior,
                previous_registration_different=replaced,
                previous_command=before_command,
                new_command=after_command,
                restore_supported=can_restore,
                report=report,
                dry_run=dry_run,
            )
        )

    return results


def _registration_config_path(client: str, target_dir: Path) -> Path | None:
    """The file this host keeps its ModelQuad MCP registration in."""

    from .hostruntime import json_mcp_config_path

    return json_mcp_config_path(client, Path(target_dir))


def _dry_run_registration_status(
    client: str, target_dir: Path, report: Any, *, had_prior: bool, before_command: str | None,
) -> tuple[RegistrationStatus | None, str | None]:
    """What a dry run WOULD do to the registration, from the adapter's report.

    Returns ``(None, None)`` when the adapter would skip the file, so the
    caller reports it as not registered, exactly as a real run would.
    """

    from .adapters.report import ActionKind
    from .hostruntime import mcp_launch_command

    config_path = _registration_config_path(client, target_dir)
    kind = None
    for action in getattr(report, "actions", ()) or ():
        if config_path is not None and Path(action.path) == Path(config_path):
            kind = action.kind
            break
    if kind is ActionKind.SKIPPED:
        return None, None
    if kind in (ActionKind.WOULD_CREATE, ActionKind.WOULD_UPDATE):
        if had_prior:
            return RegistrationStatus.CONNECTED_REPLACED, mcp_launch_command()
        return RegistrationStatus.CONNECTED_NEW, mcp_launch_command()
    if had_prior:
        return RegistrationStatus.CONNECTED_UNCHANGED, before_command
    return RegistrationStatus.CONNECTED_NEW, mcp_launch_command()


def _registration_action_detail(report: Any, client: str, target_dir: Path) -> str | None:
    """The adapter's own words about what it did to the registration file.

    Taken from the adapter's report rather than composed here, because the
    adapter is the only thing that knows *why* it declined -- "is not saved as
    UTF-8 text ... save it as UTF-8 and run the command again" is already the
    actionable sentence, and rewriting it in onboarding's voice would
    reintroduce the second interpretation this whole result model removed.
    """

    config_path = _registration_config_path(client, target_dir)
    if config_path is None or report is None:
        return None
    for action in getattr(report, "actions", ()) or ():
        try:
            same = Path(action.path) == Path(config_path)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        if same and action.detail:
            return str(action.detail)
    return None


def _not_registered(
    client: str,
    target_dir: Path,
    *,
    report: Any,
    had_prior: bool,
    before_command: str | None,
    can_restore: bool,
    dry_run: bool,
) -> HostRegistrationOutcome:
    """The honest outcome when an install finished with nothing registered.

    Two statuses, because the user's next action differs: an unparseable
    config is something they must fix by hand, while a registration the
    adapter declined to rewrite is something it deliberately left alone.
    ``mcp_config_parses`` decides which, structurally.

    Note what this function does NOT do: it does not retire a provisional
    recovery record. Reaching here with ``had_prior`` true means a
    registration existed before and none exists now, which is the one shape
    where the record is the only remaining copy. Keeping evidence that turns
    out unnecessary costs a purgeable file; deleting evidence that was
    necessary costs the user their configuration.
    """

    from .hostruntime import mcp_config_parses

    parses = mcp_config_parses(client, Path(target_dir))
    detail = _registration_action_detail(report, client, target_dir)
    status = (
        RegistrationStatus.CONFIG_UNREADABLE
        if parses is False
        else RegistrationStatus.REGISTRATION_SKIPPED
    )
    return HostRegistrationOutcome(
        host=client,
        status=status,
        previous_registration_present=had_prior,
        previous_command=before_command,
        restore_supported=can_restore,
        error=detail,
        report=report,
        dry_run=dry_run,
    )


def _host_step_wording(
    host: HostAdapterDescriptor,
    outcome: HostRegistrationOutcome,
    *,
    dry_run: bool,
) -> tuple[str, str]:
    """The diagnostic and the plain sentence for one connected host.

    Kept beside the outcome model rather than inline in ``run_onboarding``
    because the two halves have to stay in step and have opposite rules: the
    ``detail`` half may name the old and new launch commands, and the
    ``user_detail`` half may not name either -- they are absolute paths, which
    are kept off primary UI. What the user
    is owed is the *fact* that something of theirs was replaced, not the path
    that replaced it; the path is one ``modelquad doctor`` away.
    """

    name = host.display_name
    target = host.default_target_dir()
    if outcome.status is RegistrationStatus.CONFIG_UNREADABLE:
        # The adapter's own sentence is the actionable one and is carried
        # through verbatim in `detail`. The user-facing half may not name the
        # path, so it names the file by role and
        # points at the one command that will re-check.
        reason = outcome.error or (
            f"{target} could not be parsed, so the registration was not written"
        )
        return (
            f"Did not register {name} at {target}: {reason}",
            (
                f"{name} was not connected: its configuration file could not be "
                f"read. Fix that file, then run `{CLI_COMMAND} repair`."
            ),
        )
    if outcome.status is RegistrationStatus.REGISTRATION_SKIPPED:
        reason = outcome.error or (
            "the existing registration is in a form this install does not rewrite"
        )
        return (
            f"Did not register {name} at {target}: {reason}",
            (
                f"{name} was not connected: it already records ModelQuad in a way "
                f"this installer will not change. Run `{CLI_COMMAND} doctor` for "
                f"the details."
            ),
        )
    if outcome.status is RegistrationStatus.CONNECTED_UNCHANGED:
        if dry_run:
            return (
                f"Dry run: {name} adapter is already registered at {target}",
                f"{name} is already connected.",
            )
        return (
            f"{name} adapter already registered at {target}; nothing changed",
            f"{name} was already connected.",
        )
    if outcome.status is RegistrationStatus.CONNECTED_REPLACED:
        change = f"replaced {outcome.previous_command!r} with {outcome.new_command!r}"
        # Capability-aware: promising a restore a host cannot do is the same
        # class of untruth as the single "Connected" sentence that this whole
        # result model exists to replace.
        if outcome.restore_supported:
            promise_detail = "the previous value was saved and can be restored on uninstall"
            promise_user = (
                f"Connected {name}, replacing the ModelQuad connection it already "
                f"had. The previous one was saved."
            )
        else:
            promise_detail = (
                "the previous value was detected, but automatic restoration is "
                "not supported for this host"
            )
            promise_user = (
                f"Connected {name}, replacing the ModelQuad connection it already "
                f"had. ModelQuad cannot put the previous one back for this tool."
            )
        if dry_run:
            return (
                f"Dry run: would install {name} adapter and {change}",
                f"Would connect {name}, replacing the ModelQuad connection it already had.",
            )
        return (
            f"Installed {name} adapter at {target}; {change} ({promise_detail})",
            promise_user,
        )
    if dry_run:
        return (
            f"Dry run: would install {name} adapter",
            f"Would connect {name}.",
        )
    return (
        f"Installed {name} adapter at {target}",
        f"Connected {name}.",
    )


@dataclass(frozen=True)
class OnboardingStepResult:
    """Result of one step in the onboarding flow.

    Two descriptions, deliberately, because they have different audiences and
    different rules. ``detail`` is the diagnostic one and may name paths,
    commands and internals -- it is what ``modelquad onboard`` prints in a
    terminal and what ends up in ``--json``. ``user_detail`` is one plain
    sentence for the graphical summary, and may contain none of that.

    Before this split the former setup window rendered ``detail`` directly, so a
    normal user's "you are all set" screen showed them an absolute path to a
    dotfile and the phrase "MCP registration" -- exactly the vocabulary
    banned from primary UI. An empty
    ``user_detail`` means "no user-facing sentence exists for this step", and
    the renderer substitutes a safe generic rather than falling back to the
    technical text.
    """

    name: str
    ok: bool
    detail: str
    user_detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "user_detail": self.user_detail,
        }


@dataclass(frozen=True)
class OnboardingResult:
    """Overall outcome of the onboarding wizard run."""

    steps: tuple[OnboardingStepResult, ...]
    selected_providers: tuple[str, ...]
    routing_mode: str
    ok: bool
    connected_providers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [step.to_dict() for step in self.steps],
            "selected_providers": list(self.selected_providers),
            "connected_providers": list(self.connected_providers),
            "routing_mode": self.routing_mode,
            "ok": self.ok,
        }


def _resolve_routing_mode(
    requested: str | None,
    db_path: str | Path,
    *,
    reset_routing_mode: bool = False,
) -> tuple[str, bool]:
    """Decide the routing mode for this onboarding run, and whether to write it.

    Three cases, and the second is the one that matters:

    1. **An explicit mode was requested.** That is the QA/programmatic path.
       Honour it exactly, and write it.
    2. **No mode requested, and one is already stored.** Preserve it and write
       nothing. Re-running setup (``onboard --yes`` after an upgrade) must
       not silently overwrite saved state. Retired modes read as automatic without rewriting
       their rows.
    3. **No mode requested, and nothing stored.** A genuine first run.
       Establish automatic, without presenting it as a decision.

    ``reset_routing_mode`` is the explicit opt-out of case 2: the user asking
    for normal Beta defaults back. It is never implied by ``--yes``.

    Returns ``(mode, should_write)``.
    """

    if requested is not None:
        if requested not in VALID_ROUTING_MODES:
            raise ValueError(
                f"Invalid routing mode {requested!r}; choose one of: {', '.join(VALID_ROUTING_MODES)}"
            )
        return requested, True

    if reset_routing_mode:
        return DEFAULT_ROUTING_MODE, True

    stored = routing_mode_if_set(db_path)
    if stored is not None:
        return stored, False
    return DEFAULT_ROUTING_MODE, True


def run_onboarding(
    *,
    selected_providers: Sequence[str] | None = None,
    routing_mode: str | None = None,
    reset_routing_mode: bool = False,
    assume_yes: bool = False,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
    which: Callable[[str], str | None] | None = None,
    registry: ProviderRegistry | None = None,
    db_path: str | Path | None = None,
    dry_run: bool = False,
) -> OnboardingResult:
    """Execute the end-to-end first-run onboarding wizard.

    Pure functional execution with injected I/O for CLI or UI reuse.

    ``routing_mode`` is the QA and programmatic path and accepts all three
    modes. It is deliberately not a question this wizard asks: see
    :func:`_resolve_routing_mode`.
    ``reset_routing_mode`` is the explicit request for normal Beta defaults,
    and is never implied by ``assume_yes``.
    """

    which = which if which is not None else hostruntime.discovery_which
    active_registry = registry or default_registry()
    steps: list[OnboardingStepResult] = []

    # 1. Welcome banner
    print_fn(f"{PRODUCT_NAME} - First-Run Setup")
    print_fn("==========================================")
    print_fn("Welcome! Let's get your AI coding assistants connected.")
    print_fn("")
    from .disclosure import DATA_FLOW, print_lines, record_notice_shown
    print_lines(DATA_FLOW, print_fn)
    print_fn("")
    try:
        record_notice_shown("onboard")
    except OSError:
        pass  # The first Codex call then shows the notice instead.
    config_dir_warning = hostruntime.claude_config_dir_warning()
    if config_dir_warning is not None:
        print_fn(f"Warning: {config_dir_warning}")
        print_fn("")
        steps.append(OnboardingStepResult(
            name="Claude Code settings folder",
            ok=False,
            detail=config_dir_warning,
            user_detail=(
                "Claude Code is set to use a different settings folder, so it "
                "will not see ModelQuad."
            ),
        ))

    # 2. Detection table
    #
    # Rendered from `provider_connection`, the single authority for what a
    # provider's state is. This wizard used to derive its own answer from
    # `ProviderDetection.adapter_installed` and print "Connected" /
    # "Not connected" from it. That was wrong in both directions: a Codex
    # AGENTS.md managed block put "Connected" next to a machine with no Codex
    # CLI, and a provider with no CLI and no host integration -- which this
    # wizard cannot connect at all -- was labelled "Not connected", which
    # describes a tool that is one action away from working.
    from .provider_connection import InstallState, describe_connections

    connections = describe_connections(which=which, registry=active_registry)
    known_provider_ids = tuple(c.provider_id for c in connections)

    # A provider is selectable when it is installed, on PATH or off it.
    connectable = tuple(
        c
        for c in connections
        if c.install in (InstallState.INSTALLED_ON_PATH, InstallState.INSTALLED_OFF_PATH)
    )
    unmanaged = {
        c.provider_id for c in connections if c.install is InstallState.NOT_APPLICABLE
    }

    print_fn("Detected AI Tools:")
    for c in connections:
        if c.provider_id in unmanaged:
            print_fn(
                f"  - {c.display_name}: set up separately - ModelQuad does not manage it here"
            )
        else:
            print_fn(f"  - {c.display_name}: {c.headline}")
    print_fn("")

    # 3. Provider selection
    if selected_providers is not None:
        for provider_id in selected_providers:
            if provider_id not in known_provider_ids:
                raise ValueError(
                    f"Unknown provider {provider_id!r}; valid choices are: {', '.join(known_provider_ids)}"
                )
        effective_selected = tuple(selected_providers)
    else:
        detected_ids = [c.provider_id for c in connectable]
        if assume_yes:
            effective_selected = tuple(detected_ids)
        else:
            default_str = ", ".join(detected_ids)
            prompt_msg = f"Which AI tools do you use? [{default_str}]: "
            try:
                user_input = input_fn(prompt_msg).strip()
            except EOFError:
                # No terminal to answer from (a script, a pipe, an agent):
                # choose nothing on the user's behalf.
                raise ValueError(
                    "Could not read an answer because this is not an interactive terminal. "
                    "Run onboard in a terminal, or pass --providers claude,codex (or --yes "
                    "to use every tool found)."
                ) from None
            if not user_input:
                effective_selected = tuple(detected_ids)
            else:
                chosen = [p.strip() for p in user_input.split(",") if p.strip()]
                for p in chosen:
                    if p not in known_provider_ids:
                        raise ValueError(
                            f"Unknown provider {p!r}; valid choices are: {', '.join(known_provider_ids)}"
                        )
                effective_selected = tuple(chosen)

    # 4. Host adapter installation for selected providers
    integrations_enabled = global_integrations_enabled()
    registration_outcomes: dict[str, list[HostRegistrationOutcome]] = {}
    for provider_id in effective_selected:
        plugin = active_registry.get(provider_id)
        if plugin is None:
            continue
        for host in plugin.host_adapters:
            install_results = install_detected_hosts([host.id], dry_run=dry_run, registry=active_registry)
            for res in install_results:
                registration_outcomes.setdefault(provider_id, []).append(res)
                if not integrations_enabled:
                    steps.append(OnboardingStepResult(
                        name=f"Install {host.id} adapter", ok=True,
                        detail=OFF_DETAIL,
                        user_detail="Connections to your AI tools are off by choice.",
                    ))
                    continue
                if not res.detected:
                    continue
                if res.status is RegistrationStatus.FAILED:
                    steps.append(
                        OnboardingStepResult(
                            name=f"Install {host.id} adapter",
                            ok=False,
                            detail=f"Failed to install {host.display_name}: {res.error}",
                            user_detail=(
                                f"ModelQuad could not finish connecting {host.display_name}."
                            ),
                        )
                    )
                elif not res.ok:
                    # The adapter finished without raising and registered
                    # nothing. Gated on `res.ok` -- the outcome model's own
                    # definition of "connected" -- rather than on a list of
                    # bad statuses, so a status added later cannot silently
                    # default back into the success branch.
                    detail, user_detail = _host_step_wording(host, res, dry_run=dry_run)
                    steps.append(
                        OnboardingStepResult(
                            name=f"Install {host.id} adapter",
                            ok=False,
                            detail=detail,
                            user_detail=user_detail,
                        )
                    )
                else:
                    detail, user_detail = _host_step_wording(host, res, dry_run=dry_run)
                    steps.append(
                        OnboardingStepResult(
                            name=f"Install {host.id} adapter",
                            ok=True,
                            detail=detail,
                            user_detail=user_detail,
                        )
                    )

    # 5. Routing mode: established, never asked about.
    #
    # Automatic is the only mode; pause and disconnect remain.
    effective_db_path = db_path if db_path is not None else service_db.DEFAULT_DB_PATH
    effective_mode, mode_is_new = _resolve_routing_mode(
        routing_mode,
        effective_db_path,
        reset_routing_mode=reset_routing_mode,
    )
    if not dry_run and mode_is_new:
        put_routing_mode(effective_db_path, effective_mode)

    # 6. Verification diagnostics
    try:
        config = load_config(None)
        doctor_report = run_doctor(config, which=which, registry=active_registry)
        checks_by_name = {check.name: check for check in doctor_report.checks}

        for provider_id in effective_selected:
            plugin = active_registry.get(provider_id)
            if plugin is None:
                continue

            if plugin.cli_executable is not None:
                cli_check = checks_by_name.get(f"{plugin.id} CLI")
                if cli_check is not None:
                    # `is OK`, not `is not MISSING`. A WARN check is a check
                    # that did NOT pass, and mapping it to a success sentence
                    # is what printed "Codex CLI can reach ModelQuad." above a
                    # detail reading "Not registered in ...config.toml".
                    # `doctor` grades WARN as advisory for its own overall
                    # roll-up, which is its contract; onboarding turns a check
                    # into a claim made to the user, and a claim needs the
                    # check to have actually passed.
                    cli_ok = cli_check.status is CheckStatus.OK
                    steps.append(
                        OnboardingStepResult(
                            name=f"{plugin.display_name} CLI check",
                            ok=cli_ok,
                            detail=cli_check.detail,
                            user_detail=(
                                f"{plugin.display_name} is installed on this computer."
                                if cli_ok
                                else f"{plugin.display_name} is not installed on this computer."
                            ),
                        )
                    )

            for host in plugin.host_adapters:
                if not integrations_enabled:
                    continue
                adapter_check = checks_by_name.get(f"{host.id} adapter installed")
                if adapter_check is not None:
                    registration_ok = dry_run or (
                        all(result.ok for result in registration_outcomes.get(plugin.id, ()))
                        and bool(registration_outcomes.get(plugin.id, ()))
                    )
                    adapter_ok = adapter_check.status is CheckStatus.OK and registration_ok
                    steps.append(
                        OnboardingStepResult(
                            name=f"{host.display_name} adapter verified",
                            ok=adapter_ok,
                            detail=adapter_check.detail,
                            user_detail=(
                                f"{host.display_name} is set up."
                                if adapter_ok
                                else f"{host.display_name} is not set up yet."
                            ),
                        )
                    )

                mcp_check = checks_by_name.get(f"{host.id} MCP registered")
                if mcp_check is not None:
                    mcp_ok = mcp_check.status is CheckStatus.OK
                    steps.append(
                        OnboardingStepResult(
                            name=f"{host.display_name} MCP registration",
                            ok=mcp_ok,
                            detail=mcp_check.detail,
                            user_detail=(
                                f"{host.display_name} can reach ModelQuad."
                                if mcp_ok
                                else f"{host.display_name} cannot reach ModelQuad yet."
                            ),
                        )
                    )
    except Exception as exc:
        steps.append(
            OnboardingStepResult(
                name="Verification diagnostics",
                ok=False,
                detail=f"Verification failed: {exc}",
                user_detail="ModelQuad could not finish checking your AI tools.",
            )
        )

    # 7. Final summary
    connected_providers = tuple(
        provider_id
        for provider_id in effective_selected
        if registration_outcomes.get(provider_id)
        and all(result.ok for result in registration_outcomes[provider_id])
    )
    all_ok = all(step.ok for step in steps)
    print_fn("")
    print_fn("Setup Summary:")
    print_fn(f"  Connected providers: {', '.join(connected_providers) if connected_providers else 'none'}")
    print_fn("  Connections to your AI tools: on" if integrations_enabled
             else "  Connections to your AI tools: off by choice")
    if integrations_enabled:
        print_fn(
            f"You're ready. In Claude Code, Claude can offer to ask Codex for a read-only second "
            f"opinion through {PRODUCT_NAME}; Claude Code asks you before each call."
        )
    else:
        print_fn("Connections to your AI tools remain off. Run modelquad integrations on to enable them.")
    print_fn("")

    return OnboardingResult(
        steps=tuple(steps),
        selected_providers=tuple(effective_selected),
        routing_mode=effective_mode,
        ok=all_ok,
        connected_providers=connected_providers,
    )
