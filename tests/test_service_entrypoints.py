# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline service entry/cwd coverage and exact CLI bypass inventory."""
import ast
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from modelquad import background_cwd, cli, executable_resolution as resolver
from modelquad.service import entry, lifecycle


@pytest.fixture
def layout(tmp_path, monkeypatch):
    from modelquad import windowless_diagnostics as diagnostics
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    monkeypatch.setattr(diagnostics, "_active", False)
    monkeypatch.setattr(diagnostics, "_sink", None)
    images = {}
    for name in ("python.exe", "pythonw.exe"):
        image = tmp_path / name
        image.write_bytes(b"fixture; never execute")
        image.chmod(0o700)
        images[name] = image
    system = tmp_path / "system & directory"
    system.mkdir()
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.sys, "executable", str(images["python.exe"]))
    monkeypatch.setattr(cli.sys, "_base_executable", str(images["python.exe"]), raising=False)
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("MODELQUAD_STATE_MODE", "test")
    monkeypatch.setattr(lifecycle, "system_directory", lambda: str(system))
    return SimpleNamespace(**locals())


@pytest.mark.parametrize("case", ["poisoned_parent", "poisoned_systemroot", "poisoned_comspec", "system_api_failed",
                                  "escaped_xml", "relative_state_override", "source_windowless",
                                  "windowless_missing"],
                         ids=["poisoned_parent", "poisoned_systemroot", "poisoned_comspec", "system_api_failed",
                              "escaped_xml", "relative_state_override", "source_windowless",
                              "windowless_missing"])
def test_logon_cwd(layout, monkeypatch, case):
    h = layout
    poisoned = h.tmp_path / "poisoned"
    poisoned.mkdir()
    (poisoned / "modelquad.py").write_text("raise RuntimeError('planted')", encoding="utf-8")
    if case == "poisoned_parent":
        monkeypatch.chdir(poisoned)
        monkeypatch.setenv("PYTHONPATH", str(poisoned))
    if case in {"poisoned_systemroot", "poisoned_comspec"}:
        monkeypatch.setenv("SystemRoot" if case == "poisoned_systemroot" else "ComSpec", str(poisoned))
    if case == "system_api_failed":
        monkeypatch.setattr(lifecycle, "system_directory", lambda: None)
        calls = []
        result = lifecycle.TaskSchedulerRegistrar(run=lambda *a, **kw: calls.append(True)).register(
            command=cli._service_autostart_command(8765), dry_run=True)
        assert not result.success and not calls
        assert result.message == lifecycle.SERVICE_DETAILS["service_cwd_unavailable"]
        return
    if case == "relative_state_override":
        monkeypatch.setenv("MODELQUAD_STATE_ROOT", "relative-state")
        with pytest.raises(ValueError, match="service_cwd_unavailable"):
            cli._service_autostart_command(8765)
        return
    if case == "windowless_missing":
        h.images["pythonw.exe"].unlink()
        with pytest.raises(resolver.UnsafeExecutable, match="not_found"):
            cli._service_autostart_command(8765)
        return
    controller = cli._service_autostart_command(8765)
    cwd = lifecycle.service_working_directory()
    definition = lifecycle.TaskSchedulerRegistrar._task_definition_xml(controller, "fixture-user", cwd=cwd)
    assert "<WorkingDirectory>" + str(h.system).replace("&", "&amp;") + "</WorkingDirectory>" in definition
    child = cli._service_launch_command(8765)
    assert background_cwd.background_child_kwargs(child, cwd=cwd)["cwd"] == str(h.system)
    bootstrap = str(Path(entry.__file__).resolve())
    assert controller[:5] == [str(h.images["pythonw.exe"]), "-I", bootstrap, "service", "start"]
    assert child[:5] == [str(h.images["python.exe"]), "-I", bootstrap, "serve", "--managed-child"]
    assert "-m" not in child and "-m" not in controller


@pytest.mark.parametrize("selection", ["missing", "mismatch", "matching"], ids=["missing", "mismatch", "matching"])
def test_private_child_admission_does_not_take_guard(layout, monkeypatch, selection):
    h = layout
    now, sleeps = [0.0], []
    def sleep(seconds):
        now[0] += seconds
        sleeps.append(seconds)
    command = cli._service_launch_command(8765)
    manager = lifecycle.ServiceProcessManager(
        state_dir=h.tmp_path, process_created_at=lambda pid: 1000.0,
        process_image_path=lambda pid: command[0], clock=lambda: now[0], sleep=sleep,
    )
    monkeypatch.setattr(lifecycle, "_service_start_guard", lambda *a: pytest.fail("child never takes guard"))
    if selection != "missing":
        manager.pid_path.write_text(json.dumps({
            "pid": os.getpid() if selection == "matching" else 9753, "argv": command,
            "started_at": datetime.fromtimestamp(1000.0, timezone.utc).isoformat()}), encoding="utf-8")
    options = lifecycle._service_options(command, h.tmp_path)
    assert manager.admit_child(options=options) is (selection == "matching")
    assert now[0] <= 5.0 and all(s <= 0.1 for s in sleeps)
    if selection != "matching":
        assert now[0] == 5.0


def test_source_bootstrap_uses_own_file_and_forwards_without_cwd_imports(layout, monkeypatch):
    h = layout
    seen = []
    monkeypatch.setattr(cli, "main", lambda args: seen.append((args, sys.path[0])) or 0)
    before = list(sys.path)
    try:
        arguments = ["service", "start", "--state-root", str(h.tmp_path), "--state-mode", "test"]
        assert entry.main(arguments) == 0
        assert seen == [(arguments, str(Path(entry.__file__).resolve().parents[2]))]
        assert os.environ["MODELQUAD_STATE_ROOT"] == str(h.tmp_path)
        assert entry.main(["serve"]) == 1
    finally:
        sys.path[:] = before


def test_dependency_restore_failure_logs_only_in_explicit_selected_state(layout, monkeypatch):
    from modelquad import windowless_diagnostics as diagnostics
    chosen = layout.tmp_path / "explicit-state"
    previous_path = list(sys.path)
    monkeypatch.setattr(diagnostics, "_budgets", {})
    monkeypatch.setattr(entry, "_restore_installation", lambda image: (_ for _ in ()).throw(
        OSError("private dependency path canary")))
    try:
        assert entry.main(["--installation-python", "fixture", "service", "start",
                           "--state-root=" + str(chosen), "--state-mode=test"]) == 1
    finally:
        sys.path[:] = previous_path
    data = (chosen / "logs" / "service-diagnostics.log").read_text(encoding="utf-8")
    assert [line.split(" ", 1)[1] for line in data.splitlines()] == ["entry", "startup_failed", "exit"]
    assert not (layout.tmp_path / "state" / "logs").exists()


@pytest.mark.parametrize("windowless", [False, True], ids=["console", "windowless"])
def test_source_venv_redirector_is_not_the_managed_child(layout, monkeypatch, windowless):
    h = layout
    redirector = h.tmp_path / "venv" / "Scripts" / "python.exe"
    redirector.parent.mkdir(parents=True)
    redirector.write_bytes(b"fixture redirector; never execute")
    redirector.with_name("pythonw.exe").write_bytes(b"fixture redirector; never execute")
    redirector.chmod(0o700)
    redirector.with_name("pythonw.exe").chmod(0o700)
    (redirector.parent.parent / "pyvenv.cfg").write_text(f"home = {h.tmp_path}\ninclude-system-site-packages = false\n", encoding="utf-8")
    monkeypatch.setattr(cli.sys, "executable", str(redirector))
    monkeypatch.setattr(cli.sys, "_base_executable", str(h.images["python.exe"]))
    command = cli._service_autostart_command(8765) if windowless else cli._service_launch_command(8765)
    owning_image = redirector.with_name("pythonw.exe") if windowless else redirector
    assert command[:6] == [str(h.images["pythonw.exe" if windowless else "python.exe"]), "-I", "-S",
                           str(Path(entry.__file__).resolve()), "--installation-python", str(owning_image)]
    assert command[6:8] == (["service", "start"] if windowless else ["serve", "--managed-child"])


@pytest.mark.parametrize("include_base", [False, True], ids=["venv-only", "with-system"])
def test_bootstrap_restores_owning_venv_dependencies(layout, monkeypatch, include_base):
    import importlib.util
    import site

    h = layout
    root = h.tmp_path / "venv"
    owning = root / "Scripts" / "python.exe"
    owning.parent.mkdir(parents=True)
    owning.write_bytes(b"fixture; never execute")
    owning.chmod(0o700)
    (root / "pyvenv.cfg").write_text(
        f"home = {h.tmp_path}\ninclude-system-site-packages = {str(include_base).lower()}\n", encoding="utf-8")
    dependencies = root / "Lib" / "site-packages"
    dependencies.mkdir(parents=True)
    (dependencies / "w02b_fixture_dependency.py").write_text("VALUE = 42\n", encoding="utf-8")
    base_site = h.tmp_path / "Lib" / "site-packages"
    base_site.mkdir(parents=True)
    (base_site / "w02b_base_dependency.py").write_text("VALUE = 1\n", encoding="utf-8")
    # Metadata is synthetic; run Python's real site/pyvenv policy without
    # interpreter children, installs, inherited roots or machine customizers.
    monkeypatch.setattr(sys, "base_prefix", str(h.tmp_path))
    monkeypatch.setattr(sys, "base_exec_prefix", str(h.tmp_path))
    monkeypatch.setattr(sys, "prefix", str(h.tmp_path))
    monkeypatch.setattr(sys, "exec_prefix", str(h.tmp_path))
    monkeypatch.setattr(sys, "_home", None, raising=False)
    monkeypatch.setattr(sys, "path", [str(base_site)])
    monkeypatch.setattr(site, "PREFIXES", [str(h.tmp_path)])
    monkeypatch.setattr(site, "ENABLE_USER_SITE", False)
    monkeypatch.setattr(site, "execsitecustomize", lambda: None)
    monkeypatch.setattr(site, "execusercustomize", lambda: None)
    seen = []

    def dispatch(arguments):
        dependency = importlib.util.find_spec("w02b_fixture_dependency")
        assert dependency is not None and dependency.origin == str(dependencies / "w02b_fixture_dependency.py")
        assert (importlib.util.find_spec("w02b_base_dependency") is not None) is include_base
        seen.append(arguments)
        assert sys.prefix == str(root) and sys.executable == str(owning)
        assert sys.path[0] == str(Path(entry.__file__).resolve().parents[2])
        # Subsequent controller -> child launch still uses the exact base image
        # and carries the owning dependency environment through the same seam.
        child = cli._service_launch_command(8765)
        assert child[0] == str(h.images["python.exe"]) and child[1:3] == ["-I", "-S"]
        assert child[5] == str(owning)
        return 0

    monkeypatch.setattr(cli, "main", dispatch)
    assert entry.main(["--installation-python", str(owning), "service", "start"]) == 0
    assert seen == [["service", "start"]]


@pytest.mark.parametrize("case", ["relative", "no-config", "wrong-home", "nearer-config"],
                         ids=["relative", "no-config", "wrong-home", "nearer-config"])
def test_bootstrap_refuses_unproved_dependency_environment(layout, monkeypatch, case):
    root = layout.tmp_path / "venv"
    image = root / "Scripts" / "python.exe"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"fixture; never execute")
    image.chmod(0o700)
    if case == "wrong-home":
        (root / "pyvenv.cfg").write_text(f"home = {image.parent}\n", encoding="utf-8")
    if case == "nearer-config":
        (root / "pyvenv.cfg").write_text(f"home = {layout.tmp_path}\n", encoding="utf-8")
        (image.parent / "pyvenv.cfg").write_text(f"home = {image.parent}\n", encoding="utf-8")
    monkeypatch.setattr(cli, "main", lambda *a: pytest.fail("unproved environment must not dispatch"))
    selected = "relative/Scripts/python.exe" if case == "relative" else str(image)
    assert entry.main(["--installation-python", selected, "service", "start"]) == 1


@pytest.mark.parametrize("reason", ["service_pid_record_unavailable", "service_unmanaged", "service_cleanup_unconfirmed"],
                         ids=["corrupt", "unmanaged", "cleanup"])
def test_process_result_renders_unknown_pid_conservatively(layout, capsys, reason):
    result = lifecycle.ProcessResult(False, True, True, None, layout.tmp_path / "service.log",
                                    lifecycle.SERVICE_DETAILS[reason], "start", ready=False, reason=reason)
    cli._render_process_result(result)
    text = capsys.readouterr().out
    assert "possibly running; PID unknown; not ready" in text
    assert "not running" not in text


@pytest.mark.parametrize("case", ["ready", "unmanaged", "identity-unknown", "record-unavailable", "absent"],
                         ids=["ready", "unmanaged", "identity-unknown", "record-unavailable", "absent"])
def test_doctor_uses_public_managed_readiness(layout, monkeypatch, case):
    from modelquad import doctor
    from modelquad.service import control_plane as shell

    h = layout
    code = {"unmanaged": "service_unmanaged", "identity-unknown": "service_process_identity_unconfirmed",
            "record-unavailable": "service_pid_record_unavailable", "absent": "service_absent"}.get(case)
    status = lifecycle.ProcessStatus(case != "absent", None if case != "ready" else 2468,
                                     h.tmp_path / "service.log", ready=case == "ready", reason=code)
    calls = []
    class Manager:
        state_dir = h.tmp_path
        def status(self, **kwargs):
            calls.append(kwargs)
            return status
    monkeypatch.setattr(lifecycle, "ServiceProcessManager", Manager)
    monkeypatch.setattr(shell, "probe_control_plane", lambda *a: pytest.fail("doctor must use managed status"))
    check = doctor._control_plane_check()
    assert calls == [{}]
    assert check.status is (doctor.CheckStatus.OK if case in {"ready", "absent"} else doctor.CheckStatus.MISSING)
    if case not in {"ready", "absent"}:
        assert lifecycle.SERVICE_DETAILS[code] in check.detail
        assert "Running and verified" not in check.detail


def test_explicit_state_selection_also_selects_its_default_database(layout):
    root = layout.tmp_path / "other-state"
    args = cli.build_parser().parse_args(["serve", "--state-root", str(root), "--state-mode", "test"])
    command = cli._service_launch_command(8765, args)
    assert command[command.index("--db") + 1] == str(root / "modelquad.db")


def _cli_service_sites(source):
    """Collect process/server calls and references, including import/assignment aliases."""
    tree = ast.parse(source)
    aliases = {}
    modules = {"uvicorn", "subprocess", "os", "procutil", "lifecycle"}
    managed = {"_run_service_command", "_run_service_lifecycle_command", "_run_managed_service_start",
               "_run_managed_service_child", "_service_launch_command", "_service_autostart_command"}
    methods = {"Popen", "run", "call", "check_call", "check_output", "system", "popen", "kill", "poll", "wait",
               "terminate_pid", "terminate_verified", "_process_exists", "windows_process_image_path",
               "process_creation_epoch", "_launch_background_child", "start", "stop", "status",
               "wait_started_child", "admit_child"}
    def spelling(node):
        if isinstance(node, ast.Name):
            if node.id == "ServiceProcessManager":
                return node.id
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return spelling(node.value) + "." + node.attr
        if isinstance(node, ast.Call):
            return spelling(node.func) + "()"
        return ""
    # Fixed-point propagation catches `spawn = Popen; launch = spawn` too.
    for _ in range(8):
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = alias.name
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    aliases[alias.asname or alias.name] = (node.module or "") + "." + alias.name
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and isinstance(node.value, (ast.Name, ast.Attribute)):
                        aliases[target.id] = spelling(node.value)
    sites = []
    class Scan(ast.NodeVisitor):
        def __init__(self):
            self.functions = []
        def visit_FunctionDef(self, node):
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_Call(self, node):
            name = spelling(node.func)
            tail = name.rsplit(".", 1)[-1]
            root = name.split(".", 1)[0]
            if (tail in managed or (tail in methods and (tail != "run" or root in modules))
                    or root == "uvicorn" or tail.startswith(("CreateProcess", "ShellExecute", "exec", "spawn"))):
                sites.append((".".join(self.functions) or "<module>", name))
            self.generic_visit(node)
    Scan().visit(tree)
    return Counter(sites)


def test_installed_command_entry_has_no_service_call_sites():
    """cli_entry is the installed command (PERF-1010-1): it may only dispatch
    the pending hook or hand over to cli.main, never start or stop the service."""
    from modelquad import cli_entry
    source = Path(cli_entry.__file__).read_text(encoding="utf-8")
    assert _cli_service_sites(source) == Counter()
    bypass = source + "\nimport subprocess as s\ndef bypass():\n s.Popen(['serve'])\n"
    assert _cli_service_sites(bypass)


@pytest.mark.parametrize("snippet", [
    "import subprocess as s\ndef bypass():\n s.Popen(['serve'])",
    "from subprocess import Popen as spawn\ndef bypass():\n launch = spawn\n launch(['serve'])",
    "import uvicorn as u\ndef bypass():\n u.run(object())",
    "from modelquad.procutil import terminate_pid as stop\ndef bypass():\n stop(42)",
    "from modelquad.service.lifecycle import _process_exists as alive\ndef bypass():\n alive(42)",
], ids=["direct-launch", "aliased-launch", "direct-server", "direct-stop", "direct-process-check"])
def test_cli_scanner_detects_synthetic_bypasses(snippet):
    assert _cli_service_sites(snippet)


@pytest.mark.parametrize("port,poll", [(0, 60), (65536, 60), (8765, float("nan")), (8765, float("inf")), (8765, 0)],
                         ids=["port-zero", "port-large", "nan-poll", "infinite-poll", "zero-poll"])
def test_invalid_service_options_refuse_before_launch(layout, port, poll):
    args = cli.build_parser().parse_args(["serve", "--port", str(port), "--poll-interval", str(poll)])
    with pytest.raises(ValueError):
        cli._service_launch_command(port, args)


def test_every_cli_service_call_site_uses_managed_lifecycle():
    source = Path(cli.__file__).read_text(encoding="utf-8")
    expected = Counter({
        ("render_global_status", "ServiceProcessManager().status"): 1,
        ("_run_service_command", "_run_managed_service_child"): 1,
        ("_run_service_command", "_run_managed_service_start"): 1,
        ("_run_managed_service_start", "_service_launch_command"): 1,
        ("_run_managed_service_start", "manager.start"): 1,
        ("_run_managed_service_start", "manager.wait_started_child"): 1,
        ("_run_managed_service_child", "ServiceProcessManager().admit_child"): 1,
        ("_run_managed_service_child", "uvicorn.run"): 1,
        # install registers it; uninstall rebuilds it for the ownership check.
        ("_run_service_lifecycle_command", "_service_autostart_command"): 2,
        ("_run_service_lifecycle_command", "_run_managed_service_start"): 1,
        ("_run_service_lifecycle_command", "manager.stop"): 1,
        ("_run_service_lifecycle_command", "manager.status"): 1,
        ("_run_command", "_run_service_command"): 1,
        ("_run_command", "_run_service_lifecycle_command"): 1,
        ("_run_command", "ServiceProcessManager().status"): 1,
    })
    assert _cli_service_sites(source) == expected
