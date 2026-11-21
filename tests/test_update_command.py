# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""`modelquad update`: one user-started step, refused while Claude Code runs.

Every outside effect is a stand-in: no test here starts pip, git or claude,
touches the network or reads the real Claude Code configuration.
"""

import json
import sys
from pathlib import Path

import pytest

from modelquad import netpolicy, updater
from modelquad.updater import Install, Result, Tools

PY = r"C:\venv\Scripts\python.exe"
PLUGIN_LIST = "Installed plugins:\n  modelquad@modelquad  Version: 0.4.1\n"


class Fake:
    def __init__(self, install, running=(), claude="C:/bin/claude.exe", listing=PLUGIN_LIST, fail=None):
        self.calls: list[tuple[list[str], Path | None]] = []
        self.lines: list[str] = []
        self._install = install
        self._running = running
        self._claude = claude
        self._listing = listing
        self._fail = fail or {}

    def run(self, argv, cwd=None):
        self.calls.append((list(argv), cwd))
        key = " ".join(argv[1:3]) if argv[0] == PY else " ".join(argv[:3])
        if key in self._fail:
            return Result(self._fail[key], "it failed")
        if argv[:3] == ["claude", "plugin", "list"]:
            return Result(0, self._listing)
        if argv[0] == PY and "__version__" in argv[-1]:
            return Result(0, "0.5.0\n")
        return Result(0, "done\n")

    def tools(self):
        return Tools(running=lambda: None if self._running is None else list(self._running),
                     run=self.run, which=lambda name: self._claude if name == "claude" else None,
                     install=lambda: self._install, python=PY, out=self.lines.append)

    @property
    def text(self):
        return "\n".join(self.lines)

    @property
    def argvs(self):
        return [argv for argv, _ in self.calls]


DOCTOR = [PY, "-c", "from modelquad.cli_entry import main; raise SystemExit(main(['doctor']))"]
VERSION = [PY, "-c", "import modelquad; print(modelquad.__version__)"]
PLUGIN = [["claude", "plugin", "marketplace", "update", "modelquad"],
          ["claude", "plugin", "uninstall", "modelquad@modelquad"],
          ["claude", "plugin", "install", "modelquad@modelquad"]]


@pytest.mark.parametrize("running", [["claude.exe"], ["Claude.exe"], ["modelquad-mcp.exe"],
                                     ["Claude.exe", "claude.exe", "modelquad-mcp.exe"]])
def test_refused_while_claude_code_runs(running):
    fake = Fake(Install(updater.PYPI), running=running)
    assert updater.update(tools=fake.tools()) == 1
    assert fake.calls == []
    assert fake.lines[0].startswith("Not updated: close these first")
    for name in running:
        assert f"  {name}" in fake.lines


def test_refused_when_it_cannot_tell_what_runs():
    fake = Fake(Install(updater.PYPI), running=None)
    assert updater.update(tools=fake.tools()) == 1
    assert fake.calls == [] and "could not be checked" in fake.text
    assert "pip install --upgrade modelquad" in fake.text


def test_pypi_install_upgrades_with_the_same_interpreter_then_refreshes_the_plugin():
    fake = Fake(Install(updater.PYPI))
    assert updater.update(tools=fake.tools()) == 0
    assert fake.argvs == [[PY, "-m", "pip", "install", "--upgrade", "modelquad"],
                          ["claude", "plugin", "list"], *PLUGIN, DOCTOR, VERSION]
    # Said before anything starts: pip goes online, not ModelQuad.
    first_command = fake.lines.index("> " + " ".join(fake.argvs[0]))
    assert any("connect to the internet now; ModelQuad itself does not" in line
               for line in fake.lines[:first_command])
    assert fake.lines[-1] == "ModelQuad is now version 0.5.0. Start Claude Code again to use it."


@pytest.mark.parametrize("kind, pip", [(updater.CLONE, ["."]), (updater.EDITABLE, ["-e", "."])])
def test_clone_install_pulls_then_installs_in_the_checkout(tmp_path, kind, pip):
    fake = Fake(Install(kind, tmp_path))
    assert updater.update(tools=fake.tools()) == 0
    assert fake.calls[:2] == [(["git", "pull", "--ff-only"], tmp_path),
                              ([PY, "-m", "pip", "install", *pip], tmp_path)]
    assert "pip and git connect to the internet now" in fake.text


def test_unknown_install_runs_nothing_and_prints_both_ways():
    fake = Fake(Install(updater.UNKNOWN, why="it was installed from a file or a link"))
    assert updater.update(tools=fake.tools()) == 1
    assert fake.calls == []
    assert "cannot tell how it was installed (it was installed from a file or a link)" in fake.text
    assert "From PyPI:" in fake.text and "From a clone:" in fake.text
    for argv in PLUGIN:
        assert " ".join(argv) in fake.text


def test_a_failed_pull_installs_nothing():
    fake = Fake(Install(updater.CLONE, Path("C:/clone")), fail={"git pull --ff-only": 1})
    assert updater.update(tools=fake.tools()) == 1
    assert fake.argvs == [["git", "pull", "--ff-only"]]
    assert "Nothing was installed" in fake.text


def test_a_failed_pip_stops_before_the_plugin():
    fake = Fake(Install(updater.PYPI), fail={"-m pip": 1})
    assert updater.update(tools=fake.tools()) == 1
    assert len(fake.calls) == 1 and "Claude Code closed" in fake.text


def test_plugin_not_installed_is_left_alone():
    fake = Fake(Install(updater.PYPI), listing="No plugins installed.\n")
    assert updater.update(tools=fake.tools()) == 0
    assert not any(argv in fake.argvs for argv in PLUGIN)
    assert "plugin is not installed" in fake.text


def test_without_claude_on_path_the_plugin_steps_are_printed():
    fake = Fake(Install(updater.PYPI), claude=None)
    assert updater.update(tools=fake.tools()) == 0
    assert not any(argv[0] == "claude" for argv in fake.argvs)
    for argv in PLUGIN:
        assert "  " + " ".join(argv) in fake.lines


def test_a_failed_plugin_step_names_the_rest():
    fake = Fake(Install(updater.PYPI), fail={"claude plugin uninstall": 1})
    assert updater.update(tools=fake.tools()) == 1
    assert PLUGIN[2] not in fake.argvs
    assert "  claude plugin install modelquad@modelquad" in fake.lines


@pytest.mark.parametrize("running", [[], ["claude.exe"], None])
def test_dry_run_runs_nothing(running):
    fake = Fake(Install(updater.PYPI), running=running)
    assert updater.update(dry_run=True, tools=fake.tools()) == 0
    assert fake.calls == []
    assert "nothing is run now" in fake.lines[0]
    assert "pip install --upgrade modelquad" in fake.text


def test_the_cli_passes_dry_run(monkeypatch):
    from modelquad import cli

    seen = []
    monkeypatch.setattr(updater, "update", lambda dry_run=False: seen.append(dry_run) or 0)
    assert cli.main(["update", "--dry-run"]) == 0
    assert cli.main(["update"]) == 0
    assert seen == [True, False]


def test_the_real_tools_in_a_dry_run_start_no_process(monkeypatch, capsys):
    monkeypatch.setattr(updater, "_run", lambda *a, **k: pytest.fail("a process was started"))
    assert updater.update(dry_run=True) == 0
    assert "nothing is run now" in capsys.readouterr().out


# --- How the package was installed ------------------------------------------

class Dist:
    def __init__(self, direct_url=None, installer="pip\n", error=False):
        self.files = {"direct_url.json": direct_url, "INSTALLER": installer}
        self.error = error

    def read_text(self, name):
        if self.error:
            raise OSError("unreadable")
        return self.files.get(name)


def _url(folder: Path, editable: bool | None = None) -> str:
    info = {} if editable is None else {"editable": editable}
    return json.dumps({"url": folder.as_uri(), "dir_info": info})


def test_no_direct_url_is_pypi():
    assert updater.detect_install(Dist()).kind == updater.PYPI


def test_a_git_checkout_is_a_clone(tmp_path):
    (tmp_path / ".git").mkdir()
    found = updater.detect_install(Dist(_url(tmp_path)))
    assert found.kind == updater.CLONE and found.folder == tmp_path
    assert updater.detect_install(Dist(_url(tmp_path, editable=True))).kind == updater.EDITABLE


def test_a_folder_that_is_not_a_checkout_is_unknown(tmp_path):
    assert updater.detect_install(Dist(_url(tmp_path))).kind == updater.UNKNOWN


@pytest.mark.parametrize("dist", [
    Dist(json.dumps({"url": "https://example.invalid/modelquad.whl", "archive_info": {}})),
    Dist("{broken"),
    Dist(json.dumps({"no": "url"})),
    Dist(installer="uv\n"),
    Dist(error=True),
])
def test_anything_else_is_unknown(dist):
    found = updater.detect_install(dist)
    assert found.kind == updater.UNKNOWN and found.why


def test_this_checkout_is_detected_without_error():
    # The suite's own install: whatever it is, the answer is one of the kinds.
    assert updater.detect_install().kind in (updater.PYPI, updater.CLONE, updater.EDITABLE, updater.UNKNOWN)


# --- ModelQuad's own process opens no socket ---------------------------------

_SOCKET_EVENTS: list[str] = []
_RECORDING = False


def _record(event, args):
    if _RECORDING and event.startswith("socket."):
        _SOCKET_EVENTS.append(event)


sys.addaudithook(_record)


def test_the_update_opens_no_socket_itself():
    """The children go online; ModelQuad's own process passes the loopback guard
    and opens no socket at all, even with the real install detection."""
    global _RECORDING
    netpolicy.install_loopback_guard()
    fake = Fake(Install(updater.PYPI))
    tools = fake.tools()
    tools.install = updater.detect_install
    _SOCKET_EVENTS.clear()
    _RECORDING = True
    try:
        updater.update(tools=tools)
        updater.update(dry_run=True, tools=tools)
    finally:
        _RECORDING = False
    assert _SOCKET_EVENTS == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process table")
def test_the_real_process_check_answers():
    found = updater._blocking_running()
    assert found is None or all(name.casefold() in {"claude.exe", "modelquad-mcp.exe"} for name in found)
