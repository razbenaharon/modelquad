# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Windows path source regressions; native shell execution belongs to a maintainer.

ModelQuad registers the bare ``modelquad`` names, so these paths reach the
renderer only when a host records a pinned launcher. Synthetic .cmd files
prove launcher strings only.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from conftest import no_window_kwargs
from orchestrator import hostruntime
from orchestrator.adapters import claude_adapter
from orchestrator.adapters.cliintegration import owns_cli_command
from orchestrator.adapters.hooksmerge import wired_commands
from orchestrator.adapters.report import ActionKind
from orchestrator.adapters.textblock import render_block, upsert_block
from orchestrator.statecontext import STATE_MODE_ENV_VAR, STATE_ROOT_ENV_VAR
from test_host_integration_runtime import _git_bash


TIMEOUT_SECONDS = 60
PATH_CASES = [
    pytest.param("with space", id="space"),
    pytest.param("O'Brien", id="apostrophe"),
    pytest.param("Dana(work)", id="parentheses"),
    pytest.param("Dana&work", id="ampersand"),
    pytest.param("100%", id="percent"),
    pytest.param("%OS%", id="percent_var"),
    pytest.param("Wow!", id="bang"),
    pytest.param("שלום", id="unicode"),
    pytest.param("price$literal", id="dollar"),
    pytest.param("pytest-of-HOST$", id="dollar_profile"),
    pytest.param("tick`literal", id="backtick"),
    pytest.param("Dana (work)", id="space_parentheses"),
    pytest.param("Dana @home", id="space_at"),
]


@pytest.fixture(autouse=True)
def isolated_host_state(monkeypatch, tmp_path):
    """Redirect state/home and fail before any write could reach a real profile.

    No real Host config is opened, even to fingerprint it. Guarding every
    Python write/replace proves that real ~/.claude* and ~/.codex
    remain untouched without reading their potentially private contents.
    """
    from orchestrator import paths
    from orchestrator.adapters import backup, file_security

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
            return fixed.replace(tzinfo=None) if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(backup, "datetime", FixedClock)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv(STATE_MODE_ENV_VAR, "installed")
    monkeypatch.setenv(STATE_ROOT_ENV_VAR, str(tmp_path / "state"))
    monkeypatch.setattr(paths, "state_root", lambda: tmp_path / "state")

    writes = []
    root = tmp_path.resolve()

    def check_write(path):
        if isinstance(path, int):
            return  # already-open fd, originating in a guarded os.open
        resolved = Path(path).resolve()
        assert resolved.is_relative_to(root), f"write outside tmp_path: {resolved}"
        writes.append(resolved)

    original_open = Path.open
    original_os_open = os.open
    original_replace = os.replace
    original_unlink = os.unlink
    original_create_exclusive = file_security.create_exclusive

    def guarded_open(path, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            check_write(path)
        return original_open(path, mode, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            check_write(path)
        return original_os_open(path, flags, *args, **kwargs)

    def guarded_replace(source, destination, *args, **kwargs):
        check_write(source)
        check_write(destination)
        return original_replace(source, destination, *args, **kwargs)

    def guarded_unlink(path, *args, **kwargs):
        check_write(path)
        return original_unlink(path, *args, **kwargs)

    def guarded_create_exclusive(path):
        check_write(path)
        return original_create_exclusive(path)

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(os, "replace", guarded_replace)
    monkeypatch.setattr(os, "unlink", guarded_unlink)
    monkeypatch.setattr(file_security, "create_exclusive", guarded_create_exclusive)
    yield
    assert all(path.is_relative_to(root) for path in writes)


@pytest.mark.skipif(sys.platform != "win32", reason="launcher execution requires native Windows shells")
@pytest.mark.parametrize("shell", ["cmd", "bash"], ids=["cmd", "git_bash"])
@pytest.mark.parametrize("folder", PATH_CASES)
@pytest.mark.parametrize("exit_code", [0, 2], ids=["success", "blocking_exit"])
def test_host_launcher_executes_literal_windows_path(tmp_path, shell, folder, exit_code):
    """Execute a synthetic shim, never a Host or an installed ModelQuad CLI."""
    probe = tmp_path / folder / "modelquad.cmd"
    probe.parent.mkdir()
    marker = tmp_path / "marker.txt"
    # The batch body contains no probe path and no non-ASCII text. It cannot
    # accidentally introduce the batch-code-page bug that the retired installer work owned.
    probe.write_bytes(
        b'@echo off\r\necho W09_PROBE %*> "%W09_MARKER%"\r\n'
        + f"exit /b {exit_code}\r\n".encode("ascii")
    )
    arguments = ("hook-gate", "literal-argument")
    environment = dict(os.environ, W09_MARKER=str(marker))
    environment.pop("BASH_ENV", None)
    rendered = hostruntime.render_launcher(str(probe), shell="bash" if shell == "bash" else "bash-and-cmd")
    command = rendered + " " + " ".join(arguments)
    assert hostruntime.split_command(command) == (probe.as_posix(), arguments)
    assert owns_cli_command(rendered + " hook-gate", ("hook-gate",))
    if shell == "bash":
        if any(character in folder for character in "&|<>()^%!@"):
            # Git Bash dispatches a .cmd through cmd.exe, which re-parses these
            # characters. The cmd handoff that worked around it served only the
            # retired installer shims; pip installs .exe launchers.
            pytest.skip("a .cmd launcher with cmd punctuation is not a shipped Claude launcher")
        bash = _git_bash()
        if bash is None:
            pytest.skip("Git Bash unavailable; cmd cases still run independently")
        invocation = [bash, "--noprofile", "--norc", "-c", command]
    else:
        if probe.as_posix().count("%") >= 2:
            pytest.skip("cmd expands paired percent signs; the unknown-shell form cannot protect them")
        cmd_exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
        invocation = f'"{cmd_exe}" /d /v:off /s /c "{command}"'
    result = subprocess.run(
        invocation, shell=False, env=environment, capture_output=True,
        timeout=TIMEOUT_SECONDS,
        **no_window_kwargs(),
    )
    assert result.returncode == exit_code, (result.stdout, result.stderr)
    assert marker.read_bytes().strip() == b"W09_PROBE hook-gate literal-argument"


@pytest.mark.parametrize("kind", ["markdown", "toml"], ids=["markdown", "toml"])
@pytest.mark.parametrize("tag", ["modelquad"], ids=["current"])
def test_malformed_markers_still_refuse(tmp_path, kind, tag):
    options = {"comment_prefix": "<!--", "marker_suffix": "-->"} if kind == "markdown" else {}
    broken = render_block("content", tag=tag, **options).splitlines()[0] + "\nOwner bytes.\n"
    with pytest.raises(ValueError, match="missing or duplicated"):
        upsert_block(broken, "content", **options)


def _adapter_install(host, tmp_path):
    target = tmp_path / host
    adapter = claude_adapter.ClaudeAdapter()
    options = {}
    settings = target / "settings.json"
    mcp = target.with_suffix(".json")
    return adapter, target, options, settings, mcp


@pytest.mark.parametrize("folder", PATH_CASES)
@pytest.mark.parametrize("arguments", [
    pytest.param(("hook-gate",), id="claude_gate"),
    pytest.param(("usage-hook", "claude"), id="claude_status"),
])
def test_installer_rendering_is_canonical(monkeypatch, tmp_path, folder, arguments):
    """Classify the exact Host-specific string, including any temp ancestors.

    Claude requires Bash escaping. It should not draw a repeated repair
    finding for the command its own install just emitted.
    """
    shim = tmp_path / folder / "modelquad.cmd"
    monkeypatch.setattr(hostruntime, "cli_launch_command", lambda: str(shim))
    rendered = hostruntime.render_launcher(str(shim), shell="bash")
    command = rendered + " " + " ".join(arguments)
    assert command == hostruntime.integration_command(*arguments)
    assert hostruntime.split_command(command) == (shim.as_posix(), arguments)
    verdict = hostruntime.classify_cli_registration(command, which=lambda _: str(shim))
    # Correctly spelled for bash, so the only finding is that it is pinned.
    assert verdict.explicit and "pinned" in verdict.reason, verdict.reason


@pytest.mark.parametrize("host", ["claude"], ids=["claude"])
@pytest.mark.parametrize("folder", PATH_CASES)
def test_adapter_install_repair_and_uninstall(monkeypatch, tmp_path, host, folder):
    """A pinned launcher in any folder is ours, is replaced by the bare name, and is removed."""
    shim = tmp_path / folder / "modelquad.cmd"
    shim.parent.mkdir()
    shim.touch()
    which = lambda _: str(shim)
    adapter, target, options, settings, mcp = _adapter_install(host, tmp_path)
    settings.parent.mkdir(parents=True, exist_ok=True)
    args = ("usage-hook", host)
    old_launcher = f'"{shim.as_posix()}"' if any(character.isspace() for character in str(shim)) else shim.as_posix()
    old = old_launcher + " " + " ".join(args)
    settings.write_text(json.dumps({"owner": "preserve", "statusLine": {"command": old}}), encoding="utf-8")
    assert owns_cli_command(old, args)
    first = adapter.install(target, **options)
    assert not any(action.kind is ActionKind.SKIPPED for action in first.actions)
    assert hostruntime.read_registered_command(host, target) == "modelquad-mcp"
    payload = json.loads(settings.read_text(encoding="utf-8"))
    command = payload["statusLine"]["command"]
    assert command == hostruntime.integration_command("usage-hook", "claude") == "modelquad usage-hook claude"
    assert hostruntime.classify_cli_registration(command, which=which).canonical
    assert payload["owner"] == "preserve"
    # The advisory gate is no longer installed.
    assert wired_commands(settings, "PreToolUse") == ()
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    second = adapter.install(target, **options)
    assert not any(action.kind is ActionKind.UPDATED for action in second.actions)
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    adapter.uninstall(target, **options)
    payload = json.loads(settings.read_text(encoding="utf-8"))
    assert "statusLine" not in payload
    assert payload["owner"] == "preserve"
    assert not wired_commands(settings, "PreToolUse")


# Literal fixtures captured from the base's whitespace-only renderer. These
# expected bytes do not call the renderer or derive from the new implementation.
@pytest.mark.parametrize("cli,mcp,hook,status,toml_line,json_line", [
    pytest.param(
        r"C:\Users\example\AppData\Local\MultiAgentOrchestrator\bin\modelquad.cmd",
        r"C:\Users\example\AppData\Local\MultiAgentOrchestrator\bin\modelquad-mcp.cmd",
        b'C:/Users/example/AppData/Local/MultiAgentOrchestrator/bin/modelquad.cmd hook-gate',
        b'{"command": "C:/Users/example/AppData/Local/MultiAgentOrchestrator/bin/modelquad.cmd usage-hook claude", "type": "command"}',
        b"command = 'C:\\Users\\example\\AppData\\Local\\MultiAgentOrchestrator\\bin\\modelquad-mcp.cmd'",
        b'"command": "C:\\\\Users\\\\example\\\\AppData\\\\Local\\\\MultiAgentOrchestrator\\\\bin\\\\modelquad-mcp.cmd"',
        id="owner_plain",
    ),
    pytest.param(
        r"C:\Users\Example User\bin\modelquad.cmd", r"C:\Users\Example User\bin\modelquad-mcp.cmd",
        b'"C:/Users/Example User/bin/modelquad.cmd" hook-gate',
        b'{"command": "\\"C:/Users/Example User/bin/modelquad.cmd\\" usage-hook claude", "type": "command"}',
        b"command = 'C:\\Users\\Example User\\bin\\modelquad-mcp.cmd'",
        b'"command": "C:\\\\Users\\\\Example User\\\\bin\\\\modelquad-mcp.cmd"',
        id="space",
    ),
    pytest.param(
        "C:\\Users\\שלום\\bin\\modelquad.cmd", "C:\\Users\\שלום\\bin\\modelquad-mcp.cmd",
        'C:/Users/שלום/bin/modelquad.cmd hook-gate'.encode("utf-8"),
        b'{"command": "C:/Users/\\u05e9\\u05dc\\u05d5\\u05dd/bin/modelquad.cmd usage-hook claude", "type": "command"}',
        "command = 'C:\\Users\\שלום\\bin\\modelquad-mcp.cmd'".encode("utf-8"),
        # The host's own file keeps non-ASCII characters as written.
        '"command": "C:\\\\Users\\\\שלום\\\\bin\\\\modelquad-mcp.cmd"'.encode("utf-8"),
        id="unicode_letters",
    ),
])
@pytest.mark.parametrize("host", ["claude"], ids=["claude"])
def test_plain_path_output_is_unchanged(monkeypatch, tmp_path, host, cli, mcp, hook, status, toml_line, json_line):
    monkeypatch.setattr(hostruntime, "cli_launch_command", lambda: cli)
    monkeypatch.setattr(hostruntime, "mcp_launch_command", lambda: mcp)
    adapter, target, options, settings, mcp_path = _adapter_install(host, tmp_path)
    report = adapter.install(target, **options)
    assert not any(action.kind is ActionKind.SKIPPED for action in report.actions)
    if host == "codex":
        assert toml_line in mcp_path.read_bytes().splitlines()
    else:
        assert json_line in [line.strip().rstrip(b",") for line in mcp_path.read_bytes().splitlines()]
        payload = json.loads(settings.read_text(encoding="utf-8"))
        if host == "claude":
            # The gate is no longer installed; its spelling rule still holds.
            assert wired_commands(settings, "PreToolUse") == ()
            assert hostruntime.integration_command("hook-gate").encode("utf-8") == hook
            assert json.dumps(payload["statusLine"], sort_keys=True).encode("utf-8") == status


@pytest.mark.parametrize("command", [
    pytest.param("modelquad hook-gate", id="legacy_bare"),
    pytest.param(r'C:\Users\example\bin\modelquad.cmd hook-gate', id="legacy_backslash"),
    pytest.param(r'"C:\Users\Example User\bin\modelquad.cmd" hook-gate', id="legacy_quoted"),
    pytest.param("C:/Users/O'Brien/bin/modelquad.cmd hook-gate", id="legacy_apostrophe"),
])
def test_previously_shipped_launcher_ownership(command):
    assert owns_cli_command(command, ("hook-gate",))


@pytest.mark.parametrize("folder,expected", [
    pytest.param("pytest-of-HOST$", '"C:/Users/pytest-of-HOST$/bin/modelquad.cmd"', id="dollar_profile"),
    pytest.param("tick`literal", '"C:/Users/tick`literal/bin/modelquad.cmd"', id="backtick"),
    pytest.param("%OS%", '"C:/Users/%OS%/bin/modelquad.cmd"', id="percent_var"),
    pytest.param("100%", '"C:/Users/100%/bin/modelquad.cmd"', id="percent"),
    pytest.param("Wow!", '"C:/Users/Wow!/bin/modelquad.cmd"', id="bang"),
    pytest.param("Dana&work", '"C:/Users/Dana&work/bin/modelquad.cmd"', id="ampersand"),
    pytest.param("O'Brien", '"C:/Users/O\'Brien/bin/modelquad.cmd"', id="apostrophe"),
    pytest.param("Dana (work)", '"C:/Users/Dana (work)/bin/modelquad.cmd"', id="space_parentheses"),
    pytest.param("Example User", '"C:/Users/Example User/bin/modelquad.cmd"', id="space"),
    pytest.param("שלום", "C:/Users/שלום/bin/modelquad.cmd", id="unicode"),
])
def test_unknown_shell_character_classes_keep_absolute_binding(folder, expected):
    launcher = f"C:/Users/{folder}/bin/modelquad.cmd"
    assert hostruntime.render_launcher(launcher) == expected
    assert hostruntime.split_command(expected + " zeta-hook") == (launcher, ("zeta-hook",))


def test_unknown_shell_keeps_quoted_path_but_claude_preserves_literal_path(tmp_path):
    for folder in ("$(echo injected)", "`echo injected`", "%PATH%", "100%", "Wow!", "Dana&work", "Dana @home"):
        shim = tmp_path / folder / "modelquad.cmd"
        compatible = hostruntime.render_launcher(str(shim))
        assert compatible == f'"{shim.as_posix()}"'
        assert hostruntime.split_command(compatible + " zeta-hook") == (shim.as_posix(), ("zeta-hook",))
        assert not hostruntime.launcher_spelling_problem(compatible + " zeta-hook")
        rendered = hostruntime.render_launcher(str(shim), shell="bash")
        assert hostruntime.split_command(rendered + " hook-gate") == (shim.as_posix(), ("hook-gate",))
        assert owns_cli_command(rendered + " hook-gate", ("hook-gate",))
        assert not hostruntime.launcher_spelling_problem(rendered + " hook-gate")


@pytest.mark.parametrize("folder", [
    pytest.param("राज", id="devanagari_marks"),
    pytest.param("שָׁלוֹם", id="hebrew_marks"),
    pytest.param("ไทยี", id="thai_marks"),
    pytest.param("name~#plus+", id="safe_punctuation"),
])
@pytest.mark.parametrize("shell", ["bash", "bash-and-cmd"], ids=["claude", "unknown_shell"])
def test_safe_unicode_and_internal_punctuation_keep_original_spelling(folder, shell):
    launcher = f"C:/Users/{folder}/bin/modelquad.cmd"
    assert hostruntime.render_launcher(launcher, shell=shell) == launcher
    assert not hostruntime.launcher_spelling_problem(launcher + " usage-hook claude")
    assert not hostruntime.launcher_spelling_problem(launcher + " zeta-hook")
