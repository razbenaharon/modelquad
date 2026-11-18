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
                     "each call needs your approval in Claude Code",
                     "no file list shown to you first",
                     "a secret under an ordinary", "keeps a record of each call on this computer",
                     "API key or a ChatGPT workspace",
                     "employer or a client", CHATGPT_DATA_CONTROLS, CLAUDE_PRIVACY_SETTINGS,
                     "Opting out does not cover feedback", "limit or suspend accounts",
                     "Automated runs", "other licences", "does not change any usage limit"):
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


def test_automation_and_the_account_rule_are_stated_as_they_are():
    from pathlib import Path

    def flat(text: str) -> str:
        return " ".join(text.replace("`", "").split())

    notice = flat("\n".join(DATA_FLOW))
    readme = flat((Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8"))
    # No blanket ban stated as if it were a provider rule.
    for text in (notice, readme):
        assert "Do not run ModelQuad unattended" not in text
        assert "Do not script or schedule ModelQuad" not in text
        # The account rule as the providers state it, and what is not settled.
        assert "making your account available to anyone else" in text
        assert "not settled" in text
    # What was not tested is said where automation is described.
    assert "CI, other schedulers and an API-key sign-in were not" in notice
    assert "Not tested: CI runners" in readme
    assert "## Automated runs" in readme


def test_install_prints_the_data_flow_notice(tmp_path, capsys):
    assert cli.main(["install", "--all", "--claude-dir", str(tmp_path / "claude")]) == 0
    assert DATA_FLOW[0] in capsys.readouterr().out


def test_a_dry_run_uninstall_prints_no_notice(tmp_path, capsys):
    cli.main(["uninstall", "--all", "--claude-dir", str(tmp_path / "claude"), "--dry-run"])
    assert DATA_FLOW[0] not in capsys.readouterr().out

