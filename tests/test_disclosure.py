# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A user is told where their work goes before it goes."""

from __future__ import annotations

from modelquad import cli
from modelquad.disclosure import (
    CHATGPT_DATA_CONTROLS,
    CLAUDE_PRIVACY_SETTINGS,
    DATA_FLOW,
)


def test_the_data_flow_notice_names_every_destination_and_setting():
    text = "\n".join(DATA_FLOW)
    for required in ("OpenAI", "Anthropic", "ModelQuad reads the files the request names",
                     "asks you before each call",
                     "employer or a client", CHATGPT_DATA_CONTROLS, CLAUDE_PRIVACY_SETTINGS,
                     "Opting out does not cover feedback", "limit or suspend accounts",
                     "unattended", "other licences", "does not change any usage limit"):
        assert required in text
    # Codex has no file tools in this release; the notice must not say it reads files.
    assert "Codex can read files" not in text


def test_the_data_flow_notice_names_no_removed_feature():
    """The notice describes this build: no background quota read, no job runner,
    no account kind shown to Claude."""
    text = " ".join(" ".join(DATA_FLOW).split())
    for removed in ("MODELQUAD_CODEX_QUOTA_REFRESH", "usage percentages", "job-runner",
                    "Background jobs", "kind of each account"):
        assert removed not in text, removed


def test_unattended_is_defined_once_and_the_readme_says_the_same():
    from pathlib import Path

    def flat(text: str) -> str:
        return " ".join(text.replace("`", "").split())

    notice = flat("\n".join(DATA_FLOW))
    definition = ("every Codex call should come from a Claude Code session a person"
                  " started for their own work.")
    for required in ("Do not run ModelQuad unattended: " + definition,
                     "Do not script or schedule ModelQuad, run it in CI"):
        assert required in notice
    readme = flat((Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8"))
    for required in ("Do not run ModelQuad unattended: " + definition,
                     "Do not script or schedule ModelQuad, run it in CI"):
        assert required in readme


def test_install_prints_the_data_flow_notice(tmp_path, capsys):
    assert cli.main(["install", "--all", "--claude-dir", str(tmp_path / "claude")]) == 0
    assert DATA_FLOW[0] in capsys.readouterr().out


def test_a_dry_run_uninstall_prints_no_notice(tmp_path, capsys):
    cli.main(["uninstall", "--all", "--claude-dir", str(tmp_path / "claude"), "--dry-run"])
    assert DATA_FLOW[0] not in capsys.readouterr().out

