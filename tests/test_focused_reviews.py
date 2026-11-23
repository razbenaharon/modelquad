# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Focused reviews: three fixed questions through the one review flow.

A focused review is the ordinary flow with ModelQuad's own instruction put
in front of Claude's task text. These tests show that each entry reaches
Codex with its task kind and its instruction, that the general review is
unchanged, and that only the approved text and files go: a file a document
mentions is offered, never added; an excluded file is neither sent nor
offered; a change after the preview sends nothing; a repository that removes
the task kind stops the focus with it.

A fake executor stands in for Codex (it packs like the real one); what Codex
itself answers is the pilot's business, not this suite's.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import anyio
import pytest

from modelquad import focus, repo_policy, review, review_ledger
from modelquad.execution import noshell
from modelquad.mcp import server as review_server, worker_guard
from modelquad.procutil import no_window_kwargs

from test_review_flow import FakeExecutor

ROOT = Path(__file__).resolve().parents[1]

PLAN = """# Sync plan

## Requirements
R1: Align audio and video by the original presentation timestamps (see requirements.md).

## Approach
Compute each frame's time as frame_number / 30 and align on that.
The decoder lives in src/decoder.py; settings in config/sync.toml.
"""


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "project"
    (folder / "src").mkdir(parents=True)
    (folder / "config").mkdir()
    (folder / "plan.md").write_text(PLAN, encoding="utf-8")
    (folder / "requirements.md").write_text("R1: use the original timestamps.\n", encoding="utf-8")
    (folder / "src" / "decoder.py").write_text("def pts(frame):\n    return frame.pts\n", encoding="utf-8")
    (folder / "config" / "sync.toml").write_text("fps = 30\n", encoding="utf-8")
    return folder


def request_for(project, name="plan", **changes):
    values = dict(task_type=focus.FOCI[name].task_type, prompt="plan.md is the plan.",
                  working_directory=str(project), files=("plan.md",), focus=name)
    values.update(changes)
    return review.ReviewRequest(**values)


def call(function, **arguments):
    async def invoke():
        return await function(**arguments)

    return anyio.run(invoke)


# ------------------------------------------------------------- the three entries


def test_the_three_entries_and_their_task_kinds():
    assert focus.NAMES == ("plan", "decision", "requirements")
    assert {name: item.task_type for name, item in focus.FOCI.items()} == {
        "plan": "architecture", "decision": "architecture", "requirements": "review"}
    for item in focus.FOCI.values():
        assert item.task_type in repo_policy.TASK_KINDS


@pytest.mark.parametrize("name", focus.NAMES)
def test_each_entry_reaches_codex_with_its_kind_and_its_instruction(project, name):
    executor = FakeExecutor()
    shown = review.package(request_for(project, name))

    reply = review.run(request_for(project, name, package_id=shown["package_id"]), executor)

    assert review.headline(reply) == "Codex answered."
    sent = executor.requests[0]
    assert sent.task_type.value == focus.FOCI[name].task_type
    assert sent.prompt.startswith(focus.FOCI[name].instruction)
    assert sent.prompt.endswith("TASK:\nplan.md is the plan.")
    # Every focus asks for the anchor, the decision affected, what is missing, and allows none.
    for phrase in ("Affects: <decision>", "MISSING:", "FINDINGS: none", "files_needed"):
        assert phrase in sent.prompt
    assert reply["focus"] == name and reply["package"]["focus"] == name
    assert reply["package"]["package_id"] == shown["package_id"]


def test_each_entry_asks_its_own_questions():
    assert all(heading in focus.FOCI["plan"].instruction
               for heading in ("Contradictions", "Assumptions to verify", "Decide before coding"))
    assert all(heading in focus.FOCI["decision"].instruction
               for heading in ("Holds when", "Could reverse it", "Check that would decide"))
    assert "REQ <id or short name> | met | gap | unproven" in focus.FOCI["requirements"].instruction


def test_the_general_review_is_unchanged(project):
    general = review.ReviewRequest(task_type="review", prompt="Review it.",
                                   working_directory=str(project), files=("src/decoder.py",))
    executor = FakeExecutor()

    shown = review.package(general)
    reply = review.run(general, executor)

    assert review.sent_text(general) == "Review it."
    assert executor.requests[0].prompt == "Review it."
    assert "focus" not in shown and "focus" not in reply and "task_text" not in shown
    assert "mentioned_files" not in shown
    # The id of a general package is computed as before focused reviews existed.
    digest = __import__("hashlib").sha256(b"Review it.").hexdigest()[:16]
    body = json.dumps({"schema": review.MANIFEST_SCHEMA, "task_type": "review", "prompt": digest,
                       "files": [[item["path"], item["bytes"], item["sha256"]]
                                 for item in shown["files"]]},
                      sort_keys=True, separators=(",", ":"))
    assert shown["package_id"] == __import__("hashlib").sha256(body.encode()).hexdigest()[:32]


def test_the_same_text_under_another_focus_is_another_package(project):
    plan = review.package(request_for(project, "plan"))
    decision = review.package(request_for(project, "decision"))
    general = review.package(review.ReviewRequest(task_type="architecture", prompt="plan.md is the plan.",
                                                  working_directory=str(project), files=("plan.md",)))

    assert len({plan["package_id"], decision["package_id"], general["package_id"]}) == 3


@pytest.mark.parametrize(("name", "kind", "reason"), [
    ("security", "review", review.FOCUS_NOT_VALID),
    ("", "review", review.FOCUS_NOT_VALID),
    ("plan", "review", review.FOCUS_KIND_MISMATCH),
    ("requirements", "architecture", review.FOCUS_KIND_MISMATCH),
    ("decision", "exploration", review.FOCUS_KIND_MISMATCH),
])
def test_an_unknown_focus_or_the_wrong_kind_starts_nothing(project, name, kind, reason):
    executor = FakeExecutor()
    request = request_for(project, task_type=kind, focus=name)

    reply = review.run(request, executor)

    assert (reply["refused"], reply["reason"]) == (True, reason)
    assert executor.requests == []
    assert review.gate(request, review.Session(), starts_codex=False)["reason"] == reason
    assert "nothing was started" in reply["message"]


# --------------------------------------------------------- what goes, and only that


def test_a_file_a_document_mentions_is_offered_not_sent(project):
    executor = FakeExecutor()
    shown = review.package(request_for(project))

    assert [item["path"] for item in shown["files"]] == ["plan.md"]
    assert shown["mentioned_files"] == ["requirements.md", "src/decoder.py", "config/sync.toml"]
    assert any(line.startswith("Named in the task text or a sent document, not sent")
               for line in review.package_lines(shown))

    reply = review.run(request_for(project, package_id=shown["package_id"]), executor)

    report: dict = {}
    noshell.supplied_prompt(executor.requests[0].prompt, str(project),
                            files=executor.requests[0].metadata["supplied_files"], report=report)
    assert [item["path"] for item in report["sent"]] == ["plan.md"]
    assert [item["path"] for item in reply["package"]["files"]] == ["plan.md"]
    assert reply["mentioned_files"] == shown["mentioned_files"]


def test_the_mention_hint_does_not_change_the_package_id_and_is_not_recorded(project):
    with_hint = review.package(request_for(project))
    (project / "requirements.md").unlink()
    without = review.package(request_for(project))

    assert "requirements.md" not in without["mentioned_files"]
    assert with_hint["package_id"] == without["package_id"]
    reply = review.run(request_for(project, package_id=without["package_id"]), FakeExecutor())
    conn = review_ledger.connect()
    row = conn.execute("SELECT manifest FROM packages WHERE decision_id = ?",
                       (reply["decision_id"],)).fetchone()
    conn.close()
    stored = json.loads(row["manifest"])
    assert stored["focus"] == "plan"
    assert "mentioned_files" not in stored and "task_text" not in stored


def test_a_mentioned_file_that_is_excluded_or_secret_is_never_offered(project):
    (project / ".modelquadignore").write_text("config/\n", encoding="utf-8")
    (project / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    (project / "plan.md").write_text(PLAN + "\nKeys live in .env.\n", encoding="utf-8")

    shown = review.package(request_for(project))

    assert "config/sync.toml" not in shown["mentioned_files"]
    assert ".env" not in shown["mentioned_files"]
    assert shown["mentioned_files"] == ["requirements.md", "src/decoder.py"]


def test_naming_an_excluded_file_stops_the_focused_review(project):
    (project / ".modelquadignore").write_text("config/\n", encoding="utf-8")
    executor = FakeExecutor()

    reply = review.run(request_for(project, files=("plan.md", "config/sync.toml")), executor)

    assert (reply["refused"], reply["reason"]) == (True, review.FILES_WITHHELD)
    assert executor.requests == []


def test_a_mentioned_file_reached_through_a_junction_is_never_offered(project, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret_plan.md").write_text("x\n", encoding="utf-8")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(project / "linked"), str(elsewhere)],
                          capture_output=True, check=False, **no_window_kwargs())
    if made.returncode != 0:
        pytest.skip("this machine cannot make a junction")
    (project / "plan.md").write_text(PLAN + "\nSee linked/secret_plan.md.\n", encoding="utf-8")

    assert "linked/secret_plan.md" not in review.package(request_for(project))["mentioned_files"]


def test_the_hint_reads_only_what_the_packer_sent_and_opens_no_file(project, monkeypatch):
    import builtins

    opened = []
    real_open = builtins.open

    def spy(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    packed = [("plan.md", "See requirements.md and `src/decoder.py`.")]
    monkeypatch.setattr(builtins, "open", spy)
    found = focus.mentioned_files(str(project), "Also config/sync.toml.", packed)
    monkeypatch.setattr(builtins, "open", real_open)

    assert found == ["config/sync.toml", "src/decoder.py", "requirements.md"]
    assert not any(name.endswith((".md", ".py", ".toml")) for name in opened), opened


def test_quoted_names_with_spaces_and_drive_relative_names(project):
    (project / "docs").mkdir()
    (project / "docs" / "Release Notes.md").write_text("x\n", encoding="utf-8")
    (project / "Notes.md").write_text("x\n", encoding="utf-8")
    (project / "outside.md").write_text("x\n", encoding="utf-8")
    text = "See `docs/Release Notes.md` and C:outside.md."

    assert focus.mentioned_files(str(project), text, []) == ["docs/Release Notes.md"]


def test_the_instruction_keeps_the_location_a_path():
    for name in focus.NAMES:
        text = focus.task_text(name, "x")
        assert "relative/path.ext:line" in text and "never a heading" in text


def test_a_mentioned_path_outside_the_folder_is_never_offered(project, tmp_path):
    (tmp_path / "outside.md").write_text("x\n", encoding="utf-8")
    (project / "plan.md").write_text(PLAN + "\nSee ../outside.md and C:/x/y.md.\n", encoding="utf-8")

    assert "outside.md" not in json.dumps(review.package(request_for(project)))


def test_a_change_to_the_task_text_after_the_preview_sends_nothing(project):
    executor = FakeExecutor()
    shown = review.package(request_for(project))

    reply = review.run(request_for(project, prompt="plan.md is the plan. Also check R2.",
                                   package_id=shown["package_id"]), executor)

    assert reply["reason"] == review.PACKAGE_CHANGED
    assert executor.requests == []


def test_a_change_to_a_file_after_the_preview_sends_nothing(project):
    executor = FakeExecutor()
    shown = review.package(request_for(project))
    (project / "plan.md").write_text(PLAN + "\nOne more line.\n", encoding="utf-8")

    reply = review.run(request_for(project, package_id=shown["package_id"]), executor)

    assert reply["reason"] == review.PACKAGE_CHANGED
    assert executor.requests == []


def test_another_focus_with_the_previewed_package_id_sends_nothing(project):
    executor = FakeExecutor()
    shown = review.package(request_for(project, "plan"))

    reply = review.run(request_for(project, "decision", package_id=shown["package_id"]), executor)

    assert reply["reason"] == review.PACKAGE_CHANGED
    assert executor.requests == []


def test_a_secret_in_the_task_text_stops_the_focused_review(project):
    excerpt = "plan excerpt: token ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    shown = review.package(request_for(project, prompt=excerpt))

    assert shown["refused"] is True and shown["reason"] == "task_text_secret"


# ----------------------------------------------------------- the repository's rules


@pytest.mark.parametrize(("name", "blocked"), [("plan", True), ("decision", True),
                                               ("requirements", False)])
def test_a_repository_that_removes_the_kind_stops_its_focus(project, name, blocked):
    (project / ".git").mkdir()
    (project / repo_policy.FILE_NAME).write_text(
        'schema = 1\n[codex]\ntask_kinds = ["review"]\n', encoding="utf-8", newline="\n")
    executor = FakeExecutor()

    reply = review.run(request_for(project, name), executor)

    if blocked:
        assert reply["refused"] is True and reply["reason"] == "repo_policy_kind_removed"
        assert executor.requests == []
    else:
        assert review.headline(reply) == "Codex answered."


# ------------------------------------------------------------ asking for more context


def test_codex_asking_for_more_files_adds_nothing(project):
    executor = FakeExecutor("MISSING: the requirements | R1 is quoted only in the plan\nFINDINGS: none",
                            needed=["requirements.md"])

    reply = review.run(request_for(project), executor)

    assert reply["completion_request"]["files"] == ["requirements.md"]
    assert [item["path"] for item in reply["package"]["files"]] == ["plan.md"]
    assert reply["completion_request"]["rounds_left"] == review.MAX_COMPLETION_ROUNDS

    executor_two = FakeExecutor()
    second = review.run(request_for(project, files=("plan.md", "requirements.md"),
                                    follows=reply["decision_id"]), executor_two)
    assert second["package_round"] == 1 and second["focus"] == "plan"
    assert executor_two.requests[0].prompt.startswith(focus.FOCI["plan"].instruction)


# ------------------------------------------------------------------ the MCP tools


def test_the_preview_tool_shows_the_whole_task_text_and_the_run_takes_the_package(project, monkeypatch):
    executor = FakeExecutor()
    monkeypatch.setattr(review_server, "_executor", lambda: executor)

    shown = call(review_server.review_package, task_type="architecture", prompt="plan.md is the plan.",
                 working_directory=str(project), files=["plan.md"], focus="plan")
    data = shown.structured_content
    assert data["task_text"] == review.shown_text(request_for(project))
    text = shown.content[0].text
    assert "Task text as sent (ModelQuad's fixed instruction for this focus, then yours):" in text
    assert focus.FOCI["plan"].instruction in text
    assert "- plan.md (" in text

    result = call(review_server.delegate, task_type="architecture", prompt="plan.md is the plan.",
                  working_directory=str(project), files=["plan.md"], focus="plan",
                  package_id=data["package_id"], timeout=60.0)
    assert result.structured_content["focus"] == "plan"
    assert result.structured_content["mentioned_files"] == data["mentioned_files"]
    assert "Named in the task text or a sent document, not sent" in result.content[0].text
    # Neither the task text nor the instruction reaches the ledger.
    with open(review_ledger.ledger_path(), "rb") as handle:
        stored = handle.read()
    assert b"plan.md is the plan" not in stored and b"FOCUSED REVIEW" not in stored


def test_the_preview_shows_the_task_text_after_redaction_as_codex_gets_it(project, monkeypatch):
    executor = FakeExecutor()
    monkeypatch.setattr(review_server, "_executor", lambda: executor)
    prompt = 'plan.md is the plan. The staging config has this line:\npassword = "hunter2"'

    shown = call(review_server.review_package, task_type="architecture", prompt=prompt,
                 working_directory=str(project), files=["plan.md"], focus="plan")
    call(review_server.delegate, task_type="architecture", prompt=prompt,
         working_directory=str(project), files=["plan.md"], focus="plan",
         package_id=shown.structured_content["package_id"], timeout=60.0)

    task_text = shown.structured_content["task_text"]
    assert "hunter2" not in task_text and "<credential>" in task_text
    assert "hunter2" not in shown.content[0].text
    # What Codex gets starts with exactly the text the user was shown.
    rendered = noshell.supplied_prompt(executor.requests[0].prompt, str(project), files=["plan.md"])
    assert rendered.startswith(task_text)


def test_the_general_preview_tool_shows_no_task_text(project):
    shown = call(review_server.review_package, task_type="review", prompt="Review it.",
                 working_directory=str(project), files=["src/decoder.py"])

    assert "task_text" not in shown.structured_content
    assert "Task text as sent" not in shown.content[0].text


def test_both_tools_take_focus_and_describe_it():
    tools = {tool.name: tool for tool in anyio.run(review_server.server.list_tools)}
    for name in ("review_package", "delegate"):
        schema = tools[name].input_schema["properties"]["focus"]
        assert "requirements" in json.dumps(schema)


# ------------------------------------------------------------------ the commands


_SKILLS = {"plan-review": ("plan", "architecture"),
           "challenge-decision": ("decision", "architecture"),
           "check-requirements": ("requirements", "review")}


@pytest.mark.parametrize("skill", sorted(_SKILLS))
def test_each_command_starts_its_focus_with_its_kind(skill):
    name, kind = _SKILLS[skill]
    text = (ROOT / "plugin" / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")

    assert f'`focus` "{name}", `task_type` "{kind}"' in text.replace("\n   ", " ")
    assert "disable-model-invocation: true" in text
    for step in ("review_package", "whole task text as sent", "package_id", "review_verdict",
                 "completion_request", "word for word"):
        assert step in text, (skill, step)
    # The focused command names only its own focus.
    others = {other for other, _ in _SKILLS.values()} - {name}
    assert not any(f'`focus` "{other}"' in text for other in others)


def test_the_general_review_command_is_still_there_and_unfocused():
    text = (ROOT / "plugin" / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8")

    assert '`task_type` "review"' in text and "focus" not in text


def test_the_terminal_preview_takes_a_focus(project):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    done = subprocess.run([sys.executable, "-m", "modelquad", "review", "package", "plan.md",
                           "--focus", "decision", "--project", str(project), "--json",
                           "--task", "plan.md holds the decision."],
                          capture_output=True, text=True, env=env, timeout=120,
                          **no_window_kwargs())

    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    assert data["task_type"] == "architecture" and data["focus"] == "decision"
    assert data["task_text"].startswith(focus.FOCI["decision"].instruction)
    assert re.fullmatch(r"[0-9a-f]{32}", data["package_id"])
