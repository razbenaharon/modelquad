# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Every surface a person reads uses plain words, and none of the banned ones.

The surfaces: the reply text of modelquad_delegate (answers, refusals, the
first-call notice), modelquad_review_package and modelquad_status, the CLI's
output and help, the setup check's lines, and the skills. A reply says first
what happened; an internal code stays in the data and never reaches the text
a person reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import anyio
import pytest

from modelquad import cli, grant, repo_policy, repo_settings, review, setupcheck, switch
from modelquad.execution import noshell
from modelquad.mcp import server as mcp
from modelquad.mcp import worker_guard
from test_review_flow import FakeExecutor, request_for

ROOT = Path(__file__).resolve().parents[1]

#: Wording the legal self-review (3.5) rules out, also in the negative.
BANNED = (
    "save quota", "save usage", "save your", "waste", "every last percent", "stretch", "extend your",
    "maximise", "maximize", "get more out of", "beat ", "bypass", "get around", "escape",
    "skip the limit", "skip a limit", "never hit", "keep coding", "runs out", "continue after",
    "failover", "more usage", "double your", "unlimited", "combine", "pool", "merge", "route",
    "routing", "router", "load balancing", "auto-switch", "arbitrage", "cheaper",
    "official", "partner", "endorsed", "integrated with", "safe", "secure", "guarantee",
    "locked-down", "locked down", "nothing leaves", "autonomous", "take over", "hand off to",
)
#: An internal code: two or more lowercase words joined by underscores.
CODE = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b")

#: Names, not claims: Claude Code's own mode name and the providers' "safety review" name.
NAMES = ("bypass-permissions", "safety review")
#: Names a person types or reads as names: the tools, an argument, the repository file's keys.
SPOKEN_NAMES = frozenset({"modelquad_review_package", "modelquad_delegate", "modelquad_review_verdict",
                          "modelquad_status", "package_id", "decision_id", "task_kinds", "max_files",
                          "max_total_kib"})


def banned_in(text: str) -> list[str]:
    folded = text.casefold()
    for name in NAMES:
        folded = folded.replace(name, "")
    return [phrase for phrase in BANNED if phrase in folded]


def check_person_text(text: str) -> None:
    assert banned_in(text) == [], text
    assert [code for code in CODE.findall(text) if code not in SPOKEN_NAMES] == [], text


def person_text(result) -> str:
    """The block a person reads: the first one, before the data."""
    blocks = [block.text for block in result.content]
    assert blocks, result
    assert not blocks[0].lstrip().startswith("{"), "the first block is data, not a sentence"
    return blocks[0]


@pytest.fixture
def project(tmp_path, monkeypatch):
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(worker_guard, "is_worker", lambda *args: False)
    return folder


def delegate(project, **arguments):
    async def invoke():
        return await mcp.modelquad_delegate(task_type="review", prompt="Look at run().",
                                            working_directory=str(project), files=["app.py"],
                                            timeout=60.0, **arguments)
    return anyio.run(invoke)


# ------------------------------------------------------------------ the replies


#: Every closed reason a refusal can carry, plus one nobody listed.
REASONS = sorted({
    review.WORKER, review.DUPLICATE, review.PACKAGE_CHANGED, review.ROUNDS_EXHAUSTED,
    review.FOLLOWS_UNKNOWN, review.FILES_WITHHELD, "task_type_not_read_only",
    "working_directory_missing", "working_directory_not_a_folder", "model_not_valid",
    "reasoning_effort_not_valid", "prompt_empty", "files_not_valid", "invalid_request",
    "cli_not_found", switch.OFF, switch.UNKNOWN, grant.NO_GRANT, grant.OTHER_FOLDER,
    repo_settings.USER_CODEX_OFF, *repo_policy.REASONS, *noshell.REFUSAL_LABELS,
    "something_new_and_unlisted",
})


@pytest.mark.parametrize("reason", REASONS)
def test_a_refusal_line_is_a_sentence_and_never_the_code(reason):
    messages = {switch.OFF: switch.OFF_MESSAGE, switch.UNKNOWN: switch.UNKNOWN_MESSAGE,
                grant.NO_GRANT: grant.NO_GRANT_MESSAGE, grant.OTHER_FOLDER: grant.OTHER_FOLDER_MESSAGE,
                repo_settings.USER_CODEX_OFF: repo_settings.USER_OFF_MESSAGE}
    payload = review.refusal(reason, messages.get(reason))
    if reason in repo_policy.REASONS:
        payload["message"] = repo_policy.message(reason)

    line = review.refusal_line(payload)

    assert reason not in line
    assert " " in line
    assert banned_in(line) == [], line
    assert review.headline(payload) == "Codex was not started."


def test_a_refused_call_says_codex_was_not_started_then_why(project, monkeypatch):
    monkeypatch.setattr(mcp, "_executor", FakeExecutor)
    switch.write(False)

    result = delegate(project)

    text = person_text(result)
    assert text.splitlines() == ["Codex was not started.", switch.OFF_MESSAGE]
    check_person_text(text)
    assert result.structured_content["reason"] == switch.OFF


def test_an_answer_says_what_was_sent_and_what_codex_reported(project, monkeypatch):
    monkeypatch.setattr(mcp, "_executor", lambda: FakeExecutor(needed=["app.py"]))

    text = person_text(delegate(project))

    assert text.splitlines()[0] == "Codex answered."
    assert "Sent: the task text and 1 file(s)" in text
    assert "Codex reported 1200 input and 80 output tokens" in text
    assert banned_in(text) == [], text


def test_the_first_call_notice_starts_with_the_outcome(project, monkeypatch):
    monkeypatch.setattr("modelquad.disclosure.notice_shown", lambda: False)
    monkeypatch.setattr("modelquad.disclosure.record_notice_shown", lambda *a: None)
    monkeypatch.setattr(mcp, "_executor", FakeExecutor)

    result = delegate(project)

    text = person_text(result)
    assert text.startswith("Codex was not started.")
    assert banned_in(text) == [], text
    assert result.structured_content["codex_started"] is False


def test_the_package_reply_reads_as_a_list_a_person_can_check(project):
    async def invoke():
        return await mcp.modelquad_review_package(task_type="review", prompt="Look.",
                                                  working_directory=str(project), files=["app.py"])

    text = person_text(anyio.run(invoke))

    assert text.splitlines()[0].startswith("Codex would get the task text")
    assert "- app.py (" in text
    check_person_text(text)


def test_the_status_reply_names_no_code(project, monkeypatch):
    from modelquad import settings_view

    monkeypatch.setattr(settings_view, "_claude_installed", lambda: True)

    text = person_text(mcp.modelquad_status(str(project)))

    assert text.splitlines()[0] == "ModelQuad settings"
    assert banned_in(text) == [], text


# ----------------------------------------------------------------- the command


def test_cli_outputs_carry_no_banned_phrase(project, monkeypatch, capsys):
    monkeypatch.setattr(setupcheck, "_codex", lambda: setupcheck._check("codex", True, "Codex CLI 0.160.0."))
    monkeypatch.setattr(setupcheck, "_connection",
                        lambda: setupcheck._check("claude_code", True, "ModelQuad is connected to Claude Code."))
    outputs = []
    for command in (["off"], ["on"], ["settings", "proactive", "off"], ["settings", "proactive", "on"],
                    ["settings", "show", "--project", str(project)],
                    ["settings", "repo", "--project", str(project)],
                    ["settings", "auto", "status", "--project", str(project)],
                    ["review", "package", "app.py", "--project", str(project)],
                    ["review", "report", "--project", str(project)],
                    ["doctor", "--project", str(project)], ["privacy"]):
        cli.main(command)
        captured = capsys.readouterr()
        outputs.append(captured.out + captured.err)
    assert any("Codex would get the task text" in text for text in outputs)
    for text in outputs:
        assert banned_in(text) == [], text


def test_cli_help_texts_carry_no_banned_phrase(capsys):
    for command in ([], ["doctor"], ["settings"], ["settings", "repo"], ["settings", "proactive"],
                    ["settings", "auto"], ["review"], ["review", "package"], ["install"], ["privacy"]):
        with pytest.raises(SystemExit):
            cli.main([*command, "--help"])
        assert banned_in(capsys.readouterr().out) == [], command


def test_the_setup_check_lines_say_what_to_do_in_words():
    report = {"ok": False, "checks": [
        setupcheck._check("codex", False, noshell.refusal_message("codex_version_unsupported"),
                          "Update the Codex CLI, then run this check again."),
        setupcheck._check("settings", None, "Whether ModelQuad is connected to Claude Code could not be read."),
    ]}

    text = "\n".join(setupcheck.lines(report))

    assert "[PROBLEM]" in text and "[unknown]" in text
    check_person_text(text)


# ----------------------------------------------------------------- the skills


def test_skills_have_one_line_descriptions_and_no_banned_phrase():
    skills = [*(ROOT / "plugin" / "skills").glob("*/SKILL.md"),
              *(ROOT / "src" / "modelquad" / "adapters" / "templates" / "claude" / "skills").glob("*/SKILL.md")]
    assert len(skills) == 8
    for path in skills:
        text = path.read_text(encoding="utf-8")
        assert banned_in(text) == [], path
        (description,) = [line for line in text.splitlines() if line.startswith("description: ")]
        assert len(description) <= 160 and description.endswith("."), path
        assert "worker" not in description.casefold(), path


def test_one_name_per_concept_in_the_skills():
    """Claude Code's prompt is the "approval prompt"; Codex through ModelQuad is read-only."""
    for path in (ROOT / "plugin" / "skills").glob("*/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        assert "Claude Code asks them again" not in text and "ModelQuad's Codex worker" not in text, path
        for retired in ("modelquad_handoff", "modelquad_split", "modelquad_job", "modelquad_doctor",
                        "host disconnect", "usage-hook"):
            assert retired not in text, (path, retired)
