# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline diagnostics regressions; no providers, services or installed launch."""
from __future__ import annotations

import io
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

import modelquad
from modelquad import windowless_diagnostics as diagnostics
from conftest import no_window_kwargs

CANARY = 'secret-canary C:\\private\\task https://private.invalid/?token=secret argv hook-payload'
LINE = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z [a-z_]+(?: [a-z_]+)?\n")


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    # Bootstrap mutates process streams on purpose; restore them between tests.
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    monkeypatch.setattr(diagnostics, "_active", False)
    monkeypatch.setattr(diagnostics, "_sink", None)
    monkeypatch.setattr(diagnostics, "_budgets", {})


@pytest.fixture
def sink(tmp_path):
    sink = diagnostics.EventSink(tmp_path / "logs", "service")
    assert sink.prepare()
    return sink


class BadStream:
    closed = False

    def fileno(self):
        return -1

    def write(self, value):
        raise OSError(CANARY)

    def flush(self):
        raise OSError(CANARY)

    def isatty(self):
        raise OSError(CANARY)


@pytest.mark.parametrize("stream", [None, BadStream(), io.StringIO()])
def test_null_bootstrap_never_opens_state_and_discards_arbitrary_writes(monkeypatch, stream):
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)
    from modelquad import paths
    monkeypatch.setattr(paths, "logs_dir", lambda: pytest.fail("bootstrap selected state"))
    assert diagnostics.bootstrap(force=True)
    assert sys.stdout.write(CANARY) == len(CANARY)
    assert sys.stderr.write(CANARY) == len(CANARY)
    assert sys.stdout.buffer.write(CANARY.encode()) == len(CANARY.encode())
    for stream in (sys.stdout, sys.stderr, sys.stdout.buffer, sys.stderr.buffer):
        stream.flush()
        assert stream.isatty() is False
        assert stream.writable() is True
        stream.close()
        stream.flush()  # Interpreter shutdown remains harmless.
    assert sys.stdout.encoding == "utf-8"
    assert sys.stderr.errors == "replace"
    sys.stdout.reconfigure(encoding="ascii")
    diagnostics.emit(CANARY)


def test_invalid_live_handle_is_repaired_even_when_stream_is_not_none(monkeypatch):
    monkeypatch.setattr(sys, "stdout", BadStream())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert diagnostics.bootstrap()
    assert isinstance(sys.stderr, diagnostics.NullText)


def test_real_console_output_is_preserved(monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert not diagnostics.bootstrap()
    assert sys.stdout is out and sys.stderr is err


@pytest.mark.parametrize("command", sorted(diagnostics.PROTOCOL_COMMANDS),
                         ids=sorted(diagnostics.PROTOCOL_COMMANDS))
def test_live_text_and_binary_protocol_are_not_diagnostics(monkeypatch, sink, command):
    binary = io.BytesIO()
    out = io.TextIOWrapper(binary, encoding="utf-8", newline="")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", BadStream())
    monkeypatch.setattr(diagnostics, "_sink", sink)
    assert command in diagnostics.PROTOCOL_COMMANDS
    assert diagnostics.bootstrap(force=True, protocol_stdout=True)
    frame = '{"protocol":"' + CANARY.replace("\\", "\\\\") + '"}\n'
    sys.stdout.write(frame)
    sys.stdout.flush()
    sys.stdout.buffer.write(b"binary-frame\n")
    sys.stdout.buffer.flush()
    assert binary.getvalue() == frame.encode() + b"binary-frame\n"
    sys.stderr.write(CANARY)
    assert not list(sink.directory.glob("*.log"))
    # MCP's stdio transport builds its own TextIOWrapper over stdout.buffer.
    wrapper = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="")
    wrapper.write("transport-frame\n")
    wrapper.flush()
    assert binary.getvalue().endswith(b"transport-frame\n")
    wrapper.detach()
    out.detach()


@pytest.mark.parametrize("stream", [None, BadStream()])
def test_broken_protocol_writes_flushes_and_isatty_fail_silent(monkeypatch, stream):
    monkeypatch.setattr(sys, "stdout", stream)
    diagnostics.bootstrap(force=True, protocol_stdout=True)
    assert sys.stdout.write(CANARY) == len(CANARY)
    assert sys.stdout.buffer.write(b"protocol") == 8
    sys.stdout.flush()
    sys.stdout.buffer.flush()
    assert sys.stdout.isatty() is False


def test_selected_state_is_bound_after_explicit_flags_and_never_to_default(monkeypatch, tmp_path):
    from modelquad import paths
    default = tmp_path / "default"
    chosen = tmp_path / "chosen"
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(default))
    diagnostics.bootstrap(force=True)
    assert not default.exists()
    assert diagnostics.select_state(["serve", "--state-root", str(chosen), "--state-mode=test"])
    assert paths.state_root() == chosen
    assert diagnostics.bind_selected_state("service")
    diagnostics.emit("entry")
    assert (chosen / "logs" / "service-diagnostics.log").is_file()
    assert not default.exists()


@pytest.mark.parametrize("argv", [["--state-root"], ["--state-root", "relative"],
                                  ["--state-mode", "other"],
                                  ["--state-mode=test", "--state-mode", "installed"]])
def test_invalid_declaration_keeps_null_sink(argv):
    assert diagnostics.select_state(argv) is False
    assert diagnostics._sink is None


@pytest.mark.parametrize("role", ["../other", "private/path", "unknown", None, []])
def test_unknown_roles_never_create_an_arbitrary_destination(tmp_path, role):
    diagnostics.bootstrap(force=True)
    assert not diagnostics.bind_selected_state(role)
    with pytest.raises(ValueError):
        diagnostics.EventSink(tmp_path / "logs", role)
    assert not (tmp_path / "logs").exists()


def test_only_closed_labels_are_persisted_and_streams_never_retain_canaries(monkeypatch, sink):
    diagnostics.bootstrap(force=True)
    monkeypatch.setattr(diagnostics, "_sink", sink)
    sys.stderr.write(CANARY)
    sys.stdout.write("entry\n")  # Even a word that happens to be allowed is raw output.
    sys.stdout.buffer.write(CANARY.encode())
    diagnostics.emit(CANARY)
    diagnostics.emit("entry", CANARY)
    assert sink.emit(CANARY) is False
    diagnostics.emit("entry")
    diagnostics.emit("startup_failed", "unknown")
    data = (sink.directory / sink.name).read_bytes()
    assert CANARY.encode() not in data
    assert data.splitlines()[0].endswith(b" entry")
    assert b"startup_failed unknown" in data
    assert all(LINE.fullmatch(line) for line in data.splitlines(keepends=True))


def test_service_events_use_only_content_free_allowed_labels(sink):
    for event in ("service_start_contained", "backend_failed", "startup_failed"):
        assert sink.emit(event)
        assert not sink.emit(event + ": private/path")
    data = (sink.directory / sink.name).read_bytes()
    assert b"private/path" not in data and b"service_start_failed" not in data
    assert len(data.splitlines()) == 3


@pytest.mark.parametrize("failure", ["protect", "open", "write", "rotate", "lock", "directory"])
def test_failed_sinks_never_raise_or_append_after_failed_rotation(monkeypatch, sink, failure):
    path = sink.directory / sink.name
    if failure == "rotate":
        path.write_bytes(b"x" * diagnostics.MAX_LOG_BYTES)
    def fail(*args, **kwargs):
        raise OSError(CANARY)
    if failure == "protect":
        monkeypatch.setattr(diagnostics, "_protect_logs", fail)
    elif failure == "directory":
        monkeypatch.setattr(diagnostics, "_directory", fail)
    elif failure == "lock":
        monkeypatch.setattr(diagnostics, "_file_lock", fail)
    elif failure == "open":
        monkeypatch.setattr(diagnostics, "_open_file", fail)
    else:
        monkeypatch.setattr(diagnostics.os, "replace" if failure == "rotate" else "write", fail)
    assert sink.emit("entry") is False
    monkeypatch.setattr(diagnostics, "_sink", sink)
    diagnostics.emit("exit")
    if failure == "rotate":
        assert path.stat().st_size == diagnostics.MAX_LOG_BYTES
        assert not path.with_name(path.name + ".1").exists()


def test_bind_fails_silent_if_acl_cannot_be_protected(monkeypatch, tmp_path):
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(diagnostics, "_protect_logs", lambda *args: (_ for _ in ()).throw(PermissionError(CANARY)))
    diagnostics.bootstrap(force=True)
    assert diagnostics.bind_selected_state("service") is False
    diagnostics.emit("entry")
    assert not list(tmp_path.rglob("*.log"))


def test_rotation_is_checked_on_every_write_and_keeps_only_one_generation(monkeypatch, sink):
    monkeypatch.setattr(diagnostics, "MAX_LOG_BYTES", 200)
    for _ in range(40):
        assert sink.emit("entry")
    files = sorted(path.name for path in sink.directory.iterdir())
    assert files == [sink.name, sink.name + ".1", sink.name + ".lock"]
    for name in (sink.name, sink.name + ".1"):
        data = (sink.directory / name).read_bytes()
        assert len(data) <= 200
        assert all(LINE.fullmatch(line) for line in data.splitlines(keepends=True))


def test_lifetime_budget_and_nonblocking_lock(monkeypatch, sink):
    monkeypatch.setattr(diagnostics, "MAX_PROCESS_EVENTS", 2)
    assert sink._lock.acquire(blocking=False)
    try:
        assert not sink.emit("entry")
    finally:
        sink._lock.release()
    assert sink.emit("entry") and sink.emit("exit")
    assert not sink.emit("entry")


def test_rebinding_same_role_does_not_reset_process_budget(monkeypatch, sink):
    monkeypatch.setattr(diagnostics, "MAX_PROCESS_EVENTS", 1)
    assert sink.emit("entry")
    rebound = diagnostics.EventSink(sink.directory, "service")
    assert not rebound.emit("exit")


def test_existing_oversized_generations_are_capped_without_reading_contents(monkeypatch, sink):
    monkeypatch.setattr(diagnostics, "MAX_LOG_BYTES", 200)
    (sink.directory / sink.name).write_bytes(b"x" * 400)
    (sink.directory / (sink.name + ".1")).write_bytes(b"y" * 400)
    assert sink.emit("entry")
    assert (sink.directory / sink.name).stat().st_size <= 200
    assert (sink.directory / (sink.name + ".1")).stat().st_size <= 200


def test_process_byte_budget_is_a_lifetime_bound(monkeypatch, sink):
    monkeypatch.setattr(diagnostics, "MAX_PROCESS_BYTES", 70)
    outcomes = [sink.emit("entry") for _ in range(10)]
    assert 0 < sum(outcomes) < 10
    assert (sink.directory / sink.name).stat().st_size <= 70


def test_contended_kernel_lock_drops_event_without_wait(sink):
    with diagnostics._directory(sink.directory) as directory_fd:
        lock_fd = diagnostics._open_file(sink.directory, directory_fd, sink.name + ".lock")
        try:
            with diagnostics._file_lock(lock_fd):
                assert sink.emit("entry") is False
        finally:
            os.close(lock_fd)


@pytest.mark.parametrize("destination", ["directory", "ancestor", "current", "prior", "lock"])
def test_symlink_and_redirected_destinations_are_rejected(tmp_path, destination):
    outside = tmp_path / "outside"
    outside.mkdir()
    logs = tmp_path / "state" / "logs"
    logs.mkdir(parents=True)
    name = "service-diagnostics.log"
    target = logs
    source = outside
    if destination in {"current", "prior", "lock"}:
        target = logs / (name + {"current": "", "prior": ".1", "lock": ".lock"}[destination])
        source = outside / "keep"
        source.write_bytes(b"untouched")
    else:
        logs.rmdir()
        if destination == "ancestor":
            logs.parent.rmdir()
            target = logs.parent
    try:
        target.symlink_to(source, target_is_directory=source.is_dir())
    except OSError:
        pytest.skip("symlink creation unavailable on this test account")
    sink = diagnostics.EventSink(logs, "service")
    if destination in {"directory", "ancestor"}:
        assert not sink.prepare()
    else:
        assert not sink.prepare()
        assert not sink.emit("entry")
        assert source.read_bytes() == b"untouched"
    assert not list(outside.glob("*-diagnostics.log*"))


@pytest.mark.parametrize("suffix", ["", ".1", ".lock"])
def test_hardlinked_log_files_are_rejected(tmp_path, suffix):
    sink = diagnostics.EventSink(tmp_path / "logs", "service")
    assert sink.prepare()
    source = tmp_path / "outside"
    source.write_bytes(b"untouched")
    os.link(source, sink.directory / (sink.name + suffix))
    assert not sink.emit("entry")
    assert source.read_bytes() == b"untouched"


@pytest.mark.skipif(os.name != "nt", reason="Windows directory-entry cache")
@pytest.mark.parametrize("linked", [False, True], ids=["regular", "hardlink"])
def test_windows_log_scan_queries_link_counts_instead_of_cached_zero(monkeypatch, tmp_path, linked):
    from contextlib import contextmanager
    from types import SimpleNamespace

    logs = tmp_path / "logs"
    logs.mkdir()
    source = tmp_path / "outside"
    source.write_bytes(b"untouched")
    destination = logs / "service-diagnostics.log"
    if linked:
        os.link(source, destination)
    else:
        destination.write_bytes(b"")
    real_scandir = os.scandir
    cached_reads = []

    @contextmanager
    def cached_scandir(path):
        with real_scandir(path) as entries:
            cached = []
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                def cached_stat(*, follow_symlinks, info=info):
                    cached_reads.append(True)
                    assert follow_symlinks is False
                    return SimpleNamespace(st_mode=info.st_mode, st_nlink=0,
                                           st_file_attributes=info.st_file_attributes)
                cached.append(SimpleNamespace(path=entry.path, stat=cached_stat))
            yield iter(cached)

    monkeypatch.setattr(diagnostics.os, "scandir", cached_scandir)
    sink = diagnostics.EventSink(logs, "service")
    assert sink.prepare() is (not linked)
    assert sink.emit("entry") is (not linked)
    assert sink.emit("exit") is (not linked)
    assert cached_reads == []
    assert source.read_bytes() == b"untouched"
    if linked:
        assert destination.read_bytes() == b"untouched"
    else:
        assert [line.split(" ", 1)[1] for line in destination.read_text().splitlines()] == ["entry", "exit"]


def test_same_role_processes_serialize_runtime_rotation(tmp_path):
    # Real kernel locks across Python processes, not just two Lock instances.
    import_root = str(Path(modelquad.__file__).resolve().parent.parent)
    code = """
import sys
from pathlib import Path
from modelquad import windowless_diagnostics as d
d.MAX_LOG_BYTES = 512
sink = d.EventSink(Path(sys.argv[1]), 'service')
assert sink.prepare()
for index in range(160):
    sink.emit('entry')
"""
    logs = tmp_path / "logs"
    processes = [subprocess.Popen([sys.executable, "-c", code, str(logs)],
                 env={**os.environ, "PYTHONPATH": import_root}, stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE, **no_window_kwargs()) for _ in range(3)]
    try:
        for proc in processes:
            out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, (out, err)
        for name in ("service-diagnostics.log", "service-diagnostics.log.1"):
            data = (logs / name).read_bytes()
            assert 0 < len(data) <= 512
            assert all(LINE.fullmatch(line) for line in data.splitlines(keepends=True))
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=10)
