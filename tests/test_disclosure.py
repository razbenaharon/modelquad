# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A user is told where their work goes before it goes, and background work
needs its own explicit yes."""

from __future__ import annotations

import argparse
import io

import pytest

from modelquad import cli
from modelquad.disclosure import (
    BACKGROUND_WORK,
    CHATGPT_DATA_CONTROLS,
    CLAUDE_PRIVACY_SETTINGS,
    DATA_FLOW,
)


def test_the_data_flow_notice_names_every_destination_and_setting():
    text = "\n".join(DATA_FLOW)
    for required in ("OpenAI", "Anthropic", "ModelQuad reads the files the request names",
                     "asks you before each call", "MODELQUAD_CODEX_QUOTA_REFRESH",
                     "employer or a client", CHATGPT_DATA_CONTROLS, CLAUDE_PRIVACY_SETTINGS,
                     "Opting out does not cover feedback", "limit or suspend accounts",
                     "unattended", "other licences", "does not change any usage limit",
                     "kind of each account (personal, work or unknown)",
                     "this repository's Codex setting"):
        assert required in text
    # Codex has no file tools in this release; the notice must not say it reads files.
    assert "Codex can read files" not in text


def test_unattended_is_defined_once_and_the_readme_says_the_same():
    # "Unattended" must not forbid what the product offers: the definition
    # names the job runner the user turned on as the consented exception.
    from pathlib import Path

    def flat(text: str) -> str:
        return " ".join(text.replace("`", "").split())

    notice = flat("\n".join(DATA_FLOW))
    definition = ("every Codex call should come from a Claude Code session a person"
                  " started for their own work.")
    exception = "Background jobs you turned on yourself (modelquad settings job-runner)"
    for required in ("Do not run ModelQuad unattended: " + definition, exception,
                     "Do not script or schedule ModelQuad, run it in CI"):
        assert required in notice
    readme = flat((Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8"))
    for required in ("Do not run ModelQuad unattended: " + definition, exception,
                     "Do not script or schedule ModelQuad, run it in CI"):
        assert required in readme


def test_the_background_notice_says_what_runs_unattended_and_how_to_stop_it():
    text = "\n".join(BACKGROUND_WORK)
    assert "after Claude Code" in text and "closes" in text
    assert "Codex plan's usage" in text
    assert "modelquad settings job-runner off" in text


def test_install_prints_the_data_flow_notice(tmp_path, capsys):
    assert cli.main(["install", "--all", "--claude-dir", str(tmp_path / "claude")]) == 0
    assert DATA_FLOW[0] in capsys.readouterr().out


def test_a_dry_run_uninstall_prints_no_notice(tmp_path, capsys):
    cli.main(["uninstall", "--all", "--claude-dir", str(tmp_path / "claude"), "--dry-run"])
    assert DATA_FLOW[0] not in capsys.readouterr().out


def _args(consent: bool) -> argparse.Namespace:
    return argparse.Namespace(consent=consent)


class _Stdin(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__("")
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_background_work_with_yes_flag_is_consent(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Stdin(False))
    assert cli._background_work_consented(_args(True)) is True
    assert BACKGROUND_WORK[0] in capsys.readouterr().out


def test_background_work_without_a_terminal_or_flag_is_refused(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Stdin(False))
    assert cli._background_work_consented(_args(False)) is False
    captured = capsys.readouterr()
    assert BACKGROUND_WORK[0] in captured.out
    assert "--yes" in captured.err


@pytest.mark.parametrize("answer, expected", [("yes", True), ("YES ", True), ("y", False), ("", False)])
def test_background_work_at_a_terminal_needs_a_typed_yes(monkeypatch, answer, expected):
    monkeypatch.setattr("sys.stdin", _Stdin(True))
    assert cli._background_work_consented(_args(False), input_fn=lambda prompt: answer) is expected


def test_turning_the_runner_on_without_consent_never_reaches_the_service(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Stdin(False))

    def refuse(*args, **kwargs):
        raise AssertionError("the setting was sent without consent")

    monkeypatch.setattr("modelquad.jobs.service_client.connect_management", refuse)
    assert cli.main(["settings", "job-runner", "on"]) == 2


def test_turning_the_runner_off_needs_no_consent(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _Stdin(False))

    class Client:
        def job_runner_setting(self, enabled):
            assert enabled is False
            return {"job_runner_readiness": "off"}

    monkeypatch.setattr("modelquad.jobs.service_client.connect_management", lambda db: Client())
    assert cli.main(["settings", "job-runner", "off"]) == 0
    assert BACKGROUND_WORK[0] not in capsys.readouterr().out


def test_accepted_consent_is_recorded_before_the_service_is_asked(monkeypatch, capsys, tmp_path):
    from modelquad.service import history

    db_path = tmp_path / "consent.db"
    seen = []

    class Client:
        def job_runner_setting(self, enabled):
            # The service refuses to enable without this record, so it must exist first.
            seen.append(history.get_job_runner_consent_at(db_path))
            return {"job_runner_readiness": "ready"}

    monkeypatch.setattr("modelquad.jobs.service_client.connect_management", lambda db: Client())
    assert cli.main(["--history-db", str(db_path), "settings", "job-runner", "on", "--yes"]) == 0
    assert seen and seen[0] is not None
    assert history.get_job_runner_consent_at(db_path) == seen[0]


def test_refused_consent_records_nothing(monkeypatch, capsys, tmp_path):
    from modelquad.service import history

    db_path = tmp_path / "no-consent.db"
    monkeypatch.setattr("sys.stdin", _Stdin(False))
    assert cli.main(["--history-db", str(db_path), "settings", "job-runner", "on"]) == 2
    assert history.get_job_runner_consent_at(db_path) is None
