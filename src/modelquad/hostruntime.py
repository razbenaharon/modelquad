# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Which ModelQuad runtime a host integration is told to launch.

The rule
--------

ModelQuad is installed with pip, which puts the ``modelquad`` and
``modelquad-mcp`` console commands on PATH. Those bare names are the canonical
registration: Claude Code's MCP entry, its PreToolUse gate, its status line
and its hooks all invoke them by name, so the copy the user
installed is the copy the host launches, and an upgrade with pip needs no
reinstall of the host wiring.

The reverse direction (given a registration, what does it name?) is what
``doctor`` uses. It is deliberately structural -- no subprocess is spawned to
interrogate a registered command, matching the rest of ``doctor``'s host
checks. A bare name is healthy when it resolves on PATH. An absolute path is
judged without any lookup: it pins one copy regardless of PATH and upgrades,
so it is reported, and ``repair`` rewrites it to the bare name.

How the launcher is spelled
---------------------------
MCP registrations are structured (a JSON ``command`` the host spawns
directly). The CLI-invoking integrations are single strings a host hands to a
shell -- and on Windows, Claude Code hands them to **bash** (Git Bash), not
cmd.exe. An unquoted backslash is an escape character to a POSIX shell, so a
Windows path written without quotes loses its separators and the hook exits
127. ``render_launcher`` therefore spells a path with POSIX separators and
double-quotes it when it contains shell punctuation or whitespace, and
``launcher_spelling_problem`` reports a recorded path that bash cannot run.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .branding import CLI_COMMAND, MCP_COMMAND, MCP_SERVER_NAME
from .executable_resolution import admit_executable, resolve_executable


def discovery_which(name: str) -> str | None:
    """Safe presence lookup only; discovery never launches the returned file.

    Batch filenames are valid discovery evidence. Codex/Claude match
    probe_which; launchers retain their own policy.
    """
    resolve = (admit_executable if not name or any(char in name for char in ("/", "\\", ":", "\x00"))
               else resolve_executable)
    return resolve(name, allow_batch=True).path


def unsupported_platform_message(
    *,
    os_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    release: str | None = None,
) -> str | None:
    """Why this platform cannot be set up, or None on native Windows.

    ModelQuad drives Windows-only process control and writes Windows paths
    into Claude Code's configuration. WSL is Linux to Python, so it is refused
    too, and named, because a WSL user's Claude Code and ModelQuad would sit
    on two different machines as far as paths are concerned.
    """

    if (os.name if os_name is None else os_name) == "nt":
        return None
    environ = os.environ if environ is None else environ
    if release is None:
        try:
            release = os.uname().release
        except (AttributeError, OSError):
            release = ""
    wsl = bool(environ.get("WSL_DISTRO_NAME") or environ.get("WSL_INTEROP")) or (
        "microsoft" in release.lower()
    )
    if wsl:
        return (
            "ModelQuad supports native Windows only; WSL is not supported. "
            "Run this command from Windows (PowerShell or Command Prompt). Nothing was changed."
        )
    return "ModelQuad supports native Windows only. Nothing was changed."


def claude_config_dir_warning(environ: Mapping[str, str] | None = None) -> str | None:
    """A warning when ``CLAUDE_CONFIG_DIR`` moves Claude Code's settings.

    With that variable set, Claude Code reads its settings and MCP servers
    from the named folder instead of ``~/.claude`` and ``~/.claude.json``.
    ModelQuad does not follow it, so the user has to be told rather than shown
    a successful install that Claude Code never sees.
    """

    environ = os.environ if environ is None else environ
    value = (environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if not value:
        return None
    home = Path.home()
    return (
        f"CLAUDE_CONFIG_DIR is set to {value}. Claude Code then reads its settings "
        f"from that folder, but ModelQuad uses {home / '.claude'} and "
        f"{home / '.claude.json'}, so Claude Code will not see ModelQuad. ModelQuad "
        "does not support CLAUDE_CONFIG_DIR yet: remove the variable, or copy the "
        "ModelQuad entries into that folder yourself."
    )


@dataclass(frozen=True)
class RegistrationVerdict:
    """What a host's recorded integration command names, and whether that is canonical.

    Shared by every surface a host can be wired with -- the MCP server entry
    and the CLI-invoking gate hook and status line alike -- because the
    question each one answers is the same question.
    """

    #: The command string as written in the host's configuration.
    command: str
    #: True when the command names a runtime explicitly (an absolute path).
    explicit: bool
    #: Where the launcher resolves right now, if it resolves at all.
    resolved: str | None
    #: True when the registration launches the ModelQuad on this PATH.
    canonical: bool
    #: Short, user-facing reason. Empty when canonical.
    reason: str


def mcp_launch_command() -> str:
    """The command Claude Code's MCP entry is registered with: the bare name."""

    return MCP_COMMAND


def cli_launch_command() -> str:
    """The CLI launcher a host integration invokes: the bare name."""

    return CLI_COMMAND


def render_launcher(launcher: str, *, shell: str = "bash-and-cmd") -> str:
    """Render one executable token, without changing the selected executable.

    Unquoted safe characters are Unicode letters, marks and numbers and
    ``_-./:+``; ``~#`` are also safe inside a token. Everything else requires
    double quotes. Dollar signs and backticks expand inside double quotes in
    bash, so the ``bash`` spelling escapes them (and any double quote).
    """

    if shell not in ("bash", "bash-and-cmd"):
        raise ValueError(f"Unsupported launcher shell: {shell}")
    spelled = launcher.replace(_WINDOWS_SEPARATOR, "/")
    if _launcher_needs_quotes(spelled):
        if shell == "bash":
            for character in '$`"':
                spelled = spelled.replace(character, _WINDOWS_SEPARATOR + character)
        return f'"{spelled}"'
    return spelled


def _launcher_needs_quotes(launcher: str) -> bool:
    return any(
        not (
            unicodedata.category(character)[0] in "LMN"
            or character in "_-./:+"
            or (index > 0 and character in "~#")
        )
        for index, character in enumerate(launcher)
    )


#: The one character a POSIX shell strips from an unquoted Windows path.
_WINDOWS_SEPARATOR = chr(92)

#: The surfaces Claude Code hands to bash: gate, status line and reminder.
_CLAUDE_BASH_ARGUMENTS = (("hook-gate",), ("usage-hook", "claude"), ("hook-pending", "claude"))


def integration_command(*arguments: str, shell: str | None = None) -> str:
    """Join our fixed subcommand literals to the launcher.

    Claude's hook, status line and reminder select bash, including callers
    that omit the shell. The launcher is spelled by ``render_launcher``; the
    arguments are product-owned literals.
    """

    if shell is None:
        shell = "bash" if arguments in _CLAUDE_BASH_ARGUMENTS else "bash-and-cmd"
    return " ".join((render_launcher(cli_launch_command(), shell=shell), *arguments))


def split_command(command: str) -> tuple[str, tuple[str, ...]]:
    """Read a recorded integration command without interpreting shell syntax.

    These are strings we write, not arbitrary shell programs. Be tolerant of
    foreign or incomplete strings: an unclosed quote consumes the remainder
    as the executable token instead of raising during host inspection.
    """

    executable, _quoted, arguments = _split_launcher(command)
    return executable, arguments


def _split_launcher(command: str) -> tuple[str, bool, tuple[str, ...]]:
    """``split_command`` plus whether the launcher token was double-quoted."""

    raw = (command or "").strip()
    if not raw:
        return "", False, ()
    if raw.startswith('"'):
        # Decode only the bash escapes we emit. Older quoted Windows paths
        # retain their separators; this is inspection, never shell evaluation.
        executable: list[str] = []
        index = 1
        while index < len(raw):
            character = raw[index]
            if character == '"':
                return "".join(executable), True, tuple(raw[index + 1:].split())
            if character == _WINDOWS_SEPARATOR and index + 1 < len(raw) and raw[index + 1] in '$`"':
                index += 1
                character = raw[index]
            executable.append(character)
            index += 1
        return "".join(executable), True, ()
    parts = raw.split()
    return parts[0], False, tuple(parts[1:])


def launcher_spelling_problem(command: str) -> str:
    """Why a recorded launcher cannot be executed by the host's shell, or "".

    Judges the *spelling* the host will hand to its shell, not the path it
    names: a correct path in a form bash strips to garbage is a dead
    integration. An unquoted launcher carrying Windows separators loses them
    in bash; unquoted punctuation and unescaped expansion characters break it
    too. A bare name has no separator to lose.
    """

    executable, quoted, arguments = _split_launcher(command)
    if not quoted and _WINDOWS_SEPARATOR in executable:
        return (
            "it is spelled with Windows separators and no quotes, which a POSIX shell "
            "strips as escapes -- Claude Code runs hooks and status lines through bash, "
            "where this exits 127 and the gate fails open"
        )
    if not quoted and _launcher_needs_quotes(executable):
        return "the launcher contains unquoted shell punctuation; repair must quote the absolute path"
    if quoted and any(character in executable for character in '$`"'):
        # Only Claude's own surfaces are known to run under bash.
        shell = "bash" if arguments in _CLAUDE_BASH_ARGUMENTS else "bash-and-cmd"
        expected = render_launcher(executable, shell=shell)
        if not command.strip().startswith(expected + " ") and command.strip() != expected:
            return "the launcher contains shell expansion characters without the required bash escaping"
    return ""


def _bare_name(raw: str) -> str:
    name = PureWindowsPath(raw).name.lower()
    return name[:-4] if name.endswith((".exe", ".cmd", ".bat")) else name


def _classify(
    command: str,
    launcher: str,
    expected: str,
    which: Callable[[str], str | None],
    *,
    what: str,
    check_spelling: bool,
) -> RegistrationVerdict:
    raw = (launcher or "").strip()
    if not raw:
        return RegistrationVerdict(
            command=command, explicit=False, resolved=None, canonical=False,
            reason=f"No launch command is recorded for the ModelQuad {what}.",
        )
    explicit = Path(raw).is_absolute()
    bare = not any(char in raw for char in ("/", "\\", ":", "\x00"))
    if bare:
        resolved = which(raw)
        if _bare_name(raw) != expected:
            return RegistrationVerdict(
                command=command, explicit=False, resolved=resolved, canonical=False,
                reason=f"The host launches {raw!r}, not ModelQuad's {expected!r} command.",
            )
        if resolved is None:
            return RegistrationVerdict(
                command=command, explicit=False, resolved=None, canonical=False,
                reason=(
                    f"The host launches {raw!r}, which is not on this PATH, so it cannot "
                    "start ModelQuad. Reinstall ModelQuad with pip, or add its Scripts "
                    "folder to PATH."
                ),
            )
        return RegistrationVerdict(command, False, resolved, True, "")
    if not explicit:
        # A relative path depends on the host's working directory; it is
        # never looked up, so a planted file cannot answer for it.
        return RegistrationVerdict(
            command=command, explicit=False, resolved=None, canonical=False,
            reason=f"The host launches {raw!r}, a relative path that does not resolve on this PATH at all.",
        )

    # An absolute path is judged structurally, never by a PATH lookup: it
    # pins one copy, whatever the user's PATH or a later upgrade says.
    problem = launcher_spelling_problem(command) if check_spelling else ""
    if problem:
        reason = f"The host records the launcher as {raw!r}, but {problem}."
    else:
        reason = (
            f"The host launches ModelQuad pinned to {raw}; ModelQuad registers the "
            f"bare name {expected!r}, so the copy installed on PATH is the one launched."
        )
    return RegistrationVerdict(
        command=command, explicit=True, resolved=raw, canonical=False, reason=reason,
    )


def classify_registration(
    command: str, *, which: Callable[[str], str | None] | None = None,
) -> RegistrationVerdict:
    """Judge a recorded host MCP command against the ModelQuad on PATH.

    ``which`` is injectable for the same reason ``doctor`` injects it: the
    real PATH is a property of the developer's machine, not of the test.
    """

    which = which if which is not None else discovery_which
    raw = (command or "").strip()
    return _classify(raw, raw, MCP_COMMAND, which, what="MCP server", check_spelling=False)


def classify_cli_registration(
    command: str, *, which: Callable[[str], str | None] | None = None,
) -> RegistrationVerdict:
    """Judge a recorded host CLI integration command against the ModelQuad on PATH.

    The verdict retains the full command while resolving only its launcher.
    """

    which = which if which is not None else discovery_which
    launcher, _arguments = split_command(command)
    return _classify(command, launcher, CLI_COMMAND, which, what="CLI integration",
                     check_spelling=True)


def classify_status_line_registration(
    command: str, *, which: Callable[[str], str | None] | None = None,
) -> RegistrationVerdict:
    """Judge a status line; the same rule as every other CLI integration."""

    return classify_cli_registration(command, which=which)


def read_registered_command(host_id: str, target_dir: Path) -> str | None:
    """The launch command a host currently has recorded for ModelQuad.

    Returns ``None`` when there is no ModelQuad registration to judge, or when
    the file cannot be read or parsed -- "absent" and "unreadable" are the
    existing ``<host> adapter installed`` / ``<host> MCP registered`` checks'
    business, and duplicating their verdicts here would double-report one
    problem. This function answers only: *given* a registration, what runtime
    does it name?
    """

    entry = read_registered_entry(host_id, target_dir)
    if entry is None:
        return None
    command = entry.get("command")
    return command if isinstance(command, str) else None


def json_mcp_config_path(host_id: str, target_dir: Path) -> Path | None:
    """The JSON file that carries this host's MCP server table, if it has one."""

    if host_id == "claude":
        return target_dir.parent / f"{target_dir.name}.json"
    return None


def registration_is_readable(host_id: str) -> bool:
    """Whether this module knows where ``host_id`` records ModelQuad at all.

    False for a host it has no reader for -- a third-party or test adapter.
    Callers need this before they may treat "no registration found" as
    evidence: for a host nothing can read, absence of a reading is not a
    reading of absence, and concluding "not registered" from it would invent a
    confident answer out of no evidence (``AGENTS.md`` invariant 4). Such a
    host's adapter report remains the only thing that can speak for it.
    """

    return host_id == "claude"


def mcp_config_parses(host_id: str, target_dir: Path) -> bool | None:
    """Whether this host's MCP config file is currently *parseable*.

    ``True`` it parses, ``False`` it exists but does not, ``None`` there is no
    file to judge (an absent config is not a broken one -- the adapter will
    create it).

    This answers a question the readers above deliberately conflate. Both
    ``read_registered_command`` and ``read_registered_entry`` return ``None``
    for "absent", "unreadable" and "not a ModelQuad entry" alike, because
    reporting a config's *health* is the adapter checks' job and a second
    opinion from there would double-report it. But once an install has
    finished with no registration in place, "the file is broken" and "the
    adapter chose not to write" are different facts owed to the user in
    different words, and this is where that one bit is decided -- structurally,
    by parsing, never by matching on an error message.
    """

    path = json_mcp_config_path(host_id, Path(target_dir))
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None if not path.exists() else False
    if not text.strip():
        # An empty file is not malformed: the adapter creates the table from
        # scratch on top of one.
        return True
    import json as _json

    try:
        _json.loads(text)
    except _json.JSONDecodeError:
        return False
    return True


def read_registered_entry(host_id: str, target_dir: Path) -> dict | None:
    """The WHOLE MCP entry a JSON host currently records for ModelQuad.

    ``read_registered_command`` answers "what runtime does this name?", which
    is all ``doctor`` needs. Restoring a registration needs more than that: an
    entry the user wrote may carry ``args``, ``env`` or a transport alongside
    ``command``, and putting back only the command string would quietly
    discard the rest. So the two readers share one interpretation of where the
    entry lives -- this module -- and differ only in how much of it they take.

    Returns ``None`` when there is no ModelQuad entry, when the host is not a
    JSON host, or when the file cannot be read or parsed. "Unreadable" and
    "absent" deliberately look the same here for the same reason they do in
    ``read_registered_command``: reporting a config's health is the adapter
    checks' job, and a second opinion from here would double-report it.
    """

    import json

    config_path = json_mcp_config_path(host_id, target_dir)
    if config_path is None:
        return None
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    entry = servers.get(MCP_SERVER_NAME) if isinstance(servers, dict) else None
    return dict(entry) if isinstance(entry, dict) else None


def _same_path(left: Path, right: Path) -> bool:
    """Compare two Windows-ish paths without requiring either to exist."""

    def normalize(path: Path) -> str:
        try:
            return str(path.resolve()).casefold()
        except OSError:
            return str(path).casefold()

    return normalize(left) == normalize(right)
