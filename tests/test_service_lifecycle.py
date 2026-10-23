# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Fake-only tests for Windows control-plane service lifecycle helpers."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import modelquad.cli as cli_module
import modelquad.service.lifecycle as lifecycle
from modelquad.executable_resolution import Resolution
from modelquad.execution import no_window_kwargs
from modelquad.service.lifecycle import (
    ProcessResult,
    ProcessStatus,
    RegistrationResult,
    ServiceProcessManager,
    TaskSchedulerRegistrar,
)


class RecordingRun:
    def __init__(self, result: subprocess.CompletedProcess | None = None, exc: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = result or subprocess.CompletedProcess(args=[], returncode=0, stdout="ok", stderr="")
        self.exc = exc

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        if self.exc is not None:
            raise self.exc
        return self.result


class DefinitionReadingRun(RecordingRun):
    """A ``RecordingRun`` that reads the ``/xml`` definition while it exists.

    ``register`` removes the temporary definition in a ``finally``, so a test
    that looked for the file afterwards would find nothing. Reading it at call
    time is what a real ``schtasks`` does, and it lets the test assert on the
    definition that was actually handed over.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.definitions: list[str] = []
        self.definition_paths: list[str] = []

    def __call__(self, argv, **kwargs):
        if "/query" in argv:
            self.calls.append({"argv": argv, "kwargs": kwargs})
            if not self.definitions:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="ERROR: The system cannot find the file specified.")
            return subprocess.CompletedProcess(argv, 0, stdout=self.definitions[-1], stderr="")
        if "/create" in argv and "/xml" in argv:
            path = argv[argv.index("/xml") + 1]
            self.definition_paths.append(path)
            with open(path, encoding="utf-16") as stream:
                self.definitions.append(stream.read())
        return super().__call__(argv, **kwargs)


class RecordingPopen:
    def __init__(self, pid: int = 2468) -> None:
        self.pid = pid
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, "kwargs": kwargs})
        return self

    def poll(self):
        return None

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def windows_platform(tmp_path, monkeypatch):
    executable = tmp_path / 'synthetic system' / 'schtasks.exe'
    executable.parent.mkdir()
    executable.write_bytes(b'synthetic fixture - never execute\n')
    resolved = os.path.realpath(executable)

    def resolve(name):
        assert name == 'schtasks'
        return Resolution(resolved, 'system', 'binary', None)

    monkeypatch.setattr('modelquad.service.auth.current_user_sid', lambda: 'S-1-5-21-1')
    monkeypatch.setattr(lifecycle, 'system_executable', resolve)
    monkeypatch.setattr(lifecycle, 'system_directory', lambda: str(executable.parent))
    monkeypatch.setattr(lifecycle.sys, "platform", "win32")
    monkeypatch.setattr("modelquad.jobs.handle._launch_policy", lambda: (0, False, None))
    monkeypatch.setattr("modelquad.jobs.handle._process_in_any_job", lambda handle: False)
    return resolved


def _command() -> list[str]:
    return [
        r"C:\Program Files\Python\python.exe",
        "-I",
        str(Path(cli_module.__file__).resolve().parent / "service" / "entry.py"),
        "serve",
        "--managed-child",
        "--port",
        "8765",
    ]


_STARTED_AT = "2026-08-16T00:00:00+00:00"
_STARTED_EPOCH = datetime(2026, 8, 16, tzinfo=timezone.utc).timestamp()


def _write_pidfile(
    state_dir, *, pid: int = 2468, argv: list[str] | None = None, started_at: str = _STARTED_AT
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "service.pid").write_text(
        json.dumps({"argv": argv or _command(), "pid": pid, "started_at": started_at}),
        encoding="utf-8",
    )


def _identity(
    *, image: str | None = _command()[0], created: float | None = _STARTED_EPOCH - 0.2
) -> dict[str, object]:
    """Fake identity probes; by default, those of the service ``_write_pidfile`` records.

    ``start`` stamps ``started_at`` just after ``Popen`` returns, so the real
    service was created a fraction of a second before it. ``None`` is a probe
    that could not read its answer.
    """

    return {"process_image_path": lambda pid: image, "process_created_at": lambda pid: created}


def _ready_authenticate(port, **kwargs):
    """Explicit fake readiness: absence before launch, proof for a managed PID."""
    from types import SimpleNamespace
    from modelquad.service.socket_identity import ListenerError
    if "expected_pid" not in kwargs:
        raise ListenerError("service_absent")
    assert kwargs["require_runner"] is False
    return SimpleNamespace(close=lambda: None)


def test_task_scheduler_register_names_this_user_in_the_logon_trigger(windows_platform, monkeypatch):
    """A bare ``/sc onlogon`` is an any-user trigger Windows refuses.

    Measured non-elevated on Windows 11 Pro 10.0.26200: the argv this method
    used to emit returns ``Access is denied``, and so does the same argv with
    ``/ru``. A definition whose ``LogonTrigger`` carries ``UserId`` succeeds.
    So the trigger must name the user, and ``/sc onlogon`` must not appear.
    """

    monkeypatch.setenv("USERDOMAIN", "EXAMPLE-PC")
    monkeypatch.setenv("USERNAME", "example")
    run = DefinitionReadingRun()

    result = TaskSchedulerRegistrar(run=run).register(command=_command())

    assert result.success is True
    assert result.registered is True
    call = next(call for call in run.calls if "/create" in call["argv"])
    assert call["argv"][:4] == [windows_platform, "/create", "/tn", TaskSchedulerRegistrar.TASK_NAME]
    assert call["argv"][4] == "/xml"
    assert call["argv"][6] == "/f"
    assert "onlogon" not in call["argv"], "an any-user logon trigger is what Windows refuses"
    assert "/ru" not in [part.lower() for part in call["argv"]], "/ru does not fix the trigger"
    assert call["kwargs"] == {
        "capture_output": True,
        "text": True,
        "shell": False,
        "timeout": lifecycle.WINDOWS_COMMAND_TIMEOUT_SECONDS,
        "cwd": lifecycle.service_working_directory(),
        **no_window_kwargs(),
    }

    definition = run.definitions[0]
    trigger = definition.split("<Triggers>", 1)[1].split("</Triggers>", 1)[0]
    assert "<UserId>S-1-5-21-1</UserId>" in trigger
    assert "<RunLevel>LeastPrivilege</RunLevel>" in definition
    assert "<LogonType>InteractiveToken</LogonType>" in definition


def test_task_scheduler_register_passes_image_and_arguments_as_separate_elements(
    windows_platform, monkeypatch
):
    """The launch command is no longer flattened into one re-parsed string."""

    monkeypatch.setenv("USERDOMAIN", "EXAMPLE-PC")
    monkeypatch.setenv("USERNAME", "example")
    run = DefinitionReadingRun()

    TaskSchedulerRegistrar(run=run).register(command=_command())

    definition = run.definitions[0]
    assert "<Command>C:\\Program Files\\Python\\python.exe</Command>" in definition
    assert (
        "<Arguments>"
        f'"-I" "{_command()[2]}" "serve" "--managed-child" "--port" "8765"'
        "</Arguments>"
    ) in definition


def test_task_scheduler_register_removes_the_definition_file_it_wrote(windows_platform, monkeypatch):
    """The definition is a temporary, not a file left behind in %TEMP%."""

    monkeypatch.setenv("USERDOMAIN", "EXAMPLE-PC")
    monkeypatch.setenv("USERNAME", "example")
    run = DefinitionReadingRun()

    TaskSchedulerRegistrar(run=run).register(command=_command())

    assert run.definition_paths, "register must hand schtasks a definition path"
    assert not os.path.exists(run.definition_paths[0])


def test_task_scheduler_register_removes_the_definition_file_even_when_schtasks_fails(
    windows_platform, monkeypatch
):
    monkeypatch.setenv("USERDOMAIN", "EXAMPLE-PC")
    monkeypatch.setenv("USERNAME", "example")
    run = DefinitionReadingRun(
        subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="ERROR: Access is denied.")
    )

    result = TaskSchedulerRegistrar(run=run).register(command=_command())

    assert result.success is False
    assert result.message == "Task Scheduler rejected the registration."
    assert not os.path.exists(run.definition_paths[0])


def test_task_scheduler_register_refuses_when_windows_reports_no_user_name(
    windows_platform, monkeypatch
):
    """An unreadable identity is a refusal, not a task registered for a guess."""

    monkeypatch.delenv("USERDOMAIN", raising=False)
    monkeypatch.setenv("USERNAME", "")
    monkeypatch.setattr("modelquad.service.auth.current_user_sid", lambda: (_ for _ in ()).throw(OSError("fixture")))
    run = RecordingRun()

    result = TaskSchedulerRegistrar(run=run).register(command=_command())

    assert result.success is False
    assert "user identity" in result.message
    assert run.calls == []


def test_task_scheduler_register_omits_the_domain_when_windows_reports_none(
    windows_platform, monkeypatch
):
    monkeypatch.delenv("USERDOMAIN", raising=False)
    monkeypatch.setenv("USERNAME", "example")
    run = DefinitionReadingRun()

    TaskSchedulerRegistrar(run=run).register(command=_command())

    assert "<UserId>S-1-5-21-1</UserId>" in run.definitions[0]


def test_task_scheduler_register_dry_run_never_invokes_schtasks(windows_platform):
    run = RecordingRun()

    result = TaskSchedulerRegistrar(run=run).register(command=_command(), dry_run=True)

    assert result.success is True
    assert result.action == "would_register"
    assert run.calls == []
    # A dry run writes no definition, so it must not name a file that does not
    # exist -- the "reports something false about the artifact" class of defect.
    assert result.command == (
        "schtasks", "/create", "/tn", TaskSchedulerRegistrar.TASK_NAME,
        "/xml", "<generated task definition>", "/f",
    )


def test_task_scheduler_register_rejects_an_embedded_quote_before_running_anything(windows_platform):
    run = RecordingRun()

    result = TaskSchedulerRegistrar(run=run).register(command=['C:\\bad"python.exe'])

    assert result.success is False
    assert "double quotes" in result.message
    assert run.calls == []


@pytest.mark.parametrize("returncode", [0, 1])
def test_task_scheduler_status_unparseable_and_unrecognized_error_stay_unknown(windows_platform, returncode):
    run = RecordingRun(subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=""))
    result = TaskSchedulerRegistrar(run=run).is_registered()
    assert result.success is False
    assert result.registered is None
    assert run.calls[0]['argv'] == [windows_platform, '/query', '/tn', TaskSchedulerRegistrar.TASK_NAME, '/xml']
    assert run.calls[0]['kwargs']['shell'] is False
    assert run.calls[0]['kwargs']['timeout'] == lifecycle.WINDOWS_COMMAND_TIMEOUT_SECONDS


def test_task_scheduler_unregister_treats_a_missing_task_as_success(windows_platform):
    run = DefinitionReadingRun()

    result = TaskSchedulerRegistrar(run=run).unregister(command=_command())

    assert result.success is True
    assert result.registered is False
    assert [call["argv"][1] for call in run.calls] == ["/query"], "nothing to delete"


def test_task_scheduler_unregister_deletes_only_an_owned_task(windows_platform):
    """Uninstall applies install's ownership check before deleting."""

    run = DefinitionReadingRun()
    registrar = TaskSchedulerRegistrar(run=run)
    assert registrar.register(command=_command()).success is True

    foreign = [*_command()[:-1], "9999"]
    refused = registrar.unregister(command=foreign)
    assert refused.success is False
    assert refused.message == lifecycle.SERVICE_DETAILS["background_startup_foreign"]
    assert not any(call["argv"][1] == "/delete" for call in run.calls)

    deleted = registrar.unregister(command=_command())
    assert deleted.success is True
    assert run.calls[-1]["argv"] == [windows_platform, "/delete", "/tn", TaskSchedulerRegistrar.TASK_NAME, "/f"]


def test_task_scheduler_unregister_leaves_an_unreadable_task_in_place(windows_platform):
    run = RecordingRun(subprocess.CompletedProcess(args=[], returncode=0, stdout="not a task", stderr=""))
    result = TaskSchedulerRegistrar(run=run).unregister(command=_command())
    assert result.success is False
    assert not any(call["argv"][1] == "/delete" for call in run.calls)


def test_task_scheduler_is_explicitly_unsupported_without_invoking_windows_commands(monkeypatch):
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    run = RecordingRun()
    registrar = TaskSchedulerRegistrar(run=run)

    assert registrar.is_registered().supported is False
    assert registrar.register(command=_command()).supported is False
    assert registrar.unregister(command=_command()).supported is False
    assert run.calls == []


def test_process_start_uses_injected_popen_and_records_its_identifying_marker(tmp_path, windows_platform):
    popen = RecordingPopen()
    manager = ServiceProcessManager(authenticate=_ready_authenticate, working_directory=lambda: str(tmp_path), 
        state_dir=tmp_path,
        popen=popen,
        pid_exists=lambda pid: pid == 2468,
        **_identity(created=time.time()),
    )

    result = manager.start(command=_command())

    assert result.success is True
    assert result.pid == 2468
    call = popen.calls[0]
    assert call["argv"] == _command()
    assert call["kwargs"]["close_fds"] is True
    assert call["kwargs"]["creationflags"] == getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
    ) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    assert call["kwargs"]["stdout"] == subprocess.DEVNULL
    assert call["kwargs"]["stderr"] == subprocess.DEVNULL
    payload = json.loads(manager.pid_path.read_text(encoding="utf-8"))
    assert payload["pid"] == 2468
    assert payload["argv"] == _command()
    assert payload["started_at"]
    status = manager.status()
    assert status.running is True
    assert status.pid == 2468
    assert status.argv == tuple(_command())


def test_process_start_dry_run_never_creates_state_or_spawns(tmp_path, windows_platform):
    popen = RecordingPopen()
    manager = ServiceProcessManager(state_dir=tmp_path / "state", popen=popen)

    result = manager.start(command=_command(), dry_run=True)

    assert result.success is True
    assert result.action == "would_start"
    assert not manager.state_dir.exists()
    assert popen.calls == []


@pytest.mark.parametrize(
    "policy,flag,contained",
    [
        ((0, False, None), 0, None),
        ((0x01000000, True, None), 0x01000000, None),
        ((0, True, None), 0, None),
        ((None, False, "breakaway_not_permitted"), 0, "job_kill_on_close"),
        ((0, False, "contained_innermost_job_without_kill_on_close"), 0, "job_shared"),
        (OSError("probe failed"), 0, "containment_unknown"),
    ],
)
def test_service_start_job_policy_mapping(tmp_path, windows_platform, monkeypatch, policy, flag, contained):
    def probe():
        if isinstance(policy, Exception):
            raise policy
        return policy

    popen = RecordingPopen()
    popen._handle = 99
    inspected = []
    monkeypatch.setattr("modelquad.jobs.handle._launch_policy", probe)
    monkeypatch.setattr("modelquad.jobs.handle._process_in_any_job", lambda handle: inspected.append(handle) or False)
    manager = ServiceProcessManager(authenticate=_ready_authenticate, working_directory=lambda: str(tmp_path), process_created_at=lambda pid: _STARTED_EPOCH - 0.2, state_dir=tmp_path, popen=popen)
    result = manager.start(command=_command())
    assert result.success and result.running
    assert result.contained == contained
    assert result.to_dict()["contained"] == contained
    assert popen.calls[0]["kwargs"]["creationflags"] & 0x01000000 == flag
    assert popen.calls[0]["kwargs"]["creationflags"] & 0x08000000
    assert inspected == ([99] if not isinstance(policy, Exception) and policy[1] else [])
    assert json.loads(manager.pid_path.read_text(encoding="utf-8"))["pid"] == popen.pid


@pytest.mark.parametrize("policy", [(0x01000000, True, None), (0, True, None)])
@pytest.mark.parametrize("inspection,contained", [(True, "job_breakaway_ineffective"), (OSError(), "containment_unknown")])
def test_service_escape_inspection_keeps_child_and_pidfile(
    tmp_path, windows_platform, monkeypatch, policy, inspection, contained
):
    popen = RecordingPopen()
    popen._handle = 99
    monkeypatch.setattr("modelquad.jobs.handle._launch_policy", lambda: policy)

    def inspect(handle):
        assert handle == 99
        if isinstance(inspection, Exception):
            raise inspection
        return inspection

    monkeypatch.setattr("modelquad.jobs.handle._process_in_any_job", inspect)
    manager = ServiceProcessManager(authenticate=_ready_authenticate, working_directory=lambda: str(tmp_path), process_created_at=lambda pid: _STARTED_EPOCH - 0.2, state_dir=tmp_path, popen=popen)
    result = manager.start(command=_command())
    assert result.success and result.contained == contained
    assert manager.pid_path.is_file()
    assert len(popen.calls) == 1


def test_service_nested_job_denial_retries_once_with_shared_log(tmp_path, windows_platform, monkeypatch):
    monkeypatch.setattr("modelquad.jobs.handle._launch_policy", lambda: (0x01000000, True, None))
    popen = RecordingPopen()

    def spawn(argv, **kwargs):
        child = popen(argv, **kwargs)
        if len(popen.calls) == 1:
            error = OSError("outer job denies escape")
            error.winerror = 5
            raise error
        return child

    manager = ServiceProcessManager(authenticate=_ready_authenticate, working_directory=lambda: str(tmp_path), process_created_at=lambda pid: _STARTED_EPOCH - 0.2, state_dir=tmp_path, popen=spawn)
    result = manager.start(command=_command())
    assert result.success and result.contained == "job_breakaway_ineffective"
    assert len(popen.calls) == 2
    assert popen.calls[0]["kwargs"]["creationflags"] & 0x01000000
    assert not popen.calls[1]["kwargs"]["creationflags"] & 0x01000000
    assert popen.calls[0]["kwargs"]["stdout"] is popen.calls[1]["kwargs"]["stderr"]
    assert manager.pid_path.is_file()


@pytest.mark.parametrize("winerror,expected_calls", [(5, 2), (87, 1)])
def test_service_launch_retry_is_bounded(tmp_path, windows_platform, monkeypatch, winerror, expected_calls):
    monkeypatch.setattr("modelquad.jobs.handle._launch_policy", lambda: (0x01000000, True, None))
    calls = []

    def spawn(argv, **kwargs):
        calls.append(kwargs)
        error = OSError("denied")
        error.winerror = winerror
        raise error

    manager = ServiceProcessManager(authenticate=_ready_authenticate, working_directory=lambda: str(tmp_path), process_created_at=lambda pid: _STARTED_EPOCH - 0.2, state_dir=tmp_path, popen=spawn)
    result = manager.start(command=_command())
    assert not result.success
    assert len(calls) == expected_calls
    assert not manager.pid_path.exists()


def test_service_start_guard_serializes_two_starters(tmp_path, windows_platform, monkeypatch):
    from threading import Barrier, Thread

    barrier = Barrier(2)
    popen = RecordingPopen()
    managers = [ServiceProcessManager(authenticate=_ready_authenticate, working_directory=lambda: str(tmp_path), state_dir=tmp_path, popen=popen, pid_exists=lambda pid: True,
                                      **_identity()) for _ in range(2)]
    for manager in managers:
        original = manager._validate_command
        first = [True]

        def validate(command, original=original, first=first):
            result = original(command)
            if first[0]:
                first[0] = False
                barrier.wait(timeout=5)
            return result

        monkeypatch.setattr(manager, "_validate_command", validate)
    publications = []
    atomic_write = lifecycle.atomic_write

    def publish(path, *args, **kwargs):
        if path == managers[0].pid_path:
            publications.append(path)
        return atomic_write(path, *args, **kwargs)

    monkeypatch.setattr(lifecycle, "atomic_write", publish)
    results, errors = [], []

    def start(manager):
        try:
            results.append(manager.start(command=_command()))
        except Exception as exc:
            errors.append(exc)

    workers = [Thread(target=start, args=(manager,)) for manager in managers]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    assert sorted(result.action for result in results) == ["already_running", "start"]
    assert all(result.success for result in results)
    assert len(popen.calls) == len(publications) == 1


def test_service_start_guard_timeout_never_spawns(tmp_path, windows_platform, monkeypatch):
    from threading import Event, Thread

    held, release = Event(), Event()
    manager = ServiceProcessManager(state_dir=tmp_path, popen=RecordingPopen())
    monkeypatch.setattr(lifecycle, "SERVICE_START_GUARD_SECONDS", 0.05)

    def hold():
        with lifecycle._service_start_guard(manager.pid_path):
            held.set()
            release.wait(timeout=5)

    worker = Thread(target=hold)
    worker.start()
    try:
        assert held.wait(timeout=5)
        result = manager.start(command=_command())
        assert not result.success
        assert result.message == "Service start guard timed out."
        assert manager._popen.calls == []
        assert not manager.pid_path.exists()
    finally:
        release.set()
        worker.join(timeout=5)


def test_service_start_guard_unavailable_never_spawns(tmp_path, windows_platform, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def unavailable(_pid_path):
        raise lifecycle._ServiceStartGuardError("Service start guard is unavailable.")
        yield  # pragma: no cover

    manager = ServiceProcessManager(state_dir=tmp_path, popen=RecordingPopen())
    monkeypatch.setattr(lifecycle, "_service_start_guard", unavailable)
    result = manager.start(command=_command())
    assert not result.success
    assert result.message == "Service start guard is unavailable."
    assert manager._popen.calls == []
    assert not manager.pid_path.exists()


def test_service_early_returns_do_not_probe_jobs_or_acquire_guard(tmp_path, windows_platform, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("dry run must not inspect containment or acquire the guard")

    monkeypatch.setattr("modelquad.jobs.handle._launch_policy", forbidden)
    monkeypatch.setattr(lifecycle, "_service_start_guard", forbidden)
    manager = ServiceProcessManager(
        state_dir=tmp_path, popen=RecordingPopen(), pid_exists=lambda pid: True, **_identity(created=None)
    )
    result = manager.start(command=_command(), dry_run=True)
    assert result.action == "would_start" and result.contained is None and result.ready is None
    _write_pidfile(tmp_path)
    result = manager.start(command=_command(), dry_run=True)
    assert result.action == "would_start" and result.contained is None and result.ready is None
    assert result.dry_run is True
    assert manager._popen.calls == []


@pytest.mark.parametrize("contained", [None, "job_shared", "job_kill_on_close", "job_breakaway_ineffective", "containment_unknown"])
def test_cli_service_start_reports_containment_only_when_present(tmp_path, monkeypatch, capsys, contained):
    result = ProcessResult(True, True, True, 2468, tmp_path / "service.log", "started", "start", contained=contained)
    monkeypatch.setattr(cli_module.ServiceProcessManager, "start", lambda *args, **kwargs: result)
    assert cli_module.main(["service", "start"]) == 0
    lines = capsys.readouterr().out.splitlines()
    confirmed = (
        "The background service is running inside the program that started it. "
        "Work you delegate may run inside your chat instead, and may stop when the chat closes. "
        "To fix this, stop the service and start it again from a terminal of its own."
    )
    unknown = "ModelQuad could not check whether the background service runs on its own."
    assert (confirmed in lines) is (contained is not None and contained != "containment_unknown")
    assert (unknown in lines) is (contained == "containment_unknown")


def test_process_start_refuses_to_spawn_when_the_marked_service_is_already_live(tmp_path, windows_platform):
    _write_pidfile(tmp_path)
    popen = RecordingPopen()
    manager = ServiceProcessManager(authenticate=_ready_authenticate, working_directory=lambda: str(tmp_path), 
        state_dir=tmp_path, popen=popen, pid_exists=lambda pid: pid == 2468, **_identity()
    )

    result = manager.start(command=_command())

    assert result.success is True
    assert result.action == "already_running"
    assert result.pid == 2468
    assert popen.calls == []


def test_process_start_discards_raw_output_without_touching_legacy_log(tmp_path, windows_platform):
    popen = RecordingPopen()
    manager = ServiceProcessManager(authenticate=_ready_authenticate, working_directory=lambda: str(tmp_path),
        process_created_at=lambda pid: _STARTED_EPOCH - 0.2, state_dir=tmp_path,
        popen=popen, pid_exists=lambda pid: pid == 2468)
    legacy_log = tmp_path / 'logs' / 'service.log'
    legacy_log.parent.mkdir(parents=True)
    legacy_log.write_text('legacy private canary', encoding='utf-8')
    result = manager.start(command=_command())
    assert result.success is True
    assert legacy_log.read_text(encoding='utf-8') == 'legacy private canary'
    assert popen.calls[0]['kwargs']['stdout'] == subprocess.DEVNULL
    assert popen.calls[0]['kwargs']['stderr'] == subprocess.DEVNULL


def test_process_status_does_not_treat_a_stale_pidfile_as_running(tmp_path, windows_platform):
    _write_pidfile(tmp_path)
    manager = ServiceProcessManager(state_dir=tmp_path, pid_exists=lambda pid: False)

    status = manager.status()

    assert status.running is False
    assert status.pid is None
    assert "stale" in (status.message or "").lower()


def test_process_exists_probe_is_nonlethal_and_correct():
    """The default PID liveness probe must never kill what it checks.

    On Windows, os.kill(pid, 0) is NOT a probe -- CPython routes any
    non-CTRL signal to TerminateProcess, so the old implementation killed a
    live service the first time stop() checked it (B1 evidence 2026-08-23).
    Probing our own PID is the regression guard: if the probe is lethal,
    this very test process dies here.
    """

    import sys as _sys

    assert lifecycle._process_exists(os.getpid()) is True
    assert lifecycle._process_exists(0) is False
    child = subprocess.Popen([_sys.executable, "-c", "pass"], **no_window_kwargs())
    child.wait()
    # The Popen object still holds a handle, so the PID is openable -- an
    # exited process must still read as dead, not live.
    assert lifecycle._process_exists(child.pid) is False


def test_process_stop_uses_the_pid_scoped_terminator_and_removes_pidfile(tmp_path, windows_platform):
    _write_pidfile(tmp_path)
    run = RecordingRun()
    terminated: list[int] = []
    manager = ServiceProcessManager(
        state_dir=tmp_path,
        run=run,
        pid_exists=lambda pid: pid == 2468,
        terminate_verified=lambda pid, **kwargs: terminated.append(pid) or "terminated",
        **_identity(),
    )

    result = manager.stop()

    assert result.success is True
    assert result.action == "stop"
    assert result.message == "Stopped the control-plane service."
    assert result.running is False
    assert terminated == [2468]
    assert run.calls == []
    assert not manager.pid_path.exists()


# ---------------------------------------------------------------------------
# stop() and status() prove that the recorded PID is the
# recorded service before acting on it. Every ModelQuad process is an
# `modelquad.exe`, a Host's MCP server included, so an image *name* proves
# nothing once Windows hands a dead service's PID to another process.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "offset",
    [-lifecycle.SERVICE_IDENTITY_TOLERANCE_SECONDS + 0.1, 0.0, lifecycle.SERVICE_IDENTITY_TOLERANCE_SECONDS - 0.1],
)
def test_process_stop_stops_a_process_whose_identity_matches_within_the_tolerance(
    tmp_path, windows_platform, offset
):
    _write_pidfile(tmp_path)
    terminated: list[int] = []
    manager = ServiceProcessManager(
        state_dir=tmp_path,
        pid_exists=lambda pid: pid == 2468,
        terminate_verified=lambda pid, **kwargs: terminated.append(pid) or "terminated",
        # Another spelling of the recorded argv[0]: case and a `.` segment.
        **_identity(image=r"c:\PROGRAM FILES\Python\.\python.exe", created=_STARTED_EPOCH + offset),
    )

    result = manager.stop()

    assert result.success is True
    assert result.action == "stop"
    assert terminated == [2468]
    assert not manager.pid_path.exists()


@pytest.mark.parametrize(
    ("image", "created"),
    [
        # The Issue's case: the same image, created after the service started.
        (_command()[0], _STARTED_EPOCH + 3600.0),
        # Created before the recorded start: an older process, not the service.
        (_command()[0], _STARTED_EPOCH - 3600.0),
        # Another image, whatever its creation time.
        (r"C:\Program Files\ModelQuad\modelquad.exe", _STARTED_EPOCH),
        # Another image is proof enough when the creation time is unreadable.
        (r"C:\Program Files\ModelQuad\modelquad.exe", None),
        # A later creation time is proof enough when the image is unreadable.
        (None, _STARTED_EPOCH + 3600.0),
    ],
)
def test_process_stop_treats_a_reused_pid_as_a_stale_record_and_terminates_nothing(
    tmp_path, windows_platform, image, created
):
    _write_pidfile(tmp_path)
    terminated: list[int] = []
    manager = ServiceProcessManager(
        state_dir=tmp_path,
        pid_exists=lambda pid: pid == 2468,
        terminate_verified=lambda pid, **kwargs: terminated.append(pid) or "terminated",
        **_identity(image=image, created=created),
    )

    result = manager.stop()

    assert terminated == []
    assert result.success is True
    assert result.action == "not_running"
    assert result.running is False
    assert "another process" in result.message
    assert "stopped nothing" in result.message
    assert not manager.pid_path.exists()


def test_process_stop_dry_run_reports_a_reused_pid_as_stale_without_touching_it(tmp_path, windows_platform):
    _write_pidfile(tmp_path)
    terminated: list[int] = []
    manager = ServiceProcessManager(
        state_dir=tmp_path,
        pid_exists=lambda pid: pid == 2468,
        terminate_verified=lambda pid, **kwargs: terminated.append(pid) or "terminated",
        **_identity(created=_STARTED_EPOCH + 3600.0),
    )

    result = manager.stop(dry_run=True)

    assert result.success is True
    assert result.action == "would_remove_stale_pidfile"
    assert terminated == []
    assert manager.pid_path.exists()


@pytest.mark.parametrize(
    ("image", "created", "started_at", "unreadable"),
    [
        (None, _STARTED_EPOCH, _STARTED_AT, "its image path"),
        (_command()[0], None, _STARTED_AT, "its creation time"),
        (None, None, _STARTED_AT, "its image path or its creation time"),
        (_command()[0], _STARTED_EPOCH, "not a timestamp", "the pidfile start time"),
        (_command()[0], _STARTED_EPOCH, "2026-08-16T00:00:00", "the pidfile start time"),
    ],
)
def test_process_stop_refuses_and_terminates_nothing_when_identity_cannot_be_proven(
    tmp_path, windows_platform, image, created, started_at, unreadable
):
    _write_pidfile(tmp_path, started_at=started_at)
    terminated: list[int] = []
    manager = ServiceProcessManager(
        state_dir=tmp_path,
        pid_exists=lambda pid: pid == 2468,
        terminate_verified=lambda pid, **kwargs: terminated.append(pid) or "terminated",
        **_identity(image=image, created=created),
    )

    for dry_run in (True, False):
        result = manager.stop(dry_run=dry_run)

        assert terminated == []
        # The refusal shape the install handover reads as "blocked".
        assert result.success is False
        assert result.action == "stop"
        assert result.pid == 2468
        assert f"could not read {unreadable}" in result.message
        assert "Nothing was stopped." in result.message
        assert manager.pid_path.exists()


def test_process_status_reports_a_reused_pid_as_not_running_and_keeps_the_pidfile(tmp_path, windows_platform):
    _write_pidfile(tmp_path)
    manager = ServiceProcessManager(
        state_dir=tmp_path, pid_exists=lambda pid: pid == 2468, **_identity(created=_STARTED_EPOCH + 3600.0)
    )

    status = manager.status()

    assert status.running is False
    assert status.pid is None
    assert "another process" in (status.message or "")
    assert manager.pid_path.exists()


def test_process_status_reports_a_proven_service_as_running(tmp_path, windows_platform):
    _write_pidfile(tmp_path)
    manager = ServiceProcessManager(authenticate=_ready_authenticate, working_directory=lambda: str(tmp_path), state_dir=tmp_path, pid_exists=lambda pid: pid == 2468, **_identity())

    status = manager.status()

    assert status.running is True
    assert status.pid == 2468
    assert status.message == "Service is running."


def test_process_status_keeps_an_unproven_identity_running_so_start_cannot_launch_a_second(
    tmp_path, windows_platform
):
    _write_pidfile(tmp_path)
    popen = RecordingPopen()
    manager = ServiceProcessManager(
        state_dir=tmp_path, popen=popen, pid_exists=lambda pid: pid == 2468, **_identity(created=None)
    )

    status = manager.status()

    assert status.running is True
    assert "unconfirmed" in (status.message or "")
    refused = manager.start(command=_command())
    assert refused.success is False and refused.reason == "service_process_identity_unconfirmed"
    assert popen.calls == []


@pytest.mark.skipif(os.name != "nt", reason="exercises the real Windows identity probes")
def test_process_stop_leaves_a_live_process_that_reused_the_recorded_pid_running(tmp_path):
    """The recycled-PID regression, on real processes and the default probes.

    The child stands in for a Host's MCP server that inherited a dead
    service's PID: the same image as the recorded ``argv[0]``, created an hour
    after the pidfile's ``started_at``. The image name alone matched it, so
    ``stop()`` used to terminate it.
    """

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **no_window_kwargs())
    try:
        an_hour_earlier = datetime.fromtimestamp(time.time() - 3600.0, timezone.utc).isoformat()
        _write_pidfile(tmp_path, pid=child.pid, argv=[sys.executable, "-c", "service"], started_at=an_hour_earlier)
        manager = ServiceProcessManager(state_dir=tmp_path)

        assert manager.status().running is False
        result = manager.stop()

        assert result.success is True
        assert result.action == "not_running"
        assert not manager.pid_path.exists()
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=1)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_process_stop_reports_a_terminator_failure_without_raw_windows_output(tmp_path, windows_platform):
    _write_pidfile(tmp_path)
    manager = ServiceProcessManager(
        state_dir=tmp_path,
        pid_exists=lambda pid: pid == 2468,
        terminate_verified=lambda pid, **kwargs: "unconfirmed",
        **_identity(),
    )

    result = manager.stop()

    assert result.success is False
    assert "could not stop" in result.message.lower()
    assert "user name or password" not in result.message.lower()
    assert manager.pid_path.exists()


def test_process_stop_treats_a_process_that_already_exited_as_stopped(tmp_path, windows_platform):
    """Losing the race to a self-exiting service is a success, not a failure.

    ``taskkill`` reported this as its own "process not found" exit code, which
    stop() has always accepted. The PID-scoped terminator cannot open a handle
    to a dead process, so the same case arrives as a False return and has to be
    distinguished from a process that is genuinely still running.
    """

    _write_pidfile(tmp_path)
    manager = ServiceProcessManager(
        state_dir=tmp_path,
        pid_exists=lambda pid: False,
        terminate_pid=lambda pid: False,
        **_identity(),
    )

    result = manager.stop()

    assert result.success is True
    assert result.running is False
    assert not manager.pid_path.exists()


def test_task_scheduler_timeout_becomes_a_truthful_failure(tmp_path, windows_platform):
    run = RecordingRun(exc=subprocess.TimeoutExpired(["schtasks"], 10.0))

    result = TaskSchedulerRegistrar(run=run).register(command=_command())

    assert result.success is False
    assert result.registered is None
    assert "did not respond before the deadline" in result.message.lower()
    assert "outcome is unknown" in result.message.lower()
    assert run.calls[0]["kwargs"]["stdin"] is subprocess.DEVNULL
    assert run.calls[0]["kwargs"]["timeout"] == lifecycle.WINDOWS_COMMAND_TIMEOUT_SECONDS


@pytest.mark.skipif(os.name != "nt", reason="exercises the Windows PID terminator")
def test_process_stop_terminates_an_owned_service_and_releases_its_port(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import socket, time; s = socket.socket(); s.bind(('127.0.0.1', "
                + str(port)
                + ")); s.listen(); time.sleep(60)"
            ),
        ],
        **no_window_kwargs(),
    )
    # Stamped after Popen returns, as `start` does, and proven by the real
    # identity probes: this is the matching case on real processes.
    _write_pidfile(
        tmp_path,
        pid=child.pid,
        argv=[sys.executable, "-c", "service"],
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    manager = ServiceProcessManager(
        state_dir=tmp_path,
        pid_exists=lambda pid: pid == child.pid and child.poll() is None,
    )
    try:
        result = manager.stop()
        assert result.success is True
        assert not manager.pid_path.exists()
        for _ in range(40):
            if child.poll() is not None:
                break
            time.sleep(0.05)
        assert child.poll() is not None
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
        finally:
            probe.close()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)


def test_process_stop_dry_run_never_kills_or_removes_pidfile(tmp_path, windows_platform):
    _write_pidfile(tmp_path)
    run = RecordingRun()
    terminated: list[int] = []
    manager = ServiceProcessManager(
        state_dir=tmp_path,
        run=run,
        pid_exists=lambda pid: pid == 2468,
        terminate_verified=lambda pid, **kwargs: terminated.append(pid) or "terminated",
        **_identity(),
    )

    result = manager.stop(dry_run=True)

    assert result.success is True
    assert result.action == "would_stop"
    assert run.calls == []
    assert terminated == []
    assert manager.pid_path.exists()


def test_process_stop_cleans_a_stale_pidfile_without_invoking_taskkill(tmp_path, windows_platform):
    _write_pidfile(tmp_path)
    run = RecordingRun()
    manager = ServiceProcessManager(state_dir=tmp_path, run=run, pid_exists=lambda pid: False)

    result = manager.stop()

    assert result.success is True
    assert result.action == "not_running"
    assert run.calls == []
    assert not manager.pid_path.exists()


def test_process_stop_refuses_to_kill_a_pidfile_without_our_identifying_marker(tmp_path, windows_platform):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "service.pid").write_text(json.dumps({"pid": 2468}), encoding="utf-8")
    run = RecordingRun()
    manager = ServiceProcessManager(state_dir=tmp_path, run=run, pid_exists=lambda pid: True)

    result = manager.stop()

    assert result.success is False
    assert "refused" in result.message.lower()
    assert run.calls == []


def test_process_lifecycle_is_explicitly_unsupported_without_spawning_or_killing(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle.sys, "platform", "linux")
    run = RecordingRun()
    popen = RecordingPopen()
    manager = ServiceProcessManager(state_dir=tmp_path, run=run, popen=popen)

    assert manager.start(command=_command()).supported is False
    assert manager.stop().supported is False
    assert manager.status().supported is False
    assert run.calls == []
    assert popen.calls == []


def test_cli_service_install_registers_the_windowless_localhost_launch_command(monkeypatch, capsys, tmp_path):
    """What gets registered is the autostart command, not the spawn command.

    Task Scheduler owns no console, so a console-subsystem action
    gets its own console window for the life of the service. `service start`
    still uses `_service_launch_command` -- ModelQuad spawns that one itself
    with CREATE_NO_WINDOW -- and the two must not be confused again.
    """

    captured: dict[str, object] = {}

    class FakeRegistrar:
        def register(self, *, command, dry_run=False):
            captured["command"] = command
            captured["dry_run"] = dry_run
            return RegistrationResult(True, True, True, "registered", "register", dry_run=dry_run)

    class FakeManager:
        pass

    monkeypatch.setattr(cli_module, "TaskSchedulerRegistrar", FakeRegistrar)
    monkeypatch.setattr(cli_module, "ServiceProcessManager", FakeManager)
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    (tmp_path / "python.exe").chmod(0o700)
    (tmp_path / "pythonw.exe").chmod(0o700)
    monkeypatch.setattr(cli_module.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(cli_module.sys, "_base_executable", str(tmp_path / "python.exe"), raising=False)

    assert cli_module.main(["service", "install", "--dry-run", "--port", "9123"]) == 0

    from modelquad.statecontext import resolve_state_context
    context = resolve_state_context()
    assert captured == {
        "command": [str(tmp_path / "pythonw.exe"), "-I",
                    str(Path(cli_module.__file__).resolve().parent / "service" / "entry.py"),
                    "service", "start", "--supervise", "--port", "9123",
                    "--db", str(cli_module.service_db.DEFAULT_DB_PATH.expanduser().resolve()),
                    "--poll-interval", "60.0", "--state-root", str(context.state_root.expanduser().resolve()),
                    "--state-mode", context.mode.value],
        "dry_run": True,
    }
    assert "Auto-start task: registered" in capsys.readouterr().out


def test_cli_service_start_still_spawns_the_console_interpreter_itself(monkeypatch, capsys, tmp_path):
    """The windowless launcher is for Windows' launches, not ModelQuad's own.

    `ServiceProcessManager.start` passes CREATE_NO_WINDOW and matches the
    pidfile's recorded image, so routing it through a second executable would
    change what `stop` is allowed to kill for no gain.
    """

    captured: dict[str, object] = {}

    class FakeRegistrar:
        pass

    class FakeManager:
        def start(self, *, command, dry_run=False):
            captured["command"] = command
            return ProcessResult(
                success=True,
                supported=True,
                running=False,
                pid=None,
                message="would start",
                action="would_start",
                dry_run=dry_run,
            )

    monkeypatch.setattr(cli_module, "TaskSchedulerRegistrar", FakeRegistrar)
    monkeypatch.setattr(cli_module, "ServiceProcessManager", FakeManager)
    monkeypatch.setattr(cli_module, "_render_process_result", lambda result: None)
    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    (tmp_path / "python.exe").write_bytes(b"")
    (tmp_path / "pythonw.exe").write_bytes(b"")
    (tmp_path / "python.exe").chmod(0o700)
    (tmp_path / "pythonw.exe").chmod(0o700)
    monkeypatch.setattr(cli_module.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.setattr(cli_module.sys, "_base_executable", str(tmp_path / "python.exe"), raising=False)

    cli_module.main(["service", "start", "--dry-run", "--port", "9123"])

    assert captured["command"][0] == str(tmp_path / "python.exe")


def test_cli_service_status_combines_fake_scheduler_and_process_status_as_json(monkeypatch, capsys, tmp_path):
    class FakeRegistrar:
        def is_registered(self):
            return RegistrationResult(True, True, False, "not registered", "status")

    class FakeManager:
        def status(self):
            return ProcessStatus(
                running=True,
                pid=2468,
                started_at="2026-08-16T00:00:00+00:00",
                argv=("python", "-m", "modelquad.cli"),
            )

    monkeypatch.setattr(cli_module, "TaskSchedulerRegistrar", FakeRegistrar)
    monkeypatch.setattr(cli_module, "ServiceProcessManager", FakeManager)

    assert cli_module.main(["service", "status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["auto_start"]["registered"] is False
    assert payload["process"]["running"] is True
    assert payload["process"]["pid"] == 2468


def test_process_manager_defaults_state_dir_to_installed_state_root(monkeypatch):
    # With no explicit state_dir, the pidfile/log root follows the installation
    # mode's state_root() so an installed package keeps them beside the history DB.
    from modelquad import paths

    sentinel = paths.Path("/tmp/example-state-root")
    monkeypatch.setattr(paths, "state_root", lambda: sentinel)

    manager = ServiceProcessManager()
    assert manager.state_dir == sentinel
    assert manager.pid_path == sentinel / "service.pid"


def test_process_manager_default_state_dir_installed_windows_uses_localappdata(monkeypatch, tmp_path):
    from modelquad import paths

    monkeypatch.setattr(paths, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    manager = ServiceProcessManager()
    expected = tmp_path / "ModelQuad"
    assert manager.state_dir == expected
    assert manager.pid_path == expected / "service.pid"


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW only applies on Windows")
def test_lifecycle_probes_pass_create_no_window_on_windows(windows_platform, tmp_path):
    """Runs for real on Windows rather than simulating it: monkeypatching the
    global ``os.name`` would break pathlib (PosixPath on Windows)."""

    _write_pidfile(tmp_path)

    # 1. TaskSchedulerRegistrar.is_registered
    run_status = RecordingRun()
    TaskSchedulerRegistrar(run=run_status).is_registered()
    assert run_status.calls[0]["kwargs"]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    # 2. TaskSchedulerRegistrar.register
    run_register = RecordingRun()
    TaskSchedulerRegistrar(run=run_register).register(command=_command())
    assert run_register.calls[0]["kwargs"]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    # 3. TaskSchedulerRegistrar.unregister
    run_unregister = RecordingRun()
    TaskSchedulerRegistrar(run=run_unregister).unregister(command=_command())
    assert run_unregister.calls[0]["kwargs"]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)



def test_lifecycle_probes_omit_creationflags_on_non_windows(windows_platform, monkeypatch, tmp_path):
    """A posix helper result must reach every probe as *no* kwarg at all.
    The helper is patched in the lifecycle module rather than patching the
    global ``os.name``, which would break pathlib on Windows."""

    monkeypatch.setattr(lifecycle, "no_window_kwargs", lambda: no_window_kwargs("posix"))
    _write_pidfile(tmp_path)

    # 1. TaskSchedulerRegistrar.is_registered
    run_status = RecordingRun()
    TaskSchedulerRegistrar(run=run_status).is_registered()
    assert "creationflags" not in run_status.calls[0]["kwargs"]

    # 2. TaskSchedulerRegistrar.register
    run_register = RecordingRun()
    TaskSchedulerRegistrar(run=run_register).register(command=_command())
    assert "creationflags" not in run_register.calls[0]["kwargs"]

    # 3. TaskSchedulerRegistrar.unregister
    run_unregister = RecordingRun()
    TaskSchedulerRegistrar(run=run_unregister).unregister(command=_command())
    assert "creationflags" not in run_unregister.calls[0]["kwargs"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects only")
def test_service_real_breakaway_in_separate_helper(tmp_path):
    """Only the helper joins a test-owned job; pytest's job is never changed."""
    import textwrap
    from modelquad.jobs import handle

    in_job = handle._caller_in_job()
    limits = handle._caller_job_limits() if in_job else 0
    flag, escape_expected, limitation = handle._launch_policy()
    evidence = f"pytest in_job={in_job}, measured limits=0x{limits:x}"
    if in_job and not escape_expected:
        pytest.skip(f"{evidence}; helper cannot escape the test runner's job")
    output = tmp_path / "observation.json"
    code = textwrap.dedent(r"""
        import ctypes,json,os,subprocess,sys
        from ctypes import wintypes
        from pathlib import Path
        from modelquad import procutil
        from modelquad.jobs import handle
        from modelquad.service.lifecycle import ServiceProcessManager

        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', wintypes.DWORD), ('min_ws', ctypes.c_size_t),
                        ('max_ws', ctypes.c_size_t), ('active', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    wintypes.LPVOID, wintypes.DWORD]
        children = []
        job = None
        result = {}
        try:
            if handle._caller_in_job():
                result = {'skip': 'helper remained in enclosing job', 'flags': handle._caller_job_limits()}
            else:
                job = procutil._create_census_job()
                info = Extended()
                info.basic.flags = 0x800
                assert job and kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
                assert procutil._assign_to_census_job(job, os.getpid())
                assert handle._caller_job_limits() == 0x800
                def spawn(*args, **kwargs):
                    child = subprocess.Popen(*args, **kwargs)
                    children.append(child)
                    return child
                from types import SimpleNamespace
                from modelquad.service.socket_identity import ListenerError
                def readiness(port, **kwargs):
                    if 'expected_pid' not in kwargs:
                        raise ListenerError('service_absent')
                    return SimpleNamespace(close=lambda: None)
                manager = ServiceProcessManager(state_dir=Path(sys.argv[1]) / 'state', popen=spawn,
                                                authenticate=readiness)
                started = manager.start(command=[sys.executable, '-c', 'import time; time.sleep(30)'])
                assert started.success and started.contained is None, started
                result = {'flags': handle._caller_job_limits(), 'contained': started.contained,
                          'in_any_job': handle._process_in_any_job(children[0]._handle),
                          'in_own_job': procutil._process_in_job(children[0].pid, job),
                          'pidfile': manager.pid_path.is_file()}
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
                child._handle.Close()
            if job:
                procutil._close_census_job(job)
        Path(sys.argv[2]).write_text(json.dumps(result), encoding='utf-8')
    """)
    kwargs = no_window_kwargs()
    kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | 0x8 | (flag or 0)
    parent = None
    try:
        try:
            # The base interpreter (not the venv launcher, whose redirector
            # would sit in the job) sees exactly this run's import path, so it
            # finds the package in source, editable and installed modes alike.
            import os
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p and os.path.isdir(p)))
            parent = subprocess.Popen(
                [getattr(sys, "_base_executable", sys.executable), "-c", code, str(tmp_path), str(output)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, env=env, **kwargs,
            )
        except OSError as exc:
            if getattr(exc, "winerror", None) == 5:
                pytest.skip(f"{evidence}; enclosing job denied helper breakaway")
            raise
        stdout, stderr = parent.communicate(timeout=20)
        assert parent.returncode == 0, stderr.decode(errors="replace")
        observed = json.loads(output.read_text(encoding="utf-8"))
        if "skip" in observed:
            pytest.skip(f"{evidence}; {observed}")
        assert observed == {"flags": 0x800, "contained": None, "in_any_job": False,
                            "in_own_job": False, "pidfile": True}
    finally:
        if parent is not None:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            parent._handle.Close()
