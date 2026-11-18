# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The ``modelquad`` command: install, settings, the review record and a setup check."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from .adapters import CLIENT_NAMES, InstallReport, default_target_dir, get_adapter, get_host_descriptor
from .branding import CLI_COMMAND, PRODUCT_NAME

#: How long each store keeps what it holds, as PRIVACY.md's state-folder table
#: says (`modelquad privacy` prints it; tests/test_cli.py checks both agree).
PRIVACY_RETENTION: tuple[tuple[str, str], ...] = (
    ("review ledger (review-ledger.db)", "no automatic expiry"),
    ("review reminders (review-reminder)", "deleted after 2 days"),
    ("running-worker records (active-workers)", "removed when the call ends; a leftover after 1 hour"),
)

#: What earlier builds kept and this one no longer reads or writes. Listed so
#: a person can delete it; ModelQuad itself never deletes a user's data.
PRIVACY_LEGACY: tuple[str, ...] = (
    "modelquad.db (history of earlier builds)", "pending-output (answers earlier builds kept)",
    "logs", "usage-claude.json, usage-codex.json and quota-pace.json in %USERPROFILE%\\.claude",
)


def _privacy_delete_steps(privacy_state_root: Path) -> list[str]:
    """The steps of PRIVACY.md's "How to delete it: Everything", in order."""

    return [
        f"Run `{CLI_COMMAND} uninstall claude`. If you installed the plugin, "
        f"also run `claude plugin uninstall {CLI_COMMAND}`.",
        f"Close the Claude Code sessions that use {PRODUCT_NAME}, then delete {privacy_state_root}.",
        "Delete the *.modelquad-backup-* files beside your Claude Code settings. Check the "
        "backups first: they can contain secrets you put in your configuration.",
    ]


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))


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


_REPO_MODE_TEXT = {
    "on": "on: Claude may decide on its own to consult Codex in this repository.",
    "off": "off: no Codex in this repository; ModelQuad refuses every Codex call here.",
    "offer-only": ("offer-only: Claude offers Codex here when it would help and calls it "
                   "when you agree or ask."),
}


def _surface_choice(choices: Sequence[str]):
    """Validate a choice with one plain error that lists the valid values."""
    def validate(value: str) -> str:
        if value not in choices:
            raise argparse.ArgumentTypeError("Choose one of: " + ", ".join(choices))
        return value
    return validate


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
    "preferences.json), so a later install may add the direct server again. Run this "
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


def _incomplete(report: InstallReport) -> str | None:
    """Why an install or uninstall did not finish, or None.

    A file ModelQuad chose to leave alone (a link, the user's own entry) is
    reported in the table and is not an error. A file it could not read,
    write or remove is: the command must not end as a success with part of
    its work undone.
    """

    if report.dry_run or not report.failures:
        return None
    return "Not finished: " + " ".join(
        action.detail or f"{action.path} was not changed." for action in report.failures
    ) + " Run the command again."


def _run_global_adapter_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from .adapters.registration import BackupPersistenceError

    _global_adapter_arguments_are_valid(args, parser)
    if args.command == "install":
        refused = _refuse_unsupported_platform(args.as_json)
        if refused is not None:
            return refused
    method_name = "install" if args.command == "install" else "uninstall"
    notices = _install_notices(args.as_json) if method_name == "install" else {}
    results: list[dict[str, object]] = []
    for client in CLIENT_NAMES:
        try:
            descriptor = get_host_descriptor(client)
            detected = True if descriptor.detect is None else bool(descriptor.detect())
        except (OSError, ValueError) as exc:
            results.append({"client": client, "detected": False, "report": None, "error": str(exc)})
            continue
        if not detected:
            results.append({"client": client, "detected": False, "report": None, "error": None})
            continue
        target_dir = descriptor.default_target_dir()
        before = _registration_before_uninstall(client, target_dir, method_name)
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
            restore = _restore_displaced_registration(client, target_dir, args, method_name, before)
            results.append({"client": client, "detected": True, "report": None,
                            "error": str(exc), "restore": restore})
            continue
        restore = _restore_displaced_registration(client, target_dir, args, method_name, before)
        not_saved = None
        if method_name == "install":
            if client == "claude":
                not_saved = _remember_plugin_choice(args)
            skipped = _mcp_registration_skipped(client, target_dir, report, adapter)
            if skipped is not None:
                results.append({"client": client, "detected": True, "report": report,
                                "error": f"Not registered: {skipped}", "restore": None})
                continue
        not_saved = not_saved or _incomplete(report)
        results.append(
            {
                "client": client,
                "detected": True,
                "report": report,
                "error": not_saved,
                "restore": restore,
            }
        )

    if method_name == "install":
        _note_notice_shown(args, notices, [result for result in results if result.get("report")])
    if args.as_json:
        print(
            json.dumps(
                {
                    "global": True,
                    **notices,
                    "dry_run": args.dry_run,
                    "results": [
                        {
                            "client": result["client"],
                            "detected": result["detected"],
                            "report": result["report"].to_dict() if result["report"] is not None else None,
                            "error": result["error"],
                            "restore": result.get("restore"),
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
    failed_restore = any(
        isinstance(result.get("restore"), dict)
        and result["restore"].get("status") == "failed"
        for result in results
    )
    return 1 if any(result["error"] is not None for result in results) or failed_restore else 0


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
    reports: list[InstallReport] = []
    errors: list[dict[str, str]] = []
    restores: list[dict[str, object]] = []
    method_name = "install" if args.command == "install" else "uninstall"
    notices = _install_notices(args.as_json) if method_name == "install" else {}
    for client, target_dir in targets:
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
            skipped = _mcp_registration_skipped(client, target_dir, report, adapter)
            if skipped is not None:
                errors.append({"client": client, "error": f"Not registered: {skipped}"})
        incomplete = _incomplete(report)
        if incomplete is not None and not any(e["client"] == client for e in errors):
            errors.append({"client": client, "error": incomplete})
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
            payload.update(notices)
            if errors:
                payload["error"] = errors[0]["error"]
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(json.dumps({**errors[0], **notices}, indent=2, sort_keys=True))
    else:
        render_install_reports(reports)
        for restore in restores:
            _render_restore_result(restore)
        for error in errors:
            print(f"{error['client']}: {error['error']}")
    # A restore that failed is a real failure: the user asked to uninstall and
    # something they owned could not be given back.
    failed_restore = any(r.get("status") == "failed" for r in restores)
    return 1 if errors or failed_restore else 0


_NOT_LOCAL_FOLDER = ("That folder is not a local folder ModelQuad can use (a network share, a "
                     "device, or a link to one). Use the project's folder on a local drive.")


def _run_repo_setting(choice: str | None, project: str | None) -> int:
    """Show or set the user's own Codex setting for one repository."""
    from . import repo_policy, repo_settings
    from .adapters.server_instructions import read_proactive
    from .localpath import reason as local_reason

    folder = Path(os.path.abspath(project)) if project else Path.cwd()
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
    elif result.refusal is None and _grant_decides(folder):
        print("In effect: your standing grant covers this folder, so Claude consults Codex on "
              "its own here, without a prompt for each call. The choice above applies to "
              "sessions without the grant.")
    elif result.refusal is None:
        print({repo_settings.PROACTIVE: "In effect: Claude may decide on its own to consult Codex.",
               repo_settings.OFFER: "In effect: Claude offers Codex and calls it when you agree or ask.",
               repo_settings.ON_REQUEST: ("In effect: Claude calls Codex only when you ask "
                                          f"(this repository's {repo_policy.FILE_NAME}).")}[result.offer])
    print("Choices: on, off, offer-only, inherit. Claude Code asks you before each "
          "ModelQuad Codex call unless the standing grant is on (modelquad settings auto).")
    print("Stored in this computer's preferences.json under a keyed hash of the repository, "
          "never its path or name. off applies from the next call; Claude is told the rest when "
          "a new Claude Code session starts here.")
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
    print("Claude Code asks you before each ModelQuad Codex call unless the standing grant "
          "is on (modelquad settings auto).")
    print("Takes effect in new Claude Code sessions (or after /mcp reconnects modelquad).")
    return 0


def _grant_decides(folder) -> bool:
    """Whether the user's standing grant covers this folder (Claude then decides on its own)."""
    from . import grant

    try:
        return grant.allows(folder)
    except Exception:  # noqa: BLE001 -- unreadable: no grant
        return False


def _run_auto_setting(args: argparse.Namespace, *, input_fn=input) -> int:
    """Turn the standing grant on or off (grant.py), after its notice and a typed yes."""
    from . import grant
    from .disclosure import DATA_FLOW, print_lines

    folder = None if args.everywhere else Path(os.path.abspath(args.project or Path.cwd()))
    if folder is not None:
        from .localpath import reason as local_reason

        # Before any file-system call: a network share is never opened.
        if local_reason(os.path.abspath(folder)) is not None:
            print(_NOT_LOCAL_FOLDER, file=sys.stderr)
            return 2
    if folder is not None and not folder.is_dir():
        print(f"The folder does not exist: {folder}", file=sys.stderr)
        return 2
    if args.choice == "status":
        on = grant.granted(folder)
        print("Standing grant: " + ("on" if on else "off")
              + ("" if folder is None else " for this repository"))
        if on and folder is not None and not grant.allows(folder):
            print("Codex is off in this repository, which outranks the grant.")
        return 0
    if args.choice == "on" and os.environ.get("MODELQUAD_WORKER") == "1":
        # A worker never re-enters orchestration (invariant 1), so it cannot grant either.
        # Turning it off stays open to anyone.
        print("Not changed: a ModelQuad worker cannot turn the standing grant on.",
              file=sys.stderr)
        return 2
    if args.choice == "on":
        print_lines((*DATA_FLOW, *grant.NOTICE,
                     *(grant.GLOBAL_NOTICE if args.everywhere else ())))
        if not args.consent:
            if not sys.stdin or not sys.stdin.isatty():
                print("Not changed. After reading the notice above, run again with --yes.",
                      file=sys.stderr)
                return 2
            try:
                answer = input_fn("Type yes to turn the standing grant on: ")
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer.strip().lower() != "yes":
                print("Not changed.", file=sys.stderr)
                return 2
    try:
        scope = grant.write(args.choice == "on", directory=folder, everywhere=args.everywhere)
    except (OSError, ValueError) as exc:
        print(f"The setting was not saved: {getattr(exc, 'strerror', None) or exc}", file=sys.stderr)
        return 1
    if args.choice == "on":
        print(f"Standing grant: on, for {scope}.")
        print("For Claude Code to skip its prompt, add this rule to permissions.allow in your "
              "Claude Code settings (or answer \"don't ask again\" on the first prompt):")
        print("  " + grant.ALLOW_RULE)
        print("Takes effect in new Claude Code sessions.")
    elif folder is not None and grant.granted(folder):
        # The grant for every folder still covers this one: say so, never "off".
        print("This repository has no grant of its own now, but your standing grant for every "
              "folder is still on and covers it. Codex calls here still skip the prompt.")
        print("To keep Codex out of this folder: modelquad settings repo off")
        print("To turn the grant off everywhere: modelquad settings auto off --global")
    else:
        print(f"Standing grant: off, for {scope}. Codex calls ask you again in new sessions; "
              "a session that started with the grant refuses its next call.")
    return 0


def _run_review_command(args: argparse.Namespace) -> int:
    """``modelquad review report``, ``usage-report`` and ``verdict``."""
    from . import review_ledger

    if args.review_action == "usage-report":
        return _run_usage_report(args)
    if args.review_action == "verdict":
        try:
            count = review_ledger.record_verdicts(args.decision_id, [{
                "finding": args.finding, "verdict": args.verdict, "evidence": args.evidence,
                "action": "fixed" if args.fix_verified and args.action == "no_change" else args.action,
                "fix_verified": True if args.fix_verified else None,
                "note": args.note}], source="user")
        except review_ledger.VerdictRefused as exc:
            print(f"Not recorded: {exc}", file=sys.stderr)
            return 2
        except (OSError, sqlite3.Error, review_ledger.LedgerVersionError) as exc:
            print(f"Not recorded: {exc}", file=sys.stderr)
            return 1
        print(f"Recorded your verdict ({count}). Your word outranks Claude's in the report.")
        return 0
    workspace = None
    if not args.all_projects:
        from .localpath import reason as local_reason

        project = Path(os.path.abspath(args.project or Path.cwd()))
        # Before any file-system call: a network share is never opened.
        if local_reason(os.path.abspath(project)) is not None:
            print(_NOT_LOCAL_FOLDER, file=sys.stderr)
            return 2
        workspace = review_ledger.workspace_id(project)
    try:
        if args.findings:
            decisions = review_ledger.list_findings(days=args.days, workspace=workspace)
        else:
            data = review_ledger.report(days=args.days, workspace=workspace)
    except (OSError, sqlite3.Error, review_ledger.LedgerVersionError) as exc:
        print(f"The review ledger could not be read: {exc}", file=sys.stderr)
        return 1
    if args.findings:
        if args.as_json:
            print(json.dumps({"days": args.days, "decisions": decisions}, indent=2, sort_keys=True))
        else:
            for line in review_findings_lines(decisions, days=args.days,
                                              all_projects=args.all_projects):
                print(line)
        return 0
    if args.as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    for line in review_report_lines(data, all_projects=args.all_projects):
        print(line)
    return 0


def _run_usage_report(args: argparse.Namespace) -> int:
    """``modelquad review usage-report``: print the report; ``--out`` also writes it. Sends nothing."""
    from . import review_ledger, usage_report

    if not 1 <= args.days <= 3650:
        print("--days takes a whole number from 1 to 3650.", file=sys.stderr)
        return 2
    if args.out is not None:
        from .localpath import reason as local_reason

        # Before any file-system call: a network share is never opened.
        if local_reason(os.path.abspath(args.out)) is not None:
            print("Not written: that file is not on a local drive (a network share, a device, "
                  "or a link to one). Name a file on a local drive.", file=sys.stderr)
            return 2
        if os.path.lexists(args.out) and not args.force:
            print(f"Not written: {args.out} already exists. Name another file, or add --force "
                  "to replace it.", file=sys.stderr)
            return 2
    try:
        data = usage_report.build(days=args.days)
    except (OSError, sqlite3.Error, review_ledger.LedgerVersionError) as exc:
        print(f"The review ledger could not be read: {exc}", file=sys.stderr)
        return 1
    for line in usage_report.preview_lines(data):
        print(line)
    if args.out is not None:
        try:
            written = usage_report.write(data, args.out, force=args.force)
        except FileExistsError:
            print(f"Not written: {args.out} already exists.", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"Not written: {exc}", file=sys.stderr)
            return 1
        print("")
        print(f"The same JSON was written to {written}. Nothing was sent.")
    return 0


def review_findings_lines(decisions: list, *, days: float, all_projects: bool = False) -> list[str]:
    """``modelquad review report --findings``: each recent call's id and its findings.

    The decision id and the finding reference are what ``modelquad review
    verdict`` takes. Only what the ledger keeps is printed.
    """
    from datetime import datetime

    where = " (all projects)" if all_projects else " (this project)"
    if not decisions:
        return [f"No Codex findings recorded in the last {days:g} days{where}."]
    lines = [f"Codex findings, last {days:g} days{where}, newest first"]
    for decision in decisions:
        when = datetime.fromtimestamp(decision["created_at"]).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{decision['decision_id']}  {when}  {decision['task_type'] or 'unknown kind'}")
        for item in decision["findings"]:
            if item["verdict"] is None:
                verdict = "not judged yet"
            else:
                verdict = f"{item['verdict']} by {'you' if item['by'] == 'user' else 'Claude'}"
                if item["action"] == "fixed":
                    verdict += ", fixed" + (" and verified" if item["fix_verified"] else "")
                elif item["action"] == "to_fix":
                    verdict += ", fix not recorded yet"
            lines.append(f"  {item['finding']}  {item['severity']}  {item['location']}  "
                         f"[{verdict}]  {item['claim']}")
    lines.append("To record your own verdict: modelquad review verdict <decision_id> <F1> "
                 "confirmed|refuted|undecidable")
    return lines


def review_report_lines(data: dict, *, all_projects: bool = False) -> list[str]:
    """The review report as plain lines. Unknown stays unknown."""
    from . import review_ledger

    def value(item, unit=""):
        return "unknown" if item is None else f"{item}{unit}"

    def named(names):
        found = [f"{review_ledger.OUTCOME_WORDS.get(name, name)} {data['runs_by_outcome'][name]}"
                 for name in sorted(data["runs_by_outcome"]) if name in names]
        return ", ".join(found) or "none"

    f = data["findings"]
    started = review_ledger.STARTED_OUTCOMES
    outcomes = named(started)
    stopped = named(set(data["runs_by_outcome"]) - started)
    triggers = ", ".join(f"{name} {count}" for name, count in sorted(data["runs_by_trigger"].items())) or "none"
    gate = data["gate"]
    lines = [
        f"Codex reviews and consultations, last {data['days']:g} days"
        + (" (all projects)" if all_projects else " (this project)"),
        f"  Codex calls (Codex was started): {data['codex_started']} ({outcomes})",
        f"  Stopped before Codex started: {data['not_started']} ({stopped})",
        f"  Why: {triggers}",
        f"  Time: median {value(data['median_seconds'], ' s')}, total {value(data['total_seconds'], ' s')}",
        f"  Codex tokens: in {value(data['input_tokens'])}, out {value(data['output_tokens'])}",
        f"  Findings: {f['findings']}"
        + (f" (and {data['runs_with_unknown_findings']} answer(s) not in the findings format)"
           if data["runs_with_unknown_findings"] else ""),
        f"    Confirmed with independent evidence (test, reproduction, CI or your verdict): "
        f"{f['confirmed_independent']}",
        f"    Confirmed by Claude's reading only (not proof): {f['confirmed_claude_only']}",
        f"    Wrong (refuted): {f['refuted']}",
        f"    Could not be decided: {f['undecidable']}",
        f"    Not judged yet: {f['unjudged']}",
        f"  Fixed after a finding: {f['fixed']} (fix verified by a test or reproduction: "
        f"{f['fixed_and_verified']}); accepted, fix not recorded yet: {f['to_fix']}",
        f"  Acted on although refuted: {f['refuted_but_acted_on']}",
        f"  Review gate: asked {gate.get('blocked', 0)}, review done after asking "
        f"{gate.get('review_done', 0)}, not done and reported {gate.get('not_done_reported', 0)}, "
        f"review failed {gate.get('review_failed', 0)}, small changes skipped {gate.get('skipped_small', 0)}",
        f"  Consultation gate: stopped {gate.get('consult_blocked', 0)}, consulted "
        f"{gate.get('consult_done', 0)}, consultation failed {gate.get('consult_failed', 0)}, "
        f"reminders {gate.get('consult_reminded', 0)}",
    ]
    if f["by_user"]:
        lines.append(f"  Your own verdicts: {f['by_user']}")
    if data.get("by_confidence"):
        lines.append("  By Codex's stated confidence (its confidence is not proof):")
        for name in ("high", "medium", "low", "not stated"):
            bucket = data["by_confidence"].get(name)
            if bucket:
                lines.append(f"    {name}: {bucket['findings']} finding(s), confirmed "
                             f"{bucket['confirmed']}, refuted {bucket['refuted']}, undecidable "
                             f"{bucket['undecidable']}, not judged {bucket['unjudged']}")
    return lines


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


def _run_settings_show(project: str | None) -> int:
    """``modelquad settings`` / ``settings show``: the settings, changing nothing."""
    from . import settings_view
    from .localpath import reason as local_reason

    folder = Path(os.path.abspath(project)) if project else Path.cwd()
    # Before any file-system call: a network share is never opened.
    if local_reason(os.path.abspath(folder)) is not None:
        print(_NOT_LOCAL_FOLDER, file=sys.stderr)
        return 2
    if not folder.is_dir():
        print("That project folder does not exist.", file=sys.stderr)
        return 2
    folder = folder.resolve()
    for line in settings_view.lines_from(settings_view.view(folder), folder):
        print(line)
    return 0


def _run_switch(on: bool) -> int:
    """``modelquad on`` / ``modelquad off``: the one global switch."""
    from . import switch

    try:
        switch.write(on)
    except OSError as exc:
        return _setting_not_saved("The ModelQuad switch", exc)
    if on:
        print("ModelQuad is on: Claude may ask Codex for a read-only review "
              "(Claude Code asks you before each call unless the standing grant is on).")
    else:
        print("ModelQuad is off: no Codex call starts until you run: modelquad on")
    return 0


def _run_package_command(args: argparse.Namespace) -> int:
    """``modelquad review package``: what a review of these files would send. Starts nothing."""
    from . import review

    folder = os.path.abspath(args.project or os.getcwd())
    request = review.ReviewRequest(task_type=args.task_type, prompt=args.task,
                                   working_directory=folder, files=tuple(args.files))
    policies: list = []
    result = (review.gate(request, review.Session(), starts_codex=False, policy_out=policies)
              or review.package(request, policies[0].caps if policies else None))
    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for line in review.package_lines(result):
            print(line)
    return 1 if result.get("refused") else 0


def _run_doctor(args: argparse.Namespace) -> int:
    from . import setupcheck

    report = setupcheck.run(args.project)
    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for line in setupcheck.lines(report):
            print(line)
    return 0 if report["ok"] else 1


def _run_privacy(args: argparse.Namespace) -> int:
    from . import identity as identity_mod
    from . import paths as paths_mod
    from .netpolicy import LOOPBACK_ONLY_REASON

    privacy_state_root = paths_mod.state_root()
    forgotten = None
    if args.reset_identity:
        # Grants and per-folder settings are keyed by the id and cannot follow
        # a new one. They go first, so no grant outlives the user's exclusions.
        from . import grant as _grant

        try:
            forgotten = _grant.forget_keyed_settings()
        except Exception:  # noqa: BLE001 -- an unreadable file holds no grant
            forgotten = None
        installation_id = identity_mod.reset_installation_id(privacy_state_root)
    else:
        # peek, never create: asking what identity exists must not mint one.
        installation_id = identity_mod.peek_installation_id(privacy_state_root)
    payload = {
        "state_root": str(privacy_state_root),
        "installation_id": installation_id,
        "installation_id_reset": bool(args.reset_identity),
        "settings_cleared_by_reset": forgotten,
        "identity_kind": "pseudonymous, random, resettable",
        "telemetry": "none",
        "network": LOOPBACK_ONLY_REASON,
        "retention": dict(PRIVACY_RETENTION),
        "left_by_earlier_builds": list(PRIVACY_LEGACY),
        "delete_everything": _privacy_delete_steps(privacy_state_root),
    }
    if args.as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.reset_identity:
        print("Installation id reset.")
        if forgotten and any(forgotten.values()):
            print("Your per-folder settings and standing grants were stored under the old id and "
                  "cannot follow the new one, so they were removed:")
            print("  standing grant for every folder: "
                  + ("turned off" if forgotten["global_grant"] else "was off"))
            print(f"  standing grants for single repositories: {forgotten['repo_grants']} removed")
            print(f"  per-folder settings (off, offer-only, on): {forgotten['repo_settings']} removed")
            print("Codex calls ask you each time again. Set them again with "
                  "modelquad settings repo and modelquad settings auto.")
        print()
    print(f"Local state:        {privacy_state_root}")
    print(f"Installation id:    {installation_id or '(none created yet)'} (pseudonymous, resettable)")
    print(f"{PRODUCT_NAME} has no telemetry.")
    print(f"Network: {LOOPBACK_ONLY_REASON}. The Codex CLI connects to OpenAI itself.")
    print()
    print("How long things are kept:")
    for store, rule in PRIVACY_RETENTION:
        print(f"  {store}: {rule}.")
    print()
    print("Earlier builds kept more. This build does not read, write or delete it:")
    for item in PRIVACY_LEGACY:
        print(f"  {item}")
    print()
    print("Delete everything:")
    for number, step in enumerate(payload["delete_everything"], start=1):
        print(f"  {number}. {step}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).stem or CLI_COMMAND,
        description="A read-only Codex review inside Claude Code: connect it, choose how it "
                    "asks, and see what each review sent and found.",
    )
    parser.add_argument("-V", "--version", action=_BuildVersionAction,
                        help="Show the installed product version and the exact build, then exit.")
    subcommands = parser.add_subparsers(dest="command", title="subcommands")
    subcommands.add_parser("on", help="Turn ModelQuad on (the default).")
    subcommands.add_parser("off", help="Turn ModelQuad off: no Codex call starts until you turn it on.")
    settings_cmd = subcommands.add_parser(
        'settings', help='With no subcommand, show ModelQuad on or off, the mode and the '
        'history for the current folder (settings show --project DIR for another). '
        'The subcommands change a persisted preference.')
    settings_actions = settings_cmd.add_subparsers(dest='settings_action')
    show_cmd = settings_actions.add_parser(
        'show', help='Show ModelQuad on or off, automatic or approve each call, the rules of '
        'this folder and the history here, with the command that changes each. Changes nothing.')
    show_cmd.add_argument('--project', metavar='DIR',
                          help='The project folder (default: the current folder).')
    proactive_cmd = settings_actions.add_parser(
        'proactive',
        help='Let Claude decide on its own to consult Codex (default on; off: Claude offers it). '
        'Claude Code asks you before each call unless the standing grant is on.')
    proactive_cmd.add_argument('choice', choices=('on', 'off'))
    auto_cmd = settings_actions.add_parser(
        'auto',
        help="The standing grant: let Claude ask Codex without Claude Code's prompt for each "
        'call, in this repository (or every folder with --global). status shows it.')
    auto_cmd.add_argument('choice', choices=('on', 'off', 'status'))
    auto_cmd.add_argument('--project', metavar='DIR',
                          help='The project folder (default: the current folder).')
    auto_cmd.add_argument('--global', action='store_true', dest='everywhere',
                          help='Every folder, instead of one repository.')
    auto_cmd.add_argument('--yes', action='store_true', dest='consent',
                          help='Confirm you have read the notice (needed when not typing at a prompt).')
    repo_cmd = settings_actions.add_parser(
        'repo',
        help='Your own Codex setting for one repository: on (Claude may decide on its own), '
        'off (no Codex here), offer-only (Claude offers), inherit (the global setting). '
        'With no choice, show it.')
    repo_cmd.add_argument('choice', nargs='?', choices=('on', 'off', 'offer-only', 'inherit'))
    repo_cmd.add_argument('--project', metavar='DIR',
                          help='The project folder (default: the current folder).')
    privacy_cmd = subcommands.add_parser(
        "privacy", help="Show what this installation stores locally and how to delete it.")
    privacy_cmd.add_argument("--reset-identity", action="store_true",
                             help="Mint a fresh installation id. Per-folder settings and grants "
                                  "stored under the old id are removed.")
    privacy_cmd.add_argument("--json", action="store_true", dest="as_json", help="Print JSON.")
    for command_name in ("install", "uninstall"):
        adapter_command = subcommands.add_parser(
            command_name, help=f"{command_name.title()} ModelQuad in Claude Code.")
        clients = tuple(CLIENT_NAMES)
        adapter_command.add_argument("client", nargs="?", type=_surface_choice(clients),
                                     metavar="{" + ",".join(clients) + "}")
        adapter_command.add_argument("--all", action="store_true", dest="all_clients")
        adapter_command.add_argument("--global", action="store_true", dest="global_install",
                                     help="Apply at Claude Code's own settings folder, if it is detected.")
        adapter_command.add_argument("--target-dir", type=Path)
        adapter_command.add_argument("--claude-dir", type=Path)
        adapter_command.add_argument("--dry-run", action="store_true")
        adapter_command.add_argument("--json", action="store_true", dest="as_json",
                                     help="Print the report as JSON.")
        if command_name == "install":
            via_plugin = adapter_command.add_mutually_exclusive_group()
            via_plugin.add_argument(
                "--plugin", action="store_true", dest="plugin", default=None,
                help="The ModelQuad Claude Code plugin supplies the server, commands and "
                "hooks: take back ModelQuad's own copies and remember the choice.")
            via_plugin.add_argument(
                "--no-plugin", action="store_false", dest="plugin",
                help="Install the server, commands and hooks directly again (the default).")
    doctor = subcommands.add_parser(
        "doctor", help="Check the setup: Codex, the Claude Code connection, your settings "
                       "and the .modelquad.toml of this folder, with what to do next.")
    doctor.add_argument("--json", action="store_true", dest="as_json", help="Print the report as JSON.")
    doctor.add_argument('--project', metavar='DIR',
                        help='The project folder (default: the current folder).')
    subcommands.add_parser("mcp", help="Run the MCP server Claude Code talks to (stdio).")
    review_hook = subcommands.add_parser(
        "hook-review", help="The review reminder Claude Code runs; not for manual use.")
    review_hook.add_argument("host", choices=("claude",))
    review_cmd = subcommands.add_parser(
        "review", help="What a review would send, and what the reviews found, what was "
                       "verified and what they used.")
    review_actions = review_cmd.add_subparsers(dest="review_action", required=True)
    package_cmd = review_actions.add_parser(
        "package", help="Show what a review of these files would send to Codex. Starts nothing.")
    package_cmd.add_argument("files", nargs="*", metavar="FILE",
                             help="Paths relative to the project folder (at most 32).")
    package_cmd.add_argument('--project', metavar='DIR',
                             help='The project folder (default: the current folder).')
    package_cmd.add_argument('--task', default="Review these files.",
                             help='The task text that would go with them.')
    package_cmd.add_argument('--kind', dest='task_type', default='review',
                             choices=('review', 'exploration', 'architecture'))
    package_cmd.add_argument('--json', action='store_true', dest='as_json', help='Print JSON.')
    review_report_cmd = review_actions.add_parser(
        "report", help="Calls, findings by verdict and evidence, fixes, time and tokens.")
    review_report_cmd.add_argument('--project', metavar='DIR',
                                   help='The project folder (default: the current folder).')
    review_report_cmd.add_argument('--all-projects', action='store_true',
                                   help='Every project instead of one.')
    review_report_cmd.add_argument('--days', type=float, default=30.0,
                                   help='The period in days (default: 30).')
    review_report_cmd.add_argument('--json', action='store_true', dest='as_json', help='Print JSON.')
    review_report_cmd.add_argument('--findings', action='store_true',
                                   help='List the recent calls with their decision id and each '
                                        'finding, for modelquad review verdict.')
    usage_report_cmd = review_actions.add_parser(
        "usage-report", help="Print a usage report (counts and versions only, every project) "
                             "for you to read and pass on yourself. Sends nothing.")
    usage_report_cmd.add_argument('--days', type=int, default=30,
                                  help='The period in days (default: 30).')
    usage_report_cmd.add_argument('--out', metavar='FILE',
                                  help='Also write the JSON to this file.')
    usage_report_cmd.add_argument('--force', action='store_true',
                                  help='With --out: replace the file if it exists.')
    review_verdict_cmd = review_actions.add_parser(
        "verdict", help="Record your own verdict on one finding; it outranks the one Claude recorded.")
    review_verdict_cmd.add_argument("decision_id")
    review_verdict_cmd.add_argument("finding", help="The reference of the finding, such as F1.")
    review_verdict_cmd.add_argument("verdict", choices=("confirmed", "refuted", "undecidable"))
    review_verdict_cmd.add_argument("--evidence", default="none",
                                    choices=("test", "reproduced", "ci", "code_reading", "none"))
    review_verdict_cmd.add_argument("--action", default="no_change",
                                    choices=("to_fix", "fixed", "no_change", "deferred"))
    review_verdict_cmd.add_argument("--fix-verified", action="store_true",
                                    help="The fix was confirmed by a test or a reproduction "
                                         "(records the action as fixed).")
    review_verdict_cmd.add_argument("--note", help="A short reason, no code.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from .cli_entry import dispatch_retired, dispatch_review

    arguments = list(sys.argv[1:] if argv is None else argv)
    for dispatch in (dispatch_retired, dispatch_review):
        result = dispatch(arguments)
        if result is not None:
            return result
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command in ("on", "off"):
        return _run_switch(args.command == "on")
    if args.command == 'settings':
        if args.settings_action in (None, 'show'):
            return _run_settings_show(getattr(args, 'project', None))
        if args.settings_action == 'proactive':
            return _run_proactive_setting(args.choice == 'on')
        if args.settings_action == 'repo':
            return _run_repo_setting(args.choice, args.project)
        return _run_auto_setting(args)
    if args.command == 'review':
        if args.review_action == 'package':
            return _run_package_command(args)
        return _run_review_command(args)
    if args.command in {"install", "uninstall"}:
        return _run_adapter_command(args, parser)
    if args.command == "mcp":
        from .mcp.server import main as mcp_main

        mcp_main()
        return 0
    if args.command == "privacy":
        return _run_privacy(args)
    if args.command == "doctor":
        return _run_doctor(args)
    parser.error(f"unknown command {args.command!r}")
    return 2
