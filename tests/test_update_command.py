# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""`python -m modelquad update`: one user-started step, refused while anything
holds ModelQuad's programs open.

Every outside effect is a stand-in: no test here starts pip, git or claude,
touches the network or reads the real Claude Code configuration. The two
child processes started below run `python -m modelquad update --dry-run` and
the console-script form of it, which run nothing.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from modelquad import netpolicy, updater
from modelquad.procutil import no_window_kwargs
from modelquad.updater import Install, Result, Tools

PY = r"C:\venv\Scripts\python.exe"
MAIN = r"C:\venv\Lib\site-packages\modelquad\__main__.py"
ENTRY = {"id": "modelquad@modelquad", "version": "0.4.1", "scope": "user", "enabled": True}
OTHER = {"id": "other@market", "version": "1.0", "scope": "user", "enabled": True}


class Fake:
    def __init__(self, install, running=(), claude=("C:/bin/claude.exe", None), plugins=(ENTRY,),
                 fail=None, git_status="", branch="main", doctor=(0, "[ok] fine\n"), worker=False,
                 argv0=MAIN, listing=None):
        self.calls: list[tuple[list[str], Path | None]] = []
        self.lines: list[str] = []
        self._install = install
        self._running = running
        self._claude = claude
        self._listing = listing if listing is not None else json.dumps([OTHER, *plugins])
        self._fail = fail or {}
        self._git_status = git_status
        self._branch = branch
        self._doctor = doctor
        self._worker = worker
        self._argv0 = argv0

    def run(self, argv, cwd=None):
        self.calls.append((list(argv), cwd))
        joined = " ".join(argv)
        for key, code in self._fail.items():
            if key in joined:
                return Result(code, "it failed")
        if argv[:2] == ["git", "status"]:
            return Result(0, self._git_status)
        if argv[:2] == ["git", "rev-parse"]:
            return Result(0, self._branch + "\n")
        if argv[:4] == ["claude", "plugin", "list", "--json"]:
            return Result(0, self._listing)
        if argv[0] == PY and "main(['doctor'])" in argv[-1]:
            return Result(*self._doctor)
        if argv[0] == PY and "__version__" in argv[-1]:
            return Result(0, "0.5.0\n")
        return Result(0, "done\n")

    def tools(self):
        return Tools(running=lambda: None if self._running is None else list(self._running),
                     run=self.run, which=lambda name: self._claude if name == "claude" else (None, "not_found"),
                     install=lambda: self._install, worker=lambda: self._worker, python=PY,
                     argv0=self._argv0, out=self.lines.append)

    @property
    def text(self):
        return "\n".join(self.lines)

    @property
    def argvs(self):
        return [argv for argv, _ in self.calls]


DOCTOR = [PY, "-P", "-c", "from modelquad.cli_entry import main; raise SystemExit(main(['doctor']))"]
VERSION = [PY, "-P", "-c", "import modelquad; print(modelquad.__version__)"]
LIST = ["claude", "plugin", "list", "--json"]
MARKET = ["claude", "plugin", "marketplace", "update", "modelquad"]
UPDATE = ["claude", "plugin", "update", "modelquad@modelquad", "--scope", "user"]


# --- When it refuses ----------------------------------------------------------

@pytest.mark.parametrize("argv0", [r"C:\venv\Scripts\modelquad.exe", r"C:\venv\Scripts\modelquad",
                                   "-c", ""])
def test_a_console_script_start_is_refused_with_the_exact_command(argv0):
    """The blocker: `modelquad.exe update` made pip replace the running modelquad.exe."""
    fake = Fake(Install(updater.PYPI), argv0=argv0)
    assert updater.update(tools=fake.tools()) == 1
    assert fake.calls == []
    assert f'  "{PY}" -m modelquad update' in fake.lines


def test_python_m_modelquad_is_the_module_start():
    assert updater.started_as_module(MAIN)
    assert not updater.started_as_module(r"C:\venv\Scripts\modelquad.exe")
    assert not updater.started_as_module(r"C:\other\__main__.py")


@pytest.mark.parametrize("running", [["claude.exe"], ["Claude.exe"], ["modelquad-mcp.exe"],
                                     ["modelquad.exe"], ["Claude.exe", "claude.exe", "modelquad-mcp.exe"]])
def test_refused_while_something_holds_modelquad_open(running):
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
    assert "-P -m pip install --upgrade modelquad" in fake.text


def test_refused_in_a_worker():
    fake = Fake(Install(updater.PYPI), worker=True)
    assert updater.update(tools=fake.tools()) == 1
    assert fake.calls == [] and "worker" in fake.text


def test_the_real_worker_check_reads_the_environment(monkeypatch):
    monkeypatch.setenv("MODELQUAD_WORKER", "1")
    assert updater._is_worker() is True


# --- The package --------------------------------------------------------------

def test_pypi_install_upgrades_with_the_same_interpreter_then_refreshes_the_plugin():
    fake = Fake(Install(updater.PYPI))
    assert updater.update(tools=fake.tools()) == 0
    assert fake.argvs == [[PY, "-P", "-m", "pip", "install", "--upgrade", "modelquad"],
                          LIST, MARKET, UPDATE, DOCTOR, VERSION]
    # Said before anything starts: pip goes online, not ModelQuad.
    first_command = fake.lines.index("> " + updater._show(fake.argvs[0]))
    assert any("connect to the internet now; ModelQuad itself does not" in line
               for line in fake.lines[:first_command])
    assert "The package was upgraded: ModelQuad is now version 0.5.0." in fake.lines
    assert fake.lines[-1] == "Start Claude Code again to use it."


@pytest.mark.parametrize("kind, pip", [(updater.CLONE, ["."]), (updater.EDITABLE, ["-e", "."])])
def test_clone_install_pulls_then_installs_in_the_checkout(tmp_path, kind, pip):
    fake = Fake(Install(kind, tmp_path))
    assert updater.update(tools=fake.tools()) == 0
    assert fake.calls[:4] == [(["git", "status", "--porcelain"], tmp_path),
                              (["git", "rev-parse", "--abbrev-ref", "HEAD"], tmp_path),
                              (["git", "pull", "--ff-only"], tmp_path),
                              ([PY, "-P", "-m", "pip", "install", *pip], tmp_path)]
    assert "pip and git connect to the internet now" in fake.text


@pytest.mark.parametrize("change, why", [({"git_status": " M src/x.py\n"}, "local changes"),
                                         ({"branch": "feature/x"}, "on feature/x, not main"),
                                         ({"fail": {"git status": 128}}, "could not say")])
def test_a_clone_not_clean_on_main_is_left_alone(tmp_path, change, why):
    fake = Fake(Install(updater.CLONE, tmp_path), **change)
    assert updater.update(tools=fake.tools()) == 1
    assert not any(argv[:2] == ["git", "pull"] or "pip" in argv for argv in fake.argvs)
    assert why in fake.text and "git pull --ff-only" in fake.text


def test_unknown_install_runs_nothing_and_prints_both_ways():
    fake = Fake(Install(updater.UNKNOWN, why="it was installed from a file or a link"))
    assert updater.update(tools=fake.tools()) == 1
    assert fake.calls == []
    assert "cannot tell how it was installed (it was installed from a file or a link)" in fake.text
    assert "From PyPI:" in fake.text and "From a clone:" in fake.text
    assert "claude plugin update modelquad@modelquad --scope user" in fake.text


def test_a_failed_pull_installs_nothing():
    fake = Fake(Install(updater.CLONE, Path("C:/clone")), fail={"git pull": 1})
    assert updater.update(tools=fake.tools()) == 1
    assert fake.argvs[-1] == ["git", "pull", "--ff-only"]
    assert "Nothing was installed" in fake.text


def test_a_failed_pip_says_what_to_run_again_and_does_not_blame_claude_code():
    fake = Fake(Install(updater.PYPI), fail={"-m pip": 1})
    assert updater.update(tools=fake.tools()) == 1
    assert len(fake.calls) == 1
    assert "pip did not finish" in fake.text and "Claude Code" not in fake.text
    assert "  " + updater._show(fake.argvs[0]) in fake.lines


def test_a_failed_doctor_says_the_upgrade_succeeded():
    fake = Fake(Install(updater.PYPI), doctor=(1, "[ok] a\n[PROBLEM] The Codex CLI was not found.\n"))
    assert updater.update(tools=fake.tools()) == 1
    assert ("The upgrade itself succeeded, but modelquad doctor found a problem: "
            "[PROBLEM] The Codex CLI was not found..") in fake.lines


# --- The plugin -----------------------------------------------------------------

def test_plugin_not_installed_is_left_alone():
    fake = Fake(Install(updater.PYPI), plugins=())
    assert updater.update(tools=fake.tools()) == 0
    assert MARKET not in fake.argvs and "plugin is not installed" in fake.text


def test_a_disabled_plugin_is_not_refreshed_nor_enabled():
    fake = Fake(Install(updater.PYPI), plugins=({**ENTRY, "enabled": False},))
    assert updater.update(tools=fake.tools()) == 1
    assert UPDATE not in fake.argvs
    assert not any("enable" in argv for argv in fake.argvs)
    assert "disabled" in fake.text and "NOT refreshed" in fake.text


@pytest.mark.parametrize("scope", ["project", "local"])
def test_another_scope_is_named_not_guessed(scope):
    fake = Fake(Install(updater.PYPI), plugins=({**ENTRY, "scope": scope},))
    assert updater.update(tools=fake.tools()) == 1
    assert not any(argv[:3] == ["claude", "plugin", "update"] for argv in fake.argvs)
    assert f"  claude plugin update modelquad@modelquad --scope {scope}" in fake.lines


def test_a_failed_update_falls_back_to_a_reinstall_that_keeps_data():
    fake = Fake(Install(updater.PYPI), fail={"plugin update": 1})
    assert updater.update(tools=fake.tools()) == 0
    assert fake.argvs[3:6] == [UPDATE,
                               ["claude", "plugin", "uninstall", "modelquad@modelquad", "--scope", "user",
                                "--keep-data"],
                               ["claude", "plugin", "install", "modelquad@modelquad", "--scope", "user"]]


def test_a_failed_reinstall_is_a_failure():
    fake = Fake(Install(updater.PYPI), fail={"plugin update": 1, "plugin install": 1})
    assert updater.update(tools=fake.tools()) == 1
    assert "NOT refreshed" in fake.text


@pytest.mark.parametrize("listing", ["not json", "{}", ""])
def test_an_unreadable_plugin_list_is_a_skip(listing):
    fake = Fake(Install(updater.PYPI), listing=listing)
    assert updater.update(tools=fake.tools()) == 1
    assert MARKET not in fake.argvs and "could not be read" in fake.text


def test_without_claude_on_path_the_plugin_steps_are_printed_and_it_fails():
    fake = Fake(Install(updater.PYPI), claude=(None, "not_found"))
    assert updater.update(tools=fake.tools()) == 1
    assert not any(argv[0] == "claude" for argv in fake.argvs)
    assert "was not found on PATH" in fake.text
    assert "    claude plugin update modelquad@modelquad --scope user" in fake.lines


def test_a_batch_claude_is_named_as_found_but_not_run():
    fake = Fake(Install(updater.PYPI), claude=(None, "batch_script"))
    assert updater.update(tools=fake.tools()) == 1
    assert "found, but it is a batch script (claude.cmd)" in fake.text
    assert "not found" not in fake.text


def test_a_failed_marketplace_update_is_a_failure():
    fake = Fake(Install(updater.PYPI), fail={"marketplace update": 1})
    assert updater.update(tools=fake.tools()) == 1
    assert UPDATE not in fake.argvs


# --- Dry run and the command line -----------------------------------------------------

@pytest.mark.parametrize("running", [[], ["claude.exe"], None])
def test_dry_run_runs_nothing(running):
    fake = Fake(Install(updater.PYPI), running=running, argv0="modelquad.exe")
    assert updater.update(dry_run=True, tools=fake.tools()) == 0
    assert fake.calls == []
    assert "nothing is run now" in fake.lines[0]
    assert "-m modelquad update" in fake.text
    assert "-P -m pip install --upgrade modelquad" in fake.text


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


def _child(*argv):
    env = dict(os.environ, PYTHONPATH=str(Path(updater.__file__).resolve().parents[1]))
    env.pop("MODELQUAD_WORKER", None)
    return subprocess.run([sys.executable, *argv], capture_output=True, text=True, env=env,
                          timeout=120, **no_window_kwargs())


def test_python_m_modelquad_runs_the_update_as_a_module():
    out = _child("-m", "modelquad", "update", "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "nothing is run now" in out.stdout
    assert "First it would stop: run it as" not in out.stdout


def test_a_start_that_is_not_python_m_modelquad_is_refused_for_real():
    """The console script's argv[0] is not modelquad/__main__.py: refused, nothing run."""
    out = _child("-c", "import sys; sys.argv[0] = r'C:\\venv\\Scripts\\modelquad.exe'; "
                       "from modelquad.cli_entry import main; raise SystemExit(main(['update']))")
    assert out.returncode == 1
    assert f'"{sys.executable}" -m modelquad update' in out.stdout
    assert "Updating ModelQuad" not in out.stdout


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
    assert found is None or all(name.casefold() in {"claude.exe", "modelquad-mcp.exe", "modelquad.exe"}
                                for name in found)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process table")
def test_the_process_check_skips_only_its_own_modelquad_exe(monkeypatch):
    from modelquad import procutil

    me = os.getpid()
    table = [(me, 500, "python.exe"), (500, 400, "modelquad.exe"), (400, 4, "Claude.exe"),
             (900, 4, "modelquad.exe"), (901, 4, "notepad.exe")]
    monkeypatch.setattr(procutil, "_windows_process_snapshot", lambda: table)
    # Its own modelquad.exe launcher is left out; another modelquad.exe is not, and
    # Claude Code counts even when this process runs inside it.
    assert updater._blocking_running() == ["Claude.exe", "modelquad.exe"]
    table[3] = (900, 4, "notepad.exe")
    assert updater._blocking_running() == ["Claude.exe"]
    monkeypatch.setattr(procutil, "_windows_process_snapshot", lambda: None)
    assert updater._blocking_running() is None
