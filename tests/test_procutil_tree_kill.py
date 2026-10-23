# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A timed-out worker must actually STOP.

subprocess.run's timeout kills only the direct child; on Windows every
provider CLI is a .cmd shim (cmd.exe -> node.exe), so the real worker
survived a timeout and kept mutating the workspace unobserved (seen live in
a live bootstrap run). run_with_tree_kill is the fix; these tests
prove the tree dies and that executors default to it.

`taskkill /T` walks parent pids, so it missed a grandchild whose
parent had already exited and a child born while the kill ran -- and exited 0
either way, so the record said `tree_kill: ok`. The worker now runs inside a
Job Object from its first instruction, a stop terminates the job, and `ok` is
read from the job census afterwards. Every dummy process below expires on its
own within `_SHAPE_LIFETIME` seconds (the idle pipe fixture uses
`_DUMMY_LIFETIME`), so a failing test cannot leave anything
running for long, and nothing here ever stops a process by image name. The
one exception is the same-image sentinel: it lives until its test lets go of
it (at most `_SENTINEL_LIFETIME` seconds), and a test that dies lets go.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import no_window_kwargs
from modelquad import procutil
from modelquad.execution.base import describe_worker_termination, worker_confirmed_dead
from modelquad.procutil import WorkerContainmentError, run_with_tree_kill

_PARENT_SCRIPT = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(sys.argv[1], "w") as f:
    f.write(str(child.pid))
time.sleep(120)
"""

#: The idle pipe fixture's own-exit contract stays at 20 seconds.
_DUMMY_LIFETIME = 20

# Fixture startup needs slack on a shared runner before the stop clock begins.
LOAD_HEADROOM_S = 60
_SHAPE_LIFETIME = 3 * LOAD_HEADROOM_S

#: The sentinel must outlive the whole test, however loaded the machine is:
#: at 20 s it expired on its own under `check.py all` (exit code 0, where
#: every stop here uses exit code 1) and read as "stopped". It now waits on
#: a pipe its test holds, so it ends when the test lets go of it -- at
#: teardown, or when the test process dies -- and this is only a backstop.
_SENTINEL_LIFETIME = 600

_SENTINEL_SCRIPT = (
    "import os, sys, threading\n"
    f"threading.Timer({_SENTINEL_LIFETIME}, os._exit, (0,)).start()\n"
    "sys.stdin.buffer.read()\n"
    "os._exit(0)\n"
)

#: One script, several shapes, chosen by the first argument. A leaf appends
#: an increasing counter to its own file every 50 ms for as long as it lives.
#: Changed content proves life; Windows can update mtime after termination
#: when a file handle closes, and a recycled pid cannot fake these writes.
_DUMMY_SCRIPT = f"""
import os, subprocess, sys, time

role, folder = sys.argv[1], sys.argv[2]
ends = time.monotonic() + ({_DUMMY_LIFETIME} if role == "idle" else {_SHAPE_LIFETIME})

def spawn(next_role):
    return subprocess.Popen(
        [sys.executable, __file__, next_role, folder, *sys.argv[3:]],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

def own_file(kind):
    return os.path.join(folder, kind + "-" + str(os.getpid()))

if role == "leaf":
    path = own_file("leaf")
    counter = 0
    while time.monotonic() < ends:
        with open(path, "a", encoding="utf-8") as leaf:
            leaf.write(str(counter) + "\\n")
        counter += 1
        time.sleep(0.05)
elif role == "middle":
    spawn("leaf")  # and exit at once: the leaf is orphaned
elif role == "orphaner":
    spawn("middle").wait(timeout={LOAD_HEADROOM_S})
    open(os.path.join(folder, "middle-exited"), "w").close()
    while time.monotonic() < ends:
        time.sleep(0.05)
elif role == "spawner":
    for _ in range(3):
        spawn("leaf")
    # The test opens this gate inside the stop, after observing three live leaves.
    while not os.path.exists(os.path.join(folder, "spawn-during-stop")) and time.monotonic() < ends:
        time.sleep(0.05)
    for _ in range(57):  # bounded: at most 60 children
        if time.monotonic() >= ends:
            break
        spawn("leaf")
        time.sleep(0.05)
    while time.monotonic() < ends:
        time.sleep(0.05)
elif role == "parent":
    # The pid the worker itself created: under a virtual environment that is
    # the venv launcher, and the leaf that writes its own file is its child.
    open(os.path.join(folder, "child-" + str(spawn("leaf").pid)), "w").close()
elif role in ("alias", "alias-parent"):
    # A Store App Execution Alias: Windows starts the target outside the
    # worker's job, with the worker as its parent pid.
    alias = subprocess.Popen(
        [sys.argv[3], "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds {_SHAPE_LIFETIME}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    open(os.path.join(folder, "alias-" + str(alias.pid)), "w").close()
    while role == "alias" and time.monotonic() < ends:
        time.sleep(0.05)
elif role == "alias-via-middle":
    # A launcher that starts the alias target and returns: nothing the stop
    # can hold names the target as its child any more.
    spawn("alias-parent").wait(timeout={LOAD_HEADROOM_S})
    open(os.path.join(folder, "alias-launcher-exited"), "w").close()
    while time.monotonic() < ends:
        time.sleep(0.05)
elif role == "marker":
    open(own_file("marker"), "w").close()
elif role == "idle":
    # Never reads stdin, and exits on its own.
    while time.monotonic() < ends:
        time.sleep(0.05)
elif role == "stdin-sharer":
    # Starts a leaf that inherits this worker's stdin pipe, and exits at once.
    leaf = subprocess.Popen(
        [sys.executable, __file__, "leaf", folder],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    open(os.path.join(folder, "child-" + str(leaf.pid)), "w").close()
else:
    raise SystemExit("unknown role " + role)
"""

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the Job Object is Windows-only")


def _pid_alive(pid: int) -> bool:
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


@pytest.mark.skipif(sys.platform != "win32", reason="the orphaned-tree defect is Windows-specific")
@pytest.mark.serial_timing
def test_timeout_kills_the_whole_process_tree(tmp_path):
    script = tmp_path / "parent.py"
    script.write_text(_PARENT_SCRIPT, encoding="utf-8")
    pidfile = tmp_path / "grandchild.pid"

    with pytest.raises(subprocess.TimeoutExpired):
        run_with_tree_kill(
            [sys.executable, str(script), str(pidfile)],
            capture_output=True,
            text=True,
            timeout=6,
        )

    # The parent had time to record its child's pid before the timeout.
    grandchild_pid = int(pidfile.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _pid_alive(grandchild_pid):
            break
        time.sleep(0.25)
    assert not _pid_alive(grandchild_pid), (
        f"grandchild {grandchild_pid} survived the tree kill -- a timed-out "
        "worker would keep running unobserved"
    )


def test_normal_completion_matches_subprocess_run_shape():
    completed = run_with_tree_kill(
        [sys.executable, "-c", "print('hi')"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0
    assert "hi" in completed.stdout
    assert completed.stderr == ""


def test_stdin_prompt_is_delivered():
    completed = run_with_tree_kill(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
        input="prompt text",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "PROMPT TEXT" in completed.stdout


def test_a_large_stdin_prompt_is_delivered_whole():
    """1 MiB, far past a pipe buffer: the writer thread delivers all of it."""

    prompt = "".join(chr(ord("a") + i % 26) for i in range(1024 * 1024))
    completed = run_with_tree_kill(
        [sys.executable, "-c", "import hashlib, sys; d = sys.stdin.read(); print(len(d), hashlib.sha256(d.encode()).hexdigest())"],
        input=prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    import hashlib

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == [str(len(prompt)), hashlib.sha256(prompt.encode()).hexdigest()]


def test_on_spawn_reports_the_live_worker_pid():
    from modelquad.procutil import pid_alive

    seen = []
    completed = run_with_tree_kill(
        [sys.executable, "-c", "print('ok')"],
        capture_output=True,
        text=True,
        timeout=60,
        on_spawn=lambda pid: seen.append((pid, pid_alive(pid))),
    )
    assert completed.returncode == 0
    assert len(seen) == 1
    pid, was_alive_at_spawn = seen[0]
    assert pid > 0
    assert was_alive_at_spawn is True


def test_pid_alive_distinguishes_live_from_dead():
    import os
    import subprocess as sp

    from modelquad.procutil import pid_alive

    assert pid_alive(os.getpid()) is True
    proc = sp.Popen([sys.executable, "-c", "pass"], **no_window_kwargs())
    proc.wait()
    assert pid_alive(proc.pid) is False


def test_executors_default_to_tree_killing_run():
    """The defect returns silently if an executor reverts to subprocess.run."""

    from modelquad.execution.codex import CodexExecutor

    from modelquad.procutil import run_worker

    # run_worker is run_with_tree_kill with kill-on-close on by default.
    assert CodexExecutor()._run is run_worker


# --------------------------------------------------------------------------
# The job holds the whole tree, and only the census says "ok"
# --------------------------------------------------------------------------


@pytest.fixture
def dummy(tmp_path):
    script = tmp_path / "dummy.py"
    script.write_text(_DUMMY_SCRIPT, encoding="utf-8")
    folder = tmp_path / "shape"
    folder.mkdir()
    return script, folder


@pytest.fixture
def sentinel():
    """An unrelated process with the worker's image. No stop may reach it."""

    proc = subprocess.Popen(
        [sys.executable, "-c", _SENTINEL_SCRIPT],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **no_window_kwargs(),
    )
    try:
        yield proc
    finally:
        proc.kill()
        proc.wait(timeout=10)
        proc.stdin.close()


class _Pinned:
    """Exact handles to processes a test observed alive.

    A handle keeps its pid from being recycled, so "is it dead?" and the
    cleanup below always mean THAT process -- never whatever took its number.
    """

    SYNCHRONIZE_QUERY_TERMINATE = 0x00100000 | 0x1000 | 0x0001

    def __init__(self):
        import ctypes

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.OpenProcess.restype = ctypes.c_void_p
        self._kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handles: dict[int, int] = {}

    def pin(self, pid: int) -> None:
        handle = self._kernel32.OpenProcess(self.SYNCHRONIZE_QUERY_TERMINATE, False, pid)
        assert handle, f"could not pin pid {pid}: it must be alive when observed"
        self.handles[pid] = handle

    def exited_within(self, pid: int, seconds: float) -> bool:
        return self._kernel32.WaitForSingleObject(self.handles[pid], int(seconds * 1000)) == 0

    def release(self) -> None:
        for handle in self.handles.values():
            if self._kernel32.WaitForSingleObject(handle, 0) != 0:
                self._kernel32.TerminateProcess(handle, 1)
            self._kernel32.CloseHandle(handle)
        self.handles.clear()


@pytest.fixture
def pinned():
    if sys.platform != "win32":
        pytest.skip("process handles are Windows-only")
    handles = _Pinned()
    try:
        yield handles
    finally:
        handles.release()


def _wait_for(predicate, seconds: float = LOAD_HEADROOM_S, what: str = "the fixture's shape"):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"{what} did not appear within {seconds}s")


def _leaf_pids(folder: Path) -> list[int]:
    return [int(path.name.split("-", 1)[1]) for path in folder.glob("leaf-*")]


def _pin_alias_target(folder, pinned):
    marker = _wait_for(lambda: list(folder.glob("alias-[0-9]*")), what="the alias target")[0]
    pinned.pin(int(marker.name.split("-", 1)[1]))


def _leaf_contents(folder: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in folder.glob("leaf-*")}


def _changed_leaves(folder: Path, before: dict[str, bytes]) -> list[str]:
    """Changed or newly created leaf content proves a process kept writing."""

    after = _leaf_contents(folder)
    return sorted(name for name, content in after.items() if before.get(name) != content)


def test_leaf_content_check_ignores_mtime_and_detects_changed_and_new_leaves(tmp_path):
    import os

    leaf = tmp_path / "leaf-control"
    leaf.write_bytes(b"0\n")
    before = _leaf_contents(tmp_path)
    timestamp_ns = leaf.stat().st_mtime_ns + 1_000_000_000
    os.utime(leaf, ns=(timestamp_ns, timestamp_ns))
    assert _changed_leaves(tmp_path, before) == []

    leaf.write_bytes(b"0\n1\n")
    (tmp_path / "leaf-new").write_bytes(b"0\n")
    assert _changed_leaves(tmp_path, before) == ["leaf-control", "leaf-new"]


@windows_only
def test_leaf_content_check_detects_a_deliberately_unkilled_leaf(dummy, pinned):
    script, folder = dummy
    leaf = subprocess.Popen(
        [sys.executable, str(script), "leaf", str(folder)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **no_window_kwargs(),
    )
    try:
        [leaf_pid] = _wait_for(lambda: _leaf_pids(folder), what="the live control leaf")
        pinned.pin(leaf_pid)
        before = _leaf_contents(folder)
        _wait_for(lambda: _changed_leaves(folder, before), what="the control leaf's next write")
        assert leaf.poll() is None, "the control leaf stopped before the self-check"
        assert _changed_leaves(folder, before) == [f"leaf-{leaf_pid}"]
    finally:
        leaf.kill()
        leaf.wait(timeout=10)


def _run_ready_worker(argv, ready, **kwargs):
    """Start the real timeout only after the test observes and pins its shape."""
    readiness_errors = []

    def on_spawn(pid):
        try:
            ready()
        except Exception as error:
            # on_spawn is advisory and swallows exceptions; surface them after
            # the real timeout path has stopped and drained this worker.
            readiness_errors.append(error)

    try:
        return run_with_tree_kill(argv, on_spawn=on_spawn, **kwargs)
    finally:
        assert not readiness_errors, readiness_errors


def _run_in_background(argv, *, ready=None, **kwargs):
    box: dict = {}

    def target():
        try:
            box["result"] = (run_with_tree_kill(argv, **kwargs) if ready is None else
                             _run_ready_worker(argv, ready, **kwargs))
        except BaseException as exc:  # noqa: BLE001 -- handed to the test to assert on
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


@windows_only
def test_a_timeout_stops_a_grandchild_whose_parent_already_exited(dummy, sentinel, pinned):
    """The shape `taskkill /T` missed while exiting 0.

    The worker starts an intermediate, which starts a leaf and exits, so no
    parent pid links the leaf back to the worker any more. A `.cmd` shim is
    this shape: `cmd.exe` starts `node.exe` and goes.
    """

    script, folder = dummy
    def ready():
        leaves = _wait_for(lambda: (folder / "middle-exited").exists() and _leaf_pids(folder))
        [leaf] = leaves
        pinned.pin(leaf)

    thread, box = _run_in_background(
        [sys.executable, str(script), "orphaner", str(folder)], capture_output=True, text=True, timeout=6,
        ready=ready,
    )
    thread.join(timeout=2 * LOAD_HEADROOM_S)
    assert not thread.is_alive(), box
    [leaf] = pinned.handles
    stopped_contents = _leaf_contents(folder)

    error = box.get("error")
    assert isinstance(error, subprocess.TimeoutExpired), box
    assert pinned.exited_within(leaf, 5), f"orphaned grandchild {leaf} survived the stop"
    time.sleep(1.0)
    assert _changed_leaves(folder, stopped_contents) == []
    assert sentinel.poll() is None, f"an unrelated process with the same image was stopped (exit code {sentinel.returncode})"
    facts = error.worker_termination
    assert facts["tree_kill"] == "ok", facts
    assert facts["job_terminate"] == "ok"
    assert facts["confirmed_dead"] is True
    assert worker_confirmed_dead({"worker_termination": facts}) is True


@windows_only
def test_a_timeout_stops_children_born_while_the_stop_runs(dummy, sentinel, monkeypatch):
    """A worker that keeps spawning: a walk misses whatever is born after it.

    The job does not: a newborn joins it the instant it is created. Success
    is read from the census and then checked independently -- no leaf file
    may change once the stop has returned.
    """

    script, folder = dummy
    terminate = procutil._terminate_job
    born_during_stop = []

    def terminate_after_birth(job):
        before = set(_leaf_pids(folder))
        (folder / "spawn-during-stop").touch()
        try:
            born_during_stop.extend(_wait_for(
                lambda: sorted(set(_leaf_pids(folder)) - before), what="a child born during the stop",
            ))
        finally:
            terminated = terminate(job)  # A failed readiness assertion still stops the fixture.
        return terminated

    monkeypatch.setattr(procutil, '_terminate_job', terminate_after_birth)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        _run_ready_worker(
            [sys.executable, str(script), "spawner", str(folder)],
            lambda: _wait_for(lambda: len(_leaf_pids(folder)) >= 3, what="three live children"),
            capture_output=True, text=True, timeout=3,
        )
    stopped_contents = _leaf_contents(folder)
    assert len(_leaf_pids(folder)) >= 3, "the spawner never got going: the test proved nothing"
    assert born_during_stop, 'no child was observed being born during the stop'

    facts = caught.value.worker_termination
    assert facts["tree_kill"] == "ok", facts
    assert worker_confirmed_dead({"worker_termination": facts}) is True
    time.sleep(1.0)
    survivors = _changed_leaves(folder, stopped_contents)
    assert survivors == [], f"children kept running after the stop: {survivors}"
    assert sentinel.poll() is None, f"an unrelated process with the same image was stopped (exit code {sentinel.returncode})"


@windows_only
def test_a_worker_that_exits_on_its_own_leaves_its_descendants_running(dummy, pinned):
    """Counting must never become killing.

    The job has no kill-on-close, so closing it after a normal exit stops
    nothing, and the result reports the leaf the worker left behind.
    """

    script, folder = dummy
    completed = run_with_tree_kill(
        [sys.executable, str(script), "parent", str(folder)], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0
    [child] = [int(path.name.split("-", 1)[1]) for path in folder.glob("child-*")]
    leaf = _wait_for(lambda: _leaf_pids(folder), what="the leaf")[0]
    pinned.pin(leaf)
    returned_contents = _leaf_contents(folder)
    _wait_for(lambda: _changed_leaves(folder, returned_contents), what="the surviving leaf's next write")
    assert not pinned.exited_within(leaf, 0), "a normal exit stopped the worker's descendant"
    assert _changed_leaves(folder, returned_contents), "the leaf stopped writing its counter"
    listed = {item["pid"] for item in completed.worker_descendants["descendants"]}
    assert completed.worker_descendants["method"] == "job_object"
    assert child in listed, completed.worker_descendants


@windows_only
def test_the_worker_runs_nothing_of_its_own_before_it_is_in_its_job(dummy, monkeypatch):
    """Created suspended, assigned, and only then resumed.

    Assignment is made to wait here; a worker already running would write its
    marker in that time. It does write it once resumed, so the run is real.
    """

    script, folder = dummy
    real_assign = procutil._assign_to_census_job
    at_assignment: list[list[str]] = []

    def slow_assign(handle, pid):
        time.sleep(0.75)
        at_assignment.append(sorted(path.name for path in folder.iterdir()))
        return real_assign(handle, pid)

    monkeypatch.setattr(procutil, "_assign_to_census_job", slow_assign)
    completed = run_with_tree_kill(
        [sys.executable, str(script), "marker", str(folder)], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    assert at_assignment == [[]], "the worker ran before it was inside its job"
    assert list(folder.glob("marker-*")), "the worker never ran after it was resumed"


@windows_only
@pytest.mark.parametrize(
    "broken, replacement",
    [
        ("_create_census_job", lambda: None),
        ("_assign_to_census_job", lambda handle, pid: False),
        ("_resume_suspended_process", lambda pid: False),
    ],
    ids=["no-job", "assignment-refused", "resume-failed"],
)
def test_a_worker_that_cannot_be_contained_never_runs(dummy, monkeypatch, broken, replacement):
    """No uncontained fallback: the process is stopped while still suspended."""

    script, folder = dummy
    created: list[subprocess.Popen] = []
    spawned: list[int] = []

    class Recorded(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(procutil.subprocess, "Popen", Recorded)
    monkeypatch.setattr(procutil, broken, replacement)

    with pytest.raises(WorkerContainmentError) as caught:
        run_with_tree_kill(
            [sys.executable, str(script), "marker", str(folder)],
            capture_output=True, text=True, timeout=60, on_spawn=spawned.append,
        )
    assert isinstance(caught.value, OSError), "executors classify it as a launch failure"
    [worker] = created
    assert worker.returncode is not None, "the suspended worker was left behind"
    assert spawned == [], "a worker that never ran was reported as spawned"
    time.sleep(1.0)
    assert list(folder.iterdir()) == [], "the worker ran although it was never contained"


@windows_only
def test_a_census_that_cannot_be_read_is_unknown_never_ok(monkeypatch):
    monkeypatch.setattr(procutil, "_census_job_pids", lambda handle: None)

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_with_tree_kill([sys.executable, "-c", "import time; time.sleep(60)"],
                           capture_output=True, text=True, timeout=2)
    facts = caught.value.worker_termination
    assert facts["job_terminate"] == "ok", "the stop was still attempted"
    assert facts["tree_kill"] == "census_unknown"
    assert worker_confirmed_dead({"worker_termination": facts}) is False
    sentence = describe_worker_termination({"worker_termination": facts})
    assert "NOT confirmed dead" in sentence and "census_unknown" in sentence


@windows_only
def test_tree_kill_is_never_ok_while_the_census_lists_a_member(monkeypatch):
    monkeypatch.setattr(procutil, "_census_job_pids", lambda handle: [4242])
    monkeypatch.setattr(procutil, "_JOB_EMPTY_SECONDS", 0.3)

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_with_tree_kill([sys.executable, "-c", "import time; time.sleep(60)"],
                           capture_output=True, text=True, timeout=2)
    facts = caught.value.worker_termination
    assert facts["tree_kill"] == "job_not_empty"
    assert facts["job_members"] == 1
    assert worker_confirmed_dead({"worker_termination": facts}) is False
    assert time.monotonic() - started < 30, "the wait for an empty job is not bounded"


@windows_only
def test_a_cancelled_wait_stops_the_whole_tree(dummy, sentinel, pinned, monkeypatch):
    """KeyboardInterrupt (or anything raised into the wait) is a stop too.

    `subprocess.run` kills the direct child when its wait is interrupted; a
    worker's tree must not outlive the call that owned it either.
    """

    script, folder = dummy
    argv = [sys.executable, str(script), "orphaner", str(folder)]

    class CancelledWhileWaiting(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            if self.args != argv:
                return super().communicate(input=input, timeout=timeout)
            _wait_for(lambda: (folder / "middle-exited").exists() and _leaf_pids(folder))
            pinned.pin(_leaf_pids(folder)[0])
            raise KeyboardInterrupt

    monkeypatch.setattr(procutil.subprocess, "Popen", CancelledWhileWaiting)
    with pytest.raises(KeyboardInterrupt):
        run_with_tree_kill(argv, capture_output=True, text=True, timeout=60)
    stopped_contents = _leaf_contents(folder)

    [leaf] = pinned.handles
    assert pinned.exited_within(leaf, 5), f"grandchild {leaf} outlived the cancelled call"
    time.sleep(1.0)
    assert _changed_leaves(folder, stopped_contents) == []
    assert sentinel.poll() is None, f"an unrelated process with the same image was stopped (exit code {sentinel.returncode})"


def _store_alias(name: str) -> "Path | None":
    """A Store App Execution Alias on this machine, or None. Only aliases that
    run a real program are used: a missing app's alias opens the Store."""

    import ctypes
    import os
    from ctypes import wintypes

    # The real profile, from the account itself: conftest points USERPROFILE
    # and LOCALAPPDATA at a scratch directory for every test, and the shell's
    # folder lookup expands those same variables.
    kernel32 = ctypes.WinDLL("kernel32")
    advapi32 = ctypes.WinDLL("advapi32")
    userenv = ctypes.WinDLL("userenv")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    userenv.GetUserProfileDirectoryW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not userenv.GetUserProfileDirectoryW(token, buffer, ctypes.byref(size)):
            return None
    finally:
        kernel32.CloseHandle(token)
    base = os.path.join(buffer.value, "AppData", "Local")
    path = Path(base) / "Microsoft" / "WindowsApps" / name if base else None
    # An alias is a reparse point that `stat` cannot follow, so ask without following.
    return path if path is not None and os.path.lexists(path) else None


@windows_only
def test_a_process_windows_starts_outside_the_job_is_stopped_too(dummy, sentinel, pinned):
    """An App Execution Alias target runs outside the worker's job, with the
    worker as its parent pid. The census cannot see it; `ok` may not be
    claimed while it runs (a review found it surviving)."""

    alias = _store_alias("pwsh.exe")
    if alias is None:
        pytest.skip("no Store PowerShell alias on this machine")
    script, folder = dummy
    thread, box = _run_in_background(
        [sys.executable, str(script), "alias", str(folder), str(alias)],
        capture_output=True, text=True, timeout=8, ready=lambda: _pin_alias_target(folder, pinned),
    )
    thread.join(timeout=2 * LOAD_HEADROOM_S)
    assert not thread.is_alive(), box
    [target] = pinned.handles

    error = box.get("error")
    assert isinstance(error, subprocess.TimeoutExpired), box
    assert pinned.exited_within(target, 5), f"alias target {target} survived the stop"
    facts = error.worker_termination
    assert facts["tree_kill"] == "ok", facts
    assert sentinel.poll() is None, f"an unrelated process with the same image was stopped (exit code {sentinel.returncode})"


@windows_only
def test_a_process_windows_started_outside_the_job_is_reported_after_a_normal_exit(dummy, pinned):
    alias = _store_alias("pwsh.exe")
    if alias is None:
        pytest.skip("no Store PowerShell alias on this machine")
    script, folder = dummy
    completed = run_with_tree_kill(
        [sys.executable, str(script), "alias-parent", str(folder), str(alias)],
        capture_output=True, text=True, timeout=60,
    )
    target = int(next(folder.glob("alias-[0-9]*")).name.split("-", 1)[1])
    pinned.pin(target)
    assert completed.returncode == 0
    facts = completed.worker_descendants
    listed = {item["pid"] for item in facts["descendants"]}
    assert not pinned.exited_within(target, 0), "a normal exit stopped the worker's descendant"
    if target not in listed:
        # Under a virtual environment the process that started the target is
        # the venv launcher's child, and it exited before the count: the
        # documented limit in `_kill_process_tree`.
        pytest.xfail("the target's launcher exited before the count (venv shape)")
    assert facts["complete"] is True, facts


@windows_only
def test_a_cancellation_after_the_worker_starts_but_before_the_wait_stops_it(dummy, pinned):
    """`on_spawn` runs after the resume; Ctrl+C while it writes its row must
    not leave the worker running with its job handle closed."""

    script, folder = dummy

    def cancel(pid):
        pinned.pin(pid)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_with_tree_kill(
            [sys.executable, str(script), "spawner", str(folder)],
            capture_output=True, text=True, timeout=60, on_spawn=cancel,
        )
    stopped_contents = _leaf_contents(folder)
    [worker] = pinned.handles
    assert pinned.exited_within(worker, 5), "the worker outlived the cancelled call"
    time.sleep(1.0)
    assert _changed_leaves(folder, stopped_contents) == []


# --------------------------------------------------------------------------
# The parent-pid walk behind it: a number is not an identity
# --------------------------------------------------------------------------


def _fake_table(monkeypatch, rows, created):
    """rows: (pid, parent, image); created: pid -> creation epoch (None = unreadable)."""

    monkeypatch.setattr(procutil, "_windows_process_snapshot", lambda: list(rows))
    monkeypatch.setattr(procutil, "process_creation_epoch", lambda pid, **kw: created.get(pid))


def test_the_walk_follows_a_chain_outside_the_job_and_skips_members(monkeypatch):
    # 10 is the worker (gone), 11 a member; 20 and 21 were started outside the job.
    _fake_table(
        monkeypatch,
        [(11, 10, "python.exe"), (20, 11, "pwsh.exe"), (21, 20, "python.exe")],
        {11: 101.0, 20: 102.0, 21: 103.0},
    )
    found = procutil._descendants_outside_job({10: 100.0, 11: 101.0}, {11}, 100.0)
    assert sorted(found) == [(20, "pwsh.exe", 102.0), (21, "python.exe", 103.0)]


def test_the_walk_never_adopts_a_process_older_than_its_supposed_parent(monkeypatch):
    # 30 names 11 as its parent, but existed before 11 did: an earlier holder of that pid.
    _fake_table(monkeypatch, [(11, 10, "python.exe"), (30, 11, "svchost.exe")], {11: 101.0, 30: 50.0})
    assert procutil._descendants_outside_job({10: 100.0, 11: 101.0}, {11}, None) == []


def test_the_walk_does_not_follow_a_root_whose_pid_was_reused(monkeypatch):
    # Root 11 died; its number now belongs to a process created at 500.
    _fake_table(monkeypatch, [(11, 4, "other.exe"), (40, 11, "child.exe")], {11: 500.0, 40: 501.0})
    assert procutil._descendants_outside_job({11: 101.0}, set(), 100.0) == []


def test_the_walk_takes_nothing_it_cannot_date(monkeypatch):
    _fake_table(monkeypatch, [(50, 10, "unknown.exe")], {50: None})
    assert procutil._descendants_outside_job({10: 100.0}, set(), 100.0) == []


def test_an_unreadable_process_table_is_unknown_not_empty(monkeypatch):
    monkeypatch.setattr(procutil, "_windows_process_snapshot", lambda: None)
    assert procutil._descendants_outside_job({10: 100.0}, set(), 100.0) is None


def test_the_walk_does_not_follow_a_root_it_cannot_date(monkeypatch):
    # A member that exited before it could be held: its number proves nothing.
    _fake_table(monkeypatch, [(60, 11, "child.exe")], {60: 200.0})
    assert procutil._descendants_outside_job({11: None}, set(), 100.0) == []


def test_a_member_the_census_missed_is_walked_through_but_not_counted_outside(monkeypatch):
    _fake_table(monkeypatch, [(70, 10, "python.exe"), (71, 70, "pwsh.exe")], {70: 101.0, 71: 102.0})
    found = procutil._descendants_outside_job({10: 100.0}, set(), 100.0, in_job=lambda pid: pid == 70)
    assert found == [(71, "pwsh.exe", 102.0)]


class _FakePins:
    def __init__(self, running=()):
        self.created = {10: 100.0}
        self._running = list(running)

    def still_running(self, pids, *, seconds):
        return [pid for pid in pids if pid in self._running]


def test_a_descendant_outside_the_job_that_will_not_die_is_escaped_never_ok(monkeypatch):
    monkeypatch.setattr(procutil, "_stop_outside_job", lambda pins, job, floor: [])
    facts = procutil._account_outside_job(_FakePins(running=[20]), 1234, 100.0, [20])
    assert facts == {"outside_job": 1, "tree_kill": "escaped_job", "escaped": 1}


def test_a_table_that_cannot_be_read_during_the_stop_is_outside_unknown(monkeypatch):
    assert procutil._account_outside_job(_FakePins(), 1234, 100.0, None) == {"tree_kill": "outside_unknown"}
    monkeypatch.setattr(procutil, "_stop_outside_job", lambda pins, job, floor: None)
    assert procutil._account_outside_job(_FakePins(), 1234, 100.0, []) == {"tree_kill": "outside_unknown"}


def _refuse_taskkill(monkeypatch):
    monkeypatch.setattr(procutil, "_taskkill_tree", lambda pid: "launch_error: FileNotFoundError")


@windows_only
def test_only_the_walk_stops_an_alias_target_when_taskkill_cannot(dummy, pinned, monkeypatch):
    """With the sweep refused, the parent-pid walk and the exact-pid stop are
    the only things that can reach a process outside the job."""

    alias = _store_alias("pwsh.exe")
    if alias is None:
        pytest.skip("no Store PowerShell alias on this machine")
    _refuse_taskkill(monkeypatch)
    script, folder = dummy
    thread, box = _run_in_background(
        [sys.executable, str(script), "alias", str(folder), str(alias)],
        capture_output=True, text=True, timeout=8, ready=lambda: _pin_alias_target(folder, pinned),
    )
    thread.join(timeout=2 * LOAD_HEADROOM_S)
    assert not thread.is_alive(), box
    [target] = pinned.handles

    facts = box["error"].worker_termination
    assert facts["taskkill"].startswith("launch_error"), facts
    assert pinned.exited_within(target, 5), f"alias target {target} survived: the walk did not reach it"
    assert facts.get("outside_job", 0) >= 1, facts
    assert facts["tree_kill"] == "ok", facts


@windows_only
@pytest.mark.xfail(
    strict=True,
    reason="documented limit: a process Windows started outside the job for a member "
    "that exited before the stop is linked to nothing held (see _kill_process_tree)",
)
def test_an_alias_target_whose_launcher_exited_is_not_claimed_stopped(dummy, pinned):
    """Nothing held names the target as its child. Until alias targets are
    contained in the job while the worker runs, a stop cannot reach it, and
    this test says so by failing: the day it passes, the limit is gone."""

    alias = _store_alias("pwsh.exe")
    if alias is None:
        pytest.skip("no Store PowerShell alias on this machine")
    script, folder = dummy
    def ready():
        _wait_for(lambda: (folder / "alias-launcher-exited").exists(), what="the alias launcher's exit")
        _pin_alias_target(folder, pinned)

    thread, box = _run_in_background(
        [sys.executable, str(script), "alias-via-middle", str(folder), str(alias)],
        capture_output=True, text=True, timeout=8, ready=ready,
    )
    thread.join(timeout=2 * LOAD_HEADROOM_S)
    assert not thread.is_alive(), box
    [target] = pinned.handles

    assert pinned.exited_within(target, 5), f"alias target {target} survived the stop"


@windows_only
def test_a_second_interrupt_during_the_stop_cannot_skip_the_job(dummy, pinned, monkeypatch):
    """A Ctrl+C raised while the sweep runs still leaves the job terminated."""

    script, folder = dummy

    def interrupted(pid):
        raise KeyboardInterrupt

    def ready():
        leaves = _wait_for(lambda: (folder / "middle-exited").exists() and _leaf_pids(folder))
        [leaf] = leaves
        pinned.pin(leaf)
        monkeypatch.setattr(procutil, "_taskkill_tree", interrupted)

    thread, box = _run_in_background(
        [sys.executable, str(script), "orphaner", str(folder)], capture_output=True, text=True, timeout=6,
        ready=ready,
    )
    thread.join(timeout=2 * LOAD_HEADROOM_S)
    assert not thread.is_alive(), box
    [leaf] = pinned.handles

    assert isinstance(box.get("error"), KeyboardInterrupt), box
    assert pinned.exited_within(leaf, 5), f"orphaned grandchild {leaf} survived an interrupted stop"


#: Codex's keyword arguments to run_with_tree_kill (execution/codex.py).
_CODEX_KWARGS = dict(capture_output=True, text=True, encoding="utf-8", errors="replace")
_MIB_PROMPT = "x" * (1024 * 1024)


@windows_only
def test_a_timeout_fires_while_a_large_prompt_is_still_being_written(dummy, pinned):
    """The worker never reads its 1 MiB prompt. Before the fix the
    call sat in the stdin write until the dummy exited on its own, 20 s in."""

    script, folder = dummy
    started = time.monotonic()
    thread, box = _run_in_background(
        [sys.executable, str(script), "idle", str(folder)], input=_MIB_PROMPT, timeout=2, **_CODEX_KWARGS
    )
    # Headroom for a loaded runner (train 17 preflight took 12.2 s); the claim
    # is that the stop beats the dummy's own exit, asserted below.
    thread.join(timeout=30)
    elapsed = time.monotonic() - started

    assert not thread.is_alive(), f"still in the stdin write after {elapsed:.1f}s"
    error = box.get("error")
    assert isinstance(error, subprocess.TimeoutExpired), box
    assert elapsed < _DUMMY_LIFETIME
    facts = error.worker_termination
    assert facts["confirmed_dead"] is True, facts
    assert worker_confirmed_dead({"worker_termination": facts}) is True


@windows_only
def test_a_worker_that_exits_while_a_sharer_holds_its_stdin_returns_on_time(dummy, pinned):
    """The worker starts a child that shares its stdin pipe and exits at once.
    With a 1 MiB prompt the call returns as it does for a short one: a
    CompletedProcess naming the child, which is left running."""

    script, folder = dummy
    started = time.monotonic()
    thread, box = _run_in_background(
        [sys.executable, str(script), "stdin-sharer", str(folder)], input=_MIB_PROMPT, timeout=30, **_CODEX_KWARGS
    )
    thread.join(timeout=12)
    elapsed = time.monotonic() - started

    assert not thread.is_alive(), f"held by the sharer's pipe for {elapsed:.1f}s"
    completed = box.get("result")
    assert isinstance(completed, subprocess.CompletedProcess), box
    assert completed.returncode == 0
    [child] = [int(path.name.split("-", 1)[1]) for path in folder.glob("child-*")]
    listed = {item["pid"] for item in completed.worker_descendants["descendants"]}
    assert child in listed, completed.worker_descendants
    leaf = _wait_for(lambda: _leaf_pids(folder), what="the leaf")[0]
    pinned.pin(leaf)
    assert not pinned.exited_within(leaf, 0), "a normal exit stopped the worker's descendant"


@windows_only
def test_an_unexpected_writer_error_is_raised_and_stdin_still_closed():
    """Bytes into a text-mode stdin: the writer's TypeError is raised promptly,
    and stdin is closed anyway, so the reader gets EOF and exits."""

    started = time.monotonic()
    with pytest.raises(TypeError):
        run_with_tree_kill(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            input=b"not text",
            timeout=30,
            **_CODEX_KWARGS,
        )
    assert time.monotonic() - started < 15


@windows_only
def test_an_unexpected_writer_error_carries_surviving_descendants(dummy, pinned):
    """An invalid prompt does not hide or stop a child left by a normal exit."""

    script, folder = dummy
    with pytest.raises(TypeError) as raised:
        run_with_tree_kill(
            [sys.executable, str(script), "parent", str(folder)],
            input=b"not text", timeout=30, **_CODEX_KWARGS,
        )
    facts = raised.value.worker_descendants
    assert facts["enumerated"] is True, facts
    [child] = [int(path.name.split("-", 1)[1]) for path in folder.glob("child-*")]
    assert child in {item["pid"] for item in facts["descendants"]}, facts
    leaf = _wait_for(lambda: _leaf_pids(folder), what="the surviving leaf")[0]
    pinned.pin(leaf)
    assert not pinned.exited_within(leaf, 0), "the writer error stopped the descendant"
