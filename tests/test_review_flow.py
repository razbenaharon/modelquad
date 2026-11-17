# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The one review flow: its gates, the context package, the run and the record.

A fake executor stands in for Codex here (it packs the files exactly as the
real one does and returns a canned answer). What the real executor does with a
request is in tests/test_codex_read_only.py and tests/test_noshell_workers.py;
nothing in this file shows that Codex itself behaves.
"""

from __future__ import annotations

import json
import sqlite3
import threading

import anyio
import pytest

from modelquad import disclosure, grant, repo_settings, review, review_ledger, switch
from modelquad.adapters.server_instructions import preferences_path
from modelquad.execution import noshell
from modelquad.execution.base import ExecutionResult, WorkerTaskStatus
from modelquad.mcp import server as review_server, worker_guard

ENVELOPE = '\n```json\n{"task_status": "completed", "reason": null, "limitations": [], ' \
           '"files_needed": %s, "evidence": {"summary": "read it"}}\n```\n'


class FakeExecutor:
    """Packs like the real executor, then answers without starting anything."""

    def __init__(self, answer="FINDINGS: none", needed=()):
        self.answer = answer + ENVELOPE % json.dumps(list(needed))
        self.requests = []
        self.before_answer = None

    def execute(self, request):
        self.requests.append(request)
        report = {}
        noshell.supplied_prompt(request.prompt, request.working_directory,
                                files=request.metadata["supplied_files"],
                                completion_protocol=True, report=report)
        if noshell.changed_since_approval(report, request.metadata["approved_digests"]):
            return ExecutionResult(provider="codex", success=False, output="", exit_code=None,
                                   task_status=WorkerTaskStatus.BLOCKED,
                                   task_status_reason="supplied_file_changed",
                                   metadata={"worker_started": False})
        if request.on_spawn is not None:
            request.on_spawn(4242)
        if self.before_answer is not None:
            self.before_answer()
        return ExecutionResult(
            provider="codex", success=True, output=self.answer, exit_code=0,
            task_status=WorkerTaskStatus.COMPLETED,
            metadata={"worker_started": True, "command": ["codex.exe", "C:\\secret\\path"],
                      "file_audit": noshell.file_audit(report, "delegate"),
                      "worker_tokens": {"input_tokens": 1200, "cached_input_tokens": 200,
                                        "output_tokens": 80}})


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (folder / "util.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
    return folder


def request_for(project, **changes):
    values = dict(task_type="review", prompt="Review the change to run().",
                  working_directory=str(project), files=("app.py",))
    values.update(changes)
    return review.ReviewRequest(**values)


# ----------------------------------------------------------------- the package


def test_the_package_says_what_would_be_sent_and_starts_nothing(project):
    shown = review.package(request_for(project, files=("app.py", "util.py")))

    assert [item["path"] for item in shown["files"]] == ["app.py", "util.py"]
    assert shown["file_count"] == 2
    assert shown["total_bytes"] == sum(item["bytes"] for item in shown["files"])
    assert all(len(item["sha256"]) == 16 for item in shown["files"])
    assert len(shown["package_id"]) == 32
    assert shown["limits"]["max_files"] == noshell.MAX_FILES
    # Metadata only: no line of either file is in the manifest.
    assert "return 1" not in json.dumps(shown)


def test_the_same_content_has_the_same_package_id_and_a_change_has_another(project):
    first = review.package(request_for(project))
    assert review.package(request_for(project))["package_id"] == first["package_id"]

    (project / "app.py").write_text("def run():\n    return 3\n", encoding="utf-8")
    assert review.package(request_for(project))["package_id"] != first["package_id"]
    assert review.package(request_for(project, prompt="Another task."))["package_id"] != first["package_id"]


def test_removing_a_file_from_the_request_removes_it_from_the_package(project):
    both = review.package(request_for(project, files=("app.py", "util.py")))
    one = review.package(request_for(project, files=("util.py",)))

    assert [item["path"] for item in one["files"]] == ["util.py"]
    assert one["package_id"] != both["package_id"]


def test_a_secret_file_makes_the_package_unsendable_and_says_which(project):
    (project / ".env").write_text("TOKEN=abc\n", encoding="utf-8")

    shown = review.package(request_for(project, files=("app.py", ".env")))

    assert shown["refused"] is True
    assert shown["reason"] in noshell.REFUSAL_LABELS
    assert review.package_lines(shown)[0] == "This package cannot be sent."


def test_the_package_lines_separate_bytes_from_tokens_and_promise_no_price(project):
    text = "\n".join(review.package_lines(review.package(request_for(project))))

    assert "app.py" in text and "bytes" in text
    assert "does not estimate" in text


def test_a_repository_file_can_only_lower_the_package_limits(project):
    (project / ".modelquad.toml").write_text(
        'schema = 1\n[codex]\nmax_files = 1\n', encoding="utf-8")

    shown = review.package(request_for(project, files=("app.py", "util.py")))

    assert shown["refused"] is True
    assert shown["reason"] == "supplied_files_over_limit"


# --------------------------------------------------------------------- the run


def test_a_review_sends_the_package_and_records_it(project):
    executor = FakeExecutor("FINDING F1 | high | app.py:2 | bug | high: read it | wrong value | it returns 1")
    shown = review.package(request_for(project))

    reply = review.run(request_for(project, package_id=shown["package_id"]), executor)

    assert review.headline(reply) == "Codex answered."
    assert reply["package"]["package_id"] == shown["package_id"]
    assert reply["usage"] == {"model_calls": 1, "input_tokens": 1200, "cached_input_tokens": 200,
                              "output_tokens": 80, "reasoning_output_tokens": None,
                              "seconds": reply["usage"]["seconds"], "tokens_source": "codex"}
    assert reply["findings"][0]["status"] == "claimed_by_codex"
    # Closed metadata: what the executor put beside the closed keys does not leave.
    assert "command" not in reply["metadata"]
    conn = review_ledger.connect()
    row = conn.execute("SELECT * FROM packages WHERE decision_id = ?", (reply["decision_id"],)).fetchone()
    run = conn.execute("SELECT * FROM runs WHERE decision_id = ?", (reply["decision_id"],)).fetchone()
    conn.close()
    assert (row["package_id"], row["round"], row["file_count"], row["model_calls"]) == (
        shown["package_id"], 0, 1, 1)
    assert json.loads(row["manifest"])["files"][0]["path"] == "app.py"
    assert (run["outcome"], run["input_tokens"], run["findings_count"]) == ("answered", 1200, 1)


def _ledger_rows():
    conn = review_ledger.connect()
    try:
        runs = [dict(row) for row in conn.execute("SELECT * FROM runs")]
        packages = [dict(row) for row in conn.execute("SELECT * FROM packages")]
    finally:
        conn.close()
    return runs, packages


def test_the_call_is_recorded_when_codex_starts_and_replaced_when_it_ends(project):
    executor = FakeExecutor()
    during = {}
    executor.before_answer = lambda: during.update(zip(("runs", "packages"), _ledger_rows()))

    reply = review.run(request_for(project), executor)

    # While Codex ran: one row that says so, with what it was given and nothing invented.
    (run,), (sent,) = during["runs"], during["packages"]
    assert run["decision_id"] == sent["decision_id"] == reply["decision_id"]
    assert run["outcome"] == review_ledger.UNFINISHED
    assert (run["duration_seconds"], run["input_tokens"], run["findings_count"]) == (None, None, None)
    assert sent["package_id"] == reply["package"]["package_id"] and sent["file_count"] == 1
    # After: the same call, once, with how it ended.
    (run,), (sent,) = _ledger_rows()
    assert (run["outcome"], run["input_tokens"]) == ("answered", 1200)
    assert sent["model_calls"] == 1


def test_a_session_that_closes_mid_run_leaves_a_record_of_the_start(project):
    """The end of the call never runs; the package still went to Codex."""
    executor = FakeExecutor()

    def session_closes():
        raise KeyboardInterrupt

    executor.before_answer = session_closes
    with pytest.raises(KeyboardInterrupt):
        review.run(request_for(project, trigger="user"), executor)

    (run,), (sent,) = _ledger_rows()
    assert (run["outcome"], run["trigger"], run["task_type"]) == (review_ledger.UNFINISHED, "user", "review")
    assert json.loads(sent["manifest"])["files"][0]["path"] == "app.py"
    counts = review_ledger.report()
    assert (counts["codex_started"], counts["answered"]) == (1, 0)
    # The lock is released, so the same package can be reviewed again.
    assert review.headline(review.run(request_for(project), FakeExecutor())) == "Codex answered."


def test_a_call_that_never_starts_codex_writes_no_start_record(project):
    executor = FakeExecutor()
    shown = review.package(request_for(project))
    (project / "app.py").write_text("def run():\n    return 99\n", encoding="utf-8")

    review.run(request_for(project, package_id=shown["package_id"]), executor)

    runs, _ = _ledger_rows()
    assert review_ledger.UNFINISHED not in {run["outcome"] for run in runs}


def test_a_package_the_user_was_not_shown_is_not_sent(project):
    executor = FakeExecutor()
    shown = review.package(request_for(project))

    reply = review.run(request_for(project, files=("app.py", "util.py"),
                                   package_id=shown["package_id"]), executor)

    assert (reply["refused"], reply["reason"]) == (True, review.PACKAGE_CHANGED)
    assert executor.requests == []


def test_a_file_that_changes_after_the_package_was_shown_is_not_sent(project):
    executor = FakeExecutor()
    shown = review.package(request_for(project))
    (project / "app.py").write_text("def run():\n    return 99\n", encoding="utf-8")

    reply = review.run(request_for(project, package_id=shown["package_id"]), executor)

    assert reply["reason"] == review.PACKAGE_CHANGED
    assert executor.requests == []


def test_the_executor_gets_the_digests_of_what_was_approved(project):
    executor = FakeExecutor()

    review.run(request_for(project), executor)

    sent = executor.requests[0]
    shown = review.package(request_for(project))
    assert sent.metadata["approved_digests"] == {"app.py": shown["files"][0]["sha256"]}
    assert sent.metadata["supplied_files"] == ["app.py"]
    assert sent.provider == "codex"


def test_only_one_run_of_the_same_package_at_a_time(project):
    executor = FakeExecutor()
    second = {}
    executor.before_answer = lambda: second.update(review.run(request_for(project), FakeExecutor()))

    first = review.run(request_for(project), executor)

    assert review.headline(first) == "Codex answered."
    assert second["reason"] == review.DUPLICATE
    # And the lock is released afterwards.
    assert review.headline(review.run(request_for(project), FakeExecutor())) == "Codex answered."


def test_a_long_answer_is_cut_and_says_so(project):
    reply = review.run(request_for(project), FakeExecutor("x" * (review.MAX_SHOWN_OUTPUT_CHARS + 50)))

    assert reply["output_truncated"] is True
    assert "ModelQuad cut this answer here" in reply["output"]


def test_a_refusal_after_packing_is_reported_as_a_refusal_not_an_answer(project):
    class Refusing:
        def execute(self, request):
            return ExecutionResult(provider="codex", success=False, output="", exit_code=None,
                                   error="C:\\Users\\someone\\codex.exe is old",
                                   task_status=WorkerTaskStatus.BLOCKED,
                                   task_status_reason="codex_version_unsupported",
                                   metadata={"worker_started": False})

    reply = review.run(request_for(project), Refusing())

    assert (reply["refused"], reply["started"]) == (True, False)
    assert review.headline(reply) == "Codex was not started."
    assert reply["message"] == noshell.REFUSAL_MESSAGES["codex_version_unsupported"]
    assert "someone" not in json.dumps(reply)


# ------------------------------------------------------------ completion rounds


def test_missing_files_come_back_as_a_request_and_nothing_is_added_by_itself(project):
    executor = FakeExecutor(needed=["util.py", "nope.py", ".env"])

    reply = review.run(request_for(project), executor)

    request = reply["completion_request"]
    assert request["files"] == ["util.py"]
    assert request["rounds_left"] == review.MAX_COMPLETION_ROUNDS
    assert len(executor.requests) == 1
    assert [item["path"] for item in reply["package"]["files"]] == ["app.py"]


def test_a_completion_round_is_counted_and_the_third_is_refused(project):
    first = review.run(request_for(project), FakeExecutor(needed=["util.py"]))
    second = review.run(request_for(project, files=("app.py", "util.py"),
                                    follows=first["decision_id"]), FakeExecutor())
    third = review.run(request_for(project, prompt="again", files=("app.py", "util.py"),
                                   follows=second["decision_id"]), FakeExecutor())
    fourth = review.run(request_for(project, prompt="and again", files=("app.py", "util.py"),
                                    follows=third["decision_id"]), FakeExecutor())

    assert (second["package_round"], third["package_round"]) == (1, 2)
    assert (fourth["refused"], fourth["reason"]) == (True, review.ROUNDS_EXHAUSTED)
    conn = review_ledger.connect()
    row = conn.execute("SELECT round, follows FROM packages WHERE decision_id = ?",
                       (second["decision_id"],)).fetchone()
    conn.close()
    assert (row["round"], row["follows"]) == (1, first["decision_id"])


def test_a_completion_round_must_follow_a_recorded_review_of_this_folder(project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "app.py").write_text("x = 1\n", encoding="utf-8")
    elsewhere = review.run(request_for(other), FakeExecutor())

    for follows in ("0" * 32, elsewhere["decision_id"]):
        reply = review.run(request_for(project, follows=follows), FakeExecutor())
        assert reply["reason"] == review.FOLLOWS_UNKNOWN


# -------------------------------------------------------------------- the gates


def test_a_worker_is_refused_before_anything_else(project):
    executor = FakeExecutor()

    reply = review.run(request_for(project), executor, review.Session(worker=True))

    assert reply["reason"] == review.WORKER
    assert executor.requests == []
    assert review.gate(request_for(project), review.Session(worker=True), starts_codex=False)["reason"] == review.WORKER


def test_a_network_folder_is_refused_before_it_is_opened(monkeypatch):
    from modelquad import localpath

    reply = review.gate(request_for("\\\\host\\share\\project"), review.Session())

    assert reply["reason"] == localpath.UNUSABLE


def test_modelquad_turned_off_starts_nothing(project):
    switch.write(False)
    executor = FakeExecutor()

    reply = review.run(request_for(project), executor)

    assert (reply["refused"], reply["reason"]) == (True, switch.OFF)
    assert executor.requests == []
    switch.write(True)
    assert review.headline(review.run(request_for(project), executor)) == "Codex answered."


def test_an_unreadable_switch_refuses(project):
    target = preferences_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")

    reply = review.run(request_for(project), FakeExecutor())

    assert reply["reason"] == switch.UNKNOWN


@pytest.mark.parametrize("rows", [
    {"disconnected_hosts": '["claude"]'}, {"routing_paused": "true"}])
def test_an_off_stored_by_an_earlier_build_still_counts(project, rows):
    target = preferences_path().parent / "modelquad.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany("INSERT INTO settings VALUES (?, ?)", rows.items())
    conn.commit()
    conn.close()

    assert switch.enabled() is False
    assert review.run(request_for(project), FakeExecutor())["reason"] == switch.OFF
    # The user's own choice here outranks the old file.
    switch.write(True)
    assert switch.enabled() is True


def test_a_session_without_the_prompt_needs_the_grant_for_its_own_folder(project, tmp_path):
    session = review.Session(standing=True, project=str(project))
    executor = FakeExecutor()

    assert review.run(request_for(project), executor, session)["reason"] == grant.NO_GRANT

    grant.write(True, everywhere=True)
    other = tmp_path / "other"
    other.mkdir()
    (other / "app.py").write_text("x = 1\n", encoding="utf-8")
    assert review.run(request_for(other), executor, session)["reason"] == grant.OTHER_FOLDER
    assert executor.requests == []
    assert review.headline(review.run(request_for(project), executor, session)) == "Codex answered."


def test_the_users_repository_off_outranks_everything(project):
    repo_settings.write_mode(project, "off")

    reply = review.run(request_for(project), FakeExecutor())
    shown = review.gate(request_for(project), review.Session(), starts_codex=False)

    assert reply["reason"] == repo_settings.USER_CODEX_OFF
    assert shown["reason"] == repo_settings.USER_CODEX_OFF


@pytest.mark.parametrize("change, reason", [
    ({"task_type": "implementation"}, "task_type_not_read_only"),
    ({"prompt": "  "}, "prompt_empty"),
    ({"model": "bad model!"}, "model_not_valid"),
    ({"reasoning_effort": "extreme"}, "reasoning_effort_not_valid"),
    ({"files": tuple(f"f{index}.py" for index in range(33))}, "files_not_valid"),
])
def test_a_request_that_cannot_be_used_names_the_problem(project, change, reason):
    reply = review.run(request_for(project, **change), FakeExecutor())

    assert (reply["refused"], reply["reason"]) == (True, reason)
    assert review.refusal_line(reply) != review.REFUSAL_FALLBACK


def test_a_missing_folder_is_refused(tmp_path):
    reply = review.run(request_for(tmp_path / "absent"), FakeExecutor())

    assert reply["reason"] == "working_directory_missing"


def test_a_named_file_the_rules_exclude_stops_the_call(project):
    (project / ".modelquadignore").write_text("util.py\n", encoding="utf-8")
    executor = FakeExecutor()

    reply = review.run(request_for(project, files=("app.py", "util.py")), executor)

    assert reply["reason"] == review.FILES_WITHHELD
    assert executor.requests == []


def test_the_first_call_shows_the_notice_instead_of_starting(project, monkeypatch):
    shown = []
    monkeypatch.setattr(disclosure, "notice_shown", lambda: bool(shown))
    monkeypatch.setattr(disclosure, "record_notice_shown", lambda via: shown.append(via))
    executor = FakeExecutor()

    first = review.run(request_for(project), executor)
    second = review.run(request_for(project), executor)

    assert first["status"] == review.NOTICE_FIRST and executor.requests[1:] == []
    assert review.headline(first).startswith("Codex was not started")
    assert review.headline(second) == "Codex answered."
    # Showing a package is not a Codex call: it never spends the notice.
    shown.clear()
    assert review.gate(request_for(project), review.Session(), starts_codex=False) is None
    assert shown == []


# ------------------------------------------------------------------ the server


def tool(name):
    return next(item for item in anyio.run(review_server.server.list_tools) if item.name == name)


def test_the_server_offers_exactly_four_tools():
    names = sorted(item.name for item in anyio.run(review_server.server.list_tools))

    assert names == ["modelquad_delegate", "modelquad_review_package",
                     "modelquad_review_verdict", "modelquad_status"]


def test_only_the_review_starts_codex_and_it_asks_the_user_each_time():
    delegate = tool("modelquad_delegate")

    assert delegate.meta == {"anthropic/requiresUserInteraction": True}
    assert delegate.annotations.read_only_hint is False
    for name in ("modelquad_review_package", "modelquad_status"):
        assert tool(name).annotations.read_only_hint is True
        assert not tool(name).meta


def call(function, **arguments):
    async def invoke():
        return await function(**arguments)

    return anyio.run(invoke)


def test_the_package_tool_shows_the_package_without_an_executor(project, monkeypatch):
    monkeypatch.setattr(review_server, "_executor", lambda: pytest.fail("a preview starts nothing"))

    result = call(review_server.modelquad_review_package, task_type="review", prompt="Look.",
                  working_directory=str(project), files=["app.py"])

    assert result.content[0].text.startswith("Codex would get the task text")
    assert result.structured_content["files"][0]["path"] == "app.py"


def test_the_review_tool_replies_with_the_outcome_first_and_the_data_after(project, monkeypatch):
    executor = FakeExecutor("Looks right.\nFINDINGS: none")
    monkeypatch.setattr(review_server, "_executor", lambda: executor)
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)

    result = call(review_server.modelquad_delegate, task_type="review", prompt="Look.",
                  working_directory=str(project), files=["app.py"], timeout=60.0)

    lines = result.content[0].text.splitlines()
    data = result.structured_content
    assert lines[0] == "Codex answered."
    assert any(line.startswith("Sent: the task text and 1 file(s)") for line in lines)
    assert data["output"].startswith("Looks right.")
    assert "metadata" not in data
    assert json.loads(result.content[1].text) == data
    # The worker's record is removed when the call ends.
    assert worker_guard.active_workers() == []


def test_a_refused_review_says_so_on_the_first_line(project, monkeypatch):
    monkeypatch.setattr(review_server, "_executor", FakeExecutor)
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)
    switch.write(False)

    result = call(review_server.modelquad_delegate, task_type="review", prompt="Look.",
                  working_directory=str(project), files=["app.py"], timeout=60.0)

    assert result.content[0].text.splitlines()[0] == "Codex was not started."
    assert result.structured_content["refused"] is True


def test_a_cancelled_call_asks_the_worker_to_stop(project, monkeypatch):
    release = threading.Event()
    seen = {}

    class Slow:
        def execute(self, request):
            seen["stop"] = request.stop_requested
            release.wait(5)
            return ExecutionResult(provider="codex", success=False, output="", exit_code=None,
                                   metadata={"worker_started": True, "stop_requested": True})

    monkeypatch.setattr(review_server, "_executor", Slow)
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)

    async def scenario():
        with anyio.move_on_after(0.5):
            await review_server.modelquad_delegate(
                task_type="review", prompt="Look.", working_directory=str(project),
                files=["app.py"], timeout=60.0)

    anyio.run(scenario)
    try:
        assert seen["stop"]() is True
    finally:
        release.set()


def test_the_status_tool_reads_the_switch_and_the_folder(project, monkeypatch):
    from modelquad import settings_view

    monkeypatch.setattr(settings_view, "_claude_installed", lambda: True)
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)
    switch.write(False)

    result = review_server.modelquad_status(str(project))

    assert result.structured_content["modelquad"] == "off"
    assert "1. ModelQuad in Claude Code: off" in result.content[0].text
    assert result.structured_content["folder_rules"]["state"] == "builtin"


def test_a_ledger_that_cannot_be_written_is_said_in_the_reply(project, monkeypatch):
    """Found with a real run while another program held the ledger: the
    answer came back, no row was written, and the lines a person reads did
    not say so."""
    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(review_ledger, "record_start", locked)
    monkeypatch.setattr(review_ledger, "record_run", locked)
    executor = FakeExecutor("FINDING F1 | high | app.py:2 | bug | high: read it | wrong | because")
    monkeypatch.setattr(review_server, "_executor", lambda: executor)
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)

    result = call(review_server.modelquad_delegate, task_type="review", prompt="Look.",
                  working_directory=str(project), files=["app.py"], timeout=60.0)

    lines = result.content[0].text.splitlines()
    data = result.structured_content
    assert lines[0] == "Codex answered." and data["output"].startswith("FINDING F1")
    assert data["ledger_note"] == review.NOT_RECORDED_NOTE and lines[-1] == review.NOT_RECORDED_NOTE
    # No finding is offered for a verdict that could not be stored.
    assert "findings" not in data and "findings_note" not in data
    assert _ledger_rows() == ([], [])


class FailingExecutor(FakeExecutor):
    def execute(self, request):
        if request.on_spawn is not None:
            request.on_spawn(4242)
        return ExecutionResult(provider="codex", success=False, output="", exit_code=1,
                               task_status=WorkerTaskStatus.FAILED,
                               task_status_reason="worker_reported",
                               metadata={"worker_started": True})


def test_an_error_codex_reports_is_named_without_its_text(project, monkeypatch):
    """A model id Codex does not have came back as "returned no answer" and
    nothing else (real run, 2026-11-18)."""
    monkeypatch.setattr(review_server, "_executor", FailingExecutor)
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)

    result = call(review_server.modelquad_delegate, task_type="review", prompt="Look.",
                  working_directory=str(project), files=["app.py"], timeout=60.0,
                  model="gpt-not-a-model")

    lines = result.content[0].text.splitlines()
    assert lines[0] == "Codex did not finish: it returned no answer."
    assert lines[1].startswith("Codex itself reported an error") and "modelquad doctor" in lines[1]
    # Claude Code hands Claude the data, so the sentence is there too.
    assert result.structured_content["reason"] == "worker_reported"
    assert result.structured_content["message"] == lines[1]
    assert [row["outcome"] for row in _ledger_rows()[0]] == [review_ledger.FAILED]
