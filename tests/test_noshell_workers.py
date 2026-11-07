# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Offline no-shell worker contracts.

On the supplied base, integration cases expose shell-enabled argv, normal
answers containing forbidden events, no supplied contents, or admitted resume.
Pure helper cases specify new behavior; no import failure is a base verdict.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import Mock

import pytest

from modelquad.execution import codex
from modelquad.execution.base import ExecutionRequest, WorkerTaskStatus, attempt_resolution
from modelquad.models import TaskType


FIXTURES = Path(__file__).parent / "fixtures" / "noshell"
READ_ONLY = (TaskType.EXPLORATION, TaskType.REVIEW, TaskType.ARCHITECTURE)
WRITE = (TaskType.IMPLEMENTATION, TaskType.DEBUGGING, TaskType.TESTING, TaskType.TRIVIAL)
CRITICAL = ("shell_tool", "unified_exec", "hooks", "code_mode_host", "code_mode", "apps", "plugins",
            "remote_plugin", "browser_use", "computer_use")
OPTIONAL = ("view_image", "multi_agent", "multi_agent_v2", "shell_snapshot", "image_generation")
DISABLES = (*CRITICAL, *OPTIONAL)
REQUIRED_FALSE = tuple(feature for feature in CRITICAL if feature != "unified_exec")
noshell = getattr(codex, "noshell", None)


def listing(**overrides):
    values = {name: False for name in DISABLES}
    values["unified_exec"] = True  # What the two recorded probes observed.
    values.update(overrides)
    return "\n".join(f"{key} under development {str(value).lower()}" for key, value in values.items()) + "\n"


def stream(item=None, *, kind="item.completed", text="Diagnosis supplied."):
    events = [{"type": "thread.started", "thread_id": "inert"}, {"type": "turn.started"}]
    if item:
        events.append({"type": kind, "item": item})
    events += [{"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": text}},
               {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}]
    return "\n".join(json.dumps(event) for event in events) + "\n"


@pytest.fixture
def worker(tmp_path, monkeypatch, synthetic_worker_binary):
    noshell._QUALIFICATIONS.clear()
    monkeypatch.setattr(codex, "_probe", lambda executable: "0.160.0")
    calls = []
    def inventory(argv, cwd, env):
        calls.append((list(argv), cwd, dict(env)))
        output = listing() if "features" in argv else "[]"
        return subprocess.CompletedProcess(argv, 0, output, "")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stream(), ""))
    executor = codex.CodexExecutor(which=lambda _: synthetic_worker_binary("codex.exe"),
                                   run=run, inventory=inventory)
    request = ExecutionRequest(provider="codex", task_type=TaskType.REVIEW, prompt="Inspect named.txt",
                               working_directory=tmp_path)
    return executor, request, run, calls


def test_synthetic_install_is_outside_request_workspace(tmp_path, synthetic_worker_binary):
    from modelquad.executable_resolution import admit_executable
    executable = synthetic_worker_binary("codex.exe")
    workspace = os.path.normcase(os.path.realpath(tmp_path))
    assert os.path.commonpath((executable, workspace)) != workspace
    admitted = admit_executable(executable, cwd=str(tmp_path), allow_batch=False)
    assert admitted.path is not None
    assert admitted.kind == "binary"
    assert admitted.refusal is None


def test_workspace_executable_still_refuses_before_qualification(worker):
    executor, request, run, calls = worker
    directory = request.working_directory / "private Program Files worker install"
    directory.mkdir()
    executable = directory / "codex.exe"
    executable.write_bytes(b"synthetic fixture - never execute\n")
    executable.chmod(0o755)
    executor._which = lambda _: str(executable)
    result = executor.execute(request)
    assert calls == []
    run.assert_not_called()
    assert result.metadata["worker_started"] is False
    assert result.task_status_reason == "codex_executable_unverified"
    assert attempt_resolution(result) == ("refused", "policy_refused")


@pytest.mark.parametrize("task_type", READ_ONLY, ids=["exploration", "review", "architecture"])
def test_readonly_argv_and_same_listing_disables(worker, task_type):
    executor, request, run, calls = worker
    result = executor.execute(replace(request, task_type=task_type))
    assert calls and calls[0][0][1:3] == ["features", "list"]
    run.assert_called_once()
    argv = run.call_args.args[0]
    for feature in DISABLES:
        assert any(argv[i:i + 2] == ["--disable", feature] for i in range(len(argv) - 1))
    # The probe passes only the critical disables: each must be known.
    probe = calls[0][0]
    assert [probe[i + 1] for i in range(len(probe) - 1) if probe[i] == "--disable"] == list(CRITICAL)
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[1:4] == ["--ask-for-approval", "never", "exec"]
    assert {"--ignore-rules", "--ignore-user-config", "--json", "--ephemeral"} <= set(argv)
    assert not any(token.startswith("mcp_servers.") for token in argv)
    assert 'web_search="disabled"' in argv
    assert not {"--enable", "--search", "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox",
                "--add-dir", "--profile", "resume"} & set(argv)
    assert run.call_args.kwargs["cwd"] == calls[0][1]
    assert run.call_args.kwargs["cwd"] != str(request.working_directory)
    assert not Path(run.call_args.kwargs["cwd"]).exists()
    assert result.metadata["noshell_unified_exec_observed"] is True
    assert result.metadata["noshell_verified_by_event_stream"] is True


@pytest.mark.parametrize("task_type", READ_ONLY, ids=["exploration", "review", "architecture"])
def test_exact_noshell_argv_omits_user_mcp_overrides(worker, task_type):
    from modelquad.executable_resolution import admit_executable

    executor, request, run, calls = worker
    names = ("node_repl", "another_server")

    def inventory(argv, cwd, env):
        calls.append((list(argv), cwd, dict(env)))
        if "features" in argv:
            output = listing()
        else:
            output = json.dumps([
                {"name": name, "enabled": f"mcp_servers.{name}.enabled=false" not in argv}
                for name in names
            ])
        return subprocess.CompletedProcess(argv, 0, output, "")

    executor._inventory = inventory
    result = executor.execute(replace(request, task_type=task_type,
                                      model="gpt-6.1-sol", reasoning_effort="low"))
    # One provider-free probe: no MCP inventory, since --ignore-user-config
    # loads no registrations.
    assert [call[0][1:3] for call in calls] == [["features", "list"]]
    run.assert_called_once()
    argv = run.call_args.args[0]
    resolved = executor._resolve()
    assert resolved is not None
    admitted = admit_executable(resolved, cwd=str(request.working_directory), allow_batch=False)
    assert admitted.path is not None
    assert admitted.kind == "binary"
    assert admitted.refusal is None
    expected = [
        admitted.path, "--ask-for-approval", "never", "exec",
        "-m", "gpt-6.1-sol", "-c", "model_reasoning_effort=low",
        *[token for feature in DISABLES for token in ("--disable", feature)],
        "--ignore-user-config", "-c", 'web_search="disabled"',
        "--ignore-rules", "--strict-config", "--ephemeral", "--json",
        "--sandbox", "read-only", "--skip-git-repo-check", "-",
    ]
    assert argv == expected
    overrides = [argv[index + 1] for index, token in enumerate(argv) if token == "-c"]
    assert overrides == ["model_reasoning_effort=low", 'web_search="disabled"']
    assert not any(token.startswith("mcp_servers.") for token in argv)
    assert result.metadata["command"] == [Path(argv[0]).name, *argv[1:]]
    assert result.metadata["noshell_verified_by_event_stream"] is True


@pytest.mark.parametrize("feature", REQUIRED_FALSE, ids=REQUIRED_FALSE)
def test_each_required_feature_true_starts_no_worker(worker, feature):
    executor, request, run, _ = worker
    inventory = Mock(return_value=subprocess.CompletedProcess([], 0, listing(**{feature: True}), ""))
    executor._inventory = inventory
    result = executor.execute(request)
    inventory.assert_called_once()
    assert inventory.call_args.args[0][1:3] == ["features", "list"]
    run.assert_not_called()
    assert result.task_status is WorkerTaskStatus.BLOCKED
    assert result.task_status_reason == "codex_feature_unverified"
    assert result.metadata["worker_started"] is False
    assert attempt_resolution(result) == ("refused", "policy_refused")
    assert attempt_resolution(replace(result, output="normalized refusal")) == ("refused", "policy_refused")


@pytest.mark.parametrize("output,code", [
    pytest.param("", 1, id="unknown-flag"),
    pytest.param("not a feature list", 0, id="parse-failure"),
    pytest.param("shell_tool stable false\n", 0, id="missing-features"),
    pytest.param(listing().replace("browser_use under development false\n", ""), 0, id="missing-critical"),
    pytest.param(listing() + "shell_tool stable false\n", 0, id="duplicate-feature"),
], ids=None)
def test_unqualified_listings_refuse(worker, output, code):
    executor, request, run, _ = worker
    inventory = Mock(return_value=subprocess.CompletedProcess([], code, output, "Unknown flag or private detail"))
    executor._inventory = inventory
    result = executor.execute(request)
    inventory.assert_called_once()
    assert inventory.call_args.args[0][1:3] == ["features", "list"]
    run.assert_not_called()
    assert result.task_status_reason == "codex_feature_unverified"
    assert "private detail" not in str(result.to_dict())


@pytest.mark.parametrize("version", [None, "invalid", "0.155.0"], ids=["unknown-version", "bad-version", "too-old"])
def test_unknown_version_refuses_without_listing(worker, monkeypatch, version):
    executor, request, run, calls = worker
    monkeypatch.setattr(codex, "_probe", lambda executable: version)
    result = executor.execute(request)
    assert calls == []
    run.assert_not_called()
    assert result.task_status_reason == "codex_version_unsupported"
    assert result.error.endswith(noshell.REFUSAL_MESSAGES["codex_version_unsupported"])
    assert attempt_resolution(result) == ("refused", "policy_refused")


def test_qualification_cache_is_digest_and_version_bound(tmp_path):
    executable = tmp_path / "inert.exe"
    executable.write_bytes(b"inert executable fixture")
    noshell._QUALIFICATIONS.clear()
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, listing(), ""))
    for version in ("0.160.0", "0.160.0", "0.161.0"):
        assert noshell.qualify(executable, version, cwd=str(tmp_path), env={}, runner=runner).qualified
    assert runner.call_count == 2
    executable.write_bytes(b"different inert executable fixture")
    assert noshell.qualify(executable, "0.161.0", cwd=str(tmp_path), env={}, runner=runner).qualified
    assert runner.call_count == 3


@pytest.mark.parametrize("kind", ["item.started", "item.updated", "item.completed"], ids=["started", "updated", "completed"])
@pytest.mark.parametrize("item_type", ["command_execution", "local_shell_call", "file_change", "patch", "apply_patch", "mcp_tool_call"],
                         ids=["command", "local-shell", "file-change", "patch", "apply-patch", "mcp"])
def test_forbidden_events_withhold_answer(worker, kind, item_type):
    executor, request, run, _ = worker
    run.return_value = subprocess.CompletedProcess([], 0, stream({"type": item_type, "command": "private command"},
                                                              kind=kind, text="PRIVATE NORMAL ANSWER"), "")
    result = executor.execute(request)
    assert result.task_status_reason == "noshell_violated"
    assert result.output == ""
    assert result.success is False
    assert result.safe_to_retry is False
    assert result.metadata["noshell_violated"] is True
    assert attempt_resolution(result) == ("failed", "exception")
    assert "PRIVATE" not in str(result.to_dict())
    assert "private command" not in str(result.to_dict())
    assert "worker_has_no_command_tools" not in result.limitations


@pytest.mark.parametrize("task_type", READ_ONLY, ids=["exploration", "review", "architecture"])
@pytest.mark.parametrize("image", ["powershell.exe", "cmd.exe", "codex.exe"],
                         ids=["powershell", "cmd", "uncertified-codex-child"])
def test_clean_stream_with_unexpected_descendant_withholds_answer(worker, task_type, image):
    executor, request, run, calls = worker
    completed = subprocess.CompletedProcess([], 0, stream(text="PRIVATE DESCENDANT ANSWER"), "")
    completed.worker_descendants = {
        "enumerated": True, "method": "synthetic", "observed_at": 1.0,
        "descendants": [{"pid": 42, "image": image, "created": 1.0}], "truncated": False,
    }
    run.return_value = completed
    result = executor.execute(replace(request, task_type=task_type))
    assert calls[0][0][1:3] == ["features", "list"]
    run.assert_called_once()
    assert result.metadata["noshell_qualified"] is True
    assert result.metadata["worker_descendants"]["descendants"] == completed.worker_descendants["descendants"]
    assert result.metadata["noshell_violated"] is True
    assert result.metadata["noshell_verified_by_event_stream"] is False
    assert result.metadata["noshell_collaboration_called"] is False
    assert result.output == ""
    assert result.success is False
    assert result.safe_to_retry is False
    assert result.task_status_reason == noshell.VIOLATED
    assert attempt_resolution(result) == ("failed", "exception")
    assert noshell.VIOLATED in result.limitations
    assert "worker_has_no_command_tools" not in result.limitations
    assert "noshell_verified_by_event_stream" not in result.limitations
    assert "PRIVATE" not in str(result.to_dict())


def test_clean_stream_with_empty_descendant_census_keeps_certification(worker):
    executor, request, run, _ = worker
    run.return_value.worker_descendants = {"enumerated": True, "descendants": [], "truncated": False}
    result = executor.execute(request)
    run.assert_called_once()
    assert result.output == "Diagnosis supplied."
    assert result.metadata["noshell_verified_by_event_stream"] is True
    assert result.metadata["noshell_violated"] is False
    assert "worker_has_no_command_tools" in result.limitations


@pytest.mark.parametrize("fixture,collaboration", [("events.jsonl", False), ("events-2.jsonl", True)],
                         ids=["native-probe-1", "native-probe-2"])
def test_recorded_probe_streams(fixture, collaboration):
    observed = noshell.verify_stream((FIXTURES / fixture).read_text(encoding="utf-8"))
    assert observed.verified is (not collaboration)
    assert observed.violated is collaboration
    assert observed.collaboration_called is collaboration


@pytest.mark.parametrize("raw", [
    pytest.param("plain answer", id="plain-text"),
    pytest.param('{"type":"turn.completed",', id="truncated-json"),
    pytest.param('{"type":"turn.started"}\n', id="unfinished-turn"),
    pytest.param('{"type":"turn.started","type":"turn.completed"}', id="duplicate-json"),
    pytest.param(stream({"type": "unknown_tool"}), id="unknown-tool"),
], ids=None)
def test_malformed_streams_never_degrade_to_plain_answers(worker, raw):
    executor, request, run, _ = worker
    run.return_value = subprocess.CompletedProcess([], 0, raw, "private stderr answer")
    result = executor.execute(request)
    assert result.output == ""
    assert result.task_status_reason == noshell.UNAVAILABLE
    assert result.metadata["noshell_verified_by_event_stream"] is False
    assert attempt_resolution(result) == ("failed", "exception")


@pytest.mark.parametrize("outcome", ["timeout", "stopped"], ids=["timeout", "stopped"])
def test_partial_exception_streams_cannot_leak(worker, outcome):
    from modelquad.procutil import WorkerStopRequested
    executor, request, run, _ = worker
    raw = stream({"type": "command_execution"}, text="private result")
    run.side_effect = (subprocess.TimeoutExpired([], 1, output=raw) if outcome == "timeout"
                       else WorkerStopRequested([], stdout=raw))
    result = executor.execute(request)
    assert result.output == ""
    assert result.task_status_reason == noshell.VIOLATED
    assert "private result" not in str(result.to_dict())


@pytest.mark.parametrize("task_type", READ_ONLY, ids=["exploration", "review", "architecture"])
def test_readonly_resume_refuses_even_known_cli(worker, task_type):
    executor, request, run, calls = worker
    result = executor.execute(replace(request, task_type=task_type, session_id="inert-session"))
    assert calls == []
    run.assert_not_called()
    assert result.task_status_reason == "read_only_resume_unpinned"


@pytest.mark.parametrize("task_type", WRITE, ids=["implementation", "debugging", "testing", "trivial"])
def test_writable_tasks_are_refused_before_any_process(worker, task_type):
    executor, request, run, calls = worker
    result = executor.execute(replace(request, task_type=task_type))
    run.assert_not_called()
    assert calls == []
    assert result.task_status_reason == "task_type_not_read_only"
    assert result.metadata["worker_started"] is False
    assert attempt_resolution(result) == ("refused", "policy_refused")


def with_files(request, *names, approval=None):
    """The request as the MCP tools build it: the approved files in metadata."""
    metadata = {**request.metadata, "supplied_files": list(names)}
    if approval is not None:
        metadata["approval"] = approval
    return replace(request, metadata=metadata)


def test_executor_supplies_only_the_explicit_files(worker):
    executor, request, run, _ = worker
    (request.working_directory / "named.py").write_text("def named():\n    return 1\n", encoding="utf-8")
    (request.working_directory / "failure.py").write_text("raise ValueError('fixture')\n", encoding="utf-8")
    (request.working_directory / "listed.py").write_text("LISTED ONLY IN TASK TEXT\n", encoding="utf-8")
    request = replace(request, prompt='Relevant files:\n- listed.py\n\nCurrent failure evidence:\n'
                                      'File "failure.py", line 1\ndiff --git a/listed.py b/listed.py')
    result = executor.execute(with_files(request, "named.py"))
    prompt = run.call_args.kwargs["input"]
    assert "def named():" in prompt
    assert "1: def named():" in prompt
    assert "You have no command or file tools" in prompt
    # A name in the task text, a traceback or a diff is never read or sent.
    assert "raise ValueError" not in prompt
    assert "LISTED ONLY IN TASK TEXT" not in prompt
    audit = result.metadata["file_audit"]
    assert audit["approval"] == "delegate"
    assert [item["path"] for item in audit["sent"]] == ["named.py"]
    sent = "def named():\n    return 1\n".encode("utf-8")
    assert audit["sent"][0]["bytes"] == len(sent)
    import hashlib
    assert audit["sent"][0]["sha256"] == hashlib.sha256(sent).hexdigest()[:16]
    assert audit["total_bytes"] == len(sent)


def test_without_files_nothing_is_read(worker, monkeypatch):
    executor, request, run, _ = worker
    (request.working_directory / "named.txt").write_text("NOT APPROVED\n", encoding="utf-8")
    read = Mock(side_effect=AssertionError("no file may be opened without files"))
    monkeypatch.setattr(noshell, "_read_file", read)
    result = executor.execute(request)  # The task text names named.txt.
    run.assert_called_once()
    read.assert_not_called()
    assert "NOT APPROVED" not in run.call_args.kwargs["input"]
    assert result.metadata["file_audit"]["sent"] == []


@pytest.mark.parametrize("change_source", [False, True], ids=["unchanged", "changed-after-preparation"])
def test_selected_notes_stay_bounded_unique_and_frozen_in_noshell_worker(worker, monkeypatch, change_source):
    from modelquad import project_notes

    executor, request, run, _ = worker
    root = request.working_directory
    (root / "named.py").write_text("supplied task evidence\n", encoding="utf-8")
    (root / "advisory_only.py").write_text("unrequested advisory-linked body\n", encoding="utf-8")
    source = (project_notes.SCAFFOLD + '\n## linked\nDate: 2000-01-01T00:00:00Z\n'
              'Files: ["named.py"]\nDecision: selected decision: See PROJECT_NOTES.md and advisory_only.py.\n')
    for index in range(10):
        source += (f'\n## global-{index}\nDate: 2000-01-01T00:00:00Z\n'
                   f'Files: ["*"]\nDecision: global decision {index}.\n')
    source += ('\n## irrelevant\nDate: 2000-01-01T00:00:00Z\nFiles: ["unrelated.py"]\n'
               'Decision: irrelevant private decision.\n\n## malformed\nmalformed private decision\n')
    assert len(source.encode("utf-8")) < noshell.MAX_FILE_BYTES
    notes_path = root / "PROJECT_NOTES.md"
    notes_path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(project_notes, "_validated_root", lambda *args: root)
    monkeypatch.setattr(project_notes, "_history", lambda *args: {})
    frozen = project_notes.attach('Relevant files:\n- named.py\n- PROJECT_NOTES.md\n', root, None,
                                  ("named.py",))
    packet_text = frozen.split(project_notes.PACKET_MARKER + "\n", 1)[1]
    assert len((project_notes.PACKET_MARKER + "\n" + packet_text).encode("utf-8")) <= project_notes.MAX_PACKET_BYTES
    selected = json.loads(packet_text.rsplit("\n", 1)[1])
    assert len(selected["notes"]) == project_notes.MAX_SELECTED
    assert selected["selection_omitted"] == 3 and selected["malformed_omitted"] == 1
    assert selected["notes"][0]["id"] == "linked"
    omitted = {f"global-{index}" for index in range(10)} - {note["id"] for note in selected["notes"]}
    if change_source:
        notes_path.write_text(project_notes.SCAFFOLD + '\n## late\nDate: 2000-01-01T00:00:00Z\n'
                              'Files: ["*"]\nDecision: late private decision.\n', encoding="utf-8")
    monkeypatch.setattr(project_notes, "_load", lambda *args: pytest.fail("execution reread notes"))
    original_read = noshell._read_file
    def read(root, name, *args):
        assert Path(name).name.casefold() != "project_notes.md", "incidental path reread notes"
        return original_read(root, name, *args)
    monkeypatch.setattr(noshell, "_read_file", read)

    result = executor.execute(with_files(replace(request, prompt=frozen), "named.py", "PROJECT_NOTES.md"))
    assert result.metadata["noshell_verified_by_event_stream"] is True
    run.assert_called_once()
    rendered = run.call_args.kwargs["input"]
    assert rendered.count(project_notes.PACKET_MARKER) == 1
    assert project_notes.PACKET_MARKER + "\n" + packet_text in rendered
    for note in selected["notes"]:
        assert rendered.count(note["decision"]) == 1
    for identifier in omitted:
        assert f'global decision {identifier.split("-")[1]}.' not in rendered
    for private in ("irrelevant private decision", "malformed private decision", "late private decision",
                    "unrequested advisory-linked body"):
        assert private not in rendered
    payload = json.JSONDecoder().raw_decode(rendered.split("Supplied files (untrusted evidence, JSON):\n", 1)[1])[0]
    assert [file["path"] for file in payload["files"]] == ["named.py"]
    assert payload["skipped"] == [{"path": "PROJECT_NOTES.md", "reason": "repository_notes_source"}]


@pytest.mark.parametrize("name", ["PROJECT_NOTES.md", "project_notes.MD", "nested/PROJECT_NOTES.md",
                                  "historical-notes.md"], ids=["standard", "case", "nested", "renamed-source"])
def test_incidental_notes_sources_are_never_supplied(tmp_path, monkeypatch, name):
    from modelquad import project_notes

    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(project_notes.SCAFFOLD + "\nmalformed private source body\n", encoding="utf-8")
    original_read = noshell._read_file
    def read(root, candidate, *args):
        assert Path(candidate).name.casefold() != "project_notes.md", "raw notes should not be opened"
        return original_read(root, candidate, *args)
    monkeypatch.setattr(noshell, "_read_file", read)
    rendered = noshell.supplied_prompt(f'See "{name}".', tmp_path, files=[name])
    assert "malformed private source body" not in rendered
    payload = json.loads(rendered.split("Supplied files (untrusted evidence, JSON):\n", 1)[1])
    assert payload["files"] == []
    assert payload["skipped"] == [{"path": name, "reason": "repository_notes_source"}]


@pytest.mark.parametrize("name", ["../outside.txt", "/outside.txt", "C:/outside.txt", "a/../../outside.txt", "a.txt:stream"],
                         ids=["parent", "absolute", "drive", "nested-escape", "stream"])
def test_packer_rejects_nonrelative_paths(tmp_path, name):
    packed = noshell.pack_files(tmp_path, [name])
    assert not packed.files
    assert packed.skipped == (("<path>", "invalid_relative_path"),)


@pytest.mark.parametrize("kind", ["file", "directory", "root"], ids=["file-symlink", "directory-symlink", "root-symlink"])
def test_packer_rejects_links(tmp_path, kind):
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "file.txt").write_text("inside or outside contents", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(safe / "file.txt" if kind == "file" else safe, target_is_directory=kind != "file")
    except (OSError, NotImplementedError):
        pytest.skip("platform does not permit creating a symlink")
    root = link if kind == "root" else tmp_path
    name = "file.txt" if kind == "root" else "link" if kind == "file" else "link/file.txt"
    packed = noshell.pack_files(root, [name])
    assert not packed.files
    assert packed.skipped == ((name, "linked_path"),)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_packer_rejects_junction(tmp_path):
    # Native Python API only: no shell or provider process creates the fixture.
    import _winapi
    target = tmp_path / "target"
    target.mkdir()
    (target / "file.txt").write_text("private junction target", encoding="utf-8")
    junction = tmp_path / "junction"
    if not hasattr(_winapi, "CreateJunction"):
        pytest.skip("Python does not expose the native junction fixture API")
    try:
        _winapi.CreateJunction(str(target), str(junction))
    except OSError:
        pytest.skip("platform does not permit creating a junction")
    packed = noshell.pack_files(tmp_path, ["junction/file.txt"])
    assert packed.skipped == (("junction/file.txt", "linked_path"),)
    assert not packed.files


def test_packer_rejects_hardlinks(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("private hardlink contents", encoding="utf-8")
    try:
        os.link(source, tmp_path / "linked.txt")
    except OSError:
        pytest.skip("platform does not permit hardlinks")
    assert noshell.pack_files(tmp_path, ["linked.txt"]).skipped == (("linked.txt", "not_regular_file"),)


def test_replaced_open_file_is_not_supplied(tmp_path, monkeypatch):
    path = tmp_path / "target.txt"
    path.write_text("original file", encoding="utf-8")
    original = noshell.os.open
    def replace_before_open(candidate, flags):
        path.unlink()
        path.write_text("replaced private file", encoding="utf-8")
        return original(candidate, flags)
    monkeypatch.setattr(noshell.os, "open", replace_before_open)
    packed = noshell.pack_files(tmp_path, ["target.txt"])
    assert not packed.files
    assert packed.skipped == (("target.txt", "file_changed"),)


def test_quoted_failure_references_with_spaces_are_inside_only(tmp_path):
    path = tmp_path / "a file.py"
    path.write_text("print('provided fixture')\n", encoding="utf-8")
    prompt = f'File "{path}", line 1\nFile "{tmp_path.parent / "outside.py"}", line 2'
    references = noshell.named_files(prompt, tmp_path)
    assert "a file.py" in references
    packed = noshell.pack_files(tmp_path, references)
    assert packed.files == (("a file.py", "print('provided fixture')\n"),)
    assert ("<path>", "invalid_relative_path") in packed.skipped


def test_binary_secrets_and_truthful_skips(tmp_path):
    (tmp_path / "source.py").write_text('api_key="' + "sk-" + "q" * 20 + '"\nprint(1)\n', encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"abc\0def")
    (tmp_path / ".env").write_text("private config", encoding="utf-8")
    packed = noshell.pack_files(tmp_path, ["source.py", "binary.bin", ".env", "absent.txt"])
    assert len(packed.files) == 1
    assert "<credential>" in packed.files[0][1]
    assert "q" * 20 not in packed.render()
    assert packed.skipped == (("binary.bin", "binary_file"), ("<path>", "secret_file"), ("absent.txt", "missing_file"))


def test_file_total_count_and_rendered_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(noshell, "MAX_FILE_BYTES", 8)
    monkeypatch.setattr(noshell, "MAX_TOTAL_BYTES", 12)
    monkeypatch.setattr(noshell, "MAX_FILES", 2)
    for name, text in [("a.txt", "12345678"), ("b.txt", "56789"), ("c.txt", "abc"), ("d.txt", "a"), ("big.txt", "z" * 9)]:
        (tmp_path / name).write_text(text, encoding="utf-8")
    packed = noshell.pack_files(tmp_path, ["big.txt", "a.txt", "b.txt", "c.txt", "d.txt"])
    assert [name for name, _ in packed.files] == ["a.txt", "c.txt"]
    assert packed.skipped == (("big.txt", "file_byte_cap"), ("b.txt", "total_byte_cap"),
                              ("<remaining references, if any>", "file_count_cap"))
    monkeypatch.setattr(noshell, "MAX_PROMPT_BYTES", 16)
    with pytest.raises(ValueError, match="rendered_prompt_cap"):
        noshell.supplied_prompt("Inspect a.txt", tmp_path)


@pytest.mark.parametrize("state,expected", [
    pytest.param({"noshell_qualified": True, "noshell_verified_by_event_stream": True}, "worker_has_no_command_tools", id="qualified-clean"),
    pytest.param({"noshell_qualified": False}, "worker_command_refusal_not_enforced", id="unqualified"),
    pytest.param({"noshell_qualified": True, "noshell_violated": True}, "noshell_violated", id="violated"),
], ids=None)
def test_truthful_labels(state, expected):
    labels = noshell.limitations(state, ("worker_command_refusal_not_enforced", "worker_reads_not_confined"))
    assert expected in labels
    assert ("noshell_verified_by_event_stream" in labels) is bool(state.get("noshell_verified_by_event_stream"))


@pytest.mark.skipif(os.name != "nt", reason="Windows path identity is case insensitive")
def test_windows_case_alias_uses_admitted_canonical_path(worker, monkeypatch):
    from modelquad import executable_resolution
    executor, request, run, calls = worker
    admit = executable_resolution.admit_executable
    admitted_paths = []
    def observed_admission(path, **kwargs):
        result = admit(path, **kwargs)
        admitted_paths.append(result.path)
        return result
    monkeypatch.setattr(executable_resolution, "admit_executable", observed_admission)
    version_paths = []
    monkeypatch.setattr(codex, "_probe", lambda path: version_paths.append(path) or "0.160.0")
    result = executor.execute(request)
    assert result.metadata["noshell_qualified"] is True
    run.assert_called_once()
    assert calls[0][0][1:3] == ["features", "list"]
    canonical = version_paths[0]
    assert canonical in admitted_paths
    assert canonical != os.path.normcase(canonical)  # Fixture has mixed case.
    assert calls[0][0][0] == run.call_args.args[0][0] == canonical


@pytest.mark.parametrize("kind", ["item.started", "item.updated", "item.completed"],
                         ids=["started", "updated", "completed"])
def test_collaboration_withholds_descendant_dependent_answer(worker, kind):
    executor, request, run, _ = worker
    run.return_value = subprocess.CompletedProcess([], 0, stream(
        {"type": "collab_tool_call", "tool": "spawn_agent"}, kind=kind, text="PRIVATE CHILD ANSWER"), "")
    result = executor.execute(request)
    assert result.output == ""
    assert result.task_status_reason == noshell.VIOLATED
    assert result.metadata["noshell_collaboration_called"] is True
    assert result.metadata["noshell_verified_by_event_stream"] is False
    assert attempt_resolution(result) == ("failed", "exception")
    assert noshell.VIOLATED in result.limitations
    assert "worker_has_no_command_tools" not in result.limitations
    assert "PRIVATE" not in str(result.to_dict())


def test_collaboration_facts_override_stale_success_labels():
    state = {"noshell_qualified": True, "noshell_verified_by_event_stream": True,
             "noshell_collaboration_called": True}
    labels = noshell.limitations(state, ("worker_command_refusal_not_enforced", "worker_has_no_command_tools",
                                        "noshell_verified_by_event_stream"))
    assert noshell.VIOLATED in labels
    assert "worker_has_no_command_tools" not in labels
    assert "noshell_verified_by_event_stream" not in labels
    assert "worker_command_refusal_not_enforced" in labels


@pytest.mark.parametrize("content", [
    pytest.param('{"password":"example-sensitive-value"}', id="quoted-json"),
    pytest.param("{'client_secret': 'example-sensitive-value'}", id="quoted-python"),
    pytest.param('"refresh_token": "example-sensitive-value"', id="quoted-yaml"),
    pytest.param('{"aws_secret_access_key":"example-sensitive-value"}', id="prefixed-key"),
    pytest.param('{"secret_key":"example-sensitive-value"}', id="secret-key"),
    pytest.param(json.dumps({"password": 'example-sensitive-value with spaces and "quotes"'}), id="escaped-json"),
], ids=None)
def test_structured_secrets_redacted_in_files_and_failure_text(tmp_path, content):
    (tmp_path / "config.json").write_text(content, encoding="utf-8")
    packed = noshell.pack_files(tmp_path, ["config.json"])
    assert len(packed.files) == 1
    assert "<credential>" in packed.render()
    assert "example-sensitive-value" not in packed.render()
    prompt = noshell.supplied_prompt("Current failure evidence:\n" + content, tmp_path)
    assert "<credential>" in prompt
    assert "example-sensitive-value" not in prompt


@pytest.mark.parametrize("content", [
    pytest.param('{"password":{"value":"example-sensitive-value"}}', id="nested-secret"),
    pytest.param('password: |\n  example-sensitive-value', id="yaml-block"),
    pytest.param('password = "prefix" + "example-sensitive-value"', id="expression"),
    pytest.param(r'{"\u0070assword":"example-sensitive-value"}', id="encoded-key"),
    pytest.param(r'{\"password\":\"example-sensitive-value\"}', id="escaped-key"),
    pytest.param('token = next(it)\npassword: field\ndef f(password=None):\n'
                 '    token = request.headers.get("X")\n', id="ordinary-code"),
], ids=None)
def test_ambiguous_secret_names_are_never_refused(tmp_path, content):
    # A secret-like name with no recognisable credential is ordinary code.
    (tmp_path / "config.py").write_text(content, encoding="utf-8")
    packed = noshell.pack_files(tmp_path, ["config.py"])
    assert packed.files == (("config.py", content),)
    assert not packed.skipped
    prompt = noshell.supplied_prompt("Current failure evidence:\n" + content, tmp_path, files=["config.py"])
    assert prompt.count(content) == 1


@pytest.mark.parametrize("name", [".npmrc", ".pypirc", ".netrc", ".git-credentials",
                                  ".yarnrc.yml", "pip.ini", "nuget.config"],
                         ids=["npm", "pypi", "netrc", "git", "yarn", "pip", "nuget"])
def test_additional_credential_stores_are_not_opened(tmp_path, monkeypatch, name):
    (tmp_path / name).write_text("inert credential store fixture", encoding="utf-8")
    read = Mock(side_effect=AssertionError("credential store must not be opened"))
    monkeypatch.setattr(noshell, "_read_file", read)
    assert noshell.pack_files(tmp_path, [name]).skipped == (("<path>", "secret_file"),)
    read.assert_not_called()


def test_rejected_candidates_and_omissions_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(noshell, "MAX_CANDIDATES", 3)
    monkeypatch.setattr(noshell, "MAX_SKIPPED", 2)
    candidates = Mock(side_effect=["missing1.txt", "missing2.txt", "never.txt"])
    def names():
        while True:
            yield candidates()
    packed = noshell.pack_files(tmp_path, names())
    assert candidates.call_count == 2
    assert packed.skipped == (("missing1.txt", "missing_file"), ("missing2.txt", "missing_file"),
                              ("<remaining references, if any>", "omission_count_cap"))


def test_duplicate_candidates_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(noshell, "MAX_CANDIDATES", 3)
    candidates = Mock(return_value="missing.txt")
    def names():
        while True:
            yield candidates()
    packed = noshell.pack_files(tmp_path, names())
    assert candidates.call_count == 3
    assert packed.skipped == (("missing.txt", "missing_file"),
                              ("<remaining references, if any>", "candidate_count_cap"))


def test_rejected_bytes_consume_aggregate_read_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(noshell, "MAX_READ_BYTES", 8)
    for name in ("first.bin", "second.bin", "never.txt"):
        (tmp_path / name).write_bytes(b"a\0bc")
    opened = Mock(wraps=noshell.os.open)
    monkeypatch.setattr(noshell.os, "open", opened)
    packed = noshell.pack_files(tmp_path, ["first.bin", "second.bin", "never.txt"])
    assert not packed.files
    assert opened.call_count == 1  # Two four-byte stability reads, then stop.
    assert packed.skipped == (("first.bin", "binary_file"),
                              ("<remaining references, if any>", "read_byte_cap"))


def test_exhausted_content_budget_stops_before_further_opens(tmp_path, monkeypatch):
    monkeypatch.setattr(noshell, "MAX_TOTAL_BYTES", 4)
    (tmp_path / "first.txt").write_bytes(b"abcd")
    opened = Mock(wraps=noshell.os.open)
    monkeypatch.setattr(noshell.os, "open", opened)
    packed = noshell.pack_files(tmp_path, ["first.txt", "never.txt"])
    assert packed.files == (("first.txt", "abcd"),)
    assert opened.call_count == 1
    assert packed.skipped == (("<remaining references, if any>", "total_byte_cap"),)


def test_elapsed_work_budget_stops_before_candidate_or_open(tmp_path, monkeypatch):
    monkeypatch.setattr(noshell.time, "monotonic", Mock(side_effect=[0, noshell.MAX_PACK_SECONDS + 1]))
    candidate = Mock(return_value="never.txt")
    def names():
        yield candidate()
    opened = Mock(side_effect=AssertionError("elapsed budget must stop opening"))
    monkeypatch.setattr(noshell.os, "open", opened)
    packed = noshell.pack_files(tmp_path, names())
    candidate.assert_not_called()
    opened.assert_not_called()
    assert packed.skipped == (("<remaining references, if any>", "elapsed_work_cap"),)


@pytest.mark.parametrize("reference", ["Relevant files:\n- a/config.json\n", 'File "a/config.json", line 1',
                                       "Inspect a/config.json"], ids=["capsule", "traceback", "literal"])
def test_explicit_a_prefix_never_reads_unrequested_root_file(tmp_path, reference):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/config.json").write_text("requested contents", encoding="utf-8")
    (tmp_path / "config.json").write_text("PRIVATE UNREQUESTED CONTENTS", encoding="utf-8")
    names = noshell.named_files(reference, tmp_path)
    assert names == ("a/config.json",)
    packed = noshell.pack_files(tmp_path, names)
    assert packed.files == (("a/config.json", "requested contents"),)
    assert "PRIVATE" not in packed.render()


def test_git_prefixes_only_normalized_in_diff_headers(tmp_path):
    names = noshell.named_files("diff --git a/config.json b/config.json\n"
                                "--- a/config.json\n+++ b/config.json\n"
                                'File "a/failure.py", line 1', tmp_path)
    assert set(names) == {"config.json", "a/failure.py"}
    assert "a/config.json" not in names
    assert "b/config.json" not in names


@pytest.mark.parametrize("size", [16 * 1024 + 1, 128 * 1024], ids=["ordinary-large", "exact-file-boundary"])
def test_ordinary_source_accepted_whole_at_increased_boundary(tmp_path, monkeypatch, size):
    monkeypatch.setattr(noshell, "MAX_PACK_SECONDS", 60)
    content = "#" + "x" * (size - 1)
    (tmp_path / "ordinary.py").write_bytes(content.encode("utf-8"))
    packed = noshell.pack_files(tmp_path, ["ordinary.py"])
    assert packed.files == (("ordinary.py", content),)
    assert noshell.MAX_FILE_BYTES == noshell.MAX_TOTAL_BYTES == 128 * 1024
    assert noshell.MAX_READ_BYTES == 256 * 1024
    assert noshell.MAX_PROMPT_BYTES == 192 * 1024
    assert (noshell.MAX_FILES, noshell.MAX_CANDIDATES, noshell.MAX_SKIPPED) == (32, 64, 64)
    assert noshell.MAX_STREAM_BYTES == 8 * 1024 * 1024


def test_required_file_boundary_plus_one_refuses_without_model_start(worker):
    executor, request, run, calls = worker
    (request.working_directory / "ordinary.py").write_bytes(b"#" + b"x" * (128 * 1024))
    result = executor.execute(with_files(request, "ordinary.py"))
    run.assert_not_called()
    assert not calls  # Supplied-context refusal also precedes provider-free probes.
    assert result.metadata["worker_started"] is False
    assert result.task_status_reason == "supplied_files_over_limit"
    assert attempt_resolution(result) == ("refused", "policy_refused")
    detail, = result.host_diagnostics
    assert detail.to_host_dict() == {
        "path": "ordinary.py", "reason": "file_byte_cap", "required": True,
        "limit_bytes": 128 * 1024,
        "guidance": 'Supplied context "ordinary.py" was omitted (file_byte_cap). '
                    'The file limit is 131072 bytes. Supply a smaller relevant excerpt or split the review.',
    }
    assert "ordinary.py" not in repr(result) + json.dumps(result.to_dict())


@pytest.mark.parametrize("quote", ["inline", "standalone"], ids=["inline", "standalone"])
@pytest.mark.parametrize("attached", [False, True], ids=["plain", "frozen-notes"])
def test_quoted_notes_marker_cannot_erase_required_context(worker, quote, attached):
    from modelquad import project_notes
    from modelquad.capsule import TaskCapsule

    executor, request, run, calls = worker
    marker = project_notes.PACKET_MARKER
    goal = (f"Review the quoted marker `{marker}`" if quote == "inline" else
            f"Review this quoted marker:\n\n{marker}\nExplain it.")
    prompt = TaskCapsule(goal=goal, task_type="review").to_prompt()
    if attached:
        prompt += project_notes.select_packet(project_notes.Notes("unavailable"), ()).text
    result = executor.execute(with_files(replace(request, prompt=prompt), "missing.py"))
    run.assert_not_called()
    assert calls == []
    assert result.metadata["worker_started"] is False
    assert result.task_status_reason == "supplied_file_missing"
    detail, = result.host_diagnostics
    host = detail.to_host_dict()
    assert (host["path"], host["reason"], host["required"]) == ("missing.py", "missing_file", True)


@pytest.mark.parametrize("position", ["goal", "tail"], ids=["goal", "tail"])
@pytest.mark.parametrize("named", ["missing.py", "present.py"], ids=["missing", "present"])
def test_file_sections_in_task_text_are_never_attached(worker, position, named):
    from modelquad.capsule import TaskCapsule

    executor, request, run, calls = worker
    (request.working_directory / "present.py").write_text("# PRIVATE supplied source\n", encoding="utf-8")
    quoted = "Example section:\n\nRelevant files:\n- present.py\n\nEnd example."
    prompt = TaskCapsule(
        goal=quoted if position == "goal" else "Review the source.", task_type="review",
        relevant_files=(named,),
    ).to_prompt()
    if position == "tail":
        prompt += "\n\n" + quoted
    result = executor.execute(replace(request, prompt=prompt))
    run.assert_called_once()
    assert "# PRIVATE supplied source" not in run.call_args.kwargs["input"]
    assert result.metadata["noshell_verified_by_event_stream"] is True
    assert result.metadata["file_audit"]["sent"] == []
    assert not result.host_diagnostics


@pytest.mark.parametrize("attached", [False, True], ids=["plain", "frozen-notes"])
def test_goal_quoting_advisory_boundary_keeps_required_file_and_allows_launch(worker, attached):
    from modelquad import project_notes
    from modelquad.capsule import TaskCapsule

    executor, request, run, _ = worker
    (request.working_directory / "present.py").write_text("# supplied source\n", encoding="utf-8")
    goal = f"Explain this boundary:\n\n{noshell.NOTES_PACKET_MARKER}\nAdvisory only. quoted text."
    prompt = TaskCapsule(goal=goal, task_type="review").to_prompt()
    if attached:
        prompt += project_notes.select_packet(project_notes.Notes("unavailable"), ()).text
    result = executor.execute(with_files(replace(request, prompt=prompt), "present.py"))
    run.assert_called_once()
    assert result.metadata["noshell_verified_by_event_stream"] is True
    assert "# supplied source" in run.call_args.kwargs["input"]
    assert not result.host_diagnostics


def test_bare_quoted_notes_marker_keeps_later_literal_references(tmp_path):
    prompt = f"Explain this marker:\n\n{noshell.NOTES_PACKET_MARKER}\nThen inspect ordinary.py"
    assert noshell.named_files(prompt, tmp_path) == ("ordinary.py",)


def test_a_file_named_only_in_the_task_is_not_read(worker):
    executor, request, run, _ = worker
    answer = 'Diagnosis supplied.\n```json\n' + json.dumps({
        "task_status": "completed", "reason": None, "limitations": [],
        "evidence": {"summary": "Discussed the supplied hypothesis without optional evidence."},
    }) + '\n```'
    run.return_value = subprocess.CompletedProcess([], 0, stream(text=answer), "")
    (request.working_directory / "large.py").write_bytes(b"x" * (128 * 1024 + 1))
    result = executor.execute(replace(request, prompt="Discuss this hypothesis; optional evidence: large.py"))
    run.assert_called_once()
    assert result.task_completed
    assert result.task_status == WorkerTaskStatus.COMPLETED
    assert result.metadata["noshell_verified_by_event_stream"] is True
    assert not result.host_diagnostics


def test_real_aggregate_limit_guides_required_second_file(tmp_path, monkeypatch):
    monkeypatch.setattr(noshell, "MAX_PACK_SECONDS", 60)
    (tmp_path / "first.py").write_bytes(b"#" * (64 * 1024 + 1))
    (tmp_path / "second.py").write_bytes(b"#" * (64 * 1024))
    with pytest.raises(noshell.SuppliedContextRefused) as caught:
        noshell.supplied_prompt("Review", tmp_path, files=["first.py", "second.py"])
    detail, = caught.value.host_diagnostics
    host = detail.to_host_dict()
    assert (host["path"], host["reason"], host["limit_bytes"], host["required"]) == (
        "second.py", "total_byte_cap", 128 * 1024, True)
    assert "aggregate content" in host["guidance"]
    assert "smaller relevant excerpt or split the review" in host["guidance"]


def test_exact_aggregate_exhaustion_names_required_file_without_launch(worker, monkeypatch):
    monkeypatch.setattr(noshell, "MAX_PACK_SECONDS", 60)
    executor, request, run, calls = worker
    (request.working_directory / "first.py").write_bytes(b"#" * (128 * 1024))
    (request.working_directory / "second.py").write_bytes(b"# second")
    opened = Mock(wraps=noshell.os.open)
    monkeypatch.setattr(noshell.os, "open", opened)
    result = executor.execute(with_files(request, "first.py", "second.py"))
    run.assert_not_called()
    assert calls == []
    assert opened.call_count == 1
    assert result.metadata["worker_started"] is False
    detail, = result.host_diagnostics
    host = detail.to_host_dict()
    assert (host["path"], host["reason"], host["limit_bytes"], host["required"]) == (
        "second.py", "total_byte_cap", 128 * 1024, True)
    assert "smaller relevant excerpt or split the review" in host["guidance"]
    assert "second.py" not in repr(result) + json.dumps(result.to_dict())


def test_full_double_read_budget_exhausted_by_rejected_file(tmp_path, monkeypatch):
    monkeypatch.setattr(noshell, "MAX_PACK_SECONDS", 60)
    (tmp_path / "binary.bin").write_bytes(b"\0" + b"x" * (128 * 1024 - 1))
    (tmp_path / "ordinary.py").write_bytes(b"# ordinary")
    opened = Mock(wraps=noshell.os.open)
    monkeypatch.setattr(noshell.os, "open", opened)
    with pytest.raises(noshell.SuppliedContextRefused) as caught:
        noshell.supplied_prompt("Review", tmp_path, files=["binary.bin", "ordinary.py"])
    assert opened.call_count == 1
    details = [item.to_host_dict() for item in caught.value.host_diagnostics]
    assert [item["reason"] for item in details] == ["binary_file", "read_byte_cap"]
    assert details[-1]["limit_bytes"] == 256 * 1024
    assert details[-1]["path"] == "ordinary.py"
    assert details[-1]["required"] is True


@pytest.mark.parametrize("name", ["../outside.py", ".env", ".ssh/keys.txt", "private.pem"],
                         ids=["invalid", "credential-store", "profile", "key"])
def test_budget_guidance_preserves_sensitive_path_placeholders(tmp_path, monkeypatch, name):
    monkeypatch.setattr(noshell, "MAX_PACK_SECONDS", 60)
    (tmp_path / "first.py").write_bytes(b"#" * (128 * 1024))
    opened = Mock(wraps=noshell.os.open)
    monkeypatch.setattr(noshell.os, "open", opened)
    with pytest.raises(noshell.SuppliedContextRefused) as caught:
        noshell.supplied_prompt("Review", tmp_path, files=["first.py", name, "ordinary.py"])
    assert opened.call_count == 1
    details = [item.to_host_dict() for item in caught.value.host_diagnostics]
    assert [item["path"] for item in details] == ["<path>", "ordinary.py"]
    assert all(item["reason"] == "total_byte_cap" and item["required"] for item in details)
    assert name not in json.dumps(details)


def test_budget_guidance_never_renames_a_processed_secret_refusal(tmp_path, monkeypatch):
    from dataclasses import asdict
    import pickle

    monkeypatch.setattr(noshell, "MAX_PACK_SECONDS", 60)
    content = b'api_key = "sk-' + b"A1b2" * 10 + b'"\n'
    (tmp_path / "config.json").write_bytes(content + b" " * (128 * 1024 - len(content)))
    (tmp_path / "ordinary.py").write_bytes(b"# ordinary")
    packed = noshell.pack_files(tmp_path, ["config.json", "ordinary.py"])
    assert "config.json" not in repr(packed) + json.dumps(asdict(packed))
    assert b"config.json" not in pickle.dumps(packed)
    opened = Mock(wraps=noshell.os.open)
    monkeypatch.setattr(noshell.os, "open", opened)
    with pytest.raises(noshell.SuppliedContextRefused) as caught:
        noshell.supplied_prompt("Review", tmp_path, files=["config.json", "ordinary.py"])
    assert opened.call_count == 1
    details = [item.to_host_dict() for item in caught.value.host_diagnostics]
    assert [(item["path"], item["reason"]) for item in details] == [
        ("<path>", "secret_file"), ("ordinary.py", "read_byte_cap")]
    assert all(item["required"] for item in details)
    assert "config.json" not in json.dumps(details)


def test_budget_guidance_is_bounded_with_explicit_remaining_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr(noshell, "MAX_PACK_SECONDS", 60)
    monkeypatch.setattr(noshell, "MAX_SKIPPED", 2)
    (tmp_path / "first.py").write_bytes(b"#" * (128 * 1024))
    files = ["first.py", *(f"missing{index}.py" for index in range(5))]
    opened = Mock(wraps=noshell.os.open)
    monkeypatch.setattr(noshell.os, "open", opened)
    with pytest.raises(noshell.SuppliedContextRefused) as caught:
        noshell.supplied_prompt("Review", tmp_path, files=files)
    assert opened.call_count == 1
    details = [item.to_host_dict() for item in caught.value.host_diagnostics]
    assert [item["path"] for item in details] == [
        "missing0.py", "missing1.py", "<remaining references, if any>"]
    assert len(details) == noshell.MAX_SKIPPED + 1
    assert all(item["reason"] == "total_byte_cap" and item["required"] for item in details)


def test_unicode_uses_bytes_at_file_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(noshell, "MAX_PACK_SECONDS", 60)
    content = "א" * (64 * 1024)
    (tmp_path / "unicode.py").write_bytes(content.encode("utf-8"))
    assert noshell.pack_files(tmp_path, ["unicode.py"]).files == (("unicode.py", content),)
    (tmp_path / "unicode.py").write_bytes((content + "x").encode("utf-8"))
    assert noshell.pack_files(tmp_path, ["unicode.py"]).skipped == (("unicode.py", "file_byte_cap"),)


def test_json_escaping_refuses_whole_rendered_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(noshell, "MAX_PACK_SECONDS", 60)
    # Source fits both raw content budgets, but escaping exceeds the prompt cap.
    (tmp_path / "escaped.py").write_bytes(b'"\\' * (64 * 1024))
    with pytest.raises(noshell.SuppliedContextRefused) as caught:
        noshell.supplied_prompt("Review", tmp_path, files=["escaped.py"], completion_protocol=True)
    host, = [item.to_host_dict() for item in caught.value.host_diagnostics
             if item.to_host_dict()["reason"] == "rendered_prompt_cap"]
    assert (host["path"], host["reason"], host["limit_bytes"]) == (
        "escaped.py", "rendered_prompt_cap", 192 * 1024)
    assert "complete rendered prompt" in host["guidance"]


def test_redaction_expansion_counts_against_file_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(noshell, "MAX_PACK_SECONDS", 60)
    raw = b'password="x"\n' * 10000
    assert len(raw) < 128 * 1024
    (tmp_path / "expanded.py").write_bytes(raw)
    packed = noshell.pack_files(tmp_path, ["expanded.py"])
    assert not packed.files
    assert packed.skipped == (("expanded.py", "file_byte_cap"),)


def test_completion_protocol_included_in_exact_prompt_boundary(tmp_path, monkeypatch):
    from modelquad.execution.base import append_completion_protocol
    plain = noshell.supplied_prompt("Review supplied evidence", tmp_path)
    complete = append_completion_protocol(plain)
    size = len(complete.encode("utf-8"))
    assert size > len(plain.encode("utf-8"))
    monkeypatch.setattr(noshell, "MAX_PROMPT_BYTES", size)
    assert noshell.supplied_prompt("Review supplied evidence", tmp_path, completion_protocol=True) == complete
    monkeypatch.setattr(noshell, "MAX_PROMPT_BYTES", size - 1)
    with pytest.raises(noshell.SuppliedContextRefused) as caught:
        noshell.supplied_prompt("Review supplied evidence", tmp_path, completion_protocol=True)
    assert caught.value.host_diagnostics[0].to_host_dict()["reason"] == "rendered_prompt_cap"


def test_complete_prompt_exhaustion_prevents_model_launch(worker, monkeypatch):
    from modelquad.execution.base import append_completion_protocol
    executor, request, run, calls = worker
    prompt = "Review supplied evidence"
    complete = append_completion_protocol(noshell.supplied_prompt(prompt, request.working_directory))
    monkeypatch.setattr(noshell, "MAX_PROMPT_BYTES", len(complete.encode("utf-8")) - 1)
    result = executor.execute(replace(request, prompt=prompt))
    run.assert_not_called()
    assert calls == []
    assert result.metadata["worker_started"] is False
    assert result.task_status_reason == "rendered_prompt_cap"
    assert result.host_diagnostics[0].to_host_dict()["reason"] == "rendered_prompt_cap"


@pytest.mark.parametrize("name", ["../outside.py", ".env", ".ssh/keys.txt", "config.json"],
                         ids=["invalid", "credential-store", "profile", "recognisable-secret"])
def test_sensitive_required_omissions_use_placeholder_guidance(worker, name):
    executor, request, run, _ = worker
    if name == "config.json":
        (request.working_directory / name).write_text(
            "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIfixture\n", encoding="utf-8")
    result = executor.execute(with_files(request, name))
    run.assert_not_called()
    host, = [item.to_host_dict() for item in result.host_diagnostics]
    assert host["path"] == "<path>"
    assert host["required"] is True
    assert name not in host["guidance"]


def test_packing_a_maximal_single_token_file_stays_within_the_default_work_cap(tmp_path):
    # Train 38: the unanchored triple-quote secret screen backtracked from every
    # start position; one 128 KiB token took about two minutes to pack.
    from modelquad.execution import noshell
    (tmp_path / "ordinary.py").write_text("#" + "x" * (noshell.MAX_FILE_BYTES - 1), encoding="utf-8")
    started = time.monotonic()
    noshell.pack_files(tmp_path, ["ordinary.py"])
    assert time.monotonic() - started < noshell.MAX_PACK_SECONDS


# Credential shapes the 2026-11-04 security review found passing unchanged.
# Built from parts so the test file itself holds no credential-shaped text.
_B64 = "Zm9vYmFyYmF6cXV4" * 5 + "Zm9vYmFy=="


@pytest.mark.parametrize("name,content", [
    ("settings.py", 'SQLALCHEMY_DATABASE_URI = "postgresql://admin:' + "Pr0dPassw0rd9" + '@db.internal:5432/app"\n'),
    ("docker-compose.yml", "DATABASE_URL: mongodb+srv://svc:" + "Zx81kQ0pLmn2" + "@cluster0.example.net/db\n"),
    ("cache.py", 'REDIS = "redis://default:' + "s3cretValue77" + '@cache:6379/0"\n'),
], ids=["postgres", "mongodb-srv", "redis"])
def test_credentials_in_any_url_scheme_are_redacted(tmp_path, name, content):
    (tmp_path / name).write_text(content, encoding="utf-8")
    packed = noshell.pack_files(tmp_path, [name])
    assert len(packed.files) == 1
    text = packed.files[0][1]
    assert noshell.CREDENTIAL_MARKER + "@" in text and "Passw0rd" not in text and "Zx81" not in text


@pytest.mark.parametrize("name,content", [
    ("pay.js", 'const stripe = Stripe("sk_' + 'live_' + "51HqAbCdEfGhIjKlMnOpQrStUv" + '");\n'),
    ("pay_test.js", 'const stripe = Stripe("rk_' + 'test_' + "51HqAbCdEfGhIjKlMnOpQrStUv" + '");\n'),
    ("notify.py", 'URL = "https://hooks.' + 'slack.com/services/T01ABCDEF/B02GHIJKL/'
     + "aBcDeFgHiJkLmNoPqRsTuVwX" + '"\n'),
    ("appsettings.json", '{"AzureWebJobsStorage": "DefaultEndpointsProtocol=https;AccountName=x;Account'
     + "Key=" + _B64 + ';EndpointSuffix=core.windows.net"}\n'),
    ("config.json", '{"auths": {"registry.example": {"auth": "' + "dXNlcjpQcjBkUGFzc3cwcmQ5" + '"}}}\n'),
], ids=["stripe-live", "stripe-restricted", "slack-webhook", "azure-account-key", "docker-auth"])
def test_recognisable_service_credentials_are_refused(tmp_path, name, content):
    (tmp_path / name).write_text(content, encoding="utf-8")
    packed = noshell.pack_files(tmp_path, [name])
    assert packed.files == ()
    assert packed.skipped == (("<path>", "secret_file"),)


@pytest.mark.parametrize("name", [".docker/config.json", ".gnupg/pubring.kbx", "infra/prod.tfstate",
                                  "infra/prod.tfstate.backup", "infra/prod.tfvars", ".pgpass",
                                  ".my.cnf", ".s3cfg", ".boto", ".vault-token", ".htpasswd",
                                  "keys/release.jks", "keys/app.keystore"])
def test_more_credential_stores_are_not_opened(tmp_path, monkeypatch, name):
    path = tmp_path.joinpath(*name.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("inert credential store fixture", encoding="utf-8")
    read = Mock(side_effect=AssertionError("credential store must not be opened"))
    monkeypatch.setattr(noshell, "_read_file", read)
    assert noshell.pack_files(tmp_path, [name]).skipped == (("<path>", "secret_file"),)
    read.assert_not_called()


def test_auth_named_code_stays_ordinary(tmp_path):
    content = "auth = make_auth(user)\nif auth:\n    use(auth)\nsettings = {'auth': True}\n"
    (tmp_path / "client.py").write_text(content, encoding="utf-8")
    assert noshell.pack_files(tmp_path, ["client.py"]).files == (("client.py", content),)
