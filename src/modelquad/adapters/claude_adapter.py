# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Installer for the Claude Code adapter templates.

Every run reads all four target files first (``SKILL.md``, ``~/.claude.json``,
``CLAUDE.md`` and ``settings.json``), changes them in memory, and then writes
each changed file once, backing up its original bytes immediately before that
write (see :mod:`staging`). A file that cannot be read safely is skipped with a
plain reason; the others still proceed.

The global footprint is deliberately small: the ``/modelquad`` skill and six
user-only command skills (``/modelquad-review``, ``/modelquad-handoff``,
``/modelquad-quota``, ``/modelquad-setup``, ``/modelquad-split``,
``/modelquad-settings``), the MCP
server entry (which carries its own tool ``timeout``), the status line when the
user has none, and the SessionStart reminder. The model guidance comes from the
MCP server's ``instructions`` (``server_instructions.py``), so nothing is
written into the user's global ``CLAUDE.md``. Install and uninstall also take
back what earlier builds added -- the ``CLAUDE.md`` managed block, the global
``env.MCP_TOOL_TIMEOUT`` and the advisory PreToolUse gate hook -- each only
when it is provably ModelQuad's, and each with a backup.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PureWindowsPath

from ..branding import CLI_COMMAND, MANAGED_BLOCK_TAG, MCP_COMMAND, MCP_SERVER_NAME, PRODUCT_SLUG
from .backup import backup_if_exists
from .cliintegration import (
    owns_cli_command,
    plan_owned_status_line,
    plan_remove_owned_status_line,
)
from .codex_hooks import pending_action, plan_pending_hook
from .hooksmerge import drop_hook_entry, payload_commands, wired_commands
from .jsonmerge import delete_key, set_key
from .mcpentry import mcp_entry as _mcp_template_value
from .paths import adapters_template_dir
from .report import ActionKind, FileAction, InstallReport
from .staging import Change, StagedJson, StagedText, UNCHANGED, is_link, link_refusal
from .textblock import remove_block


_MCP_KEY_PATH = ("mcpServers", MCP_SERVER_NAME)
# The skill directory name is what the user types (`/modelquad`).
_SKILL_DIR = PRODUCT_SLUG
# Commands only the user starts (``disable-model-invocation: true``). Each
# still reaches Codex only through a tool Claude Code asks about.
_COMMAND_SKILL_DIRS = tuple(f"{PRODUCT_SLUG}-{name}" for name in ("review", "handoff", "quota", "setup", "split",
                                                                     "settings"))
_SKILL_DIRS = (_SKILL_DIR, *_COMMAND_SKILL_DIRS)
# Command skills sit in folders a user could also choose, so each copy carries
# this line in its frontmatter; install and uninstall touch only a file that
# has it.
_COMMAND_SKILL_MARK = b"managed-by: modelquad"
_NOT_OURS = "{} has no ModelQuad mark, so it is not ModelQuad's and was left unchanged."
_STATUS_LINE_ARGUMENTS = ("usage-hook", "claude")
#: The compact part a user adds to a status line of their own.
SEGMENT_ARGUMENTS = ("usage-hook", "claude", "--segment")
# Earlier builds installed an advisory PreToolUse hook that added one note per
# session suggesting ModelQuad. The MCP server's instructions now carry that
# guidance into every session that has the server, so the hook only repeated
# it -- at the cost of a process start before every Bash/Edit/Write call. It
# is no longer installed; install and uninstall remove an owned one (our CLI
# name with exactly ``hook-gate``) and nothing else. ``modelquad hook-gate``
# still answers, harmlessly, for a session started before the removal.
_HOOK_EVENT = "PreToolUse"
_HOOK_MATCHER = "Bash|Edit|Write|MultiEdit|NotebookEdit|Task"
# Earlier builds raised Claude Code's tool timeout for EVERY MCP server through
# the global ``env.MCP_TOOL_TIMEOUT``. The timeout now lives on the modelquad
# server entry itself (``"timeout"`` in templates/claude/mcp/mcp-servers.json,
# milliseconds, documented by Claude Code to override MCP_TOOL_TIMEOUT for that
# server only). The constants below identify the old global value so that an
# owned one can be removed; a value the user set is never touched.
_MCP_TOOL_TIMEOUT_KEY_PATH = ("env", "MCP_TOOL_TIMEOUT")
_MCP_TOOL_TIMEOUT_MS = "900000"  # what earlier builds wrote, and recorded as owned
_HTML_MARKER = {"comment_prefix": "<!--", "marker_suffix": "-->"}

_REMOVED_BLOCK = (
    "Removed the ModelQuad block an earlier version added to this file; "
    "ModelQuad's guidance now comes from its MCP server."
)
_REMOVED_TIMEOUT = (
    "Removed the global MCP_TOOL_TIMEOUT an earlier version added; "
    "the timeout is now set on the modelquad server entry only."
)
_REMOVED_GATE = (
    "Removed the advisory PreToolUse hook an earlier version added; "
    "ModelQuad's guidance now comes from its MCP server."
)
_LEFT_TO_PLUGIN = "Removed ModelQuad's server entry; the ModelQuad plugin supplies the server."
_REMINDER_TO_PLUGIN = "Removed ModelQuad's SessionStart reminder; the ModelQuad plugin runs it."
_FOREIGN_SERVER = (
    "A modelquad server entry that ModelQuad's install did not write was left in place. "
    "Claude Code starts it next to the plugin's server; remove it yourself to keep one."
)
_FOREIGN_SERVER_KEPT = (
    "A modelquad server entry that ModelQuad did not write was left in place: this "
    "installation has no record of it and it does not start ModelQuad's own server."
)


def _status_line_command() -> str:
    from .. import hostruntime

    return hostruntime.integration_command(*_STATUS_LINE_ARGUMENTS, shell="bash")


def segment_command() -> str:
    """``modelquad usage-hook claude --segment`` spelled for Claude Code's bash."""

    from .. import hostruntime

    return hostruntime.integration_command(*SEGMENT_ARGUMENTS, shell="bash")


def mcp_config_path(target_dir: Path) -> Path:
    """``~/.claude.json`` for the default ``~/.claude`` target."""

    target = Path(target_dir)
    return target.parent / f"{target.name}.json"


def read_hook_commands(target_dir: Path) -> tuple[str, ...]:
    """Expose Claude's recorded gate wiring without duplicating its settings path."""
    return wired_commands(target_dir / "settings.json", _HOOK_EVENT)


def read_status_line_command(target_dir: Path) -> str | None:
    """Read Claude's status line; ownership is a separate structural decision."""
    from .cliintegration import read_status_line_command as read_command

    return read_command(target_dir / "settings.json")


def status_line_recipe(command: str | None) -> str | None:
    """The one line that adds ModelQuad to a status line the user already has.

    None when there is nothing to add: no status line (install writes its own),
    ModelQuad's own, or a user line that already runs ``usage-hook claude``.
    ccstatusline gets its Custom Command widget; any other command gets a
    replacement that runs theirs and then ours on the same input, so a
    multi-line status line (claude-hud) keeps every line.
    """

    if not isinstance(command, str) or not command.strip():
        return None
    if owns_cli_command(command, _STATUS_LINE_ARGUMENTS) or "usage-hook claude" in command:
        return None
    segment = segment_command()
    if "ccstatusline" in command.casefold():
        return (
            "In ccstatusline (run it with no arguments to open its editor), add a "
            f"Custom Command widget with this command: {segment}"
        )
    return (
        "Set statusLine.command in ~/.claude/settings.json to: "
        f"input=$(cat); printf '%s' \"$input\" | {command.strip()}; "
        f"printf '%s' \"$input\" | {segment}"
    )


class _OwnedFile(StagedText):
    """A file ModelQuad owns outright, so any existing bytes may be replaced."""

    content: bytes | None = None

    def _read(self, *, read_only: bool = False) -> None:
        super()._read(read_only=read_only)
        if self.refusal is not None and self.original is not None:
            # Only a decoding refusal leaves the bytes read: an owned file's
            # content is ours to replace whatever its encoding.
            self.refusal = None

    def render(self) -> bytes | None:
        if self.refusal is not None or self.content is None or self.content == self.original:
            return None
        return self.content


def _plan_owned_copy(staged: _OwnedFile, content: bytes) -> Change:
    if staged.refusal is not None:
        return UNCHANGED
    staged.content = content
    return UNCHANGED if staged.original == content else Change("set")


def _plan_command_skill(staged: _OwnedFile, content: bytes) -> Change:
    """Like :func:`_plan_owned_copy`, but never replace a file without our mark."""

    if staged.refusal is None and staged.original is not None and _COMMAND_SKILL_MARK not in staged.original:
        staged.refusal = _NOT_OURS.format(staged.path)
    return _plan_owned_copy(staged, content)


def _remove_command_skill(
    destination: Path, *, dry_run: bool, installed: bytes | None = None,
) -> FileAction:
    """Remove a command skill only when the file carries ModelQuad's mark."""

    if not is_link(destination) and destination.is_file():
        try:
            data = destination.read_bytes()
        except OSError:
            data = None
        if data is not None and _COMMAND_SKILL_MARK not in data:
            return FileAction(destination, ActionKind.SKIPPED, detail=_NOT_OURS.format(destination))
    return _remove_owned_file(destination, dry_run=dry_run, installed=installed)


def _remove_owned_file(
    destination: Path, *, dry_run: bool, installed: bytes | None = None,
) -> FileAction:
    """Remove a fully-owned file after taking a sibling backup.

    A file that still holds exactly what ModelQuad installed (``installed``)
    has nothing of the user's in it, so no copy is kept: the copy would sit in
    the skill's own folder and keep that folder alive after the uninstall.
    """

    if is_link(destination):
        return FileAction(destination, ActionKind.SKIPPED, detail=link_refusal(destination))
    if not destination.exists():
        return FileAction(destination, ActionKind.UNCHANGED)
    if not destination.is_file():
        return FileAction(
            destination,
            ActionKind.SKIPPED,
            detail="Destination exists but is not a regular file.",
        )
    try:
        backup_path = None
        if installed is None or destination.read_bytes() != installed:
            backup_path = backup_if_exists(destination, dry_run=dry_run)
        if dry_run:
            return FileAction(destination, ActionKind.WOULD_REMOVE, backup_path=backup_path)
        destination.unlink()
    except OSError as exc:
        return FileAction(
            destination, ActionKind.SKIPPED,
            detail=f"Could not remove {destination}: {exc.strerror or exc}. It was left in place.",
        )
    try:
        destination.parent.rmdir()
    except OSError:
        pass
    return FileAction(destination, ActionKind.REMOVED, backup_path=backup_path)


def _skill_templates(templates: Path) -> dict[str, bytes]:
    """What install writes for each skill, byte for byte."""

    return {name: (templates / "skills" / name / "SKILL.md").read_bytes() for name in _SKILL_DIRS}


def _remove_skills(
    destination: Path, installed: dict[str, bytes], *, dry_run: bool,
) -> dict[str, FileAction]:
    skills = {_SKILL_DIR: _remove_owned_file(
        destination / "skills" / _SKILL_DIR / "SKILL.md", dry_run=dry_run,
        installed=installed.get(_SKILL_DIR),
    )}
    skills.update({
        name: _remove_command_skill(
            destination / "skills" / name / "SKILL.md", dry_run=dry_run, installed=installed.get(name),
        )
        for name in _COMMAND_SKILL_DIRS
    })
    return skills


def _has_home_policy_block(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return f"<!-- >>> {MANAGED_BLOCK_TAG} managed block >>> -->" in text


def _plan_remove_policy_block(staged: StagedText) -> Change:
    """Take back the managed block earlier builds wrote into the global CLAUDE.md.

    Only the marked block is removed; the user's own text stays, and the
    staged write backs the file up first.
    """

    if staged.refusal is not None or not staged.existed:
        return UNCHANGED
    try:
        updated, changed = remove_block(staged.text, **_HTML_MARKER)
    except ValueError as exc:
        return Change("skipped", str(exc))
    if not changed:
        return UNCHANGED
    staged.text = updated
    return Change("remove", _REMOVED_BLOCK)


def _policy_action(policy: StagedText, change: Change, *, dry_run: bool) -> list[FileAction]:
    """Report CLAUDE.md only when there was a ModelQuad block to take back.

    ModelQuad no longer writes this file, so an untouched CLAUDE.md -- or one
    that is a link and holds no block -- is not part of the report.
    """

    if change.kind == "unchanged" and (
        policy.refusal is None or not _has_home_policy_block(policy.path)
    ):
        return []
    return [policy.action(change, dry_run=dry_run)]


def _timeout_ownership_key(settings: Path) -> str:
    # Scope ownership to this target without persisting the user's full path.
    key = hashlib.sha256(os.path.normcase(str(settings.resolve())).encode("utf-8")).hexdigest()
    return f"claude_timeout:{key}"


def _timeout_env_key(settings: Path) -> str:
    """Records that ``env`` did not exist until ModelQuad added the timeout."""

    return _timeout_ownership_key(settings).replace("claude_timeout:", "claude_timeout_env:", 1)


def _plan_remove_tool_timeout(staged: StagedJson) -> tuple[Change, tuple[str, ...]]:
    """Remove the timeout ModelQuad added; returns the records to retire after."""

    from .registration import read_owned_settings

    if staged.refusal is not None:
        return UNCHANGED, ()
    records = read_owned_settings()
    key = _timeout_ownership_key(staged.path)
    if records.get(key) != _MCP_TOOL_TIMEOUT_MS:
        return UNCHANGED, ()
    env_key = _timeout_env_key(staged.path)
    env = staged.payload.get("env")
    if not (isinstance(env, dict) and env.get("MCP_TOOL_TIMEOUT") == records[key]):
        return UNCHANGED, (key, env_key)
    del env["MCP_TOOL_TIMEOUT"]
    if not env and records.get(env_key) is True:
        # ModelQuad created ``env`` for this one value; leave no empty shell.
        del staged.payload["env"]
    return Change("remove"), (key, env_key)


def _retire_records(keys: tuple[str, ...]) -> str | None:
    from .registration import BackupPersistenceError, read_owned_settings, write_owned_settings

    try:
        records = read_owned_settings()
        for key in keys:
            records.pop(key, None)
        write_owned_settings(records)
    except (OSError, BackupPersistenceError):
        # Registration recovery must still run after adapter cleanup.
        return "Could not retire timeout ownership; its record was kept."
    return None


def _timeout_action(
    settings: StagedJson, change: Change, retire: tuple[str, ...], *, dry_run: bool,
) -> list[FileAction]:
    """Report and settle the removal of an owned global timeout, if any."""

    if change.changes_file:
        change = Change(change.kind, _REMOVED_TIMEOUT)
    action = settings.action(change, dry_run=dry_run)
    if retire and not dry_run and action.kind is not ActionKind.SKIPPED:
        note = _retire_records(retire)
        if note is not None:
            action = FileAction(
                action.path, action.kind, backup_path=action.backup_path,
                detail=" ".join(filter(None, (action.detail, note))),
            )
    return [action] if change.kind != "unchanged" else []


def _plan_remove_gates(payload) -> list[Change]:
    """Remove every owned advisory gate hook; a user's own hooks stay.

    Only commands that invoke ModelQuad's own CLI name with exactly
    ``hook-gate`` are ours to remove.
    """

    changes: list[Change] = []
    for command in dict.fromkeys(payload_commands(payload, _HOOK_EVENT)):
        if owns_cli_command(command, ("hook-gate",)):
            change = drop_hook_entry(payload, _HOOK_EVENT, command)
            if change.changes_file:
                change = Change(change.kind, _REMOVED_GATE)
            changes.append(change)
    return changes


def _plan_leave_server_to_plugin(staged: StagedJson, template_value: dict) -> Change:
    """Remove ModelQuad's own server entry so the plugin's is the only one.

    Only an entry ModelQuad provably wrote goes: today's exact value, or one
    in the install lineage. Anything else is the user's own registration,
    which install never destroys; it stays, and the report says so.
    """

    from .registration import recorded_replacement, registration_owned

    if staged.refusal is not None:
        return UNCHANGED
    servers = staged.payload.get("mcpServers")
    entry = servers.get(MCP_SERVER_NAME) if isinstance(servers, dict) else None
    if entry is None:
        return UNCHANGED
    record = recorded_replacement("claude")
    owned = entry == template_value or (
        record is not None and isinstance(entry, dict) and registration_owned(record, entry, None)
    )
    if not owned:
        return Change("skipped", _FOREIGN_SERVER)
    change = delete_key(staged.payload, _MCP_KEY_PATH)
    return Change(change.kind, _LEFT_TO_PLUGIN) if change.changes_file else change


def own_server_entry(entry: object) -> bool:
    """Whether an entry starts ModelQuad's own server, judged by its command.

    ``modelquad-mcp`` (bare or by path) with no arguments, or ``modelquad mcp``:
    the only two spellings ModelQuad has registered. Anything else under the
    name ``modelquad`` is someone's own server.
    """

    from .. import hostruntime

    command = entry.get("command") if isinstance(entry, dict) else None
    extra = entry.get("args", []) if isinstance(entry, dict) else None
    if not isinstance(command, str) or not command.strip() or not isinstance(extra, list):
        return False
    executable, arguments = hostruntime.split_command(command)
    name = PureWindowsPath(executable).name.lower()
    if name.endswith((".cmd", ".exe", ".bat")):
        name = name[:-4]
    arguments = (*arguments, *extra)
    return (name == MCP_COMMAND and not arguments) or (name == CLI_COMMAND and arguments == ("mcp",))


def _plan_remove_own_server(staged: StagedJson) -> Change:
    """Uninstall's removal of the server entry: only one that is ModelQuad's.

    Proof is this installation's record of having written the value, or a
    command that starts ModelQuad's own server (which is all there is to go on
    when the state folder moved and the record with it). A different entry
    under the same name is the user's; it stays, and the report says so.
    """

    from .registration import recorded_replacement, registration_owned

    if staged.refusal is not None:
        return UNCHANGED
    servers = staged.payload.get("mcpServers")
    entry = servers.get(MCP_SERVER_NAME) if isinstance(servers, dict) else None
    if entry is None:
        return UNCHANGED
    record = recorded_replacement("claude")
    recorded = record is not None and isinstance(entry, dict) and registration_owned(record, entry, None)
    if not (recorded or own_server_entry(entry)):
        return Change("skipped", _FOREIGN_SERVER_KEPT)
    return delete_key(staged.payload, _MCP_KEY_PATH)


def plugin_recorded(target_dir: Path) -> bool | None:
    """Whether Claude Code's plugin record lists the ModelQuad plugin.

    Read-only, from ``plugins/installed_plugins.json`` under the Claude
    folder. Claude Code does not document that file as stable, and it records
    what was installed, not what is enabled, so this is only ever evidence for
    a report. ``None`` when the file is missing or unreadable: unknown.
    """

    try:
        payload = json.loads(
            (Path(target_dir) / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError):
        return None
    plugins = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(plugins, dict):
        return None
    return any(str(key).split("@", 1)[0] == PRODUCT_SLUG for key in plugins)


def _read_mcp_entry(target_dir: Path) -> object:
    try:
        payload = json.loads(mcp_config_path(target_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    return servers.get(MCP_SERVER_NAME) if isinstance(servers, dict) else None


class ClaudeAdapter:
    """Install Claude Code's fully-owned skill and MCP configuration entry.

    With ``via_plugin`` the ModelQuad plugin supplies the server, the commands
    and the reminder, so install keeps only the status line and takes back its
    own copies of the rest: Claude Code would otherwise run each twice.
    ``None`` reads the choice ``modelquad install claude --plugin`` saved.
    """

    name: str = "claude"

    def __init__(self, *, via_plugin: bool | None = None) -> None:
        if via_plugin is None:
            from .server_instructions import read_claude_plugin

            via_plugin = read_claude_plugin()
        self.via_plugin = via_plugin

    def install(self, target_dir: Path, *, dry_run: bool = False) -> InstallReport:
        if self.via_plugin:
            return self._install_beside_plugin(Path(target_dir), dry_run=dry_run)
        destination = Path(target_dir)
        # Resolve the shell command before the first filesystem write.
        status_line_command = _status_line_command()
        with adapters_template_dir(self.name) as templates:
            skill_contents = _skill_templates(templates)
            mcp_value = _mcp_template_value(templates)

        # Preflight: every target is read, and refused if unsafe, before any write.
        skills = {name: _OwnedFile.load(destination / "skills" / name / "SKILL.md")
                  for name in _SKILL_DIRS}
        mcp = StagedJson.load(mcp_config_path(destination))
        policy = StagedText.load(destination / "CLAUDE.md")
        settings = StagedJson.load(destination / "settings.json")

        skill_changes = {_SKILL_DIR: _plan_owned_copy(skills[_SKILL_DIR], skill_contents[_SKILL_DIR])}
        skill_changes.update({name: _plan_command_skill(skills[name], skill_contents[name])
                              for name in _COMMAND_SKILL_DIRS})
        mcp_change = set_key(mcp.payload, _MCP_KEY_PATH, mcp_value) if mcp.refusal is None else UNCHANGED
        policy_change = _plan_remove_policy_block(policy)
        status_change = timeout_change = pending_change = UNCHANGED
        gates: list[Change] = []
        retire: tuple[str, ...] = ()
        if settings.refusal is None:
            payload = settings.payload
            status_change = plan_owned_status_line(
                payload, _STATUS_LINE_ARGUMENTS, status_line_command, status_type="command"
            )
            gates = _plan_remove_gates(payload)
            timeout_change, retire = _plan_remove_tool_timeout(settings)
            pending_change = plan_pending_hook(settings, self.name, dry_run=dry_run)

        for staged in (*skills.values(), mcp, policy, settings):
            staged.commit(dry_run=dry_run)
        actions = [
            skills[_SKILL_DIR].action(skill_changes[_SKILL_DIR], dry_run=dry_run),
            mcp.action(mcp_change, dry_run=dry_run),
            *_policy_action(policy, policy_change, dry_run=dry_run),
            settings.action(status_change, dry_run=dry_run),
            *_timeout_action(settings, timeout_change, retire, dry_run=dry_run),
            pending_action(settings, pending_change, dry_run=dry_run),
            *(settings.action(change, dry_run=dry_run) for change in gates),
            # The command skills come last so earlier report positions stay put.
            *(skills[name].action(skill_changes[name], dry_run=dry_run)
              for name in _COMMAND_SKILL_DIRS),
        ]
        return InstallReport(self.name, dry_run, tuple(actions))

    def _install_beside_plugin(self, destination: Path, *, dry_run: bool) -> InstallReport:
        """The status line only; ModelQuad's own server, commands and reminder go.

        Each removal takes only what is provably ModelQuad's, with a backup,
        exactly as uninstall does. The report keeps install's order.
        """

        status_line_command = _status_line_command()
        with adapters_template_dir(self.name) as templates:
            mcp_value = _mcp_template_value(templates)

        mcp = StagedJson.load(mcp_config_path(destination))
        policy = StagedText.load(destination / "CLAUDE.md")
        settings = StagedJson.load(destination / "settings.json")

        mcp_change = _plan_leave_server_to_plugin(mcp, mcp_value)
        policy_change = _plan_remove_policy_block(policy)
        status_change = timeout_change = pending_change = UNCHANGED
        gates: list[Change] = []
        retire: tuple[str, ...] = ()
        if settings.refusal is None:
            payload = settings.payload
            status_change = plan_owned_status_line(
                payload, _STATUS_LINE_ARGUMENTS, status_line_command, status_type="command"
            )
            gates = _plan_remove_gates(payload)
            timeout_change, retire = _plan_remove_tool_timeout(settings)
            pending_change = plan_pending_hook(settings, self.name, dry_run=dry_run, remove=True)
            if pending_change.changes_file:
                pending_change = Change(pending_change.kind, _REMINDER_TO_PLUGIN)

        # The switch keeps a copy of every file it removes, as it always has.
        skills = _remove_skills(destination, {}, dry_run=dry_run)
        for staged in (mcp, policy, settings):
            staged.commit(dry_run=dry_run)
        actions = [
            skills[_SKILL_DIR],
            mcp.action(mcp_change, dry_run=dry_run),
            *_policy_action(policy, policy_change, dry_run=dry_run),
            settings.action(status_change, dry_run=dry_run),
            *_timeout_action(settings, timeout_change, retire, dry_run=dry_run),
            pending_action(settings, pending_change, dry_run=dry_run),
            *(settings.action(change, dry_run=dry_run) for change in gates),
            *(skills[name] for name in _COMMAND_SKILL_DIRS),
        ]
        return InstallReport(self.name, dry_run, tuple(actions))

    def uninstall(self, target_dir: Path, *, dry_run: bool = False) -> InstallReport:
        destination = Path(target_dir)
        with adapters_template_dir(self.name) as templates:
            installed = _skill_templates(templates)

        # Preflight: read every file before the first removal.
        mcp = StagedJson.load(mcp_config_path(destination))
        policy = StagedText.load(destination / "CLAUDE.md")
        settings = StagedJson.load(destination / "settings.json")

        mcp_change = _plan_remove_own_server(mcp)
        policy_change = _plan_remove_policy_block(policy)
        status_change = timeout_change = pending_change = UNCHANGED
        gates: list[Change] = []
        retire: tuple[str, ...] = ()
        if settings.refusal is None:
            payload = settings.payload
            status_change = plan_remove_owned_status_line(payload, _STATUS_LINE_ARGUMENTS)
            gates = _plan_remove_gates(payload)
            timeout_change, retire = _plan_remove_tool_timeout(settings)
            pending_change = plan_pending_hook(settings, self.name, dry_run=dry_run, remove=True)

        skills = _remove_skills(destination, installed, dry_run=dry_run)
        for staged in (mcp, policy, settings):
            staged.commit(dry_run=dry_run)
        actions = [
            skills[_SKILL_DIR],
            mcp.action(mcp_change, dry_run=dry_run),
            *_policy_action(policy, policy_change, dry_run=dry_run),
            settings.action(status_change, dry_run=dry_run),
            *_timeout_action(settings, timeout_change, retire, dry_run=dry_run),
            pending_action(settings, pending_change, dry_run=dry_run),
            *(settings.action(change, dry_run=dry_run) for change in gates),
            *(skills[name] for name in _COMMAND_SKILL_DIRS),
        ]
        return InstallReport(self.name, dry_run, tuple(actions))

    def is_installed(self, target_dir: Path) -> bool:
        """The skill is in place and Claude Code has the modelquad server entry.

        Whether that entry launches THIS installation is a separate question,
        answered by doctor's ``claude MCP runtime`` check. Beside the plugin,
        it is Claude Code's own record that the plugin is installed.
        """
        destination = Path(target_dir)
        if self.via_plugin:
            return plugin_recorded(destination) is True
        return (
            (destination / "skills" / _SKILL_DIR / "SKILL.md").is_file()
            and isinstance(_read_mcp_entry(destination), dict)
        )
