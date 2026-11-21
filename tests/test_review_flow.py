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


def _env_template(project):
    (project / "config").mkdir()
    (project / "config" / "billing.env.tmpl").write_text(
        "DATABASE_URL=postgresql://app@{host}:{port}/billing\n", encoding="utf-8")
    return ("app.py", "config/billing.env.tmpl")


def test_a_file_stopped_by_its_name_is_named_and_a_package_without_it_is_offered(project, monkeypatch):
    files = _env_template(project)
    opened = []
    real_read = noshell._read_file
    monkeypatch.setattr(noshell, "_read_file", lambda root, name, *a, **k: (
        opened.append(name), real_read(root, name, *a, **k))[1])

    shown = review.package(request_for(project, files=files))

    assert shown["refused"] is True and shown["started"] is False
    assert shown["reason"] == "supplied_file_secret"
    assert shown["left_out"] == [{"position": 2, "path": "<path>", "reason": review.SECRET_NAME}]
    assert "because of a file name, not its contents" in shown["message"]
    assert "number 2 in files" in shown["message"]
    assert "contains what looks like a real credential" not in shown["message"]
    assert "config/billing.env.tmpl" not in opened, "a secret-named file is never opened"
    # The packer's privacy screen hides such a path; the reply keeps it hidden everywhere.
    assert "billing.env" not in json.dumps(shown) and "{host}" not in json.dumps(shown)
    retry = shown["retry_package"]
    assert [item["path"] for item in retry["files"]] == ["app.py"]
    assert retry["package_id"] == review.package(request_for(project, files=("app.py",)))["package_id"]
    text = "\n".join(review.package_lines(shown))
    assert "Left out: file 2 in the list (stopped by its name)" in text
    assert "billing.env" not in text
    assert f"Without it, the package is {retry['package_id']}:" in text and "- app.py (" in text


def test_the_package_without_it_runs_only_when_asked_again_and_the_original_still_refuses(project):
    files = _env_template(project)
    executor = FakeExecutor()

    refused = review.run(request_for(project, files=files), executor)
    again = review.run(request_for(project, files=files), executor)

    assert refused["refused"] is True and again["refused"] is True
    assert executor.requests == [], "nothing starts until the smaller package is asked for"
    retry = refused["retry_package"]
    reply = review.run(request_for(project, files=tuple(item["path"] for item in retry["files"]),
                                   package_id=retry["package_id"]), executor)
    assert review.headline(reply) == "Codex answered."
    assert [item["path"] for item in reply["sent_files"]] == ["app.py"]
    assert reply["package"]["package_id"] == retry["package_id"]


def test_the_offered_package_id_sends_that_package_or_nothing(project):
    files = _env_template(project)
    retry = review.package(request_for(project, files=files))["retry_package"]
    executor = FakeExecutor()

    widened = review.run(request_for(project, files=("app.py", "util.py"),
                                     package_id=retry["package_id"]), executor)
    with_the_file = review.run(request_for(project, files=files, package_id=retry["package_id"]), executor)

    assert widened["refused"] is True and widened["reason"] == review.PACKAGE_CHANGED
    assert with_the_file["refused"] is True
    assert executor.requests == []


def _recording_reads(monkeypatch):
    opened = []
    real_read = noshell._read_file
    monkeypatch.setattr(noshell, "_read_file", lambda root, name, *a, **k: (
        opened.append(name), real_read(root, name, *a, **k))[1])
    return opened


def test_explaining_the_refusal_never_opens_a_file_the_rules_exclude(project, monkeypatch):
    files = _env_template(project)
    (project / "private").mkdir()
    (project / "private" / "draft.txt").write_text("not for Codex\n", encoding="utf-8")
    (project / ".modelquadignore").write_text("private/\n", encoding="utf-8")
    opened = _recording_reads(monkeypatch)

    shown = review.package(request_for(project, files=(*files, "private/draft.txt")))

    assert "private/draft.txt" not in opened, "an excluded file is never read, not even to explain"
    assert "config/billing.env.tmpl" not in opened
    assert shown["reason"] == "supplied_file_secret"
    assert shown["left_out"] == [{"position": 2, "path": "<path>", "reason": review.SECRET_NAME}]
    assert "because of a file name, not its contents" in shown["message"]
    assert "billing.env" not in json.dumps(shown)
    retry = shown["retry_package"]
    assert [item["path"] for item in retry["files"]] == ["app.py"]
    assert {"path": "private/draft.txt", "reason": "modelquad_ignored"} in retry["skipped"]


def test_the_refusal_reasons_come_from_the_one_packing_pass(project, monkeypatch):
    (project / "notes.txt").write_text("-----BEGIN " + "RSA PRIVATE KEY-----\nMIIfixture\n", encoding="utf-8")
    files = (*_env_template(project), "notes.txt")
    opened = _recording_reads(monkeypatch)

    shown = review.package(request_for(project, files=files))

    # One read for the refused pack, one for the offered package; never a third look.
    assert opened.count("notes.txt") == 1 and opened.count("app.py") == 2
    assert shown["left_out"] == [{"position": 2, "path": "<path>", "reason": review.SECRET_NAME},
                                 {"position": 3, "path": "<path>", "reason": review.SECRET_CONTENT}]
    assert "looks like it holds a secret" in shown["message"]


def test_the_offered_package_refuses_once_a_file_in_it_changed(project):
    files = _env_template(project)
    retry = review.package(request_for(project, files=files))["retry_package"]
    (project / "app.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    executor = FakeExecutor()

    reply = review.run(request_for(project, files=("app.py",), package_id=retry["package_id"]), executor)

    assert reply["refused"] is True and reply["reason"] == review.PACKAGE_CHANGED
    assert executor.requests == []


def test_a_file_stopped_by_its_contents_says_so_and_never_shows_them(project):
    (project / "notes.txt").write_text("-----BEGIN " + "RSA PRIVATE KEY-----\nMIIfixture\n", encoding="utf-8")

    shown = review.package(request_for(project, files=("app.py", "notes.txt")))

    assert shown["left_out"] == [{"position": 2, "path": "<path>", "reason": review.SECRET_CONTENT}]
    assert "looks like it holds a secret" in shown["message"]
    assert "MIIfixture" not in json.dumps(shown) and "notes.txt" not in json.dumps(shown)
    assert [item["path"] for item in shown["retry_package"]["files"]] == ["app.py"]


def test_a_path_that_looks_like_a_credential_is_not_shown(project):
    name = "ghp_" + "A1b2C3d4E5f6G7h8" + ".txt"
    (project / name).write_text("inert\n", encoding="utf-8")

    shown = review.package(request_for(project, files=(name, "app.py")))

    assert shown["left_out"] == [{"position": 1, "path": "<path>", "reason": review.SECRET_PATH}]
    assert name not in json.dumps(shown)


def test_when_only_secret_files_were_named_the_offer_says_only_the_task_text_goes(project):
    _env_template(project)

    shown = review.package(request_for(project, files=("config/billing.env.tmpl",)))

    assert shown["retry_package"]["files"] == []
    assert "only the task text" in shown["message"]


def test_the_server_passes_the_offer_to_claude_and_shows_it(project):
    shown = review.package(request_for(project, files=_env_template(project)))

    assert {"left_out", "retry_package"} <= set(review_server._VISIBLE)
    lines = review_server._review_lines(shown)
    assert any("file 2 in the list (stopped by its name)" in line for line in lines)
    assert not any("billing.env" in line for line in lines)
    assert any(line.startswith("Without it, the package is ") for line in lines)


def test_the_package_gives_the_exact_size_of_the_whole_payload(project):
    shown = review.package(request_for(project, files=("app.py", "util.py")))

    rendered = noshell.supplied_prompt("Review the change to run().", str(project),
                                       files=["app.py", "util.py"], completion_protocol=True)
    assert shown["payload_bytes"] == len(rendered.encode("utf-8"))
    # Labels, line numbers and ModelQuad's own instructions come on top of the file bytes.
    assert shown["payload_bytes"] > shown["prompt"]["bytes"] + shown["total_bytes"]
    assert (f"The whole payload is {shown['payload_bytes']} bytes"
            in "\n".join(review.package_lines(shown)))


def test_with_no_recorded_review_the_input_tokens_are_unknown_and_no_price_is_given(project):
    shown = review.package(request_for(project))

    assert shown["input_tokens_estimate"] == {"known": False, "reason": "too_few_runs", "runs": 0,
                                              "min_runs": review_ledger.MIN_ESTIMATE_RUNS}
    text = "\n".join(review.package_lines(shown))
    assert "app.py" in text and "bytes" in text
    assert ("Input tokens: unknown before a run (fewer than 20 successful reviews on this "
            "computer recorded what they sent; there are 0 so far). No price is estimated.") in text
    # Showing a package never creates the review ledger.
    assert not review_ledger.ledger_path().exists()


# --------------------------------------------------------- the token estimate


def _seed_runs(points, *, outcome="answered", start=1_000.0):
    """Recorded reviews: (task text + file bytes, input tokens Codex reported), oldest first."""
    conn = review_ledger.connect()
    try:
        for index, (sent, tokens) in enumerate(points):
            decision = f"seed{start:.0f}-{index}"
            conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (decision, start + index, "ws", "review", "claude", None, outcome, 1.0,
                          tokens, 10, 0))
            prompt = min(sent, 100)
            manifest = {"prompt": {"bytes": prompt, "sha256": "0" * 16}, "total_bytes": sent - prompt}
            conn.execute("INSERT INTO packages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (decision, "p" * 32, "ws", 0, None, 1, sent - prompt, 0, 0, 1, None, None,
                          json.dumps(manifest)))
        conn.commit()
    finally:
        conn.close()


def _steady(count, *, base=18_000, miss=0.02):
    """Runs on a straight line of 3 bytes a token, alternately 2% over and under it."""
    sizes = [1_000 + index * 5_000 for index in range(count)]
    return [(size, round((base + size / 3) * (1 + (miss if index % 2 else -miss))))
            for index, size in enumerate(sizes)]


def test_one_review_short_of_the_minimum_leaves_the_estimate_unknown():
    _seed_runs(_steady(review_ledger.MIN_ESTIMATE_RUNS - 1))

    estimate = review_ledger.input_token_estimate(20_000)

    assert estimate["known"] is False
    assert (estimate["reason"], estimate["runs"]) == ("too_few_runs", 19)
    assert "there are 19 so far" in review.estimate_line(estimate)


def test_enough_recorded_reviews_give_a_labelled_range_around_the_line():
    _seed_runs(_steady(review_ledger.MIN_ESTIMATE_RUNS))

    estimate = review_ledger.input_token_estimate(30_000)

    assert estimate["known"] is True and estimate["runs"] == 20
    assert estimate["low"] < 18_000 + 30_000 / 3 < estimate["high"]
    assert estimate["low"] % 1000 == 0 and estimate["high"] % 1000 == 0
    # The width comes from the largest miss among the same reviews: about 2% here.
    assert 1.5 <= estimate["largest_miss_percent"] <= 2.5
    line = review.estimate_line(estimate)
    assert line.startswith("Input tokens, estimated from the last 20 successful reviews on this "
                           f"computer: about {estimate['low']:,} to {estimate['high']:,}.")
    assert "it is not a count" in line and "No price is estimated." in line


def test_the_range_widens_with_the_largest_miss_it_has_seen():
    _seed_runs(_steady(20, miss=0.10))

    estimate = review_ledger.input_token_estimate(30_000)

    assert estimate["largest_miss_percent"] >= 9.5
    assert estimate["high"] - estimate["low"] >= 0.19 * (18_000 + 30_000 / 3)


def test_a_package_outside_the_recorded_sizes_has_no_estimate():
    points = _steady(20)
    _seed_runs(points)

    largest = max(size for size, _ in points)
    for size in (0, largest + 1):
        estimate = review_ledger.input_token_estimate(size)
        assert (estimate["known"], estimate["reason"]) == (False, "outside_recorded_sizes")
    assert "smaller or larger than every recorded review" in review.estimate_line(
        review_ledger.input_token_estimate(largest + 1))


def test_only_successful_reviews_with_token_counts_and_a_package_count():
    _seed_runs(_steady(19))
    _seed_runs(_steady(5), outcome="failed", start=5_000.0)
    _seed_runs([(10_000, None)], start=6_000.0)

    assert review_ledger.input_token_estimate(20_000)["runs"] == 19


def test_the_estimate_follows_the_most_recent_reviews():
    # Old runs from a Codex release with a much larger fixed part, then 50 recent ones.
    _seed_runs(_steady(20, base=40_000), start=1_000.0)
    _seed_runs(_steady(review_ledger.MAX_ESTIMATE_RUNS), start=10_000.0)

    estimate = review_ledger.input_token_estimate(30_000)

    assert estimate["runs"] == review_ledger.MAX_ESTIMATE_RUNS
    assert estimate["largest_miss_percent"] <= 2.5


def test_an_unreadable_ledger_leaves_the_estimate_unknown():
    path = review_ledger.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a database")

    estimate = review_ledger.input_token_estimate(10_000)

    assert (estimate["known"], estimate["reason"]) == (False, "ledger_unreadable")
    assert path.read_bytes() == b"not a database"


def test_the_preview_shows_the_range_and_the_run_keeps_it_out_of_the_record(project):
    sent = review.package(request_for(project))
    size = sent["prompt"]["bytes"] + sent["total_bytes"]
    _seed_runs([(size + offset, 18_000 + offset) for offset in range(0, 40_000, 2_000)])

    shown = review.package(request_for(project))
    assert shown["input_tokens_estimate"]["known"] is True
    assert any(line.startswith("Input tokens, estimated from the last 20")
               for line in review.package_lines(shown))
    assert shown["package_id"] == sent["package_id"]

    reply = review.run(request_for(project, package_id=shown["package_id"]), FakeExecutor())

    assert "input_tokens_estimate" not in reply
    conn = review_ledger.connect()
    row = conn.execute("SELECT manifest FROM packages WHERE decision_id = ?",
                       (reply["decision_id"],)).fetchone()
    conn.close()
    assert "input_tokens_estimate" not in json.loads(row["manifest"])


def test_a_repository_file_can_only_lower_the_package_limits(project):
    (project / ".modelquad.toml").write_text(
        'schema = 1\n[codex]\nmax_files = 1\n', encoding="utf-8")

    shown = review.package(request_for(project, files=("app.py", "util.py")))

    assert shown["refused"] is True
    assert shown["reason"] == "supplied_files_over_limit"


# ------------------------------------------------------------ the package hint


def _tests_folder(project, *names):
    folder = project / "tests"
    folder.mkdir(exist_ok=True)
    for name in names:
        (folder / name).write_text("def test_it():\n    assert True\n", encoding="utf-8")
    return folder


@pytest.fixture
def reads(monkeypatch):
    """The names ModelQuad opens to build a package."""
    opened: list[str] = []
    real = noshell._read_file

    def recording(root, name, *args, **kwargs):
        opened.append(name)
        return real(root, name, *args, **kwargs)

    monkeypatch.setattr(noshell, "_read_file", recording)
    return opened


def test_the_package_names_tests_codex_may_also_need_without_opening_them(project, reads):
    _tests_folder(project, "test_app_extra.py", "test_app.py", "test_util.py")

    shown = review.package(request_for(project))

    assert shown["may_also_need"] == ["tests/test_app.py", "tests/test_app_extra.py"]
    assert [item["path"] for item in shown["files"]] == ["app.py"]
    assert set(reads) == {"app.py"}
    assert ("Codex may also need (not added): tests/test_app.py, tests/test_app_extra.py."
            in review.package_lines(shown))


def test_an_excluded_or_secret_named_test_is_never_offered(project, reads):
    _tests_folder(project, "test_app.py", "test_app_kept.py", "test_app_-password=hunter2.py")
    (project / ".modelquadignore").write_text("tests/test_app.py\n", encoding="utf-8")

    shown = review.package(request_for(project))

    assert shown["may_also_need"] == ["tests/test_app_kept.py"]
    assert "hunter2" not in json.dumps(shown)
    assert set(reads) == {"app.py"}


def test_a_named_test_is_not_offered_again(project):
    _tests_folder(project, "test_app.py", "test_app_extra.py")

    shown = review.package(request_for(project, files=("app.py", "tests/test_app.py")))

    assert shown["may_also_need"] == ["tests/test_app_extra.py"]


def test_the_hint_does_not_change_the_package_id(project):
    without = review.package(request_for(project))
    _tests_folder(project, "test_app.py")

    shown = review.package(request_for(project))

    assert "may_also_need" not in without
    assert shown["may_also_need"] == ["tests/test_app.py"]
    assert shown["package_id"] == without["package_id"]


def test_the_hint_offers_at_most_three_exact_matches_first(project):
    _tests_folder(project, "test_app_a.py", "test_app_b.py", "test_util_a.py", "test_util.py",
                  "test_app.py")

    shown = review.package(request_for(project, files=("app.py", "util.py")))

    # Exact names first, then one related test per source file in turn.
    assert shown["may_also_need"] == ["tests/test_app.py", "tests/test_util.py", "tests/test_app_a.py"]
    assert len(shown["may_also_need"]) == review.MAX_MAY_ALSO_NEED


def test_a_tests_folder_reached_through_a_junction_is_not_listed(project, tmp_path):
    import subprocess

    from modelquad.procutil import no_window_kwargs

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "test_app.py").write_text("def test_it():\n    pass\n", encoding="utf-8")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(project / "tests"), str(elsewhere)],
                          capture_output=True, check=False, **no_window_kwargs())
    if made.returncode != 0:
        pytest.skip("this machine cannot make a junction")

    assert "may_also_need" not in review.package(request_for(project))


def test_the_hint_finds_script_tests_beside_the_file_and_in_tests_folders(project):
    web = project / "web"
    (web / "__tests__").mkdir(parents=True)
    (web / "button.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (web / "button.test.ts").write_text("test('a', () => {});\n", encoding="utf-8")
    (web / "__tests__" / "button.tsx").write_text("test('b', () => {});\n", encoding="utf-8")

    shown = review.package(request_for(project, files=("web/button.ts",)))

    assert shown["may_also_need"] == ["web/button.test.ts", "web/__tests__/button.tsx"]


def test_the_hint_reaches_the_reply_but_not_the_record(project, monkeypatch):
    _tests_folder(project, "test_app.py")
    executor = FakeExecutor("Looks right.\nFINDINGS: none")
    monkeypatch.setattr(review_server, "_executor", lambda: executor)
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)

    shown = call(review_server.review_package, task_type="review", prompt="Look.",
                 working_directory=str(project), files=["app.py"])
    result = call(review_server.delegate, task_type="review", prompt="Look.",
                  working_directory=str(project), files=["app.py"], timeout=60.0)

    assert shown.structured_content["may_also_need"] == ["tests/test_app.py"]
    assert result.structured_content["may_also_need"] == ["tests/test_app.py"]
    assert "Codex may also need (not added): tests/test_app.py." in result.content[0].text.splitlines()
    assert executor.requests[0].metadata["supplied_files"] == ["app.py"]
    _runs, packages = _ledger_rows()
    assert "may_also_need" not in json.loads(packages[0]["manifest"])


# --------------------------------------------------------------------- the run


def test_a_review_sends_the_package_and_records_it(project):
    executor = FakeExecutor("FINDING F1 | high | app.py:2 | bug | high: read it | wrong value | it returns 1")
    shown = review.package(request_for(project))

    reply = review.run(request_for(project, package_id=shown["package_id"]), executor)

    assert review.headline(reply) == "Codex answered."
    assert reply["package"]["package_id"] == shown["package_id"]
    assert reply["usage"] == {"model_calls": 1, "payload_bytes": shown["payload_bytes"],
                              "input_tokens": 1200, "cached_input_tokens": 200,
                              "uncached_input_tokens": 1000,
                              "output_tokens": 80, "reasoning_output_tokens": None,
                              "seconds": reply["usage"]["seconds"], "tokens_source": "codex"}
    assert reply["package"]["payload_bytes"] == shown["payload_bytes"]
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
    assert json.loads(row["manifest"])["payload_bytes"] == shown["payload_bytes"]
    assert (run["outcome"], run["input_tokens"], run["findings_count"]) == ("answered", 1200, 1)


def test_the_reply_puts_bytes_sent_beside_cached_and_uncached_tokens(project):
    reply = review.run(request_for(project), FakeExecutor())

    lines = review_server._review_lines(reply)
    assert any(f"the whole payload was {reply['package']['payload_bytes']} bytes" in line
               for line in lines)
    assert "Of the input tokens, 200 came from Codex's cache and 1000 did not." in lines


def test_an_uncached_count_is_unknown_unless_codex_reported_both_counts():
    usage = review._usage({"worker_started": True, "worker_tokens": {"input_tokens": 900}}, 1.0)
    assert usage["uncached_input_tokens"] is None and usage["payload_bytes"] is None
    usage = review._usage({"worker_tokens": {"input_tokens": 100, "cached_input_tokens": 200}}, 1.0)
    assert usage["uncached_input_tokens"] is None


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


_OTHER_SESSION = r'''
import sys
from modelquad import disclosure, review
from modelquad.execution.base import ExecutionResult, WorkerTaskStatus

disclosure.notice_shown = lambda: True


class Waits:
    def execute(self, request):
        print("running", flush=True)
        sys.stdin.readline()
        return ExecutionResult(provider="codex", success=True, output="FINDINGS: none",
                               exit_code=0, task_status=WorkerTaskStatus.COMPLETED,
                               metadata={"worker_started": True})


reply = review.run(review.ReviewRequest(task_type="review", prompt="Review the change to run().",
                                        working_directory=sys.argv[1], files=("app.py",)), Waits())
print("done", reply.get("reason"), flush=True)
'''


@pytest.fixture
def other_session(project):
    """A second server process (another Claude Code session) running the same package."""
    import subprocess
    import sys

    from modelquad.procutil import no_window_kwargs

    child = subprocess.Popen([sys.executable, "-c", _OTHER_SESSION, str(project)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             **no_window_kwargs(detach_stdin=False))
    try:
        assert child.stdout.readline().strip() == "running"
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=30)


def test_the_same_package_running_in_another_session_is_refused(project, other_session):
    executor = FakeExecutor()

    reply = review.run(request_for(project), executor)

    assert (reply["refused"], reply["reason"]) == (True, review.DUPLICATE)
    assert executor.requests == []
    # Once the other session's run ends, the same package is a fresh request.
    other_session.stdin.write("\n")
    other_session.stdin.flush()
    assert other_session.stdout.readline().split()[0] == "done"
    other_session.wait(timeout=30)
    assert review.headline(review.run(request_for(project), FakeExecutor())) == "Codex answered."


def test_a_session_that_dies_mid_run_leaves_no_lock_behind(project, other_session):
    import time

    other_session.kill()  # TerminateProcess: no finally block runs in that process
    other_session.wait(timeout=30)

    # A venv launcher can exit a few milliseconds before the interpreter it
    # started; Windows frees the lock when that interpreter is gone.
    deadline = time.monotonic() + 10
    while True:
        reply = review.run(request_for(project), FakeExecutor())
        if reply.get("reason") != review.DUPLICATE or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    assert review.headline(reply) == "Codex answered."


def test_another_package_runs_while_one_runs_in_another_session(project, other_session):
    reply = review.run(request_for(project, files=("util.py",)), FakeExecutor())

    assert review.headline(reply) == "Codex answered."


def test_the_run_lock_file_is_gone_after_the_run(project):
    review.run(request_for(project), FakeExecutor())

    from modelquad.paths import state_root

    assert list((state_root() / review.RUN_LOCK_FOLDER).glob("*.lock")) == []


def test_a_state_folder_that_cannot_hold_a_lock_does_not_stop_the_review(project):
    from modelquad.paths import state_root

    # A file where the lock folder should be: the lock cannot be opened.
    state_root().mkdir(parents=True, exist_ok=True)
    (state_root() / review.RUN_LOCK_FOLDER).write_text("", encoding="utf-8")

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

    assert names == ["delegate", "review_package",
                     "review_verdict", "status"]


def test_only_the_review_starts_codex_and_it_asks_the_user_each_time():
    delegate = tool("delegate")

    assert delegate.meta == {"anthropic/requiresUserInteraction": True}
    assert delegate.annotations.read_only_hint is False
    for name in ("review_package", "status"):
        assert tool(name).annotations.read_only_hint is True
        assert not tool(name).meta


def call(function, **arguments):
    async def invoke():
        return await function(**arguments)

    return anyio.run(invoke)


def test_the_package_tool_shows_the_package_without_an_executor(project, monkeypatch):
    monkeypatch.setattr(review_server, "_executor", lambda: pytest.fail("a preview starts nothing"))

    result = call(review_server.review_package, task_type="review", prompt="Look.",
                  working_directory=str(project), files=["app.py"])

    assert result.content[0].text.startswith("Codex would get the task text")
    assert result.structured_content["files"][0]["path"] == "app.py"


def test_the_review_tool_replies_with_the_outcome_first_and_the_data_after(project, monkeypatch):
    executor = FakeExecutor("Looks right.\nFINDINGS: none")
    monkeypatch.setattr(review_server, "_executor", lambda: executor)
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)

    result = call(review_server.delegate, task_type="review", prompt="Look.",
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

    result = call(review_server.delegate, task_type="review", prompt="Look.",
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

    async def call():
        await review_server.delegate(
            task_type="review", prompt="Look.", working_directory=str(project),
            files=["app.py"], timeout=60.0)

    async def scenario():
        # Cancel once the worker runs, not after a fixed time: on a loaded
        # runner the package's Git checks alone can take longer than that.
        async with anyio.create_task_group() as group:
            group.start_soon(call)
            with anyio.fail_after(30):
                while "stop" not in seen:
                    await anyio.sleep(0.01)
            group.cancel_scope.cancel()

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

    result = review_server.status(str(project))

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

    result = call(review_server.delegate, task_type="review", prompt="Look.",
                  working_directory=str(project), files=["app.py"], timeout=60.0)

    lines = result.content[0].text.splitlines()
    data = result.structured_content
    assert lines[0] == "Codex answered." and data["output"].startswith("FINDING F1")
    assert data["ledger_note"] == review.NOT_RECORDED_NOTE and lines[-1] == review.NOT_RECORDED_NOTE
    # No finding is offered for a verdict that could not be stored.
    assert "findings" not in data and "findings_note" not in data
    # Nothing of this call was written. Rows of other calls are not this
    # test's business: a call an earlier test cancelled can still be ending.
    runs, packages = _ledger_rows()
    assert data["decision_id"] not in {row["decision_id"] for row in (*runs, *packages)}


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

    result = call(review_server.delegate, task_type="review", prompt="Look.",
                  working_directory=str(project), files=["app.py"], timeout=60.0,
                  model="gpt-not-a-model")

    lines = result.content[0].text.splitlines()
    assert lines[0] == "Codex did not finish: it returned no answer."
    # worker_reported is also the catch-all for a crash or an outside kill,
    # so the sentence does not claim to know which.
    assert lines[1].startswith("Codex ended with an error or was stopped")
    assert "modelquad doctor" in lines[1]
    # Claude Code hands Claude the data, so the sentence is there too.
    assert result.structured_content["reason"] == "worker_reported"
    assert result.structured_content["message"] == lines[1]
    mine = result.structured_content["decision_id"]
    assert [row["outcome"] for row in _ledger_rows()[0]
            if row["decision_id"] == mine] == [review_ledger.FAILED]


def test_every_refusal_carries_its_sentence_in_the_data(project):
    """Claude Code hands Claude a reply's data, not its lines (seen in a real
    session, where a path outside the project came back as a bare code)."""
    outside = review.package(request_for(project, files=("../other.py",)))
    assert outside["reason"] == "supplied_file_unavailable"
    assert outside["message"] == noshell.REFUSAL_MESSAGES["supplied_file_unavailable"]
    missing = review.run(request_for(project, files=("nope.py",)), FakeExecutor())
    assert missing["refused"] and missing["message"] == review.refusal_line(missing)
    assert review.refusal(review.DUPLICATE)["message"] == review._LINES[review.DUPLICATE]
    # A code with no sentence of its own gets none invented.
    assert "message" not in review.refusal("some_new_code")
