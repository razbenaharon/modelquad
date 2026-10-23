# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Background cwd/import regressions; only the native import case spawns."""

from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from conftest import no_window_kwargs

import modelquad
from modelquad import background_cwd, paths, procutil
from modelquad.jobs import handle
from modelquad.jobs.seam import JobRef
from modelquad.service import history, lifecycle
from modelquad.service.socket_identity import ListenerError


LAUNCH_IDS = ["service", "runner", "breakaway-probe", "service-breakaway-retry"]
HIDDEN = 0x08000000
BREAKAWAY = 0x01000000


def canonical(path):
    return os.path.normcase(os.path.realpath(path))


class RecordingPopen:
    pid = 550
    _handle = 550

    def __init__(self, *, deny_first=False):
        self.calls = []
        self.cleanup = []
        self.deny_first = deny_first

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        if self.deny_first and len(self.calls) == 1:
            error = OSError("synthetic breakaway denial")
            error.winerror = 5
            raise error
        return self

    def kill(self):
        self.cleanup.append("kill")

    def poll(self):
        return None

    def wait(self, timeout):
        self.cleanup.append("wait")
        return 0


@pytest.fixture
def launch_context(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / ".git").mkdir()
    for name in ("python.exe", "git.exe"):
        (repository / name).write_bytes(b"planted fixture; never execute")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    state = tmp_path / "product-state"
    system = tmp_path / "system-directory"
    system.mkdir()
    monkeypatch.setattr(lifecycle, "system_directory", lambda: str(system))
    monkeypatch.chdir(repository)
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(state))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setattr(handle, "_launch_policy", lambda: (0, False, None))
    monkeypatch.setattr(handle, "_process_in_any_job", lambda process: False)
    monkeypatch.setattr(procutil, "no_window_kwargs", lambda: {"creationflags": HIDDEN})
    return repository, state, home


def invoke_launch(item, tmp_path, monkeypatch, recorder):
    """Invoke each real launch seam with deterministic lifecycle dependencies.

    Return its pre-change argv and complete kwargs, independent of the cwd
    helper. No probe, process resume, or Windows service guard runs for real.
    """
    if item in ("service", "service-breakaway-retry"):
        monkeypatch.setattr(lifecycle.sys, "platform", "win32")
        monkeypatch.setattr(lifecycle, "_service_start_guard", lambda path: nullcontext())
        if item == "service-breakaway-retry":
            monkeypatch.setattr(handle, "_launch_policy", lambda: (BREAKAWAY, True, None))
            recorder.deny_first = True
        command = [sys.executable, "-m", "modelquad.cli", "serve", "--managed-child", "--port", "8765"]
        def authenticate(port, **kwargs):
            assert port == 8765 and kwargs["require_runner"] is False
            if "expected_pid" not in kwargs:
                raise ListenerError("service_absent")
            assert kwargs["expected_pid"] == recorder.pid
            assert kwargs["expected_created"] == 550.0
            assert kwargs["expected_image"] == command[0]
            return SimpleNamespace(close=lambda: None, health={})
        manager = lifecycle.ServiceProcessManager(
            state_dir=tmp_path / "service-records", popen=recorder,
            process_created_at=lambda pid: 550.0, authenticate=authenticate,
            working_directory=lifecycle.service_working_directory,
        )
        manager.state_dir.mkdir()  # The real start guard creates this directory.
        result = manager.start(command=command)
        if result.success:
            assert result.ready is True and result.running is True
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0x8) |
                 getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) |
                 getattr(subprocess, "CREATE_NO_WINDOW", HIDDEN))
        base = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "close_fds": True, "creationflags": flags}
        return result.success, command, base
    if item == "runner":
        database = tmp_path / "jobs.sqlite"
        attempt = history.record_delegation_attempt(
            database, task_type="implementation", decision_id="550-background-cwd")
        row = history.reserve_job(database, "550-background-cwd", attempt)
        ref = JobRef(row["job_id"], row["attempt_id"], row["decision_id"])
        spec = handle.JobSpec((sys.executable, "-c", "pass"), str(tmp_path), str(database), attempt, 30)
        spec_path = tmp_path / "job-spec.json"
        spec_path.write_text("{}", encoding="utf-8")
        stream = io.StringIO()
        monkeypatch.setattr(handle, "_open_runner_log", lambda: stream)
        monkeypatch.setattr(handle.subprocess, "Popen", recorder)
        # Cwd/env coverage uses a deterministic source-module argv. The bound
        # Windows venv bootstrap has its own T12-A admission/argv tests.
        monkeypatch.setattr(handle, "_runner_command", lambda job_id, path, db:
                            [sys.executable, "-m", "modelquad.jobs.runner", "--job-id", job_id,
                             "--spec", str(path), "--db-path", str(db)])
        monkeypatch.setattr(procutil, "process_creation_epoch", lambda pid: 550.0)
        monkeypatch.setattr(procutil, "_resume_suspended_process", lambda pid: True)
        result = handle.launch_reserved(ref, spec_path, spec)
        command = [sys.executable, "-m", "modelquad.jobs.runner", "--job-id", ref.job_id,
                   "--spec", str(spec_path), "--db-path", str(database)]
        base = {"stdout": subprocess.DEVNULL, "stderr": stream, "close_fds": True,
                "creationflags": HIDDEN | 0x8 | 0x4}
        return result.launched, command, base
    if item == "breakaway-probe":
        monkeypatch.setattr(handle, "_breakaway_result", None)
        monkeypatch.setattr(handle, "_breakaway_checked", False)
        monkeypatch.setattr(handle, "_caller_job_limits", lambda: 0x800)
        monkeypatch.setattr(handle.subprocess, "Popen", recorder)
        result = handle._probe_breakaway()
        command = [sys.executable, "-c", "pass"]
        base = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True,
                "creationflags": HIDDEN | 0x8 | 0x4 | BREAKAWAY}
        return result, command, base
    raise AssertionError(item)


@pytest.mark.parametrize("item", LAUNCH_IDS, ids=LAUNCH_IDS)
def test_background_children_use_safe_cwd(tmp_path, monkeypatch, launch_context, item):
    repository, state, _ = launch_context
    # Exercise env preservation at all four sites, including both retry calls.
    shared = tmp_path / "shared-imports"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["src", str(shared), "", str(repository / "src")]))
    expected_env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(repository / "src"), str(shared)]))
    # Jobs children add the recursion guard after resolving the safe cwd/env.
    if item in ("runner", "breakaway-probe"):
        expected_env["MODELQUAD_WORKER"] = "1"
    recorder = RecordingPopen()
    success, command, base = invoke_launch(item, tmp_path, monkeypatch, recorder)
    assert success
    service = item in ("service", "service-breakaway-retry")
    expected_cwd = tmp_path / "system-directory" if service else state
    assert expected_cwd.is_dir()
    assert len(recorder.calls) == (2 if item == "service-breakaway-retry" else 1)
    for index, (actual_command, kwargs) in enumerate(recorder.calls):
        assert actual_command == command
        assert canonical(kwargs["cwd"]) == canonical(expected_cwd)
        assert canonical(kwargs["cwd"]) != canonical(repository)
        expected = dict(base, cwd=os.path.realpath(expected_cwd), env=expected_env)
        if item == "service-breakaway-retry" and index == 0:
            expected["creationflags"] |= BREAKAWAY
        assert kwargs == expected
    if item == "service-breakaway-retry":
        assert recorder.calls[0][1]["env"] == recorder.calls[1][1]["env"]
        assert recorder.calls[0][1]["cwd"] == recorder.calls[1][1]["cwd"]
    if item == "breakaway-probe":
        assert recorder.cleanup == ["kill", "wait"]


def test_caller_supplied_cwd_is_kept(tmp_path, monkeypatch, launch_context):
    directory = tmp_path / "installed-version"
    directory.mkdir()
    recorder = RecordingPopen()
    base = {"cwd": str(directory), "creationflags": HIDDEN | 0x208, "close_fds": True}
    command = [str(directory / "modelquad-child.exe")]
    child, contained = lifecycle._launch_background_child(recorder, command, base)
    assert child is recorder
    assert contained is None
    assert recorder.calls == [(command, base)]
    assert not launch_context[1].exists()


def test_caller_supplied_env_is_kept_when_cwd_is_filled(tmp_path, monkeypatch, launch_context):
    # A relative state root makes the helper return an env of its own.
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", "relative-state")
    assert "env" in background_cwd.background_child_kwargs([str(tmp_path / "child.exe")])
    recorder = RecordingPopen()
    caller_env = {"MQ_S2_CALLER": "1"}
    base = {"env": caller_env, "creationflags": HIDDEN, "close_fds": True}
    command = [str(tmp_path / "child.exe")]
    lifecycle._launch_background_child(recorder, command, base)
    (_recorded, kwargs), = recorder.calls
    assert kwargs["env"] is caller_env
    assert canonical(kwargs["cwd"]) == canonical(launch_context[2])


def test_deleted_working_folder_does_not_block_launch(monkeypatch, launch_context):
    class DeletedWorkingFolder:
        """The helper's view of os; only its own getcwd call fails."""

        def __getattr__(self, name):
            return getattr(os, name)

        @staticmethod
        def getcwd():
            raise FileNotFoundError("synthetic deleted working folder")

    monkeypatch.setattr(background_cwd, "os", DeletedWorkingFolder())
    assert canonical(background_cwd.background_directory()) == canonical(launch_context[1])


@pytest.mark.parametrize("case", ["missing-and-uncreatable", "relative", "home-invalid", "state-root-is-cwd"],
                         ids=["missing-and-uncreatable", "relative", "home-invalid", "state-root-is-cwd"])
def test_invalid_state_root_falls_back_to_home(tmp_path, monkeypatch, launch_context, caplog, case):
    repository, _, home = launch_context
    blocker = tmp_path / "f.txt"
    blocker.write_text("regular file", encoding="utf-8")
    raw = str(blocker / "state")
    expected_cwd = home
    reason = "directory_unavailable"
    if case == "relative":
        raw = "relative-state"
        reason = "path_relative"
    elif case == "home-invalid":
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: blocker))
        expected_cwd = Path(sys.executable).resolve().parent
    elif case == "state-root-is-cwd":
        raw = str(repository)
        reason = "state_root_is_cwd"
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", raw)
    recorder = RecordingPopen()
    caplog.set_level("DEBUG", logger=background_cwd.__name__)
    command = [sys.executable, "-c", "pass"]
    child, _ = lifecycle._launch_background_child(recorder, command, {"close_fds": True})
    assert child is recorder
    kwargs = recorder.calls[0][1]
    assert canonical(kwargs["cwd"]) == canonical(expected_cwd)
    assert canonical(kwargs["cwd"]) != canonical(repository)
    expected = {"close_fds": True, "creationflags": 0, "cwd": os.path.realpath(expected_cwd)}
    if case == "relative":
        expected["env"] = dict(os.environ, MODELQUAD_STATE_ROOT=os.path.abspath(raw))
        assert kwargs["env"]["MODELQUAD_STATE_ROOT"] == str(repository / raw)
    assert kwargs == expected
    messages = [record.getMessage() for record in caplog.records if record.name == background_cwd.__name__]
    assert reason in messages
    assert all(str(tmp_path) not in message for message in messages)


@pytest.mark.parametrize("case", ["relative-pythonpath", "absolute-pythonpath", "no-pythonpath-dev-checkout",
                                  "installed-unchanged"],
                         ids=["relative-pythonpath", "absolute-pythonpath", "no-pythonpath-dev-checkout",
                              "installed-unchanged"])
def test_source_child_keeps_import_root(tmp_path, monkeypatch, launch_context, case):
    repository, state, _ = launch_context
    monkeypatch.setattr(paths, "is_dev_checkout", lambda: case != "installed-unchanged")
    expected_env = None
    if case == "relative-pythonpath":
        absolute = str(tmp_path / "absolute-root")
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["src", "", absolute, "src/.", absolute]))
        expected_env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(repository / "src"), absolute]))
    elif case == "absolute-pythonpath":
        monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(tmp_path / "first"), str(tmp_path / "second")]))
    elif case == "installed-unchanged":
        monkeypatch.setenv("PYTHONPATH", "src")
        expected_env = dict(os.environ, PYTHONPATH=str(repository / "src"))
    recorder = RecordingPopen()
    # No '-m': -c launches must receive exactly the same import treatment.
    command = [sys.executable, "-c", "pass"]
    lifecycle._launch_background_child(recorder, command, {"close_fds": True})
    expected = {"close_fds": True, "creationflags": 0, "cwd": os.path.realpath(state)}
    if expected_env is not None:
        expected["env"] = expected_env
    assert recorder.calls == [(command, expected)]


@pytest.mark.parametrize("item", LAUNCH_IDS, ids=LAUNCH_IDS)
def test_no_valid_background_directory_refuses_launch(tmp_path, monkeypatch, launch_context, item):
    blocker = tmp_path / "f.txt"
    blocker.write_text("regular file", encoding="utf-8")
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(blocker / "state"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: blocker))
    monkeypatch.setattr(sys, "executable", str(blocker / "python.exe"))
    if item in ("service", "service-breakaway-retry"):
        monkeypatch.setattr(lifecycle, "system_directory", lambda: None)
    recorder = RecordingPopen()
    if item == "breakaway-probe":
        with pytest.raises(OSError, match="^background_cwd_unavailable$"):
            invoke_launch(item, tmp_path, monkeypatch, recorder)
    else:
        success, _, _ = invoke_launch(item, tmp_path, monkeypatch, recorder)
        assert not success
    assert recorder.calls == []


def test_source_runner_imports_parent_code_native(tmp_path, monkeypatch, launch_context):
    repository, state, _ = launch_context
    marker = repository / "src" / "mq_s2_marker" / "__init__.py"
    marker.parent.mkdir(parents=True)
    marker.write_text("ORIGIN = 'parent'\n", encoding="utf-8")
    decoy = state / "src" / "mq_s2_marker" / "__init__.py"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("ORIGIN = 'decoy'\n", encoding="utf-8")
    # Keep the exact parent package available in source-tree CI as well as
    # editable/wheel installs; tmp_path may be on a different Windows drive.
    package_root = Path(modelquad.__file__).resolve().parent.parent
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["src", str(package_root)]))
    command = [sys.executable, "-c", "import json,mq_s2_marker,modelquad; "
               "print(json.dumps([mq_s2_marker.__file__,modelquad.__file__]))"]
    kwargs = background_cwd.background_child_kwargs(command)
    result = subprocess.run(command, **kwargs, capture_output=True, text=True, timeout=30, check=True,
                            **no_window_kwargs())
    imported_marker, imported_modelquad = json.loads(result.stdout)
    assert canonical(imported_marker) == canonical(marker)
    assert canonical(imported_marker) != canonical(decoy)
    assert canonical(imported_modelquad) == canonical(modelquad.__file__)
