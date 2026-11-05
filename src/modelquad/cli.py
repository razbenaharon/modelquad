# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Command-line interface for setup, diagnosis, settings and task-fit advice."""

from __future__ import annotations

import argparse
import json
import sqlite3
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from .adapters import CLIENT_NAMES, InstallReport, default_target_dir, get_adapter, get_host_descriptor
from .branding import CLI_COMMAND, PRODUCT_NAME
from .config import load_config
from .doctor import DoctorReport, run_doctor
from .platformcache import preseed_windows_uname
from .models import ConsumptionBucket, ExpectedConsumption, ProviderScore, ProviderStatus, TaskType
from .paths import state_root
from .providers import build_providers
from .providers.base import DEFAULT_USAGE_DIRECTORY, read_status
from .router import Router
from .service import db as service_db
from .service import history as service_history
from .hookinput import read_hook_payload as _read_hook_payload
from .usage_hook import status_line_text as _status_line_text
from .service.lifecycle import (
    ProcessResult,
    ProcessStatus,
    RegistrationResult,
    ServiceProcessManager,
    TaskSchedulerRegistrar,
)


SERVICE_DEFAULT_HOST = "127.0.0.1"
SERVICE_DEFAULT_PORT = 8765


def _surface_choice(choices: Sequence[str]):
    """Validate a choice with one plain error that lists the valid values."""
    def validate(value: str) -> str:
        if value not in choices:
            raise argparse.ArgumentTypeError("Choose one of: " + ", ".join(choices))
        return value
    return validate


def _history_db_path(args: argparse.Namespace) -> Path:
    return args.history_db or service_db.DEFAULT_DB_PATH


def _selected_db_path(args: argparse.Namespace) -> Path:
    """The history database this invocation will actually open.

    `--history-db` is the global choice, but `serve` additionally accepts
    `--db`, and `--db` wins for the app
    they build. Anything that must agree with what the service reads and writes
    has to resolve the same file through this one function -- resolving it a
    second way is how a setting came to be stored in one database and read
    from another, one layer up from where that defect was first
    found.
    """

    return getattr(args, "db", None) or _history_db_path(args)


def _setting_not_saved(setting: str, exc: BaseException) -> int:
    """Report a setting the history database refused to store, and fail.

    A settings write raises when the database will not open; a traceback
    never says the choice was not recorded.
    The store is unchanged; this only says so in one line.
    """

    print(
        f"{setting} was not saved: the history database could not be written "
        f"({type(exc).__name__}: {exc})",
        file=sys.stderr,
    )
    return 1


def _format_percentage(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f}%"


def _format_number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))


def render_status(statuses: Sequence[ProviderStatus]) -> None:
    rows = [
        (
            status.name,
            "yes" if status.available else "no",
            _format_percentage(status.remaining_percentage),
            status.freshness.value,
            status.observed_at.isoformat() if status.observed_at else "unknown",
            status.reason or "",
        )
        for status in statuses
    ]
    _print_table(("Provider", "Available", "Remaining", "Freshness", "Observed at", "Reason"), rows)


def render_status_summary(statuses: Sequence[ProviderStatus], config, usage_directory=None) -> None:
    """Under the table: the status line's words, the plans and this folder's Codex setting."""
    from datetime import datetime, timezone

    from . import plans
    from .assistance import handoff_suggestion, statusline_text
    from .doctor import repository_checks

    now = datetime.now(timezone.utc)
    print()
    print(f"Quota now: {statusline_text(statuses, config.freshness, now=now)}")
    note = handoff_suggestion(statuses, config.freshness, now=now)
    if note is not None:
        print(f"Note: {note['message']}")
    print("Plans:")
    try:
        for line in plans.human_lines(plans.current(Path(usage_directory or DEFAULT_USAGE_DIRECTORY))):
            print(f"  {line}")
    except Exception:  # noqa: BLE001 -- the table above already answered
        print("  unknown. Set them with: modelquad settings plan")
    print("This folder:")
    try:
        for check in repository_checks(Path.cwd()):
            print(f"  {check.detail}")
    except Exception:  # noqa: BLE001
        print("  Could not check its Codex setting. Run: modelquad doctor")


def _route_row(score: ProviderScore) -> tuple[str, ...]:
    return (
        score.provider,
        "yes" if score.available else "no",
        _format_number(score.fit),
        _format_percentage(score.remaining_percentage),
        score.freshness.value,
        _format_number(score.score),
        score.reason or "",
    )


def render_route(result) -> None:
    print(f"Task type: {result.task_type.value}")
    _print_table(
        ("Provider", "Available", "Fit", "Remaining", "Freshness", "Score", "Reason"),
        [_route_row(score) for score in result.providers],
    )
    if result.recommendation:
        print(f"Recommendation: {result.recommendation}")
    else:
        print(f"Recommendation: none ({result.reason or 'no reason supplied'})")


def render_install_reports(reports: Sequence[InstallReport]) -> None:
    """Render concise per-file adapter installation results."""

    for index, report in enumerate(reports):
        if index:
            print()
        print(f"Client: {report.client}")
        _print_table(
            ("Path", "Action", "Backup", "Detail"),
            [
                (
                    str(action.path),
                    action.kind.value,
                    str(action.backup_path) if action.backup_path else "",
                    action.detail or "",
                )
                for action in report.actions
            ],
        )


def _render_install_report(report: InstallReport) -> None:
    """Render one report once its client heading has already been printed."""

    _print_table(
        ("Path", "Action", "Backup", "Detail"),
        [
            (
                str(action.path),
                action.kind.value,
                str(action.backup_path) if action.backup_path else "",
                action.detail or "",
            )
            for action in report.actions
        ],
    )


def _render_global_adapter_results(results: Sequence[dict[str, object]], command: str) -> None:
    """Render one explicit, non-fatal result block per discovered host."""

    for index, result in enumerate(results):
        if index:
            print()
        client = str(result["client"])
        descriptor = get_host_descriptor(client)
        print(f"Client: {client} ({descriptor.display_name})")
        if not result["detected"] and result["error"] is None:
            print(f"Skipped: {descriptor.display_name} was not detected on this machine.")
            continue
        if result["detected"]:
            print("Detected: yes")
        report = result["report"]
        if isinstance(report, InstallReport):
            _render_install_report(report)
        if result["error"] is not None:
            print(f"Error: {result['error']}")
        restore = result.get("restore")
        if isinstance(restore, dict):
            _render_restore_result(restore)

    succeeded = sum(1 for result in results if result["report"] is not None and result["error"] is None)
    skipped = sum(1 for result in results if not result["detected"] and result["error"] is None)
    errors = sum(1 for result in results if result["error"] is not None)
    verb = "Installed" if command == "install" else "Uninstalled"
    print()
    print(f"{verb}: {succeeded}, Skipped (not detected): {skipped}, Errors: {errors}")


def _global_adapter_arguments_are_valid(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Keep --global a distinct invocation mode from legacy target selection."""

    if args.client or args.all_clients or args.target_dir is not None or any(
        path is not None for path in (args.claude_dir,)
    ):
        parser.error("--global may not be combined with a client, --all, or target-directory overrides")


def _registration_before_uninstall(
    client: str, target_dir: Path, method_name: str
) -> tuple[dict | None, str | None]:
    """The ModelQuad registration a host records, read before anything removes it.

    The compare half of compare-before-restore. It has to be taken here,
    because the adapter's uninstall deletes the entry it would otherwise be
    compared against.

    Both halves are returned: the whole entry decides whether this is still a
    value ModelQuad wrote (an upgrade that rewrites only ``args`` is still
    ours), and the command is what may be shown to a person.
    """

    if method_name != "uninstall":
        return None, None
    from .hostruntime import read_registered_command, read_registered_entry

    return (
        read_registered_entry(client, target_dir),
        read_registered_command(client, target_dir),
    )


def _restore_displaced_registration(
    client: str,
    target_dir: Path,
    args: argparse.Namespace,
    method_name: str,
    before: tuple[dict | None, str | None],
) -> dict[str, object] | None:
    """Give back the registration this installation displaced, after removing ours.

    Uninstalling used to simply delete ``mcpServers.modelquad`` -- which, on a
    machine that had its own ModelQuad registration before ModelQuad was
    installed, destroyed a configuration the product never owned. The write
    goes after the adapter's removal so the removal cannot undo it, and the
    judgement uses ``current_command``, taken before that removal.

    Returns ``None`` when nothing was displaced, which is the ordinary case
    and stays indistinguishable from today's behaviour.
    """

    if method_name != "uninstall":
        return None
    from .host_integrations import global_integrations_enabled

    overrides = {"claude": args.claude_dir}
    explicit = args.target_dir is not None or overrides.get(client) is not None
    if not explicit and not global_integrations_enabled():
        # Restoring an earlier ModelQuad entry would undo the global opt-out.
        return None
    from .adapters.registration import RestoreStatus, restore_previous_registration

    before_entry, before_command = before
    result = restore_previous_registration(
        client,
        target_dir,
        dry_run=args.dry_run,
        current_command=before_command,
        current_entry=before_entry,
    )
    if result.status is RestoreStatus.NOTHING_RECORDED:
        return None
    return result.to_dict()


#: What each restore outcome is called on a terminal. The status codes are for
#: ``--json``; a person uninstalling needs to be told, in a sentence, whether
#: something of theirs came back -- especially in the USER_MODIFIED case, where
#: the honest news is that ModelQuad deliberately did NOT touch their newer
#: registration. Rendering nothing at all, as this path used to, left the one
#: question an uninstall raises unanswered.
#: ``removed`` is deliberately absent: it is the ordinary fresh-install
#: uninstall, and a line about it on every normal machine would be noise. It
#: is still carried in ``--json``, where distinguishing "removed ours" from
#: "left the user's alone" is exactly what a caller needs.
_RESTORE_HEADLINES = {
    "restored": "Restored the ModelQuad registration that was in place before this installation.",
    "user_modified": (
        "Kept your current ModelQuad registration: it was changed after this "
        "installation wrote it, so the earlier one was not put back."
    ),
    "failed": (
        "Could not restore the earlier ModelQuad registration. It is still "
        "recorded, so it can be restored on a later attempt."
    ),
}


def _render_restore_result(restore: dict[str, object] | None) -> None:
    """Print one line about what an uninstall did with a displaced registration.

    Prints nothing when nothing was displaced, so an ordinary uninstall looks
    exactly as it always has. Only the launch command is ever shown -- the
    stored entry may carry an ``env`` block.
    """

    if not restore:
        return
    status = str(restore.get("status", ""))
    if status == "already_removed":
        # Two different truths share this status (whether an earlier entry is
        # still kept), so the detail sentence is the headline.
        print(f"Registration: {restore.get('detail', '')}")
        return
    headline = _RESTORE_HEADLINES.get(status)
    if headline is None:
        return
    print(f"Registration: {headline}")
    restored = restore.get("restored_command")
    if status == "restored" and isinstance(restored, str) and restored:
        print(f"  Restored command: {restored}")


def _refuse_unsupported_platform(as_json: bool) -> int | None:
    """Exit code 1 with a plain message off native Windows; None otherwise."""

    from .hostruntime import unsupported_platform_message

    message = unsupported_platform_message()
    if message is None:
        return None
    if as_json:
        print(json.dumps({"error": message}))
    else:
        print(message, file=sys.stderr)
    return 1


def _install_notices(as_json: bool) -> dict[str, object]:
    """Show the data-flow notice (and any setup warning) before install writes.

    Printed first in text mode, because the notice is what a person agrees to
    by continuing; returned as fields for ``--json``, where printing it would
    corrupt the one object a caller parses.
    """

    from .disclosure import DATA_FLOW, print_lines
    from .hostruntime import claude_config_dir_warning

    warning = claude_config_dir_warning()
    if not as_json:
        print_lines(DATA_FLOW)
        print("")
        if warning is not None:
            print(f"Warning: {warning}", file=sys.stderr)
            print("")
    fields: dict[str, object] = {"data_flow_notice": list(DATA_FLOW)}
    if warning is not None:
        fields["warnings"] = [warning]
    return fields


def _adapter_for(client: str, args: argparse.Namespace, factory=None) -> object:
    """The adapter for one client, honouring a ``--plugin`` / ``--no-plugin`` given now."""

    choice = getattr(args, "plugin", None)
    if client == "claude" and choice is not None:
        from .adapters.claude_adapter import ClaudeAdapter

        return ClaudeAdapter(via_plugin=choice)
    return factory() if factory is not None else get_adapter(client)


def _note_notice_shown(args: argparse.Namespace, notices: dict, reports: list) -> None:
    """Record that an install showed the data-flow notice, so Codex calls need not.

    Only a real install that showed it and wrote something counts; a dry run,
    or an install refused before writing, does not.
    """

    if args.dry_run or "data_flow_notice" not in notices or not reports:
        return
    from .disclosure import record_notice_shown

    try:
        record_notice_shown("install")
    except OSError:
        pass  # The first Codex call then shows the notice instead.


PLUGIN_CHOICE_NOT_SAVED = (
    "Installed, but the plugin choice was not saved (ModelQuad could not write its "
    "preferences.json), so a later repair may install the direct server again. Run this "
    "install again once the file can be written.")


def _remember_plugin_choice(args: argparse.Namespace) -> str | None:
    """Save ``--plugin`` / ``--no-plugin`` once an install has used it, so a repair keeps it.

    Runs after the adapter changed files, so it never raises: a choice that
    could not be saved is returned as the client's error line instead.
    """

    choice = getattr(args, "plugin", None)
    if choice is None or args.dry_run:
        return None
    from .adapters.server_instructions import write_claude_plugin

    try:
        write_claude_plugin(choice)
    except OSError:
        return PLUGIN_CHOICE_NOT_SAVED
    return None


def _mcp_registration_skipped(
    client: str, target_dir: Path, report: InstallReport, adapter: object = None
) -> str | None:
    """The reason an install left the host unregistered, or None.

    Every other skipped action can be a deliberate preservation (a user's own
    status line), but without the MCP entry Claude Code cannot reach
    ModelQuad at all, so the install has not done its job. Beside the plugin
    install registers nothing, and the plugin's server is the registration.
    """

    from .adapters.report import ActionKind
    from .hostruntime import json_mcp_config_path

    if getattr(adapter, "via_plugin", False):
        return None

    config_path = json_mcp_config_path(client, Path(target_dir))
    for action in report.actions:
        if action.kind is ActionKind.SKIPPED and config_path is not None and Path(action.path) == config_path:
            return action.detail or f"{config_path} was not changed."
    return None


def _status_line_recipe(client: str, target_dir: Path) -> str | None:
    """The one-line recipe for adding ModelQuad to the user's own status line."""

    if client != "claude":
        return None
    try:
        from .adapters.claude_adapter import read_status_line_command, status_line_recipe

        return status_line_recipe(read_status_line_command(Path(target_dir)))
    except (OSError, ValueError):
        return None


def _print_status_line_recipe(recipe: str | None) -> None:
    if recipe is None:
        return
    print("")
    print("Your own status line was kept. To also show ModelQuad's quota on it:")
    print(f"  {recipe}")


def _run_global_adapter_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from .adapters.registration import BackupPersistenceError

    _global_adapter_arguments_are_valid(args, parser)
    from .host_integrations import OFF_GUIDANCE, global_integrations_enabled

    if args.command == "install":
        refused = _refuse_unsupported_platform(args.as_json)
        if refused is not None:
            return refused
    if args.command == "install" and not global_integrations_enabled():
        if args.as_json:
            print(json.dumps({"error": OFF_GUIDANCE}))
        else:
            print(OFF_GUIDANCE, file=sys.stderr)
        return 1
    integrations_off = getattr(args, "integrations_off", False)
    method_name = "install" if args.command == "install" else "uninstall"
    notices = _install_notices(args.as_json) if method_name == "install" else {}
    results: list[dict[str, object]] = []
    for client in CLIENT_NAMES:
        try:
            descriptor = get_host_descriptor(client)
            detected = True if integrations_off or descriptor.detect is None else bool(descriptor.detect())
        except (OSError, ValueError) as exc:
            results.append({"client": client, "detected": False, "report": None, "error": str(exc)})
            continue
        if not detected:
            results.append({"client": client, "detected": False, "report": None, "error": None})
            continue
        target_dir = descriptor.default_target_dir()
        # Turning integrations off removes ModelQuad entirely, including a
        # registration predating this installation. Ordinary uninstall keeps
        # its existing displaced-registration restoration contract.
        before = (None, None) if integrations_off else _registration_before_uninstall(client, target_dir, method_name)
        adapter = (_adapter_for(client, args, descriptor.adapter_cls) if method_name == "install"
                   else descriptor.adapter_cls())
        try:
            if method_name == "install":
                from .adapters.registration import install_with_registration

                report = install_with_registration(
                    client, target_dir, adapter, dry_run=args.dry_run
                )
            else:
                report = adapter.uninstall(target_dir, dry_run=args.dry_run)
        except (OSError, ValueError, BackupPersistenceError) as exc:
            # A failed uninstall still owes the user their earlier registration.
            restore = None if integrations_off else _restore_displaced_registration(
                client, target_dir, args, method_name, before
            )
            results.append({"client": client, "detected": True, "report": None,
                            "error": str(exc), "restore": restore})
            continue
        restore = None if integrations_off else _restore_displaced_registration(
            client, target_dir, args, method_name, before
        )
        not_saved = None
        if method_name == "install":
            if client == "claude":
                not_saved = _remember_plugin_choice(args)
            skipped = _mcp_registration_skipped(client, target_dir, report, adapter)
            if skipped is not None:
                results.append({"client": client, "detected": True, "report": report,
                                "error": f"Not registered: {skipped}", "restore": None})
                continue
        results.append(
            {
                "client": client,
                "detected": True,
                "report": report,
                "error": not_saved,
                "restore": restore,
                "status_line_recipe": (
                    _status_line_recipe(client, target_dir) if method_name == "install" else None
                ),
            }
        )

    if method_name == "install":
        _note_notice_shown(args, notices, [result for result in results if result.get("report")])
    incomplete: list[dict[str, str]] = []
    if integrations_off:
        from .adapters.report import ActionKind

        for result in results:
            report = result["report"]
            if report is None:
                continue
            for action in report.actions:
                if action.kind is ActionKind.SKIPPED:
                    incomplete.append({
                        "client": str(result["client"]),
                        "path": str(action.path),
                        "detail": action.detail or "Adapter skipped removal; wiring may remain.",
                        "next_step": (
                            "Inspect this file and remove the remaining ModelQuad wiring, "
                            "preserving unrelated settings, then rerun modelquad integrations off."
                        ),
                    })

    if args.as_json:
        print(
            json.dumps(
                {
                    "global": True,
                    **notices,
                    **({"global_host_integrations": "on" if global_integrations_enabled() else "off",
                        "incomplete": incomplete,
                        **({"would_set": "off"} if args.dry_run else {})}
                       if integrations_off else {}),
                    "dry_run": args.dry_run,
                    "results": [
                        {
                            "client": result["client"],
                            "detected": result["detected"],
                            "report": result["report"].to_dict() if result["report"] is not None else None,
                            "error": result["error"],
                            "restore": result.get("restore"),
                            "status_line_recipe": result.get("status_line_recipe"),
                        }
                        for result in results
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _render_global_adapter_results(results, args.command)
        for result in results:
            _print_status_line_recipe(result.get("status_line_recipe"))
        for item in incomplete:
            print(f"Removal incomplete: {item['client']} {item['path']}: {item['detail']}")
            print(item["next_step"])
    failed_restore = any(
        isinstance(result.get("restore"), dict)
        and result["restore"].get("status") == "failed"
        for result in results
    )
    return 1 if incomplete or any(result["error"] is not None for result in results) or failed_restore else 0


def render_state_survey(survey) -> None:
    """Render the developer-facing state survey.

    Deliberately technical: paths, executables and store contents are exactly
    what a mixed-runtime diagnosis needs, and this command exists for the
    person diagnosing one. The normal-user surface says only that ModelQuad
    needs repair, and offers the button.
    """

    context = survey.context
    print(f"Runtime type:  {context.mode.value} (decided by: {context.source.value})")
    print(f"Executable:    {context.executable or 'unknown'}")
    print(f"State root:    {context.state_root}")
    print(f"Database:      {context.database_path}")
    if context.fallback:
        print("               (fallback path)")
    print()

    rows = []
    for store in survey.stores:
        if not store.exists:
            rows.append((str(store.root), store.mode.value, "absent", "-", "-"))
            continue
        if not store.database_exists:
            rows.append((str(store.root), store.mode.value, "no database", "-", "-"))
            continue
        size = "unknown" if store.database_bytes is None else f"{store.database_bytes:,} B"
        schema = "unknown" if store.schema_version is None else str(store.schema_version)
        rows.append(
            (
                str(store.root),
                store.mode.value,
                size,
                f"schema {schema}",
                store.last_activity or "never used",
            )
        )
    _print_table(("State root", "Mode", "Database", "Schema", "Last activity"), rows)
    print()

    if survey.hosts:
        _print_table(
            ("Host", "Surface", "Launches", "Resolves to", "Canonical"),
            [
                (
                    host.host_id,
                    host.surface.replace("_", " "),
                    host.command or "-",
                    host.resolved or "unresolved",
                    "yes" if host.canonical else "NO",
                )
                for host in survey.hosts
            ],
        )
        print()

    if survey.consistent:
        print("State consistency: OK")
        if survey.notes:
            print("Preserved state notes:")
            for note in survey.notes:
                print(f"  - {note}")
        return
    print("State consistency: MIXED")
    for finding in survey.findings:
        print(f"  - {finding}")
    if survey.host_findings:
        # `repair` re-points host integrations; it stops no process, so it is
        # offered only when a host registration is what is wrong. A live
        # foreign service carries its own recovery inside its finding.
        print()
        print(f"Run `{CLI_COMMAND} repair` to point host integrations at this installation.")


def render_doctor(report: DoctorReport) -> None:
    """Render diagnostics by Core and client/provider without dropping checks."""

    core_prefixes = (
        "setup: ",
        f"{PRODUCT_NAME} package",
        f"{PRODUCT_NAME} uninstall residue",
        "usage sources",
        "MCP dependency",
        "worker-guard env",
    )
    try:
        from .registry import default_registry

        registry = default_registry()
        labels = {plugin.id: plugin.display_name for plugin in registry.all()}
        labels.update({get_host_descriptor(client).id: get_host_descriptor(client).display_name for client in CLIENT_NAMES})
    except (OSError, ValueError):
        # This render path should never conceal a usable report if registry
        # inspection unexpectedly fails.
        _print_table(
            ("Check", "Status", "Detail"),
            [(check.name, check.status.value, check.detail) for check in report.checks],
        )
    else:
        from .doctor import PERSON_ONLY_CHECKS

        sections: dict[str, list] = {"Core": []}
        for check in report.checks:
            if check.name in PERSON_ONLY_CHECKS:
                section = "Plans and this repository"
            elif check.name.startswith(core_prefixes):
                section = "Core"
            else:
                matching_ids = [
                    identifier
                    for identifier in labels
                    if check.name.startswith(f"{identifier} ")
                ]
                section = labels[max(matching_ids, key=len)] if matching_ids else "Other"
            sections.setdefault(section, []).append(check)
        for section, checks in sections.items():
            if not checks:
                continue
            print(f"{section}:")
            _print_table(
                ("Check", "Status", "Detail"),
                [(check.name, check.status.value, check.detail) for check in checks],
            )
            print()

    package_check = next((check for check in report.checks if check.name == f"{PRODUCT_NAME} package"), None)
    print(f"Package version: {package_check.detail if package_check is not None else 'n/a'}")
    attention = [check.name for check in report.checks if check.status.value in ("missing", "warn")]
    if attention:
        print(f"Needs attention ({len(attention)}): {', '.join(attention)}. "
              "Each one's Detail above says what to do.")
    print(f"Overall: {'HEALTHY' if report.ok else 'DEGRADED'}")


def render_first_run_checklist(report: DoctorReport) -> None:
    """The doctor report as a short checklist, in plain words."""
    from .doctor import first_run_checklist

    items = first_run_checklist(report)
    print()
    print("Checklist:")
    for done, item in items:
        print(f"  [{'x' if done else ' '}] {item}")
    remaining = sum(1 for done, _item in items if not done)
    print()
    print("All set." if remaining == 0 else
          f"{remaining} thing{'s' if remaining != 1 else ''} left to do; "
          f"run {CLI_COMMAND} doctor --first-run again afterwards.")


def _global_host_summaries(report: DoctorReport) -> list[dict[str, object]]:
    """Summarize hosts using doctor adapter checks as the install-state source."""

    checks = {check.name: check for check in report.checks}
    summaries: list[dict[str, object]] = []
    for client in CLIENT_NAMES:
        descriptor = get_host_descriptor(client)
        try:
            detected = True if descriptor.detect is None else bool(descriptor.detect())
        except (OSError, ValueError):
            detected = False
        installed_check = checks.get(f"{client} adapter installed")
        summaries.append(
            {
                "id": client,
                "display_name": descriptor.display_name,
                "detected": detected,
                "installed": installed_check is not None and installed_check.status.value == "ok",
                "verified": descriptor.verified,
            }
        )
    return summaries


def _service_process_summary(status: ProcessStatus) -> str:
    if not status.supported:
        return status.message or "not supported on this platform yet"
    if status.running:
        readiness = "ready" if status.ready is True else "not ready" if status.ready is False else "readiness unknown"
        if status.reason == "service_process_identity_unconfirmed":
            return f"possibly running (PID {status.pid}; identity unconfirmed; {readiness})"
        if status.pid is None:
            return f"possibly running (PID unknown; {readiness})"
        return f"running (PID {status.pid}; {readiness})"
    return "not running"


def render_global_status(
    statuses: Sequence[ProviderStatus], report: DoctorReport, process_status: ProcessStatus | None = None
) -> None:
    """Render status plus the global control-plane host overview."""

    render_status(statuses)
    print()
    print("Host adapters:")
    hosts = _global_host_summaries(report)
    _print_table(
        ("ID", "Display name", "Detected", "Installed", "Verified"),
        [
            (
                str(host["id"]),
                str(host["display_name"]),
                "yes" if host["detected"] else "no",
                "yes" if host["installed"] else "no",
                "yes" if host["verified"] else "no",
            )
            for host in hosts
        ],
    )
    package_check = next((check for check in report.checks if check.name == f"{PRODUCT_NAME} package"), None)
    print()
    print("Control plane:")
    print(f"Package version: {package_check.detail if package_check is not None else 'n/a'}")
    process_status = process_status or ServiceProcessManager().status()
    print(f"Service/background process: {_service_process_summary(process_status)}")
    print(f"Overall: {'HEALTHY' if report.ok else 'DEGRADED'}")


def render_activity(entries: Sequence[dict[str, object]]) -> None:
    """Render the mixed routing/execution history in compact readable lines."""

    if not entries:
        print("No activity found.")
        return
    for index, entry in enumerate(entries):
        if index:
            print()
        provider = entry.get("provider") or entry.get("selected_provider") or entry.get("recommendation")
        fields = [str(entry.get("created_at", "unknown")), str(entry.get("kind", "unknown"))]
        if entry.get("project"):
            fields.append(f"project={entry['project']}")
        if entry.get("task_type"):
            fields.append(f"task_type={entry['task_type']}")
        if provider:
            fields.append(f"provider={provider}")
        if entry.get("result"):
            fields.append(f"result={entry['result']}")
        elif entry.get("success") is not None:
            fields.append(f"success={'yes' if entry['success'] else 'no'}")
        print(" | ".join(fields))


def _adapter_targets(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[tuple[str, Path], ...]:
    if bool(args.client) == bool(args.all_clients):
        parser.error("specify exactly one adapter client or --all")
    per_client_options = (args.claude_dir,)
    if args.all_clients:
        if args.target_dir is not None:
            parser.error("--target-dir may only be used with a single adapter client")
        overrides = {"claude": args.claude_dir}
        return tuple(
            (client, Path(overrides[client]) if overrides.get(client) is not None else default_target_dir(client))
            for client in CLIENT_NAMES
        )
    if any(path is not None for path in per_client_options):
        parser.error("--claude-dir requires --all")
    assert args.client is not None
    return ((args.client, args.target_dir or default_target_dir(args.client)),)


def _run_adapter_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from .adapters.registration import BackupPersistenceError

    if args.global_install:
        return _run_global_adapter_command(args, parser)
    targets = _adapter_targets(args, parser)
    if args.command == "install":
        refused = _refuse_unsupported_platform(args.as_json)
        if refused is not None:
            return refused
    from .host_integrations import OFF_GUIDANCE, global_integrations_enabled

    integrations_enabled = global_integrations_enabled()
    overrides = {"claude": args.claude_dir}
    reports: list[InstallReport] = []
    errors: list[dict[str, str]] = []
    restores: list[dict[str, object]] = []
    recipes: list[str | None] = []
    method_name = "install" if args.command == "install" else "uninstall"
    will_install = method_name == "install" and any(
        integrations_enabled or (
            overrides.get(client) is not None if args.all_clients else args.target_dir is not None
        )
        for client, _target in targets
    )
    notices = _install_notices(args.as_json) if will_install else {}
    for client, target_dir in targets:
        explicit = overrides.get(client) is not None if args.all_clients else args.target_dir is not None
        if method_name == "install" and not integrations_enabled and not explicit:
            errors.append({"client": client, "error": OFF_GUIDANCE})
            continue
        before = _registration_before_uninstall(client, target_dir, method_name)
        adapter = _adapter_for(client, args) if method_name == "install" else get_adapter(client)
        try:
            if method_name == "install":
                from .adapters.registration import install_with_registration

                report = install_with_registration(
                    client, target_dir, adapter, dry_run=args.dry_run
                )
            else:
                report = adapter.uninstall(target_dir, dry_run=args.dry_run)
        except (OSError, ValueError, BackupPersistenceError) as exc:
            errors.append({"client": client, "error": str(exc)})
            # A failed uninstall still owes the user their earlier registration.
            restore = _restore_displaced_registration(client, target_dir, args, method_name, before)
            if restore is not None:
                restores.append(restore)
            continue
        restore = _restore_displaced_registration(client, target_dir, args, method_name, before)
        if restore is not None:
            restores.append(restore)
        reports.append(report)
        if method_name == "install":
            if client == "claude":
                not_saved = _remember_plugin_choice(args)
                if not_saved is not None:
                    errors.append({"client": client, "error": not_saved})
            recipes.append(_status_line_recipe(client, target_dir))
            skipped = _mcp_registration_skipped(client, target_dir, report, adapter)
            if skipped is not None:
                errors.append({"client": client, "error": f"Not registered: {skipped}"})
    if method_name == "install":
        _note_notice_shown(args, notices, reports)
    if args.as_json:
        if args.all_clients:
            print(
                json.dumps(
                    {
                        **notices,
                        "reports": [report.to_dict() for report in reports],
                        "errors": errors,
                        "restores": restores,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        elif reports:
            payload = reports[0].to_dict()
            # Attached rather than printed separately so a caller parsing one
            # object still sees what happened to the registration.
            payload["restore"] = restores[0] if restores else None
            if recipes and recipes[0] is not None:
                payload["status_line_recipe"] = recipes[0]
            payload.update(notices)
            if errors:
                payload["error"] = errors[0]["error"]
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(json.dumps({**errors[0], **notices}, indent=2, sort_keys=True))
    else:
        render_install_reports(reports)
        for recipe in recipes:
            _print_status_line_recipe(recipe)
        for restore in restores:
            _render_restore_result(restore)
        for error in errors:
            print(f"{error['client']}: {error['error']}")
    # A restore that failed is a real failure: the user asked to uninstall and
    # something they owned could not be given back.
    failed_restore = any(r.get("status") == "failed" for r in restores)
    return 1 if errors or failed_restore else 0


def _run_plan_setting(provider: str | None, value: str | None) -> int:
    """Show, set or unset the plan the user says they are on with each provider."""
    from . import plans

    if provider is not None and value is not None:
        try:
            plans.write_declared(provider, None if value.strip().casefold() == "unset" else value)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"The setting was not saved: {exc.strerror or exc}", file=sys.stderr)
            return 1
    views = plans.current(DEFAULT_USAGE_DIRECTORY)
    print("Plans:")
    for line in plans.human_lines(views):
        print(f"  {line}")
    if provider is not None and value is None:
        print(f"{provider.capitalize()} plans you can set: "
              f"{', '.join(plans.DECLARABLE_PLANS[provider])}, or unset.")
    print("Stored only on this computer, in preferences.json. Claude sees only the account kind "
          "(personal, workspace or unknown), never the plan name.")
    return 0


_REPO_MODE_TEXT = {
    "on": "on: Claude may decide on its own to consult Codex in this repository.",
    "off": "off: no Codex in this repository; ModelQuad refuses every Codex call here.",
    "offer-only": ("offer-only: Claude offers Codex here when it would help and calls it "
                   "when you agree or ask."),
}


_NOT_LOCAL_FOLDER = ("That folder is not a local folder ModelQuad can use (a network share, a "
                     "device, or a link to one). Use the project's folder on a local drive.")


def _run_split_summary(args: argparse.Namespace, config) -> int:
    """``modelquad split summary``: read-only, from ModelQuad's own history."""
    from datetime import datetime, timezone

    from . import split_summary
    from .adapters.pending_reminder import project_key

    from .localpath import reason as local_reason

    folder = Path(args.project) if args.project else Path.cwd()
    # Before any file-system call: a network share is never opened.
    if local_reason(os.path.abspath(folder)) is not None:
        print(_NOT_LOCAL_FOLDER, file=sys.stderr)
        return 2
    if not folder.is_dir():
        print("That project folder does not exist.", file=sys.stderr)
        return 2
    key = project_key(str(folder.resolve()))
    if key is None:
        print("That project folder cannot be used.", file=sys.stderr)
        return 2
    current = tuple(read_status(provider, refresh=False)
                    for provider in build_providers(config, args.usage_directory))
    summary = split_summary.summarize(
        _history_db_path(args), workspace_key=key, period=args.since,
        now=datetime.now(timezone.utc), current=current, thresholds=config.freshness)
    if args.as_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        for line in summary["lines"]:
            print(line)
    return 0 if summary["known"] else 2


def _run_repo_setting(choice: str | None, project: str | None) -> int:
    """Show or set the user's own Codex setting for one repository."""
    from . import repo_policy, repo_settings
    from .adapters.server_instructions import read_proactive
    from .localpath import reason as local_reason

    folder = Path(project) if project else Path.cwd()
    # Before any file-system call: a network share is never opened.
    if local_reason(os.path.abspath(folder)) is not None:
        print(_NOT_LOCAL_FOLDER, file=sys.stderr)
        return 2
    if not folder.is_dir():
        print("That project folder does not exist.", file=sys.stderr)
        return 2
    if choice is not None:
        try:
            repo_settings.write_mode(folder, choice)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"The setting was not saved: {exc.strerror or exc}", file=sys.stderr)
            return 1
    mode = repo_settings.read_mode(folder)
    if mode is None:
        print("This repository: inherit (your global setting applies).")
    else:
        print(f"This repository: {_REPO_MODE_TEXT[mode]}")
    global_proactive = read_proactive()
    result = repo_settings.effective(global_proactive=global_proactive, user_mode=mode,
                                     policy=repo_policy.load(folder))
    if result.refusal is not None and result.refusal != repo_settings.USER_CODEX_OFF:
        print(f"In effect: Codex is refused here by this repository's {repo_policy.FILE_NAME}.")
    elif result.refusal is None:
        print({repo_settings.PROACTIVE: "In effect: Claude may decide on its own to consult Codex.",
               repo_settings.OFFER: "In effect: Claude offers Codex and calls it when you agree or ask.",
               repo_settings.ON_REQUEST: ("In effect: Claude calls Codex only when you ask "
                                          f"(this repository's {repo_policy.FILE_NAME}).")}[result.offer])
    print("Choices: on, off, offer-only, inherit. Claude Code still asks you before each "
          "ModelQuad Codex call.")
    print("Stored in this computer's preferences.json under a keyed hash of the repository, "
          "never its path or name. off applies from the next call; Claude is told the rest when "
          "a new Claude Code session starts here.")
    return 0


def _run_settings_page(project: str | None, *, no_browser: bool = False,
                       open_browser=None) -> int:
    """``modelquad settings open``: serve the one-time settings page until Done or idle."""
    import webbrowser

    from . import settings_page

    folder = settings_page.project_folder(project)
    if project is not None and folder is None:
        print("That project folder does not exist or is not a local folder ModelQuad can use.",
              file=sys.stderr)
        return 2
    try:
        page = settings_page.SettingsPage(folder, DEFAULT_USAGE_DIRECTORY)
    except OSError as exc:
        print(f"The settings page could not start: {exc.strerror or exc}", file=sys.stderr)
        return 1
    opened = False
    if not no_browser:
        try:
            opened = bool((open_browser or webbrowser.open)(page.url))
        except Exception:  # noqa: BLE001 -- no browser means the address is printed instead
            opened = False
    if opened:
        print("ModelQuad settings are open in your browser.")
    else:
        print("Open this address in your browser (it works only on this computer, "
              "and only while this command runs):")
        print(f"  {page.url}")
    print("Press Done on the page when you finish. The page closes by itself after "
          f"{settings_page.IDLE_SECONDS // 60} minutes without use.")
    try:
        ended = page.serve()
    except KeyboardInterrupt:
        ended = "interrupted"
    print({"done": "Settings page closed. New Claude Code sessions use your settings.",
           "idle": "Settings page closed after 15 minutes without use. Saved choices stay saved.",
           "interrupted": "Settings page closed. Saved choices stay saved."}[ended])
    return 0


def _run_proactive_setting(enabled: bool) -> int:
    """Save who decides to consult Codex; the MCP server reads it at session start."""
    from .adapters.server_instructions import write_proactive

    try:
        write_proactive(enabled)
    except OSError as exc:
        print(f"The setting was not saved: {exc.strerror or exc}", file=sys.stderr)
        return 1
    if enabled:
        print("Proactive use: on. Claude may decide on its own to consult Codex.")
    else:
        print("Proactive use: off. Claude offers Codex when it would help and calls it "
              "when you agree or ask.")
    print("Claude Code still asks you before each ModelQuad Codex call.")
    print("Takes effect in new Claude Code sessions (or after /mcp reconnects modelquad).")
    return 0


def _background_work_consented(args: argparse.Namespace, *, input_fn=input) -> bool:
    """Separate, explicit consent before the job runner is turned on.

    Shows what background work means, then asks for a typed yes at a terminal
    or requires ``--yes`` where nobody can answer. Silence is never consent.
    """
    from .disclosure import BACKGROUND_CONSENT_ANSWER, BACKGROUND_WORK, print_lines

    print_lines(BACKGROUND_WORK)
    if getattr(args, "consent", False):
        return True
    if not sys.stdin or not sys.stdin.isatty():
        print("Not changed. After reading the notice above, run again with --yes.", file=sys.stderr)
        return False
    try:
        answer = input_fn(f"Type {BACKGROUND_CONSENT_ANSWER} to turn it on: ")
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer.strip().lower() != BACKGROUND_CONSENT_ANSWER:
        print("Not changed.", file=sys.stderr)
        return False
    return True


def _run_integrations_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from .host_integrations import global_integrations_enabled, set_global_integrations

    if args.integrations_action != "status" and not args.dry_run:
        try:
            set_global_integrations(args.integrations_action == "on")
        except OSError as exc:
            if args.as_json:
                print(json.dumps({"error": str(exc)}))
            else:
                print(f"Could not save host integration choice: {exc}", file=sys.stderr)
            return 1
    if args.integrations_action == "off":
        uninstall_args = parser.parse_args([
            "uninstall", "--global",
            *(["--json"] if args.as_json else []),
            *(["--dry-run"] if args.dry_run else []),
        ])
        uninstall_args.integrations_off = True
        if not args.as_json:
            print("Would turn global host integrations off." if args.dry_run
                  else "Global host integrations are off by choice; removing existing wiring.")
        return _run_global_adapter_command(uninstall_args, parser)

    enabled = global_integrations_enabled()
    payload: dict[str, object] = {
        "global_host_integrations": "on" if enabled else "off",
        "dry_run": args.dry_run,
    }
    if args.integrations_action == "on":
        payload["next_step"] = "Run modelquad onboard to connect your AI tools."
        if args.dry_run:
            payload["would_set"] = "on"
    else:
        hosts: dict[str, object] = {}
        for client in CLIENT_NAMES:
            try:
                descriptor = get_host_descriptor(client)
                hosts[client] = {"is_installed": bool(
                    descriptor.adapter_cls().is_installed(descriptor.default_target_dir()))}
            except (OSError, ValueError) as exc:
                hosts[client] = {"is_installed": None, "error": str(exc)}
        payload["hosts"] = hosts
    if args.as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Global host integrations: {payload['global_host_integrations']}")
        if "next_step" in payload:
            if args.dry_run:
                print("Would turn global host integrations on.")
            print(payload["next_step"])
        for client, host in payload.get("hosts", {}).items():
            installed = host["is_installed"]
            print(f"{client}: " + ("unknown" if installed is None else
                                      "installed" if installed else "not installed"))
    return 0


def _run_mcp_command(args: argparse.Namespace) -> int:
    try:
        from .mcp.server import main as mcp_main
    except ImportError:
        print('Install the mcp extra: pip install -e ".[mcp]"', file=sys.stderr)
        return 1
    mcp_main()
    return 0



def _bind_absent_std_streams() -> None:
    """Discard redirected service output; only closed diagnostic events persist.

    Console command results and protocol stdout remain owned by their callers.
    The managed service child is always a background process, including when
    a foreground controller waits for it.
    """

    from .windowless_diagnostics import bootstrap, bind_selected_state, emit
    bootstrap(force=True)
    bind_selected_state('service')
    emit('entry')


def _run_service_command(args: argparse.Namespace) -> int:
    """Legacy foreground serve shares managed startup with the logon controller."""
    if getattr(args, "managed_child", False):
        return _run_managed_service_child(args)
    return _run_managed_service_start(args, foreground=True)


def _apply_service_state(args: argparse.Namespace) -> None:
    from .statecontext import STATE_ROOT_ENV_VAR, STATE_MODE_ENV_VAR
    root = getattr(args, "state_root", None)
    if root is not None:
        if not root.is_absolute():
            raise ValueError("service_cwd_unavailable")
        os.environ[STATE_ROOT_ENV_VAR] = str(root.resolve())
    mode = getattr(args, "state_mode", None)
    if mode is not None:
        os.environ[STATE_MODE_ENV_VAR] = mode


def _run_managed_service_start(args: argparse.Namespace, *, foreground: bool = False) -> int:
    from .executable_resolution import UnsafeExecutable
    from .service.lifecycle import SERVICE_DETAILS
    try:
        _apply_service_state(args)
        command = _service_launch_command(args.port, args)
        if not getattr(args, "dry_run", False) and _service_dependencies() is None:
            return 1
        manager = ServiceProcessManager()
        result = manager.start(command=command, dry_run=getattr(args, "dry_run", False))
    except (UnsafeExecutable, ValueError, OSError) as exc:
        code = exc.reason if isinstance(exc, UnsafeExecutable) else (
            str(exc) if str(exc) in SERVICE_DETAILS else "invalid_path")
        print(SERVICE_DETAILS.get(code, "The service executable could not be admitted."), file=sys.stderr)
        return 1
    except Exception:
        print(SERVICE_DETAILS["invalid_path"], file=sys.stderr)
        return 1
    if foreground:
        print(PRODUCT_NAME)
        print()
    _render_process_result(result)
    if result.contained == "containment_unknown":
        print("ModelQuad could not check whether the background service runs on its own.")
    elif result.contained:
        print(
            "The background service is running inside the program that started it. "
            "Work you delegate may run inside your chat instead, and may stop when the chat closes. "
            "To fix this, stop the service and start it again from a terminal of its own."
        )
    if not result.success:
        return 1
    if (foreground or getattr(args, 'supervise', False)) and not result.dry_run and result.action == "start":
        return manager.wait_started_child()
    return 0


def _service_dependencies():
    """Keep missing-extra diagnostics visible to both controller and child."""
    try:
        import uvicorn
        from .service.app import create_app
    except ImportError:
        print('Install the service extra: pip install -e ".[service]"', file=sys.stderr)
        return None
    return uvicorn, create_app


def _run_managed_service_child(args: argparse.Namespace) -> int:
    """The sole uvicorn site, after exact child-record admission."""
    from .service.lifecycle import SERVICE_DETAILS, _service_options
    try:
        _apply_service_state(args)
        options = _service_options(_service_argument_list(args.port, args), state_root())
        admitted = ServiceProcessManager().admit_child(options=options)
    except Exception:
        admitted = False
    if not admitted:
        print(SERVICE_DETAILS["service_child_unmanaged"], file=sys.stderr)
        return 1
    # The autostart launcher is windowless on purpose, so this can
    # be the one path into uvicorn that has no standard streams at all.
    _bind_absent_std_streams()
    dependencies = _service_dependencies()
    if dependencies is None:
        return 1
    uvicorn, create_app = dependencies

    # Every change through the service needs this installation's secret.
    # A secret that cannot be created or verified does not stop
    # the service: it serves reads and refuses every change, and says why here
    # through a closed diagnostic event; the raw problem is discarded.
    from .service.auth import ensure_service_secret

    secret = ensure_service_secret(state_root())
    if not secret.usable:
        from .windowless_diagnostics import emit
        emit("backend_failed")
    try:
        app = create_app(
            db_path=options["db_path"],
            usage_directory=options["usage_directory"],
            config_path=options["config_path"],
            poll_interval=options["poll_interval"],
            write_secret=secret,
        )
    except Exception:
        print(SERVICE_DETAILS["invalid_path"], file=sys.stderr)
        return 1
    print(PRODUCT_NAME)
    print()
    print(f"API:       http://{SERVICE_DEFAULT_HOST}:{args.port}/api")
    try:
        uvicorn.run(app, host=SERVICE_DEFAULT_HOST, port=args.port, log_level="critical",
                    log_config=None, access_log=False, proxy_headers=False, timeout_keep_alive=20)
    except Exception:
        print(SERVICE_DETAILS["path_unreadable"], file=sys.stderr)
        return 1
    return 0


def _add_service_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--supervise', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=SERVICE_DEFAULT_PORT)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--poll-interval", type=float, default=60.0)
    parser.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--usage-directory", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--history-db", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--state-root", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--state-mode", choices=("installed", "development", "test"), default=None, help=argparse.SUPPRESS)


def _service_argument_list(port: int, args: argparse.Namespace | None = None, *, logon: bool = False) -> list[str]:
    """Resolve the selected nonsecret files before any controller cwd change."""
    from .statecontext import STATE_ROOT_ENV_VAR, resolve_state_context
    from .service.lifecycle import _service_options
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid_path")
    raw_root = os.environ.get(STATE_ROOT_ENV_VAR)
    if logon and raw_root and not Path(raw_root).expanduser().is_absolute():
        raise ValueError("service_cwd_unavailable")
    context = resolve_state_context()
    root = Path(getattr(args, "state_root", None) or context.state_root).expanduser().resolve()
    mode = getattr(args, "state_mode", None) or context.mode.value
    selected_db = _selected_db_path(args) if args is not None else service_db.DEFAULT_DB_PATH
    if (args is not None and (getattr(args, "state_root", None) is not None or getattr(args, "state_mode", None) is not None)
            and getattr(args, "db", None) is None and getattr(args, "history_db", None) is None):
        selected_db = root / "modelquad.db"
    db = Path(selected_db).expanduser().resolve()
    result = ["--port", str(port), "--db", str(db),
              "--poll-interval", str(getattr(args, "poll_interval", 60.0)),
              "--state-root", str(root), "--state-mode", mode]
    for flag, name in (("--config", "config"), ("--usage-directory", "usage_directory")):
        value = getattr(args, name, None)
        if value is not None:
            result += [flag, str(Path(value).expanduser().resolve())]
    _service_options(result, root)
    return result


def _admitted_service_image(image: str | Path) -> str:
    from .executable_resolution import UnsafeExecutable, admit_executable
    result = admit_executable(image, allow_batch=False)
    if result.path is None:
        raise UnsafeExecutable(result.refusal or "not_found")
    return result.path


def _service_source_prefix(image: str | Path) -> list[str]:
    # A Windows venv redirector creates another process. Launch its base image
    # directly, but carry the admitted owning interpreter to restore its site
    # environment inside our bootstrap without trusting cwd or PYTHONPATH.
    installation = None
    if sys.platform == "win32":
        executable = Path(sys.executable)
        base = Path(getattr(sys, "_base_executable", sys.executable))
        selected = Path(image)
        if selected == executable or selected == executable.with_name("pythonw.exe"):
            image = base.with_name("pythonw.exe") if selected.stem.lower().endswith("w") else base
            if selected != Path(image):
                installation = _admitted_service_image(selected)
    prefix = [_admitted_service_image(image), "-I"]
    if installation is not None:
        # Do not initialize the base installation's site packages before the
        # bootstrap restores the owning venv, or run its .pth files twice.
        prefix.append("-S")
    prefix.append(str(Path(__file__).resolve().parent / "service" / "entry.py"))
    if installation is not None:
        prefix += ["--installation-python", installation]
    return prefix


def _service_launch_command(port: int, args: argparse.Namespace | None = None) -> list[str]:
    """Only the admitted private child binds; public serve is a controller."""
    prefix = _service_source_prefix(sys.executable)
    return prefix + ["serve", "--managed-child"] + _service_argument_list(port, args)


def _windowless_launcher() -> Path | None:
    """The console-free sibling of this interpreter, or None.

    Windows decides whether a new process gets a console from its image's PE
    subsystem, at ``CreateProcess`` time, before the program's first
    instruction. Nothing the program does afterwards, and nothing in the
    scheduled task's definition, is consulted. So the only way a task-launched
    service can be windowless is to name an image that is subsystem 2 (GUI).

    CPython ships ``pythonw.exe`` beside ``python.exe`` for exactly this
    reason. Returns None when it is not there, so the caller can degrade
    rather than register a path that does not exist.
    """

    if sys.platform != "win32":
        return None
    executable = Path(sys.executable)
    if executable.stem.lower().endswith("w"):
        candidate = executable
    else:
        candidate = executable.with_name(f"{executable.stem}w{executable.suffix}")
    return candidate if candidate.is_file() else None


def _service_autostart_command(port: int, args: argparse.Namespace | None = None) -> list[str]:
    """Logon retains the admitted controller through the child's lifetime."""
    from .executable_resolution import UnsafeExecutable
    launcher = _windowless_launcher()
    if launcher is None:
        raise UnsafeExecutable("not_found")
    options = ['--supervise'] + _service_argument_list(port, args, logon=True)
    return _service_source_prefix(launcher) + ["service", "start"] + options


def _render_registration_result(result: RegistrationResult) -> None:
    if result.registered is True:
        state = "registered"
    elif result.registered is False:
        state = "not registered"
    else:
        state = "not supported" if not result.supported else "pending"
    print(f"Auto-start task: {state}")
    print(result.message)


def _render_process_result(result: ProcessResult) -> None:
    state = (f"running (PID {result.pid})" if result.pid is not None else "possibly running; PID unknown") if result.running else "not running"
    if not result.supported:
        state = "not supported"
    if result.running:
        readiness = "ready" if result.ready is True else "not ready" if result.ready is False else "readiness unknown"
        state += f"; {readiness}"
    print(f"Service process: {state}")
    print(result.message)
    print("Raw service output is discarded; windowless diagnostics use the event ledger.")


def _render_service_lifecycle_status(registration: RegistrationResult, process: ProcessStatus) -> None:
    registration_state = (
        "registered"
        if registration.registered is True
        else "not registered"
        if registration.registered is False
        else "not supported"
        if not registration.supported
        else "unknown"
    )
    print(f"Auto-start task: {registration_state}")
    if registration.message:
        print(f"Auto-start detail: {registration.message}")
    print(f"Service process: {_service_process_summary(process)}")
    if process.message:
        print(f"Service detail: {process.message}")
    if process.running:
        print(f"Started at: {process.started_at}")
    print("Raw service output is discarded; windowless diagnostics use the event ledger.")


def _run_service_lifecycle_command(args: argparse.Namespace) -> int:
    """Dispatch explicit auto-start and detached-process lifecycle actions."""

    if args.service_action is None:
        args.service_parser.print_help()
        return 0
    registrar = TaskSchedulerRegistrar()
    manager = ServiceProcessManager()
    if args.service_action == "install":
        from .executable_resolution import UnsafeExecutable
        from .service.lifecycle import SERVICE_DETAILS
        try:
            _apply_service_state(args)
            result = registrar.register(command=_service_autostart_command(args.port, args), dry_run=args.dry_run)
        except (UnsafeExecutable, ValueError, OSError) as exc:
            code = exc.reason if isinstance(exc, UnsafeExecutable) else str(exc)
            print(SERVICE_DETAILS.get(code, "The windowless service controller could not be admitted."), file=sys.stderr)
            return 1
        _render_registration_result(result)
        return 0 if result.success else 1
    if args.service_action == "uninstall":
        from .executable_resolution import UnsafeExecutable
        from .service.lifecycle import SERVICE_DETAILS
        try:
            _apply_service_state(args)
            result = registrar.unregister(command=_service_autostart_command(args.port, args),
                                          dry_run=args.dry_run)
        except (UnsafeExecutable, ValueError, OSError) as exc:
            code = exc.reason if isinstance(exc, UnsafeExecutable) else str(exc)
            print(SERVICE_DETAILS.get(code, "The windowless service controller could not be admitted."), file=sys.stderr)
            return 1
        _render_registration_result(result)
        return 0 if result.success else 1
    if args.service_action == "start":
        return _run_managed_service_start(args)
    if args.service_action == "stop":
        result = manager.stop(dry_run=args.dry_run)
        _render_process_result(result)
        return 0 if result.success else 1
    registration = registrar.is_registered()
    process = manager.status()
    if args.as_json:
        print(
            json.dumps(
                {"auto_start": registration.to_dict(), "process": process.to_dict()},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _render_service_lifecycle_status(registration, process)
    return 0 if registration.success and process.supported else 1


def _run_onboard_command(args: argparse.Namespace) -> int:
    from .onboarding import run_onboarding

    refused = _refuse_unsupported_platform(args.as_json)
    if refused is not None:
        return refused
    providers = (
        [p.strip() for p in args.providers.split(",") if p.strip()]
        if args.providers is not None
        else None
    )
    # `--yes` means "do not prompt", NOT "force automatic". Forcing it here
    # made every upgrade that ran `onboard --yes` quietly overwrite saved
    # routing state. Establishing the default is
    # now onboarding's job only on a genuine first run; see
    # onboarding._resolve_routing_mode.
    # The `--mode` flag was removed; onboarding resolves the default itself.
    mode = getattr(args, "mode", None)

    db_path = _history_db_path(args)
    print_fn = (lambda *a, **k: None) if args.as_json else print

    try:
        result = run_onboarding(
            selected_providers=providers,
            routing_mode=mode,
            reset_routing_mode=getattr(args, "reset_routing_mode", False),
            assume_yes=args.assume_yes,
            print_fn=print_fn,
            db_path=db_path,
            dry_run=args.dry_run,
        )
    except ValueError as error:
        # A wrong --providers value or no terminal to answer from: a plain
        # message, not a traceback.
        print(f"{CLI_COMMAND} onboard: {error}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        for step in result.steps:
            prefix = "[ok]" if step.ok else "[!!]"
            print(f"{prefix} {step.name}: {step.detail}")
        if not args.dry_run:
            recipe = _status_line_recipe("claude", get_host_descriptor("claude").default_target_dir())
            _print_status_line_recipe(recipe)
            print("")
            print(f"Next: run {CLI_COMMAND} doctor --first-run for a checklist of what is left.")
    return 0 if result.ok else 1


def _run_tools_command(args: argparse.Namespace) -> int:
    """Show how to install missing AI tools, and repair PATH for found ones."""

    from .onboarding import detect_providers
    from .provider_setup import fix_provider_path, providers_needing_path_fix
    from .registry import default_registry

    detections = detect_providers()
    plugins = {plugin.id: plugin for plugin in default_registry().all()}

    if args.fix_path:
        needs = providers_needing_path_fix(detections)
        if not needs:
            if args.as_json:
                print(json.dumps([], indent=2))
            else:
                print("Nothing to fix: every detected tool is already reachable on PATH.")
            return 0
        results = [fix_provider_path(d, dry_run=args.dry_run) for d in needs]
        if args.as_json:
            print(json.dumps([r.to_dict() for r in results], indent=2, sort_keys=True))
        else:
            for r in results:
                print(f"[{'ok' if r.ok else 'failed'}] {r.provider_id}: {r.detail}")
        return 0 if all(r.ok for r in results) else 1

    # One authority for what a provider's state is: `provider_connection`.
    # This command reports it; it does not derive a second opinion. The four
    # facts are printed separately because collapsing them is what made
    # "connected" untrustworthy.
    from .provider_connection import ConnectionState, InstallState, SignInState, describe_connections

    connections = describe_connections()

    if args.as_json:
        print(json.dumps([c.to_dict() for c in connections], indent=2, sort_keys=True))
        return 0

    for connection in connections:
        print(f"{connection.display_name}: {connection.headline}")

        if connection.install is InstallState.INSTALLED_ON_PATH and connection.install_path:
            print(f"  Found at: {connection.install_path}")
        elif connection.install is InstallState.INSTALLED_OFF_PATH:
            print(f"  Found at: {connection.install_path}")
            if connection.state is ConnectionState.NEEDS_PATH_FIX:
                print(f"  Fix it:   {CLI_COMMAND} tools --fix-path")

        if connection.sign_in.state is SignInState.UNKNOWN:
            print(f"  Sign-in:  {connection.sign_in.summary}")
        elif connection.sign_in.state is SignInState.SIGNED_IN:
            print("  Sign-in:  signed in")
        else:
            print("  Sign-in:  not signed in")

        if connection.action is not None:
            print(f"  Next:     {connection.next_step}")

        if connection.state is ConnectionState.NOT_INSTALLED:
            plugin = plugins.get(connection.provider_id)
            info = plugin.install if plugin is not None else None
            if info is not None:
                if info.command:
                    print(f"  Install ({info.command_shell}), copy and run this:")
                    print(f"      {info.command}")
                if info.alternative_command:
                    print(f"  Or with {info.alternative_label}:")
                    print(f"      {info.alternative_command}")
                print(f"  Install instructions: {info.docs_url}")

        if connection.install is InstallState.NOT_APPLICABLE:
            plugin = plugins.get(connection.provider_id)
            if plugin is not None and plugin.limitation:
                print(f"  Note: {plugin.limitation}")

        print()

    print(
        "ModelQuad never installs these for you: each command comes from the"
        " vendor's own documentation, and you run it yourself."
    )
    return 0


class _BuildVersionAction(argparse.Action):
    """``--version``: the package version names a release line, not a build.

    Resolved when the flag is used rather than when the parser is built, so
    no other command pays for reading provenance or asking git.
    """

    def __init__(
        self,
        option_strings: Sequence[str],
        dest: str = argparse.SUPPRESS,
        default: Any = argparse.SUPPRESS,
        help: str | None = None,
    ) -> None:
        super().__init__(option_strings=option_strings, dest=dest, default=default, nargs=0, help=help)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        from .buildinfo import read_build_info

        print(f"{PRODUCT_NAME} {read_build_info().label}")
        parser.exit()


def _routing_mode_argument(value: str) -> str:
    if value not in service_history.VALID_ROUTING_MODES:
        raise argparse.ArgumentTypeError("This setting has one value: automatic.")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        # Follow the name actually invoked, so help text under the legacy
        # `modelquad` alias keeps showing examples the user can paste back.
        prog=Path(sys.argv[0]).stem or CLI_COMMAND,
        description="See both tools' usage, check your setup, and connect ModelQuad to Claude Code.",
    )
    # The first thing anyone types at an unfamiliar CLI. Without it the
    # version was only reachable by reading the tail of `doctor`, and the
    # bare flag errored out with a usage dump.
    parser.add_argument(
        "-V",
        "--version",
        action=_BuildVersionAction,
        help="Show the installed product version and the exact build, then exit.",
    )
    parser.add_argument("--config", type=Path, help="Optional YAML configuration override.")
    parser.add_argument(
        "--usage-directory",
        type=Path,
        help="Directory containing usage-claude.json and usage-codex.json.",
    )
    parser.add_argument(
        "--history-db",
        type=Path,
        default=None,
        help="SQLite database for ModelQuad's history "
        f"(default: {service_db.DEFAULT_DB_PATH}).",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Do not record history. Codex calls are then refused, because ModelQuad must record each one; advice still works.",
    )
    subcommands = parser.add_subparsers(dest="command", title="subcommands")
    integrations = subcommands.add_parser("integrations", help="Control global host integrations.")
    integrations.add_argument("integrations_action", choices=("status", "on", "off"))
    integrations.add_argument("--json", action="store_true", dest="as_json")
    integrations.add_argument("--dry-run", action="store_true", help="Report without saving or changing host files.")
    settings_cmd = subcommands.add_parser('settings', help='Manage persisted user preferences.')
    settings_actions = settings_cmd.add_subparsers(dest='settings_action', required=True)
    job_runner_cmd = settings_actions.add_parser('job-runner', help='Keep delegated work running after closing the chat.')
    job_runner_cmd.add_argument('choice', choices=('on', 'off'))
    job_runner_cmd.add_argument(
        '--yes', action='store_true', dest='consent',
        help='Confirm you have read what turning it on means (needed when not typing at a prompt).')
    proactive_cmd = settings_actions.add_parser(
        'proactive',
        help='Let Claude decide on its own to consult Codex (default on; off: Claude offers it). '
        'Claude Code still asks you before each call.')
    proactive_cmd.add_argument('choice', choices=('on', 'off'))
    plan_cmd = settings_actions.add_parser(
        'plan',
        help='Say which plan you are on with Claude or Codex (shown to you; the account kind '
        'goes in the notice on Codex calls). With no arguments, show both.')
    plan_cmd.add_argument('provider', nargs='?', choices=('claude', 'codex'))
    plan_cmd.add_argument('value', nargs='?', metavar='plan|unset',
                          help='A listed plan, or unset. Run without it to see the list.')
    open_cmd = settings_actions.add_parser(
        'open',
        help='Open a settings page in your browser, served only on this computer. It closes '
        'when you press Done or after 15 minutes without use.')
    open_cmd.add_argument('--project', metavar='DIR',
                          help="The project whose own Codex choice the page shows (default: the current folder).")
    open_cmd.add_argument('--no-browser', action='store_true',
                          help='Print the page address instead of opening the browser.')
    repo_cmd = settings_actions.add_parser(
        'repo',
        help='Your own Codex setting for one repository: on (Claude may decide on its own), '
        'off (no Codex here), offer-only (Claude offers), inherit (the global setting). '
        'With no choice, show it.')
    repo_cmd.add_argument('choice', nargs='?', choices=('on', 'off', 'offer-only', 'inherit'))
    repo_cmd.add_argument('--project', metavar='DIR',
                          help='The project folder (default: the current folder).')
    host_cmd = subcommands.add_parser("host", help="Control which tools may start new work.")
    host_actions = host_cmd.add_subparsers(dest="host_action", required=True)
    from .routing_policy import VALID_HOST_IDS
    for action in ("disconnect", "connect"):
        command = host_actions.add_parser(action)
        hosts = sorted(VALID_HOST_IDS)
        command.add_argument("host_id", type=_surface_choice(hosts),
                             metavar="{" + ",".join(hosts) + "}")
    host_reset = host_actions.add_parser("reset", help="Recover Host controls with every tool disconnected.")
    host_reset.add_argument("--disconnect-all", action="store_true", required=True)
    host_status = host_actions.add_parser("status")
    host_status.add_argument("--json", action="store_true", dest="as_json")
    status = subcommands.add_parser("status", help="Show Claude's and Codex's quota, your plans and this folder's Codex setting.")
    status.add_argument("--global", action="store_true", dest="global_status", help="Include host integrations and control-plane health.")
    status.add_argument("--json", action="store_true", dest="as_json", help="Print status as JSON.")
    # `advise` is the command; `route`, its earlier name, still works but is
    # not listed (a parser added without help gets no help line, and the
    # usage line below leaves it out).
    advice_help = "Show which tool fits a task type. Starts nothing."
    for advice_name in ("advise", "route"):
        listed = {"help": advice_help} if advice_name == "advise" else {}
        advise = subcommands.add_parser(advice_name, description=advice_help, **listed)
        advise.add_argument("task_type", choices=[task.value for task in TaskType])
        advise.add_argument("--json", action="store_true", dest="as_json", help="Print the advice as JSON.")
    privacy_cmd = subcommands.add_parser(
        "privacy",
        help="Show what this installation stores locally, and reset the pseudonymous installation id.",
    )
    privacy_cmd.add_argument(
        "--reset-identity",
        action="store_true",
        help=(
            "Mint a fresh installation id. Severs every existing host-session "
            "fingerprint; nothing recorded under the old id can be linked to "
            "this installation again."
        ),
    )
    privacy_cmd.add_argument(
        "--json", action="store_true", dest="as_json", help="Print the privacy posture as JSON."
    )
    network_cmd = subcommands.add_parser(
        "network",
        help="Show ModelQuad's network rule: its own process connects only to loopback.",
    )
    network_cmd.add_argument("--json", action="store_true", dest="as_json", help="Print the network rule as JSON.")
    onboard = subcommands.add_parser(
        "onboard",
        help="Run the first-run onboarding wizard to detect, connect, and configure providers.",
    )
    onboard.add_argument(
        "--providers",
        help="Comma-separated list of provider IDs to connect (e.g. claude,codex).",
    )
    onboard.add_argument(
        "--yes",
        "-y",
        action="store_true",
        dest="assume_yes",
        help="Accept defaults non-interactively without prompting.",
    )
    onboard.add_argument(
        "--dry-run",
        action="store_true",
        help="Report actions without modifying adapter installations or database settings.",
    )
    onboard.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the onboarding result as JSON.",
    )
    tools = subcommands.add_parser(
        "tools",
        help="Show which AI tools are installed, how to install missing ones, "
        "and repair PATH when a tool is installed but unreachable.",
    )
    tools.add_argument(
        "--fix-path",
        action="store_true",
        help="Add the directory of any installed-but-unreachable tool to your user PATH.",
    )
    tools.add_argument(
        "--dry-run",
        action="store_true",
        help="With --fix-path, show which PATH entries would be added without changing PATH.",
    )
    tools.add_argument("--json", action="store_true", dest="as_json", help="Print as JSON.")
    for command_name in ("install", "uninstall"):
        adapter_command = subcommands.add_parser(
            command_name,
            help=f"{command_name.title()} a parent-client adapter.",
        )
        clients = tuple(CLIENT_NAMES)
        adapter_command.add_argument("client", nargs="?",
                                     type=_surface_choice(clients),
                                     metavar="{" + ",".join(clients) + "}")
        adapter_command.add_argument("--all", action="store_true", dest="all_clients")
        adapter_command.add_argument(
            "--global",
            action="store_true",
            dest="global_install",
            help="Apply to every detected host at its conventional global target.",
        )
        adapter_command.add_argument("--target-dir", type=Path)
        adapter_command.add_argument("--claude-dir", type=Path)
        adapter_command.add_argument("--dry-run", action="store_true")
        adapter_command.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Print the adapter report as JSON.",
        )
        if command_name == "install":
            via_plugin = adapter_command.add_mutually_exclusive_group()
            via_plugin.add_argument(
                "--plugin", action="store_true", dest="plugin", default=None,
                help="The ModelQuad Claude Code plugin supplies the server, commands and "
                "reminder: install only the status line, take back ModelQuad's own copies, "
                "and remember the choice.",
            )
            via_plugin.add_argument(
                "--no-plugin", action="store_false", dest="plugin",
                help="Install the server, commands and reminder directly again (the default).",
            )
    doctor = subcommands.add_parser("doctor", help="Check the setup, your plans and this folder's .modelquad.toml, with what to do next.")
    doctor.add_argument("--json", action="store_true", dest="as_json", help="Print the doctor report as JSON.")
    doctor.add_argument(
        "--first-run", action="store_true", dest="first_run",
        help="End with a plain-language checklist of what is set up and what to do next.",
    )
    state_command = subcommands.add_parser(
        "state",
        help="Report which ModelQuad state this installation owns, and whether "
        "every surface and host agrees.",
    )
    state_command.add_argument(
        "--json", action="store_true", dest="as_json", help="Print the state survey as JSON."
    )
    repair = subcommands.add_parser("repair", help="Repair degraded host integrations flagged by doctor.")
    repair.add_argument("--dry-run", action="store_true", help="Report repairs without modifying host installations.")
    repair.add_argument("--json", action="store_true", dest="as_json", help="Print repair result as JSON.")
    usage_hook = subcommands.add_parser(
        "usage-hook",
        help="Package-owned replacement for the legacy usage collector scripts. "
        "Reads a host-supplied JSON payload from stdin, normalizes it, and writes "
        "the provider's usage-*.json snapshot -- wired in by `modelquad install "
        "--global` as the statusLine/hook command in place of the old loose scripts.",
    )
    hook_providers = ("claude",)
    usage_hook.add_argument("provider", type=_surface_choice(hook_providers),
                            metavar="{" + ",".join(hook_providers) + "}")
    usage_hook.add_argument("--quiet", action="store_true", help="Record quota without writing a status line.")
    usage_hook.add_argument(
        "--segment", action="store_true",
        help="Print only the compact ModelQuad part, for a status line you already have "
        "(for example a ccstatusline Custom Command widget).",
    )
    usage_refresh = subcommands.add_parser(
        "usage-refresh",
        help="Ask every provider for the freshest usage it will give us, concurrently. "
        "Reports per provider whether a genuinely newer provider observation appeared "
        "(observed_at) rather than merely a newer time-we-checked (collected_at).",
    )
    usage_refresh.add_argument(
        "--provider",
        action="append",
        dest="providers",
        help="Refresh only this provider (repeatable). Defaults to all.",
    )
    usage_refresh.add_argument(
        "--force",
        action="store_true",
        help="Skip the minimum-interval debounce.",
    )
    usage_refresh.add_argument("--json", action="store_true", dest="as_json", help="Print JSON.")
    subcommands.add_parser(
        "hook-gate",
        help="No-op kept for Claude Code sessions that still run the PreToolUse hook "
        "earlier builds installed. Exits 0 and prints nothing; not intended to be run manually.",
    )
    pending_hook = subcommands.add_parser("hook-pending", help="Metadata-only startup context for the Host agent.")
    pending_hook.add_argument("host", choices=("claude",))
    review_hook = subcommands.add_parser(
        "hook-review", help="Advisory reminder of the Codex review moment for the Host agent.")
    review_hook.add_argument("host", choices=("claude",))
    # The same question `modelquad_delegation_status(working_directory=...)`
    # answers, at a shell. The consumer this protects is frequently not a model
    # at all: it is an operator or a script about to run a gate, review a diff
    # or commit a worktree, holding a path and no MCP client.
    workspace_cmd = subcommands.add_parser(
        "workspace",
        help="Say whether a Codex call from ModelQuad is still running for this folder.",
    )
    workspace_cmd.add_argument(
        "path",
        nargs="?",
        default=".",
        help="The workspace to ask about (default: the current directory).",
    )
    workspace_cmd.add_argument(
        "--json", action="store_true", dest="as_json", help="Print the raw answer as JSON."
    )
    activity = subcommands.add_parser("activity", help="Show ModelQuad's history: past Codex calls and their outcomes.")
    activity.add_argument("--limit", type=int, default=50, help="Maximum number of entries (default: 50).")
    activity.add_argument("--provider", help="Filter by provider.")
    activity.add_argument("--task-type", choices=[task.value for task in TaskType], help="Filter by task type.")
    activity.add_argument(
        "--result",
        choices=("success", "failure", "timeout", "error"),
        help="Filter execution result.",
    )
    activity.add_argument("--json", action="store_true", dest="as_json", help="Print raw activity JSON.")
    activity.add_argument(
        "--detail",
        metavar="KIND:ID",
        help=(
            "Fetch one entry's full record (all stored metadata and the error text), "
            "e.g. --detail execution:42. Ignores every other filter/limit flag."
        ),
    )
    split_cmd = subcommands.add_parser(
        "split", help="What happened after the work: Codex requests and quota per window.")
    split_actions = split_cmd.add_subparsers(dest="split_action", required=True)
    split_summary_cmd = split_actions.add_parser(
        "summary",
        help="Codex requests by kind and outcome for one project, and each provider's "
        "percentage points used in each of its own windows (never added together).")
    split_summary_cmd.add_argument('--project', metavar='DIR',
                                   help='The project folder (default: the current folder).')
    split_summary_cmd.add_argument('--since', choices=('1d', '7d', 'session'), default='1d',
                                   help='The period (default: 1d). session is known only '
                                   'inside Claude Code, through modelquad_activity.')
    split_summary_cmd.add_argument("--json", action="store_true", dest="as_json",
                                   help="Print the summary as JSON.")
    lifecycle = subcommands.add_parser(
        "service",
        help="Manage local control-plane auto-start and background process state.",
    )
    lifecycle.set_defaults(service_parser=lifecycle)
    lifecycle_actions = lifecycle.add_subparsers(dest="service_action", title="actions")
    lifecycle_install = lifecycle_actions.add_parser("install", help="Register the per-user auto-start task.")
    lifecycle_install.add_argument("--dry-run", action="store_true", help="Show the scheduled-task command without changing it.")
    _add_service_options(lifecycle_install)
    lifecycle_uninstall = lifecycle_actions.add_parser("uninstall", help="Unregister the per-user auto-start task.")
    lifecycle_uninstall.add_argument("--dry-run", action="store_true", help="Show the removal command without changing it.")
    # The same options install takes: uninstall rebuilds the action install
    # registers and deletes only a task that matches it.
    _add_service_options(lifecycle_uninstall)
    lifecycle_start = lifecycle_actions.add_parser("start", help="Start the detached local service now.")
    lifecycle_start.add_argument("--dry-run", action="store_true", help="Show the launch command without starting it.")
    _add_service_options(lifecycle_start)
    lifecycle_stop = lifecycle_actions.add_parser("stop", help="Stop the detached local service.")
    lifecycle_stop.add_argument("--dry-run", action="store_true", help="Show the stop command without stopping it.")
    lifecycle_status = lifecycle_actions.add_parser("status", help="Show auto-start and background-process state.")
    lifecycle_status.add_argument("--json", action="store_true", dest="as_json", help="Print lifecycle status as JSON.")
    subcommands.add_parser(
        "mcp",
        help="Run the Model Context Protocol stdio server.",
    )
    # Internal: the detached per-job runner, reachable through the CLI as well
    # as through `python -m modelquad.jobs.runner`.
    from .jobs.runner import add_runner_arguments

    add_runner_arguments(subcommands.add_parser(
        "job-runner",
        help="Internal: run one detached delegation job (started by ModelQuad itself).",
    ))
    jobs = subcommands.add_parser('jobs', help='Inspect or release a background job ModelQuad could not confirm ended.')
    job_actions = jobs.add_subparsers(dest='jobs_action', required=True)
    fate = job_actions.add_parser('fate', help='Show what ModelQuad knows about how the job ended, and live Codex processes.')
    fate.add_argument('job_id')
    fate.add_argument('--json', action='store_true', dest='as_json')
    release_fate = job_actions.add_parser('release-fate', help='Release one job ModelQuad could not confirm ended.')
    release_fate.add_argument('job_id')
    release_fate.add_argument('--acknowledge-unknown-worker-fate', action='store_true')
    for command_name, help_text in (
        ("serve", "Start the local control-plane HTTP service."),
    ):
        service_command = subcommands.add_parser(command_name, help=help_text)
        _add_service_options(service_command)
        service_command.add_argument("--managed-child", action="store_true", help=argparse.SUPPRESS)
    # The usage line lists the commands people use; hidden aliases still parse.
    subcommands.metavar = "{" + ",".join(name for name in subcommands.choices if name not in _HIDDEN_COMMANDS) + "}"
    return parser


#: Commands that still work but are not listed: earlier names kept for scripts.
_HIDDEN_COMMANDS = frozenset({"route"})


#: Commands Claude Code runs as hooks or as its status line. They must never
#: fail closed: a PreToolUse hook that exits with code 2 blocks the user's tool
#: call, and argparse exits with 2 on a usage error.
_FAIL_SAFE_HOOK_COMMANDS = frozenset({"hook-gate", "hook-review", "usage-hook"})


def main(argv: Sequence[str] | None = None) -> int:
    from .cli_entry import dispatch_pending, dispatch_review
    from .windowless_diagnostics import command_for

    arguments = list(sys.argv[1:] if argv is None else argv)
    hook_result = dispatch_pending(arguments)
    if hook_result is None:
        hook_result = dispatch_review(arguments)
    if hook_result is not None:
        return hook_result
    if command_for(arguments) in _FAIL_SAFE_HOOK_COMMANDS:
        try:
            _run_command(argv)
        except BaseException:  # noqa: BLE001 -- includes argparse's SystemExit(2)
            pass
        return 0
    return _run_command(argv)


def _run_command(argv: Sequence[str] | None) -> int:
    # A developer's terminal is where the popup was first noticed.
    # Idempotent, so an entry point that already seeded it makes this a no-op.
    preseed_windows_uname()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "integrations":
        return _run_integrations_command(args, parser)
    if args.command == 'settings' and args.settings_action == 'proactive':
        return _run_proactive_setting(args.choice == 'on')
    if args.command == 'settings' and args.settings_action == 'repo':
        return _run_repo_setting(args.choice, args.project)
    if args.command == 'settings' and args.settings_action == 'open':
        return _run_settings_page(args.project, no_browser=args.no_browser)
    if args.command == 'settings' and args.settings_action == 'plan':
        return _run_plan_setting(args.provider, args.value)
    if args.command == 'settings':
        if args.choice == 'on':
            if not _background_work_consented(args):
                return 2
            try:
                service_history.record_job_runner_consent(_selected_db_path(args))
            except (sqlite3.Error, OSError) as exc:
                return _setting_not_saved("Background work consent", exc)
        from .jobs.service_client import connect_management, Unavailable
        client = connect_management(_selected_db_path(args))
        answer = client if isinstance(client, Unavailable) else client.job_runner_setting(args.choice == 'on')
        if isinstance(answer, Unavailable):
            print('The background work preference could not be saved: ' + answer.reason, file=sys.stderr)
            return 1
        print('Keep delegated work running after closing the chat: ' + args.choice)
        print('Readiness: ' + str(answer.get('job_runner_readiness') or 'unknown'))
        if answer.get('job_runner_limitation'):
            print('Limitation: ' + answer['job_runner_limitation'])
        if args.choice == 'off':
            print('Running work continues. Future work will run inside the chat.')
        return 0
    if args.command == "host":
        from .routing_policy import VALID_HOST_IDS
        try:
            db_path = _selected_db_path(args)
            if args.host_action == "status":
                hosts = service_history.get_launch_state(db_path)["disconnected_hosts"]
            elif args.host_action == "reset":
                hosts = service_history.put_disconnected_hosts(db_path, sorted(VALID_HOST_IDS))
            else:
                hosts = service_history.set_host_disconnected(
                    db_path, args.host_id, args.host_action == "disconnect",
                )
            if getattr(args, "as_json", False):
                print(json.dumps({"disconnected_hosts": list(hosts)}, indent=2, sort_keys=True))
            else:
                for host_id in sorted(VALID_HOST_IDS):
                    status = "Disconnected" if host_id in hosts else "Connected"
                    print(f"{host_id}: {status}")
            return 0
        except (service_history.LaunchStateUnavailable, OSError, ValueError, sqlite3.Error) as exc:
            if isinstance(exc, service_history.LaunchStateUnavailable) and exc.reason == "disconnected_hosts_invalid":
                message = "Host controls are unreadable. Run modelquad host reset --disconnect-all, then connect tools one by one."
            elif isinstance(exc, (OSError, sqlite3.Error)) or (
                isinstance(exc, service_history.LaunchStateUnavailable) and exc.reason == "launch_state_unreadable"
            ):
                message = "Launch settings could not be read. Try again."
            else:
                message = "Launch settings are unreadable."
            if args.host_action == "status":
                if args.as_json:
                    print(json.dumps({"disconnected_hosts": None, "status": "unreadable"}))
                else:
                    print(message)
            else:
                print(f"{message} This command failed; no choice was changed.", file=sys.stderr)
            return 1
    if args.command in {"install", "uninstall"}:
        return _run_adapter_command(args, parser)
    if args.command == "serve":
        return _run_service_command(args)
    if args.command == "mcp":
        return _run_mcp_command(args)
    if args.command == "job-runner":
        from .jobs.runner import run_job

        return run_job(args.job_id, args.spec, db_path=args.db_path)
    if args.command == 'jobs':
        from .jobs.service_client import connect_management, Unavailable
        if args.jobs_action == 'release-fate' and not args.acknowledge_unknown_worker_fate:
            print('The --acknowledge-unknown-worker-fate flag is required. A Codex process may still be running. It cannot change your project, but it may still use your Codex usage.', file=sys.stderr)
            return 2
        client = connect_management(_history_db_path(args))
        if isinstance(client, Unavailable):
            print(client.reason, file=sys.stderr)
            return 1
        answer = (client.fate(args.job_id) if args.jobs_action == 'fate'
                  else client.release_fate(args.job_id, acknowledge=True))
        if isinstance(answer, Unavailable):
            print(answer.reason, file=sys.stderr)
            return 1
        if args.jobs_action == 'fate' and args.as_json:
            print(json.dumps(answer))
        elif answer['release'] is not None:
            row = answer['release']
            print(f"Closed — workspace block released ({row['reason']}, {row['released_at']}).")
        elif answer['blocker'] is False:
            print('ModelQuad knows how this job ended; there is nothing to release.')
        else:
            print('ModelQuad cannot confirm Codex ended for this job. Releasing it lets diagnosis go on. A Codex process may still be running; it cannot change your project, but it may still use your Codex usage.')
            listing = answer['live_codex']
            print(f"Live Codex processes on this computer ({listing['status']}, {listing['observed_at']}):")
            for process in listing['processes']:
                print(f"PID {process['pid']} — {process['created_at'] or 'Start time unknown'}")
            if listing['reason']:
                print(listing['reason'])
        return 0
    if args.command == "service":
        if getattr(args, "service_action", None) == "install":
            refused = _refuse_unsupported_platform(False)
            if refused is not None:
                return refused
        return _run_service_lifecycle_command(args)
    if args.command == "hook-gate":
        # A no-op for sessions started before the gate hook was removed; see
        # cli_entry.dispatch_gate.
        return 0
    if args.command == "onboard":
        return _run_onboard_command(args)
    if args.command == "tools":
        return _run_tools_command(args)
    if args.command == "privacy":
        from . import identity as identity_mod
        from . import paths as paths_mod
        from .netpolicy import LOOPBACK_ONLY_REASON

        # Deliberately NOT named `state_root`: `main` already imports a
        # module-level `state_root` function that the hook-gate branch calls,
        # and a local of that name would shadow it for the whole function.
        privacy_state_root = paths_mod.state_root()
        if args.reset_identity:
            installation_id = identity_mod.reset_installation_id(privacy_state_root)
            reset_performed = True
        else:
            # peek, never create: asking what identity exists must not be the
            # thing that mints one.
            installation_id = identity_mod.peek_installation_id(privacy_state_root)
            reset_performed = False
        payload = {
            "state_root": str(privacy_state_root),
            "installation_id": installation_id,
            "installation_id_reset": reset_performed,
            "identity_kind": "pseudonymous, random, resettable",
            "telemetry": "none",
            "network": LOOPBACK_ONLY_REASON,
            "retention": "local history is kept until you delete it; there is no automatic expiry",
            "delete_everything": (
                f"run `{CLI_COMMAND} service uninstall` and `{CLI_COMMAND} uninstall --global`, "
                f"then delete {privacy_state_root}"
            ),
        }
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if reset_performed:
            print("Installation id reset. Previous host-session fingerprints are now unlinkable.")
            print()
        print(f"Local state:        {privacy_state_root}")
        print(f"Installation id:    {installation_id or '(none created yet)'} (pseudonymous, resettable)")
        print(f"{PRODUCT_NAME} has no telemetry.")
        print(f"Network: {LOOPBACK_ONLY_REASON}. The Codex CLI connects to OpenAI itself.")
        print()
        print(payload["retention"].capitalize() + ".")
        print(f"Delete everything: {payload['delete_everything']}")
        return 0
    if args.command == "network":
        from .netpolicy import LOOPBACK_ONLY_REASON

        payload = {
            "modelquad_process": LOOPBACK_ONLY_REASON,
            "enforcement": "an audit hook refuses any other socket connection from this process",
            "codex_cli": "a separate program that connects to OpenAI itself, under your own sign-in",
        }
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"{PRODUCT_NAME} network rule: {LOOPBACK_ONLY_REASON}.")
            print("An audit hook refuses any other socket connection from this process.")
            print("The Codex CLI is a separate program that connects to OpenAI itself, under your own sign-in.")
        return 0
    if args.command == "usage-refresh":
        from .usage.refresh import refresh_all

        usage_directory = args.usage_directory or DEFAULT_USAGE_DIRECTORY
        results = refresh_all(
            usage_directory, providers=args.providers or None, force=args.force
        )
        payload = {name: result.to_dict() for name, result in results.items()}
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for name, item in sorted(payload.items()):
                remaining = item["remaining_percentage"]
                remaining_text = "unknown" if remaining is None else f"{remaining:.1f}% left"
                verdict = "newer observation" if item["newer_observation"] else "no newer data"
                if item["debounced"]:
                    verdict = "debounced (checked recently)"
                print(f"{name}: {remaining_text} [{item['refresh_kind']}] -- {verdict}")
                print(f"  observed_at  {item['observed_at'] or 'never'}")
                if item.get('note'):
                    print(f"  {item['note']}")
                print(f"  collected_at {item['collected_at']}")
                if item["error"]:
                    print(f"  note: {item['error']}")
        return 0
    if args.command == "usage-hook":
        # One implementation for this path and the light installed entry
        # (cli_entry.dispatch_usage_hook), so both write the same files.
        from .usage_hook import run as run_usage_hook

        return run_usage_hook(usage_directory=args.usage_directory or DEFAULT_USAGE_DIRECTORY,
                              quiet=args.quiet, segment=args.segment)
    try:
        config = load_config(args.config)
    except ValueError as exc:
        parser.error(str(exc))
    if args.command == "doctor":
        # Plan names and the repository file's key names are for the person
        # at this terminal only; the MCP doctor tool never includes them.
        report = run_doctor(config, usage_directory=args.usage_directory,
                            project_directory=Path.cwd(), include_plans=True)
        if args.as_json:
            payload = report.to_dict()
            if args.first_run:
                from .doctor import first_run_checklist

                payload["checklist"] = [{"done": done, "item": item}
                                        for done, item in first_run_checklist(report)]
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            render_doctor(report)
            if args.first_run:
                render_first_run_checklist(report)
        return 0 if report.ok else 1
    if args.command == "state":
        from .statediag import survey_state

        survey = survey_state()
        if args.as_json:
            print(json.dumps(survey.to_dict(), indent=2, sort_keys=True))
        else:
            render_state_survey(survey)
        return 0 if survey.consistent else 1
    if args.command == "repair":
        from .repair import run_repair

        refused = _refuse_unsupported_platform(args.as_json)
        if refused is not None:
            return refused
        print_fn = (lambda *a, **k: None) if args.as_json else print
        result = run_repair(dry_run=args.dry_run, print_fn=print_fn, config=config)
        if args.as_json:
            print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.command == "workspace":
        answer = service_history.workspace_delegation_liveness(
            _history_db_path(args), args.path
        )
        if args.as_json:
            print(json.dumps(answer, indent=2, sort_keys=True, default=str))
        else:
            print(f"workspace: {answer['workspace']}")
            print(f"verdict:   {answer['verdict']}")
            print(answer["statement"])
            if answer.get("reason"):
                print(f"because:   {answer['reason']}")
            for entry in answer["attempts"]:
                print(
                    f"  attempt {entry.get('id')}: {entry.get('task_type')} "
                    f"-> {entry.get('requested_provider') or 'auto'}, worker pid "
                    f"{entry.get('worker_pid')}, workspace "
                    f"{entry.get('workspace_relation')}"
                    + (
                        f" -- {entry['workspace_hazard_reason']}"
                        if entry.get("workspace_hazard_reason")
                        else ""
                    )
                )
        # Fails closed, so `modelquad workspace . && pytest` refuses to run a
        # gate over a tree that may be moving. Only a completed scan that found
        # nothing in flight exits 0; both "may be mutating" and "could not
        # establish" are non-zero, and they are distinguishable because the
        # right response differs -- wait, versus find out why.
        return {"no_delegation_in_flight": 0, "may_be_mutating": 2}.get(answer["verdict"], 3)
    if args.command == "activity":
        if args.detail:
            kind, _, raw_id = args.detail.partition(":")
            if kind not in {"routing", "execution"} or not raw_id.isdigit():
                parser.error("--detail must look like 'execution:42' or 'routing:7'")
            entry = service_history.fetch_activity_detail(_history_db_path(args), kind, int(raw_id))
            if entry is None:
                print(f"No {kind} entry with id {raw_id}.")
                return 1
            print(json.dumps(entry, indent=2, sort_keys=True))
            return 0
        entries = service_history.fetch_activity(
            _history_db_path(args),
            limit=args.limit,
            provider=args.provider,
            task_type=args.task_type,
            result=args.result,
        )
        if args.as_json:
            print(json.dumps(entries, indent=2, sort_keys=True))
        else:
            render_activity(entries)
        return 0

    if args.command == "split":
        return _run_split_summary(args, config)

    providers = build_providers(config, args.usage_directory)
    statuses = tuple(read_status(provider, refresh=args.command == "status") for provider in providers)
    history_enabled = not args.no_history
    db_path = _history_db_path(args)
    if args.command == "status":
        if args.global_status:
            report = run_doctor(config, usage_directory=args.usage_directory)
            hosts = _global_host_summaries(report)
            package_check = next((check for check in report.checks if check.name == f"{PRODUCT_NAME} package"), None)
            process_status = ServiceProcessManager().status()
            if args.as_json:
                print(
                    json.dumps(
                        {
                            "providers": [status.to_dict() for status in statuses],
                            "hosts": hosts,
                            "package_version": package_check.detail if package_check is not None else "n/a",
                            "service": _service_process_summary(process_status),
                            "overall": "HEALTHY" if report.ok else "DEGRADED",
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                render_global_status(statuses, report, process_status)
        elif args.as_json:
            print(json.dumps([status.to_dict() for status in statuses], indent=2, sort_keys=True))
        else:
            render_status(statuses)
            render_status_summary(statuses, config, args.usage_directory)
        if history_enabled:
            service_history.record_provider_usage(db_path, statuses)
        return 0
    if args.command in ("advise", "route"):
        from .netpolicy import providers_refused_by_policy

        result = Router(config).route(
            args.task_type,
            statuses,
            # A provider the network policy refuses: advice that names one is
            # advice to start a task that cannot run. The database
            # is the one `main` already declared.
            policy_excluded=providers_refused_by_policy(),
        )
        recorded = False
        if history_enabled:
            from .hostobs import capture_host_decision_snapshot, with_decision_context
            from .provenance import evidence_snapshot_json, new_decision_id

            route_decision_id = new_decision_id()
            service_history.record_provider_usage(db_path, statuses)
            # The CLI has no MCP clientInfo, and which hosts that costs is
            # now runtime-specific. A `modelquad advise` run from inside a
            # Claude Code shell is still observable, because that host was
            # spawned with an environment that says so. The same run inside
            # Codex is NOT: Codex scrubs the environment it spawns processes
            # with, so clientInfo is its only channel and the CLI has none.
            # Both degrade the same honest way -- no observer claims the
            # host, and the snapshot reads observed=False rather than
            # guessing from the shell it happens to be running in.
            recorded = service_history.record_routing_decision(
                db_path,
                args.task_type,
                result,
                source="cli",
                decision_id=route_decision_id,
                evidence_json=evidence_snapshot_json(
                    statuses,
                    config,
                    host_snapshot=with_decision_context(
                        capture_host_decision_snapshot(),
                        selected_runtime=result.recommendation,
                    ),
                ),
            )
        if args.as_json:
            payload = result.to_dict()
            payload["recorded"] = bool(recorded)
            payload["decision_id"] = route_decision_id if recorded else None
            try:
                state = service_history.get_launch_state(db_path)
                payload["launch_state"] = "known"
                payload.update({key: state[key] for key in ("routing_mode", "routing_paused", 'routing_paused_since')})
            except service_history.LaunchStateUnavailable:
                payload.update(launch_state="unknown", routing_mode=None, routing_paused=None)
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            render_route(result)
            try:
                state = service_history.get_launch_state(db_path)
                if state['routing_paused']:
                    from .routing_policy import pause_label
                    print(pause_label(state.get('routing_paused_since')))
            except service_history.LaunchStateUnavailable:
                print("ModelQuad could not read whether Codex calls are paused.")
            if not recorded:
                print("Advice was not recorded; no persisted decision identity.", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":  # pragma: no cover - module entry-point convenience
    raise SystemExit(main())
