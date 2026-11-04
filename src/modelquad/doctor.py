# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Environment and integration checks for the modelquad command line."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from importlib import util
import json
import os
from pathlib import Path
from typing import Any

from . import hostruntime
from .host_integrations import OFF_DETAIL, global_integrations_enabled
from .executable_resolution import resolve_executable
from .branding import (
    CLI_COMMAND,
    MCP_COMMAND,
    MCP_SERVER_NAME,
    PRODUCT_NAME,
    PRODUCT_SLUG,
    installed_version,
)
from .codexcli import resolve_codex_cli, which_codex
from .config import Config, ProviderConfig
from .execution.base import WORKER_ENV_VAR
from .execution.codex import CodexExecutor, filter_windows_apps_alias_from_path
from .providers.base import DEFAULT_USAGE_DIRECTORY
from .registry import ProviderPlugin, ProviderPluginContext, ProviderRegistry, default_registry
from .secrets import credentials_configured


def _service_pidfile_check(manager: Any | None = None) -> DoctorCheck | None:
    """Report a ``service.pid`` that names a process which is not running.

    None when there is no pidfile, when it cannot be read as a launch record,
    or when its PID exists. Existence is the only question, asked through the
    lifecycle's own PID-existence seam: no process is signalled and nothing is
    removed -- ``service stop`` removes the stale record. ``manager`` is the
    ``ServiceProcessManager`` to read; tests pass one with a fake seam.
    """
    try:
        if manager is None:
            from .service.lifecycle import ServiceProcessManager

            manager = ServiceProcessManager()
        record, _error = manager._read_pid_record()
        if record is None:
            return None
        pid = int(record["pid"])
        if manager._pid_exists(pid):
            return None
        pid_path = manager.pid_path
    except Exception:  # noqa: BLE001 -- a diagnostic must never crash
        return None
    return DoctorCheck(
        "service pidfile",
        CheckStatus.WARN,
        f"{pid_path} names PID {pid}, which is not running. "
        f"Run `{CLI_COMMAND} service stop` to remove the stale record.",
    )


class CheckStatus(str, Enum):
    """A doctor check's severity."""

    OK = "ok"
    WARN = "warn"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DoctorCheck:
    """One environment diagnostic."""

    name: str
    status: CheckStatus
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class DoctorReport:
    """All diagnostics emitted by one doctor run."""

    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        """Return False only when a required component is missing."""

        return all(check.status is not CheckStatus.MISSING for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "checks": [check.to_dict() for check in self.checks],
            "ok": self.ok,
        }


def _cli_check(
    name: str,
    executable: str,
    *,
    which: Callable[[str], str | None],
    missing_status: CheckStatus,
    missing_detail: str,
) -> DoctorCheck:
    try:
        resolved = which(executable)
    except OSError as exc:
        return DoctorCheck(name, missing_status, f"Could not inspect PATH: {exc}")
    if resolved:
        return DoctorCheck(name, CheckStatus.OK, f"Found at {resolved}.")
    return DoctorCheck(name, missing_status, missing_detail)


def _codex_stale_path_check(which: Callable[[str], str | None]) -> DoctorCheck | None:
    """Warn when the Codex on PATH is older than the one ModelQuad runs."""
    try:
        chosen = resolve_codex_cli(which=which)
    except OSError:
        return None
    if chosen is None or chosen.stale_path_version is None:
        return None
    return DoctorCheck(
        "codex CLI version",
        CheckStatus.WARN,
        f"The codex on PATH is {chosen.stale_path_version}, older than the official "
        f"Codex install ({chosen.version}) that ModelQuad runs. Your own terminal may "
        "still run the older one; update or remove it if that is not intended.",
    )


def _adapter_check(
    name: str,
    adapter: object,
    target_dir: Path,
    install_command: str,
    *,
    verified: bool,
) -> DoctorCheck:
    try:
        installed = adapter.is_installed(target_dir)
    except OSError as exc:
        return DoctorCheck(name, CheckStatus.WARN, f"Could not inspect {target_dir}: {exc}")
    if installed:
        detail = f"Installed at {target_dir}."
        if not verified:
            detail += " (best-effort convention; verify manually)"
        return DoctorCheck(name, CheckStatus.OK, detail)
    return DoctorCheck(name, CheckStatus.WARN, f"Not installed; run {install_command}.")


def _off_adapter_check(host, target_dir: Path) -> DoctorCheck:
    """Off is intent, not proof that every piece of global wiring is gone."""
    from .adapters.report import ActionKind

    name = f"{host.id} adapter installed"
    remaining: list[str] = []
    uncertain: list[str] = []
    try:
        adapter = host.adapter_cls()
        # The adapters' dry runs cover partial installations (hooks, status
        # lines and instruction blocks), even when is_installed is false.
        preview = adapter.uninstall(target_dir, dry_run=True)
        for action in preview.actions:
            if action.kind in (ActionKind.WOULD_REMOVE, ActionKind.WOULD_UPDATE):
                remaining.append(str(action.path))
            elif action.kind is ActionKind.SKIPPED:
                uncertain.append(f"{action.path}: {action.detail or 'removal skipped'}")
        if hostruntime.read_registered_command(host.id, target_dir) is not None:
            remaining.append(str(hostruntime.json_mcp_config_path(host.id, target_dir)))
        if not remaining and adapter.is_installed(target_dir):
            remaining.append(str(target_dir))
    except (OSError, ValueError) as exc:
        uncertain.append(f"{target_dir}: {exc}")
    details = [f"off but still wired: {host.id} {path}" for path in dict.fromkeys(remaining)]
    details.extend(f"off; could not verify removal: {host.id} {detail}" for detail in uncertain)
    if details:
        return DoctorCheck(
            name, CheckStatus.WARN,
            "; ".join(details) + "; inspect the remaining wiring and rerun modelquad integrations off.",
        )
    return DoctorCheck(name, CheckStatus.OK, OFF_DETAIL)


def _mcp_registration_check(host_id: str, target_dir: Path) -> DoctorCheck:
    """Verify the MCP entry separately from the host's instruction install state.

    This is intentionally limited to the two verified host conventions.  The
    unverified desktop path remains a best-effort adapter check
    only, rather than presenting an authoritative-looking registration result.
    """

    name = f"{host_id} MCP registered"
    install_command = f"{CLI_COMMAND} install {host_id}"
    if host_id == "claude" and _claude_via_plugin():
        return _claude_plugin_check(name, target_dir)
    if host_id == "claude":
        config_path = hostruntime.json_mcp_config_path(host_id, target_dir)
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return DoctorCheck(
                name,
                CheckStatus.WARN,
                f"Could not read MCP configuration at {config_path}: {exc}; run {install_command}.",
            )
        servers = payload.get("mcpServers") if isinstance(payload, dict) else None
        if isinstance(servers, dict) and MCP_SERVER_NAME in servers:
            return DoctorCheck(name, CheckStatus.OK, f"Registered in {config_path}.")
        return DoctorCheck(
            name,
            CheckStatus.WARN,
            f"Not registered in {config_path}; run {install_command}.",
        )

    raise ValueError(f"No verified MCP-registration check is defined for {host_id!r}")


_PLUGIN_INSTALL = "claude plugin install modelquad --marketplace razbenaharon/modelquad"


def _claude_via_plugin() -> bool:
    from .adapters.server_instructions import read_claude_plugin

    return read_claude_plugin()


def _claude_plugin_check(name: str, target_dir: Path) -> DoctorCheck:
    """Beside the plugin, the registration is the plugin's, as Claude Code records it."""

    from .adapters.claude_adapter import plugin_recorded

    recorded = plugin_recorded(target_dir)
    if recorded is True:
        return DoctorCheck(name, CheckStatus.OK, "Supplied by the ModelQuad plugin.")
    back = f"or run {CLI_COMMAND} install claude --no-plugin to install the server directly"
    if recorded is False:
        return DoctorCheck(
            name, CheckStatus.WARN,
            f"ModelQuad was installed for the plugin, but Claude Code does not list the "
            f"ModelQuad plugin. Install it with {_PLUGIN_INSTALL}, {back}.",
        )
    return DoctorCheck(
        name, CheckStatus.WARN,
        f"ModelQuad was installed for the plugin, but Claude Code's plugin record could "
        f"not be read, so whether the plugin is installed is unknown. Check with "
        f"claude plugin list, {back}.",
    )


def _claude_duplicates_check(target_dir: Path) -> DoctorCheck | None:
    """The plugin and the regular install side by side: Claude Code runs both copies.

    ``None`` when there is no evidence of both. An unreadable plugin record is
    not evidence, so it reports nothing rather than a guess.
    """

    from .adapters.claude_adapter import plugin_recorded

    name = "claude installed once"
    try:
        registered = hostruntime.read_registered_command("claude", target_dir) is not None
    except (OSError, ValueError):
        return None
    if _claude_via_plugin():
        if not registered:
            return None
        return DoctorCheck(
            name, CheckStatus.WARN,
            "A modelquad server entry is still registered next to the plugin's server, so "
            "Claude Code starts both. Remove the entry from ~/.claude.json, or run "
            f"{CLI_COMMAND} install claude --no-plugin and uninstall the plugin.",
        )
    if plugin_recorded(target_dir) is not True or not registered:
        return None
    return DoctorCheck(
        name, CheckStatus.WARN,
        "The ModelQuad plugin is installed, and so are the server, commands and reminder "
        "from the regular install, so Claude Code runs each twice. Run "
        f"{CLI_COMMAND} install claude --plugin to keep the plugin's, or uninstall the plugin.",
    )


def _mcp_runtime_check(
    host_id: str,
    target_dir: Path,
    *,
    which: Callable[[str], str | None],
) -> DoctorCheck | None:
    """Verify WHICH ModelQuad a host launches, not merely that it launches one.

    ``_mcp_registration_check`` above asserts the entry exists. That check
    passed, green, on the machine where this defect was found: the entry was
    present and named a different copy of ModelQuad than the one on PATH.
    Presence was never the question.

    Returns ``None`` when there is nothing to judge -- an absent or unreadable
    registration is already the neighbouring check's finding, and reporting it
    twice would make one problem look like two.
    """

    from .hostruntime import classify_registration, read_registered_command

    name = f"{host_id} MCP runtime"
    try:
        command = read_registered_command(host_id, target_dir)
    except (OSError, ValueError) as exc:
        return DoctorCheck(name, CheckStatus.WARN, f"Could not inspect the registration: {exc}")
    if command is None and host_id == "claude" and _claude_via_plugin():
        # The plugin's server launches the bare command, as the entry would.
        command = MCP_COMMAND
    if command is None:
        return None
    verdict = classify_registration(command, which=which)
    if verdict.canonical:
        target = verdict.resolved or verdict.command
        return DoctorCheck(name, CheckStatus.OK, f"Launches {target}.")
    return DoctorCheck(
        name,
        CheckStatus.WARN,
        f"{verdict.reason} Run {CLI_COMMAND} repair to point it at this installation.",
    )


def _cli_runtime_check(
    name: str,
    command: str | None,
    arguments: tuple[str, ...],
    *,
    which: Callable[[str], str | None],
) -> DoctorCheck | None:
    """Judge which runtime an owned CLI surface launches, not just its presence.

    An absent or user-owned command is not this check's finding. Returning
    None keeps one missing integration from looking like two problems, and
    keeps a user's replacement status line out of ModelQuad repair.
    """
    from .adapters.cliintegration import owns_cli_command
    from .hostruntime import classify_cli_registration

    if not owns_cli_command(command, arguments):
        return None
    verdict = classify_cli_registration(command, which=which)
    if verdict.canonical:
        return DoctorCheck(name, CheckStatus.OK, f"Launches {verdict.resolved or verdict.command}.")
    return DoctorCheck(
        name,
        CheckStatus.WARN,
        f"{verdict.reason} Run {CLI_COMMAND} repair to point it at this installation.",
    )


def _leftover_gate_check(target_dir: Path) -> DoctorCheck | None:
    """An advisory gate hook an earlier version installed, still in settings.json.

    The gate is no longer installed: the MCP server's instructions carry the
    guidance it used to repeat. Named like the old runtime check so that
    ``modelquad repair`` (which reinstalls Claude's adapter, and so removes an
    owned gate) picks it up. None when no owned gate is wired.
    """
    from .adapters.claude_adapter import read_hook_commands
    from .adapters.cliintegration import owns_cli_command

    try:
        wired = [command for command in read_hook_commands(target_dir)
                 if owns_cli_command(command, ("hook-gate",))]
    except OSError:
        return None
    if not wired:
        return None
    return DoctorCheck(
        "claude hook-gate runtime",
        CheckStatus.WARN,
        f"An earlier version's advisory PreToolUse hook is still in {target_dir / 'settings.json'}. "
        f"It is no longer needed; run {CLI_COMMAND} repair to remove it.",
    )


def _own_status_line_check(target_dir: Path) -> DoctorCheck | None:
    """The user kept a status line of their own: say how to add ModelQuad's part to it."""
    from .adapters.claude_adapter import read_status_line_command, status_line_recipe
    from .adapters.cliintegration import owns_cli_command

    command = read_status_line_command(target_dir)
    if owns_cli_command(command, ("usage-hook", "claude")):
        return None
    recipe = status_line_recipe(command)
    if recipe is None:
        if isinstance(command, str) and "usage-hook claude" in command:
            return DoctorCheck(
                "claude status line (yours)", CheckStatus.OK,
                "Your own status line runs ModelQuad's usage hook, so Claude's quota arrives.",
            )
        return None
    return DoctorCheck(
        "claude status line (yours)",
        CheckStatus.WARN,
        "Your own status line is kept, so ModelQuad cannot see Claude's quota yet. "
        f"To add it: {recipe}",
    )


def _claude_reading_exists(usage_directory: Path) -> bool:
    try:
        payload = json.loads((usage_directory / "usage-claude.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    return isinstance(payload, dict) and any(
        isinstance(payload.get(key), (int, float)) and not isinstance(payload.get(key), bool)
        for key in ("five_hour_used_percentage", "seven_day_used_percentage")
    )


def _claude_version_check(
    usage_directory: Path, *, read_push: Callable[[], dict | None] | None = None,
) -> DoctorCheck:
    """Claude Code's version as its own status line last reported it.

    ModelQuad never launches the ``claude`` binary to ask. The version and
    Claude's quota both arrive through the status line, which Claude Code
    runs after a reply; quota appears only on Pro and Max plans and needs a
    recent Claude Code.
    """
    name = "claude version"
    if read_push is None:
        def read_push():
            from .hostobs import read_host_push

            return read_host_push("claude_code")
    try:
        push = read_push()
    except Exception:  # noqa: BLE001 -- a diagnostic must never crash
        push = None
    version = (push or {}).get("runtime_version")
    if not version:
        return DoctorCheck(
            name, CheckStatus.UNKNOWN,
            "Not seen yet. Open Claude Code in a terminal and send one message; its version "
            "and Claude's quota arrive through the status line after the first reply. The "
            "desktop app's Code tab has no status line, so there both stay unknown; Codex's "
            "quota still shows.",
        )
    if _claude_reading_exists(usage_directory):
        return DoctorCheck(name, CheckStatus.OK, f"Claude Code {version}, from your last session.")
    return DoctorCheck(
        name, CheckStatus.WARN,
        f"Claude Code {version} has not reported Claude's quota yet. It appears after the first "
        "reply in a session on a Pro or Max plan. If it never does, update Claude Code "
        "(run: claude update).",
    )


def _first_python_on_path(path_value: str | None) -> Path | None:
    """The first ``python.exe`` a Windows shell would run from ``path_value``.

    A plain look along PATH, deliberately without the safe-discovery filters:
    the question is what typing ``python`` does, Store alias included.
    """
    for entry in (path_value or "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry:
            continue
        candidate = Path(entry) / "python.exe"
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None


def _python_alias_check(
    *,
    path_value: str | None,
    local_app_data: str | None,
    platform: str = os.name,
) -> DoctorCheck | None:
    """``python`` that opens the Microsoft Store page instead of running Python."""
    if platform != "nt" or not local_app_data:
        return None
    resolved = _first_python_on_path(path_value)
    if resolved is None:
        return None
    stub_dir = os.path.normcase(os.path.normpath(str(Path(local_app_data) / "Microsoft" / "WindowsApps")))
    if os.path.normcase(os.path.normpath(str(resolved.parent))) != stub_dir:
        return None
    return DoctorCheck(
        "setup: python command",
        CheckStatus.WARN,
        "Typing python opens the Microsoft Store, not Python. Use the Python launcher "
        f"instead, for example: py -m pip install --upgrade {PRODUCT_SLUG}. Or turn off "
        "python.exe and python3.exe under Settings > Apps > Advanced app settings > "
        "App execution aliases.",
    )


def _scripts_on_path_check(
    *,
    path_value: str | None,
    scripts_dirs: tuple[str, ...] | None = None,
    platform: str = os.name,
) -> DoctorCheck | None:
    """The folder holding the ``modelquad`` command is missing from PATH."""
    if platform != "nt":
        return None
    if scripts_dirs is None:
        import sysconfig

        candidates = [sysconfig.get_path("scripts")]
        try:
            candidates.append(sysconfig.get_path("scripts", f"{os.name}_user"))
        except KeyError:
            pass
        scripts_dirs = tuple(path for path in candidates if path)
    entries = {os.path.normcase(os.path.normpath(entry.strip().strip('"')))
               for entry in (path_value or "").split(os.pathsep) if entry.strip()}
    for folder in scripts_dirs:
        if not any((Path(folder) / name).is_file() for name in (f"{CLI_COMMAND}.exe", CLI_COMMAND)):
            continue
        if os.path.normcase(os.path.normpath(folder)) in entries:
            return None
        return DoctorCheck(
            "setup: Scripts folder on PATH",
            CheckStatus.WARN,
            f"The folder with the {CLI_COMMAND} command is not on your PATH: {folder}. "
            "Add it by running this once in PowerShell, then open a new terminal: "
            "[Environment]::SetEnvironmentVariable('Path', "
            "[Environment]::GetEnvironmentVariable('Path','User') + "
            f"';{folder}', 'User')",
        )
    return None


def _git_for_windows_check(
    *,
    which: Callable[[str], str | None],
    environ: dict[str, str] | None = None,
    platform: str = os.name,
) -> DoctorCheck | None:
    """Claude Code on Windows needs Git for Windows (its bash runs hooks and status lines)."""
    if platform != "nt":
        return None
    environ = dict(os.environ if environ is None else environ)
    candidates = [environ.get("CLAUDE_CODE_GIT_BASH_PATH")]
    for root in (environ.get("ProgramFiles"), environ.get("ProgramW6432")):
        if root:
            candidates.append(str(Path(root) / "Git" / "bin" / "bash.exe"))
    try:
        on_path = which("git")
    except OSError:
        on_path = None
    if on_path or any(path and Path(path).is_file() for path in candidates):
        return DoctorCheck("setup: Git for Windows", CheckStatus.OK, "Found.")
    return DoctorCheck(
        "setup: Git for Windows",
        CheckStatus.WARN,
        "Not found. Claude Code on Windows needs Git for Windows: install it from "
        "https://git-scm.com/downloads/win, then open a new terminal.",
    )


def first_run_checklist(report: DoctorReport) -> list[tuple[bool, str]]:
    """The doctor report as a short plain-language checklist: (done, sentence)."""
    checks = {check.name: check for check in report.checks}

    def ok(name: str) -> bool:
        check = checks.get(name)
        return check is not None and check.status is CheckStatus.OK

    items: list[tuple[bool, str]] = [(True, f"{PRODUCT_NAME} is installed.")]
    for name in ("setup: python command", "setup: Scripts folder on PATH", "setup: Git for Windows"):
        check = checks.get(name)
        if check is not None and check.status is not CheckStatus.OK:
            items.append((False, check.detail))
    items.append((ok("codex CLI"), "The Codex CLI is installed." if ok("codex CLI")
                  else "Install the Codex CLI, then sign in by running: codex login"))
    connected = ok("claude adapter installed") and ok("claude MCP registered")
    items.append((connected, f"{PRODUCT_NAME} is connected to Claude Code." if connected
                  else f"Connect {PRODUCT_NAME} to Claude Code by running: {CLI_COMMAND} install claude"))
    own_line = checks.get("claude status line (yours)")
    if own_line is not None and own_line.status is not CheckStatus.OK:
        items.append((False, own_line.detail))
    version = checks.get("claude version")
    if version is not None:
        items.append((version.status is CheckStatus.OK,
                      "Claude Code has reported Claude's quota." if version.status is CheckStatus.OK
                      else version.detail))
    leftover = checks.get("claude hook-gate runtime")
    if leftover is not None and leftover.status is not CheckStatus.OK:
        items.append((False, leftover.detail))
    return items


#: What a user does when the background service is simply not running.
_SERVICE_START_HINT = (
    f"Start it with '{CLI_COMMAND} service start', or run "
    f"'{CLI_COMMAND} service install' to start it at logon."
)


def _control_plane_check(
    port: int | None = None,
    probe: Callable[[int], Any] | None = None,
) -> DoctorCheck:
    """Whether the background service is reachable, and whether it is OURS.

    The gap this closes was not a wrong answer, it was a missing question. The
    installed product refused to open, told the user -- correctly --
    that the listener on its port was a ModelQuad *development* instance, and
    directed them to ``modelquad doctor``. ``doctor`` had no control-plane
    check at all, so it reported ``Overall: HEALTHY`` about a product the user
    could not open. A diagnostic that cannot observe the fault the product
    just told the user to diagnose is worse than no instruction.

    Read-only, like every other check here: it starts no service and stops
    nothing. It only looks.

    MISSING, not WARN, when the listener is a mismatch -- ``DoctorReport.ok``
    turns on exactly that distinction, and this is the one condition where
    "HEALTHY" would be a lie the user has already caught the product telling.
    """

    # The default diagnostic observes exactly the same managed readiness as
    # lifecycle/global status. The older injected probe remains a compatibility
    # seam for callers supplying synthetic listener-classification evidence.
    if probe is None:
        from .service.lifecycle import ServiceProcessManager, SERVICE_DETAILS, _service_options

        name = "Background service"
        try:
            manager = ServiceProcessManager()
            status = manager.status() if port is None else manager.status(port=port)
        except Exception:
            return DoctorCheck(name, CheckStatus.WARN, "Managed service readiness could not be inspected.")
        from .cli import SERVICE_DEFAULT_PORT
        selected_port = port if port is not None else SERVICE_DEFAULT_PORT
        if port is None and status.argv:
            try:
                selected_port = _service_options(status.argv, manager.state_dir)["port"]
            except Exception:
                pass  # A display endpoint must not hide the public refusal.
        endpoint = f"127.0.0.1:{selected_port}"
        if not status.supported:
            return DoctorCheck(name, CheckStatus.WARN, SERVICE_DETAILS["unsupported_platform"])
        if status.ready is True and status.running:
            return DoctorCheck(name, CheckStatus.OK, f"Running and verified on {endpoint} (managed service).")
        if not status.running and status.reason in (None, "service_absent"):
            return DoctorCheck(name, CheckStatus.OK, f"Not running. {_SERVICE_START_HINT}")
        state = (f"Possibly running (PID {status.pid})" if status.pid is not None else "Possibly running; PID unknown") if status.running else "Not running"
        detail = SERVICE_DETAILS.get(status.reason, "Managed service readiness is unconfirmed.")
        return DoctorCheck(name, CheckStatus.MISSING, f"{state}; not ready on {endpoint}. {detail}")

    from .service import control_plane

    if port is None:
        from .cli import SERVICE_DEFAULT_PORT

        port = SERVICE_DEFAULT_PORT

    name = "Background service"
    endpoint = f"127.0.0.1:{port}"
    # Injectable so a test can pose each listener case without owning the
    # developer's actual port 8765 -- otherwise every doctor assertion depends
    # on whether a service happens to be running on the machine running it.
    try:
        verdict = probe(port)
    except Exception as exc:  # noqa: BLE001 -- a diagnostic must never crash
        return DoctorCheck(name, CheckStatus.WARN, f"Could not probe {endpoint}: {type(exc).__name__}.")

    if verdict.state == control_plane.PROBE_READY:
        identity = verdict.identity()
        kind = identity.get("instance_kind", "unknown")
        return DoctorCheck(
            name,
            CheckStatus.OK,
            f"Running and verified on {endpoint} ({kind} instance).",
        )

    if verdict.state == control_plane.PROBE_ABSENT:
        # Not running is not broken: the service is started on demand or at
        # logon. Reporting it as a fault would train users to ignore this
        # check on every healthy machine.
        pid_note = _service_pid_note()
        return DoctorCheck(
            name,
            CheckStatus.OK if not pid_note else CheckStatus.WARN,
            f"Not running; nothing is listening on {endpoint}. {_SERVICE_START_HINT}"
            + (f" {pid_note}" if pid_note else ""),
        )

    # A mismatch: something owns the port and it is not this installation's
    # service. Report the identity that proves it, and the shared recovery
    # advice, so every surface gives the same next step.
    identity = verdict.identity()
    facts = ", ".join(f"{key}={value}" for key, value in sorted(identity.items()))
    detail = f"{verdict.detail} (endpoint {endpoint}"
    if facts:
        detail += f"; {facts}"
    detail += "). " + control_plane.recovery_advice(verdict, port)
    return DoctorCheck(name, CheckStatus.MISSING, detail)


def _service_pid_note() -> str:
    """A note about a recorded service PID that no longer matches reality.

    Only reported when nothing is listening: a pidfile naming a dead process
    is the difference between "never started" and "died", and the two need
    different next steps.
    """

    try:
        from .service.lifecycle import ServiceProcessManager

        status = ServiceProcessManager().status()
    except Exception:  # noqa: BLE001 -- a diagnostic must never crash
        return ""
    if status.running and status.pid:
        return (
            f"A service process is recorded as running (pid {status.pid}) but is "
            f"not answering; it may be starting up or hung."
        )
    return ""


def _python_path_check(environ=None) -> DoctorCheck | None:
    """Warn about a relative PYTHONPATH entry; None when there is nothing to say.

    Python puts PYTHONPATH ahead of the installed packages. A relative entry
    (``.`` or ``src``, or an empty one, which means the current folder) is read
    against whichever folder a process starts in. Claude Code starts ModelQuad
    in your project, so a project that carries its own ``modelquad`` folder
    would be imported in place of the installed ModelQuad.
    """
    raw = (os.environ if environ is None else environ).get("PYTHONPATH")
    if raw is None:
        return None
    relative = [entry for entry in raw.split(os.pathsep) if not entry or not os.path.isabs(entry)]
    if not relative:
        return None
    return DoctorCheck(
        "PYTHONPATH", CheckStatus.WARN,
        "PYTHONPATH has a relative or empty entry, so a project folder could replace ModelQuad's own "
        "code. Use absolute folders only, or remove PYTHONPATH from your user environment.",
    )


def _state_consistency_check() -> DoctorCheck:
    """Is there exactly one ModelQuad product state on this machine?

    The one check that would have caught the live split from either side of
    it. ``_mcp_runtime_check`` is answered from inside one runtime and is
    therefore blind to a second runtime being wrong; this one counts the
    stores on disk and compares what they hold.

    Read-only: it never creates, migrates, merges or deletes a store.
    """

    from .statediag import survey_state

    name = "state consistency"
    try:
        survey = survey_state()
    except (OSError, ValueError) as exc:
        return DoctorCheck(name, CheckStatus.WARN, f"Could not survey ModelQuad state: {exc}")
    context = survey.context
    # Per-surface runtime checks own host findings; this check owns only stores.
    # Keep the distinction in data, not a guess based on rendered host labels.
    if survey.store_findings:
        return DoctorCheck(name, CheckStatus.WARN, " ".join(survey.store_findings))
    detail = (
        f"Owning {context.mode.value} state store: {context.state_root} "
        f"(via {context.source.value})."
    )
    if survey.notes:
        detail += " " + " ".join(survey.notes)
    return DoctorCheck(name, CheckStatus.OK, detail)


def _capability_matrix_check(plugin) -> DoctorCheck | None:
    """One truthful line per provider about what its worker may do, and why not.

    The distinction this check exists to make: a capability ModelQuad withholds
    is NOT a fault, and neither is one this runtime genuinely does not have.
    Only a capability that should work and did not is something to repair. A
    doctor that reports all three as problems sends users hunting for a fix to a
    deliberate policy -- and one that reports none of them hides a real break.
    """

    profile = plugin.capability_state()
    if not profile.facts:
        return None

    by_state: dict[str, list[str]] = {}
    for name in sorted(profile.facts):
        by_state.setdefault(profile.facts[name].state.value, []).append(name)

    parts: list[str] = []
    if by_state.get("available"):
        parts.append(f"verified: {', '.join(by_state['available'])}")
    if by_state.get("unverified"):
        parts.append(f"permitted but unverified: {', '.join(by_state['unverified'])}")
    for capability in by_state.get("restricted", []):
        reason = profile.facts[capability].restriction_reason
        parts.append(
            f"{capability} withheld by ModelQuad"
            + (f" ({reason.detail})" if reason else "")
        )
    if by_state.get("unsupported"):
        parts.append(f"not supported by this runtime: {', '.join(by_state['unsupported'])}")
    if by_state.get("needs_repair"):
        parts.append(f"CONFIGURED BUT NOT WORKING: {', '.join(by_state['needs_repair'])}")

    # WARN is reserved for the one case a user can actually act on.
    status = CheckStatus.WARN if by_state.get("needs_repair") else CheckStatus.OK
    return DoctorCheck(f"{plugin.id} worker capabilities", status, "; ".join(parts) + ".")


def _codex_global_instructions_check() -> DoctorCheck:
    """Whether Codex adds the user's own global instructions to every request.

    Codex puts ``CODEX_HOME/AGENTS.md`` (or ``AGENTS.override.md``) into each
    request, even under ``--ignore-user-config``, and no setting stops it
    (local test with a recording server, Codex 0.164.0, 2026-10-25). Only the
    file's existence is read, never its contents."""
    from .codex_config import config_path

    home = config_path().parent
    present = [name for name in ("AGENTS.override.md", "AGENTS.md") if (home / name).is_file()]
    name = "codex global instructions"
    if not present:
        return DoctorCheck(name, CheckStatus.OK, "No global Codex instructions file; none is added to requests.")
    return DoctorCheck(
        name, CheckStatus.WARN,
        f"Codex adds your global instructions file ({present[0]} in your Codex folder) to every "
        "request ModelQuad sends, so OpenAI receives it too. Remove anything there you do not want sent.")


def _codex_shell_check(windows_path: str | None) -> DoctorCheck:
    """Structural (no subprocess spawn) Windows shell-availability check.

    Codex's Windows sandbox spawns tool calls via a restricted-token
    CreateProcessAsUserW, which cannot activate Microsoft-Store App
    Execution Alias reparse points (see execution/codex.py). This checks
    whether *some* real shell resolves once that one alias directory is
    filtered out of PATH -- the same filter the executor itself applies.
    """

    name = "codex shell execution"
    if not windows_path:
        return DoctorCheck(
            name, CheckStatus.WARN, "Could not read PATH to verify Windows shell availability."
        )
    filtered = filter_windows_apps_alias_from_path(windows_path)
    for candidate in ("pwsh.exe", "pwsh", "powershell.exe", "powershell", "cmd.exe", "cmd"):
        if resolve_executable(candidate, path_env=filtered, allow_batch=False).path is not None:
            return DoctorCheck(
                name,
                CheckStatus.OK,
                "A non-alias shell is available on PATH; the Windows sandbox spawn "
                "workaround (WindowsApps alias stripped from the delegated "
                "subprocess's PATH) applies.",
            )
    return DoctorCheck(
        name,
        CheckStatus.WARN,
        "No non-alias PowerShell/cmd found on PATH once the WindowsApps App "
        "Execution Alias directory is filtered out; Codex's Windows sandbox "
        "shell spawn (CreateProcessAsUserW) will fail for every tool call. "
        "Install a classic (non-Store) PowerShell, or ensure cmd.exe is on PATH.",
    )


def _model_discovery_check(plugin: ProviderPlugin, context: ProviderPluginContext) -> DoctorCheck | None:
    """Model-catalog freshness, reported independent of provider execution health.

    A failed, unsupported, or empty discovery is at most WARN -- it never
    contributes to ``DoctorReport.ok`` going False the way a MISSING CLI
    check does, since a stale or unavailable model catalog says nothing
    about whether the provider itself can still execute delegated work (see
    ``catalog.py``'s own discovered-facts-vs-priors separation). A provider
    with no model-catalog concept at all (``model_discovery`` unset) emits
    no check, rather than a misleading "not applicable" WARN.
    """

    if plugin.model_discovery is None:
        return None
    name = f"{plugin.id} model discovery"
    try:
        descriptors = plugin.model_discovery(context)
    except Exception as exc:  # noqa: BLE001 -- discovery must never crash doctor or the CLI
        return DoctorCheck(name, CheckStatus.WARN, f"Model discovery failed: {exc}")
    if not descriptors:
        return DoctorCheck(
            name, CheckStatus.WARN, "No models discovered or configured for this provider."
        )
    availabilities = sorted({descriptor.availability for descriptor in descriptors})
    return DoctorCheck(
        name,
        CheckStatus.OK,
        f"{', '.join(availabilities)}, count: {len(descriptors)}.",
    )


#: Check names of the person-only section: plan names and repository key
#: names are shown to the person running the CLI, never to the model.
PLANS_CHECK = "plans"
REPO_FILE_CHECK = "repository file"
REPO_SETTING_CHECK = "repository setting"
PERSON_ONLY_CHECKS = (PLANS_CHECK, REPO_FILE_CHECK, REPO_SETTING_CHECK)


def plans_check(usage_directory: Path) -> DoctorCheck:
    """What each side's plan is and where that comes from: declared vs reported."""
    from . import plans

    try:
        views = plans.current(usage_directory)
    except Exception:  # noqa: BLE001 -- a broken preference file reads unknown
        return DoctorCheck(PLANS_CHECK, CheckStatus.UNKNOWN,
                           "Could not read the plans. Set them with: modelquad settings plan")
    detail = " ".join(plans.human_lines(views))
    codex = views["codex"]
    differs = (codex.source == plans.SOURCE_OBSERVED and codex.declared is not None
               and codex.declared != plans._shown(codex.observed))
    if differs:
        return DoctorCheck(PLANS_CHECK, CheckStatus.WARN,
                           f"{detail} To match, run: modelquad settings plan codex unset")
    if all(view.kind == plans.UNKNOWN for view in views.values()):
        return DoctorCheck(PLANS_CHECK, CheckStatus.UNKNOWN, detail)
    return DoctorCheck(PLANS_CHECK, CheckStatus.OK, detail)


_REPO_MODE_WORDS = {"on": "on (Claude may decide on its own)", "off": "off (no Codex here)",
                    "offer-only": "offer-only (Claude offers)"}


def repository_checks(project_directory: Path) -> list[DoctorCheck]:
    """This repository's .modelquad.toml (valid, invalid with the fix, unknown keys)
    and the user's own setting for it. Reads local files only; starts no process."""
    from . import repo_policy, repo_settings

    checks = []
    try:
        mode = repo_settings.read_mode(project_directory)
    except Exception:  # noqa: BLE001 -- an unreadable setting is no setting
        mode = None
    checks.append(DoctorCheck(
        REPO_SETTING_CHECK, CheckStatus.OK,
        f"Your setting here: {_REPO_MODE_WORDS.get(mode, 'inherit (your global setting applies)')}. "
        "Change it with: modelquad settings repo <on|off|offer-only|inherit>"))
    policy = repo_settings.local_policy(project_directory)
    if policy is None:
        checks.append(DoctorCheck(REPO_FILE_CHECK, CheckStatus.OK,
                                  f"No {repo_policy.FILE_NAME} here; ModelQuad's own limits apply."))
        return checks
    if policy.refusal in (repo_policy.INVALID, repo_policy.UNREADABLE):
        checks.append(DoctorCheck(REPO_FILE_CHECK, CheckStatus.WARN,
                                  "Invalid: " + repo_policy.message(policy.refusal, policy)))
        return checks
    facts = policy.facts()
    parts = [f"Valid {repo_policy.FILE_NAME}.",
             "Codex: " + ("allowed" if facts["codex_allowed"] else "off") + ";",
             "kinds: " + (", ".join(facts["task_kinds"]) or "none") + ";",
             f"at most {facts['max_files']} files, {facts['max_total_kib']} KiB;",
             "account: " + ("work only" if facts["account"] == "workspace" else "any") + ";",
             "offers: " + ("only when you ask" if facts["offers_codex"] == "on_request" else "default") + "."]
    status = CheckStatus.OK
    if policy.unknown_keys:
        status = CheckStatus.WARN
        parts.append("Ignored unknown keys: " + ", ".join(policy.unknown_keys)
                     + ". Check their spelling against docs/REPO-POLICY.md, or remove them.")
    checks.append(DoctorCheck(REPO_FILE_CHECK, status, " ".join(parts)))
    return checks


def run_doctor(
    config: Config,
    *,
    usage_directory: Path | None = None,
    claude_dir: Path | None = None,
    codex_dir: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    registry: ProviderRegistry | None = None,
    windows_path: str | None = None,
    control_plane_probe: Callable[[int], Any] | None = None,
    project_directory: Path | None = None,
    include_plans: bool = False,
) -> DoctorReport:
    """Return non-throwing diagnostics for optional CLIs and integrations.

    ``include_plans`` and ``project_directory`` add the person-only checks
    (plan names, the repository file's key names): only the CLI asks for
    them, never a reply the model reads.
    """

    which = which if which is not None else hostruntime.discovery_which
    active_registry = registry or default_registry()
    enabled_ids = {plugin.id for plugin in active_registry.enabled(config)}
    checks: list[DoctorCheck] = []
    version = installed_version("n/a")
    checks.append(DoctorCheck(f"{PRODUCT_NAME} package", CheckStatus.OK, version))
    checks.append(_state_consistency_check())
    python_path = _python_path_check()
    if python_path is not None:
        checks.append(python_path)
    checks.append(_control_plane_check(probe=control_plane_probe))
    effective_windows_path = windows_path if windows_path is not None else os.environ.get("PATH")
    # None when there is no stale service record to report.
    pidfile_check = _service_pidfile_check()
    if pidfile_check is not None:
        checks.append(pidfile_check)
    worker_cli_checks: list[DoctorCheck] = []
    optional_cli_checks: list[DoctorCheck] = []
    adapter_checks: list[DoctorCheck] = []
    configuration_checks: list[DoctorCheck] = []
    capability_checks: list[DoctorCheck] = []
    model_discovery_checks: list[DoctorCheck] = []
    directory_overrides = {
        "claude": claude_dir,
        "codex": codex_dir,
    }
    integrations_enabled = global_integrations_enabled()
    for plugin in active_registry.all():
        matrix_check = _capability_matrix_check(plugin)
        if matrix_check is not None:
            capability_checks.append(matrix_check)
        if plugin.id not in enabled_ids:
            configuration_checks.append(
                DoctorCheck(
                    f"{plugin.id} provider",
                    CheckStatus.WARN,
                    "Disabled in configuration.",
                )
            )
        if plugin.cli_executable is not None:
            # A provider is a worker when ANY of its agent runtimes can be
            # spawned (V6: a plugin may declare several runtimes).
            is_worker = any(runtime.executor_factory is not None for runtime in plugin.agent_runtimes)
            cli_check = _cli_check(
                f"{plugin.id} CLI",
                plugin.cli_executable,
                # Codex resolves to the CLI a delegation actually runs.
                which=(lambda binary: which_codex(binary, which=which, native=True)) if plugin.id == "codex" else which,
                missing_status=CheckStatus.MISSING if is_worker else CheckStatus.WARN,
                missing_detail=(
                    f"Install {plugin.display_name} CLI; {plugin.display_name} delegation is unavailable."
                    if is_worker
                    else (
                        f"{plugin.display_name} CLI is optional because {plugin.display_name} "
                        "is not an executable worker."
                    )
                ),
            )
            (worker_cli_checks if is_worker else optional_cli_checks).append(cli_check)
            if plugin.id == "codex" and is_worker:
                reason = CodexExecutor().unavailable_reason()
                if reason:
                    detail = ("Codex found only as an npm launcher with no native codex.exe behind it."
                              if reason == "codex_shim_only" else reason)
                    worker_cli_checks.append(DoctorCheck("codex worker", CheckStatus.WARN, detail))
            if plugin.id == "codex" and is_worker and os.name == "nt":
                capability_checks.append(_codex_shell_check(effective_windows_path))
            if plugin.id == "codex" and is_worker:
                stale = _codex_stale_path_check(which)
                if stale is not None:
                    worker_cli_checks.append(stale)
                capability_checks.append(_codex_global_instructions_check())
        for host in plugin.host_adapters:
            configured_target = directory_overrides.get(host.id)
            if configured_target is None and not integrations_enabled:
                adapter_checks.append(_off_adapter_check(host, host.default_target_dir()))
                continue
            target_dir = (
                Path(configured_target)
                if configured_target is not None
                else host.default_target_dir()
            )
            adapter_checks.append(
                _adapter_check(
                    f"{host.id} adapter installed",
                    host.adapter_cls(),
                    target_dir,
                    f"{CLI_COMMAND} install {host.id}",
                    verified=host.verified,
                )
            )
            if host.verified and host.id in {"claude", "codex"}:
                adapter_checks.append(_mcp_registration_check(host.id, target_dir))
            # Unlike the registration check above, this one runs for EVERY host
            # including the best-effort ones: launching the wrong ModelQuad is
            # a real, user-visible split whether or not we have verified the
            # host's config convention, and the check reports only what the
            # config actually says.
            runtime_check = _mcp_runtime_check(host.id, target_dir, which=which)
            if runtime_check is not None:
                adapter_checks.append(runtime_check)
            if host.id == "claude":
                config_dir_warning = hostruntime.claude_config_dir_warning()
                if config_dir_warning is not None:
                    adapter_checks.append(
                        DoctorCheck("claude config folder", CheckStatus.WARN, config_dir_warning)
                    )
                from .adapters import claude_adapter

                check = _claude_duplicates_check(target_dir)
                if check is not None:
                    adapter_checks.append(check)
                check = _leftover_gate_check(target_dir)
                if check is not None:
                    adapter_checks.append(check)
                check = _own_status_line_check(target_dir)
                if check is not None:
                    adapter_checks.append(check)
                adapter_checks.append(_claude_version_check(
                    Path(usage_directory) if usage_directory is not None else DEFAULT_USAGE_DIRECTORY))
                check = _cli_runtime_check(
                    "claude status line runtime",
                    claude_adapter.read_status_line_command(target_dir),
                    ("usage-hook", "claude"), which=which,
                )
                if check is not None:
                    adapter_checks.append(check)
        if plugin.credential_env_vars:
            configured = credentials_configured(plugin.credential_env_vars)
            configuration_checks.append(
                DoctorCheck(
                    f"{plugin.id} configured",
                    CheckStatus.OK if configured else CheckStatus.WARN,
                    "Configured from environment variables."
                    if configured
                    else "Not configured; set environment variable(s): "
                    + ", ".join(plugin.credential_env_vars)
                    + ".",
                )
            )
        model_discovery_context = ProviderPluginContext(
            config=config,
            provider_config=config.providers.get(plugin.id, ProviderConfig()),
            usage_directory=usage_directory,
        )
        model_discovery_check = _model_discovery_check(plugin, model_discovery_context)
        if model_discovery_check is not None:
            model_discovery_checks.append(model_discovery_check)
    setup_checks = (
        _python_alias_check(path_value=effective_windows_path,
                            local_app_data=os.environ.get("LOCALAPPDATA")),
        _scripts_on_path_check(path_value=effective_windows_path),
        _git_for_windows_check(which=which),
    )
    checks.extend(check for check in setup_checks if check is not None)
    checks.extend(worker_cli_checks)
    checks.extend(optional_cli_checks)
    checks.extend(capability_checks)
    checks.extend(model_discovery_checks)

    usage_dir = Path(usage_directory) if usage_directory is not None else DEFAULT_USAGE_DIRECTORY
    usage_files = ("usage-claude.json", "usage-codex.json")
    missing_usage = [name for name in usage_files if not (usage_dir / name).is_file()]
    if missing_usage:
        checks.append(
            DoctorCheck(
                "usage sources",
                CheckStatus.WARN,
                f"Missing usage snapshots in {usage_dir}: {', '.join(missing_usage)}.",
            )
        )
    else:
        checks.append(DoctorCheck("usage sources", CheckStatus.OK, f"Found all snapshots in {usage_dir}."))

    try:
        has_mcp = util.find_spec("mcp") is not None
    except (ImportError, ValueError):
        has_mcp = False
    checks.append(
        DoctorCheck(
            "MCP dependency",
            CheckStatus.OK if has_mcp else CheckStatus.WARN,
            "MCP dependency is available."
            if has_mcp
            else "MCP is optional; install the mcp extra to use modelquad-mcp.",
        )
    )
    checks.extend(adapter_checks)
    checks.extend(configuration_checks)
    if include_plans:
        checks.append(plans_check(usage_dir))
    if project_directory is not None:
        try:
            checks.extend(repository_checks(Path(project_directory)))
        except Exception:  # noqa: BLE001 -- doctor itself never fails over a repository file
            checks.append(DoctorCheck(REPO_FILE_CHECK, CheckStatus.UNKNOWN,
                                      "Could not check this repository's .modelquad.toml."))
    worker_value = os.environ.get(WORKER_ENV_VAR)
    checks.append(
        DoctorCheck(
            "worker-guard env",
            CheckStatus.WARN if worker_value == "1" else CheckStatus.OK,
            "This doctor process is a delegated worker; recursive orchestration is disabled."
            if worker_value == "1"
            else "Top-level process; recursive orchestration guard is not active here.",
        )
    )
    return DoctorReport(tuple(checks))
