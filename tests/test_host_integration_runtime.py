# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Host integrations launch the ModelQuad on PATH by its bare command names."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import modelquad.paths as paths_module
from modelquad import hostruntime
from modelquad.adapters.claude_adapter import ClaudeAdapter
from modelquad.adapters.hooksmerge import wired_commands
from modelquad.adapters.report import ActionKind
from modelquad.statecontext import STATE_MODE_ENV_VAR, STATE_ROOT_ENV_VAR

from conftest import no_window_kwargs

WINDOWS_SEPARATOR = chr(92)


@pytest.fixture(autouse=True)
def isolated_mode(monkeypatch):
    """An inherited state override must not decide which installation is tested."""
    monkeypatch.delenv(STATE_MODE_ENV_VAR, raising=False)
    monkeypatch.delenv(STATE_ROOT_ENV_VAR, raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def force_installed(monkeypatch, local_appdata: Path) -> None:
    monkeypatch.setattr(paths_module, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))


def scripts_which(tmp_path):
    """A PATH on which pip put both console commands in one Scripts folder."""
    scripts = tmp_path / "Python311" / "Scripts"
    return lambda name: str(scripts / f"{name}.exe")


def write_settings(home, payload):
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(payload), encoding="utf-8")


def read_settings(home):
    return json.loads((home / "settings.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("mode", ["installed", "development"])
def test_every_integration_uses_the_bare_command_names(monkeypatch, tmp_path, mode):
    """A pip install has no shim folder: the console commands on PATH are the runtime."""
    if mode == "installed":
        force_installed(monkeypatch, tmp_path / "Local")
    else:
        monkeypatch.setattr(paths_module, "is_dev_checkout", lambda: True)
    assert hostruntime.cli_launch_command() == "modelquad"
    assert hostruntime.mcp_launch_command() == "modelquad-mcp"
    assert hostruntime.integration_command("hook-gate") == "modelquad hook-gate"
    assert not hasattr(hostruntime, "installed_mcp_shim")
    assert not hasattr(hostruntime, "installed_cli_shim")
    home = tmp_path / ".claude"
    ClaudeAdapter(via_plugin=False).install(home)
    # Neither the advisory gate nor a status line is installed: settings.json is not even created.
    assert wired_commands(home / "settings.json", "PreToolUse") == ()
    assert not (home / "settings.json").exists()
    mcp = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["modelquad"]["command"] == "modelquad-mcp"


def test_installed_adapter_and_idempotence(tmp_path):
    """Reinstalling must not rewrite any Claude surface."""
    home = tmp_path / ".claude"
    adapter = ClaudeAdapter(via_plugin=False)
    adapter.install(home)
    assert adapter.is_installed(home)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    report = adapter.install(home)
    assert all(action.kind == ActionKind.UNCHANGED for action in report.actions)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("old", [
    "modelquad hook-gate",
    '"C:\\old runtime\\MODELQUAD.EXE" hook-gate',
    "C:/old/modelquad.bat hook-gate",
])
def test_upgrade_removes_the_owned_gate_and_keeps_everything_else(tmp_path, old):
    """The gate is no longer installed: an upgrade removes an owned one and keeps user hooks."""
    home = tmp_path / ".claude"
    sibling = {"type": "command", "command": "python my_modelquad_helper.py", "timeout": 17}
    user = {"matcher": "Read", "hooks": [{"type": "command", "command": "echo user"}]}
    write_settings(home, {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": old}, sibling]}, user,
        {"matcher": "Edit", "hooks": [{"type": "command", "command": "modelquad hook-gate extra"}]},
    ]}})
    adapter = ClaudeAdapter()
    adapter.install(home)
    commands = wired_commands(home / "settings.json", "PreToolUse")
    assert not [c for c in commands if hostruntime.split_command(c)[1] == ("hook-gate",)]
    groups = read_settings(home)["hooks"]["PreToolUse"]
    assert user in groups
    assert commands == ("python my_modelquad_helper.py", "echo user", "modelquad hook-gate extra")
    adapter.uninstall(home)
    assert wired_commands(home / "settings.json", "PreToolUse") == (
        "python my_modelquad_helper.py", "echo user", "modelquad hook-gate extra",
    )


def test_another_programs_gate_is_never_treated_as_ours(tmp_path):
    """Only ModelQuad's own CLI name is ours; a same-named subcommand elsewhere is not."""
    home = tmp_path / ".claude"
    foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-cli hook-gate"}]}
    write_settings(home, {"hooks": {"PreToolUse": [foreign]}})
    ClaudeAdapter().install(home)
    ClaudeAdapter().uninstall(home)
    assert read_settings(home)["hooks"]["PreToolUse"] == [foreign]


@pytest.mark.parametrize("command,owned", [
    ("python my_modelquad_helper.py", False),
    ("modelquad usage-hook other", False),
    ('"C:\\old runtime\\other-cli.cmd" usage-hook claude', False),
    ("modelquad usage-hook claude", True),
    ('"C:\\old runtime\\modelquad.cmd" usage-hook claude', True),
])
def test_uninstall_status_ownership(tmp_path, command, owned):
    """Uninstall removes our whole status line, but preserves a user's replacement."""
    home = tmp_path / ".claude"
    write_settings(home, {"statusLine": {"command": command, "type": "command"}})
    ClaudeAdapter().uninstall(home)
    settings = read_settings(home)
    if owned:
        assert "statusLine" not in settings
    else:
        assert settings["statusLine"] == {"command": command, "type": "command"}


def test_uninstall_keeps_a_status_line_key_the_user_added(tmp_path):
    """Only the keys ModelQuad writes are ours; a user's padding keeps the object."""
    home = tmp_path / ".claude"
    write_settings(home, {"statusLine": {"type": "command", "command": "modelquad usage-hook claude",
                                         "padding": 2}})
    ClaudeAdapter().uninstall(home)
    assert read_settings(home)["statusLine"] == {"type": "command", "padding": 2}


def test_install_then_uninstall_leaves_no_status_line_or_empty_env(tmp_path):
    """A fresh install is undone exactly: no typeless statusLine, no env at all."""
    home = tmp_path / ".claude"
    write_settings(home, {"theme": "dark"})
    ClaudeAdapter(via_plugin=False).install(home)
    assert read_settings(home) == {"theme": "dark"}
    ClaudeAdapter(via_plugin=False).uninstall(home)
    settings = read_settings(home)
    assert "statusLine" not in settings
    assert "env" not in settings
    assert settings == {"theme": "dark"}


@pytest.mark.parametrize("command,expected", [
    ("", ("", ())), ("  \t", ("", ())),
    ('"unfinished path', ("unfinished path", ())),
    (" modelquad   hook-gate ", ("modelquad", ("hook-gate",))),
])
def test_tolerant_split(command, expected):
    """Foreign or incomplete configuration must not crash host inspection."""
    assert hostruntime.split_command(command) == expected


def test_bare_registration_is_canonical_when_it_resolves(tmp_path):
    """The normal pip install: the bare name resolves, so nothing is a finding."""
    which = scripts_which(tmp_path)
    for verdict in (
        hostruntime.classify_cli_registration("modelquad hook-gate", which=which),
        hostruntime.classify_registration("modelquad-mcp", which=which),
    ):
        assert verdict.canonical and not verdict.explicit and not verdict.reason
    missing = hostruntime.classify_cli_registration("modelquad hook-gate", which=lambda _: None)
    assert not missing.canonical and "not on this PATH" in missing.reason


def test_an_absolute_registration_is_pinned_and_judged_without_a_lookup(tmp_path):
    """A pinned path is reported, structurally: no PATH lookup may answer for it."""

    def no_lookup(_name):
        pytest.fail("an absolute registration must not consult PATH")

    pinned = tmp_path / "Python311" / "Scripts" / "modelquad.exe"
    verdict = hostruntime.classify_cli_registration(f'"{pinned.as_posix()}" hook-gate', which=no_lookup)
    assert not verdict.canonical and verdict.explicit
    assert "pinned" in verdict.reason and "'modelquad'" in verdict.reason
    mcp = hostruntime.classify_registration(str(pinned.with_name("modelquad-mcp.exe")), which=no_lookup)
    assert not mcp.canonical and mcp.explicit
    blank = hostruntime.classify_cli_registration("   ", which=no_lookup)
    assert not blank.canonical and blank.command == "   "
    assert "No launch command" in blank.reason


@pytest.mark.parametrize("payload", ["{", "[]", '{"statusLine": []}', '{"statusLine": {"command": 7}}'])
def test_status_reader_tolerates_foreign_shapes(tmp_path, payload):
    """Malformed host settings must not crash the reader or be rewritten by the removal."""
    from modelquad.adapters.cliintegration import read_status_line_command, remove_owned_status_line

    settings = tmp_path / "settings.json"
    assert read_status_line_command(settings) is None
    settings.write_text(payload, encoding="utf-8")
    before = settings.read_bytes()
    assert read_status_line_command(settings) is None
    remove_owned_status_line(settings, ("usage-hook", "claude"), dry_run=False)
    assert settings.read_bytes() == before


@pytest.mark.parametrize("recorded", [
    None, "owned", "python my_status.py", "modelquad usage-hook other", "other-cli usage-hook claude",
])
@pytest.mark.parametrize("via_plugin", [False, True], ids=["install", "plugin-install"])
def test_install_status_line_ownership(tmp_path, recorded, via_plugin):
    """Install writes no status line: it removes the one an earlier build wrote and keeps a user's."""

    home = tmp_path / "claude"
    command = '"C:/old/modelquad.exe" usage-hook claude' if recorded == "owned" else recorded
    original = {"type": "command", **({"command": command} if command else {})}
    write_settings(home, {"statusLine": original})
    report = ClaudeAdapter(via_plugin=via_plugin).install(home)
    action = report.actions[2]
    assert action.path == home / "settings.json"
    if recorded == "owned":
        # Only the keys ModelQuad wrote were there, so the whole object goes.
        assert "statusLine" not in read_settings(home)
        assert action.kind is ActionKind.REMOVED
    else:
        assert read_settings(home)["statusLine"] == original
        assert action.kind is ActionKind.UNCHANGED


@pytest.mark.parametrize(
    ("recorded", "ours"),
    [
        ("modelquad usage-hook claude", True),
        (r'"C:\Users\example\Scripts\modelquad.exe" usage-hook claude', True),
        (r'python "C:\Users\example\.claude\statusline.py"', False),
        (r"python C:\Users\example\.claude\router\usage_agy.py", False),
        ("other-cli usage-hook claude", False),
        ("modelquad usage-hook other", False),
        ("starship prompt", False),
    ],
)
def test_status_line_ownership_is_exactly_our_own_command(recorded, ours):
    """Only ``modelquad usage-hook claude`` is ours; every other status line is the user's."""

    from modelquad.adapters.cliintegration import owns_status_line_command

    assert owns_status_line_command(recorded, ("usage-hook", "claude")) is ours


# --------------------------------------------------------------------------
# A pinned launcher must be executable by the host's shell
# --------------------------------------------------------------------------


def _fake_exe(tmp_path, root_name):
    executable = tmp_path / root_name / "Scripts" / "modelquad.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    return executable


def _bash_words(command):
    """Ask Git Bash to remove quotes without executing the launcher.

    Bash removes a backslash before $, `, double quote or backslash inside
    double quotes; Python's POSIX shlex leaves the first two escaped.
    """
    bash = _git_bash()
    if bash is None:
        pytest.skip("Git Bash unavailable; cannot verify Bash quote removal")
    environment = dict(os.environ)
    environment.pop("BASH_ENV", None)
    result = subprocess.run(
        [bash, "--noprofile", "--norc", "-c", 'set -- ' + command + '; printf \'%s\\0\' "$@"'],
        shell=False, env=environment, capture_output=True, timeout=60,
        **no_window_kwargs(),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.endswith(b"\0")
    return result.stdout[:-1].decode("utf-8").split("\0")


@pytest.mark.skipif(sys.platform != "win32", reason="the host semantics under test are Git Bash on Windows")
@pytest.mark.parametrize("arguments", [("hook-gate",), ("usage-hook", "claude")])
@pytest.mark.parametrize("root_name", ["Local", "Local With Space"])
def test_rendered_launcher_survives_posix_word_splitting(monkeypatch, tmp_path, root_name, arguments):
    """What bash hands the OS must be the pinned path, separators and all.

    Claude Code runs hooks and status lines through bash on Windows. An
    unquoted Windows path is stripped of every backslash and exits 127.
    """
    executable = _fake_exe(tmp_path, root_name)
    monkeypatch.setattr(hostruntime, "cli_launch_command", lambda: str(executable))
    command = hostruntime.integration_command(*arguments, shell="bash")

    words = _bash_words(command)
    assert words == [executable.as_posix(), *arguments]
    assert hostruntime._same_path(Path(words[0]), executable)
    assert WINDOWS_SEPARATOR not in words[0]
    assert hostruntime.split_command(command) == (executable.as_posix(), tuple(arguments))

    # The old spelling, under the same semantics, is the observed failure.
    old_launcher = r"C:\ModelQuad\bin\modelquad.cmd"
    stripped = _bash_words(" ".join((old_launcher, *arguments)))[0]
    assert WINDOWS_SEPARATOR not in stripped and stripped != old_launcher
    assert stripped == old_launcher.replace(WINDOWS_SEPARATOR, "")


@pytest.mark.parametrize("launcher,expected", [
    ("modelquad", "modelquad"),
    ("C:" + WINDOWS_SEPARATOR + "a" + WINDOWS_SEPARATOR + "modelquad.cmd", "C:/a/modelquad.cmd"),
    ("C:" + WINDOWS_SEPARATOR + "a b" + WINDOWS_SEPARATOR + "modelquad.cmd", '"C:/a b/modelquad.cmd"'),
    ("/opt/modelquad/bin/modelquad", "/opt/modelquad/bin/modelquad"),
])
def test_render_launcher_spellings(launcher, expected):
    """POSIX separators always; quotes only where whitespace makes them necessary."""
    assert hostruntime.render_launcher(launcher) == expected


@pytest.mark.parametrize("arguments", [("hook-gate",), ("usage-hook", "claude")])
def test_unexecutable_spelling_of_the_right_path_is_a_finding(tmp_path, arguments):
    """A correct path bash cannot execute is not canonical."""
    executable = _fake_exe(tmp_path, "Local")
    which = lambda _: str(executable)
    old = " ".join((str(executable), *arguments))
    verdict = hostruntime.classify_cli_registration(old, which=which)
    assert not verdict.canonical
    if not any(character.isspace() for character in str(executable)):
        assert "Windows separators" in verdict.reason and "bash" in verdict.reason
        assert "fails open" in verdict.reason
    good = hostruntime.render_launcher(str(executable), shell="bash") + " " + " ".join(arguments)
    spelled = hostruntime.classify_cli_registration(good, which=which)
    assert not spelled.canonical and "pinned" in spelled.reason, "a correct spelling is only pinned"


def _git_bash() -> str | None:
    """The bash Claude Code hands a hook to: Git for Windows' bash, never WSL's stub.

    `shutil.which("bash")` answers `C:\\Windows\\System32\\bash.exe` on a PATH
    that carries `Git\\cmd` but not `Git\\usr\\bin` -- a service account's
    machine PATH, for one -- and that stub only
    says WSL_DISTRO_NOT_FOUND. Git for Windows ships bash under its own
    install root, which `git.exe` locates: walk up from it to a `usr/bin/bash.exe`
    or `bin/bash.exe`. None found means the host really has no Git Bash.
    """
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows")).resolve()
    found = shutil.which("bash")
    if found:
        try:
            Path(found).resolve().relative_to(system_root)
        except ValueError:
            return found  # not under the Windows directory: not the WSL stub
    git = shutil.which("git")
    if git:
        for parent in Path(git).resolve().parents:
            for candidate in (parent / "usr" / "bin" / "bash.exe", parent / "bin" / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return None


@pytest.mark.skipif(sys.platform != "win32", reason="the host semantics under test are Windows shells")
@pytest.mark.parametrize("root_name", ["plain", "with space"])
@pytest.mark.parametrize("shell", ["bash", "cmd"], ids=["git_bash", "cmd"])
def test_rendered_launcher_executes_under_the_real_host_shells(tmp_path, root_name, shell):
    """Run the renderer's appropriate form through each native Windows shell.

    A `.cmd` in a directory with (and without) a space stands in for a pinned
    launcher. Claude's Bash form and the default cmd-compatible form have
    different dollar escaping rules.
    """
    probe = tmp_path / root_name / "modelquad.cmd"
    probe.parent.mkdir(parents=True)
    probe.write_bytes(b"@echo off\r\necho PROBE_OK %*\r\n")

    rendered = hostruntime.render_launcher(str(probe), shell="bash" if shell == "bash" else "bash-and-cmd") + " hook-gate"
    environment = dict(os.environ)
    environment.pop("BASH_ENV", None)
    if shell == "bash":
        bash = _git_bash()
        if bash is None:
            pytest.skip("Git Bash unavailable; cmd cases still run independently")
        invocation = [bash, "--noprofile", "--norc", "-c", rendered]
    else:
        if probe.as_posix().count("%") >= 2:
            pytest.skip("cmd expands paired percent signs; the default form cannot protect them")
        cmd_exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
        invocation = f'"{cmd_exe}" /d /v:off /s /c "{rendered}"'
    result = subprocess.run(
        invocation, shell=False, env=environment, capture_output=True, text=True, timeout=60,
        **no_window_kwargs(),
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "PROBE_OK hook-gate"


@pytest.mark.skipif(sys.platform != "win32", reason="Git Bash resolution is a Windows concern")
def test_the_host_shell_resolver_never_returns_the_wsl_stub(monkeypatch, tmp_path):
    """A PATH whose only `bash` is Windows' WSL stub must not be mistaken for
    a host shell; Git Bash is found from `git.exe` instead, or the test skips."""
    system_root = tmp_path / "Windows"
    stub = system_root / "System32" / "bash.exe"
    stub.parent.mkdir(parents=True)
    stub.write_bytes(b"")
    git_root = tmp_path / "Git"
    git_exe = git_root / "cmd" / "git.exe"
    git_exe.parent.mkdir(parents=True)
    git_exe.write_bytes(b"")
    real_bash = git_root / "usr" / "bin" / "bash.exe"
    real_bash.parent.mkdir(parents=True)
    real_bash.write_bytes(b"")
    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.setattr(shutil, "which", lambda name: {"bash": str(stub), "git": str(git_exe)}.get(name))
    assert Path(_git_bash()) == real_bash
    monkeypatch.setattr(shutil, "which", lambda name: {"bash": str(stub)}.get(name))
    assert _git_bash() is None
