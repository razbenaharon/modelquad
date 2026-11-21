# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""What the installed ``modelquad`` entry answers before the full CLI is loaded.

``cli_entry`` has two early answers. A hook command an earlier build wrote
into Claude Code's settings (the status line, the SessionStart note, the
PreToolUse note) exits 0 with no output, reading and importing nothing. The
review hook runs its handler, except in a worker, which exits before the
handler is imported. Every other command goes on to ``cli.main``.
"""

from __future__ import annotations

import io
import sys

import pytest

from modelquad import cli_entry

RETIRED = [
    ["hook-pending", "claude"],
    ["hook-gate"],
    ["usage-hook", "claude", "--quiet"],
    ["usage-hook", "claude", "--segment"],
    # The global options earlier builds accepted before the command.
    ["--usage-directory", "DIR", "usage-hook", "claude"],
    ["--history-db", "old.db", "hook-pending", "claude"],
    ["--config", "old.yaml", "--no-history", "hook-gate"],
]

#: The modules the full CLI, the hook handler and the MCP server bring in.
_HEAVY = ("modelquad.cli", "modelquad.adapters.review_reminder", "modelquad.mcp.server",
          "modelquad.switch", "modelquad.review_ledger")


class _ForbiddenInput:
    """Standing in for stdin where nothing may be read."""

    def read(self, *args):
        raise AssertionError("stdin was read")

    @property
    def buffer(self):
        raise AssertionError("stdin was read")


def _forget(monkeypatch) -> None:
    for name in _HEAVY:
        monkeypatch.delitem(sys.modules, name, raising=False)


def _loaded() -> list[str]:
    return [name for name in _HEAVY if name in sys.modules]


@pytest.mark.parametrize("argv", RETIRED, ids=" ".join)
def test_a_retired_hook_command_exits_zero_silently(argv, monkeypatch, capsys):
    _forget(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _ForbiddenInput())
    assert cli_entry.dispatch_retired(argv) == 0
    assert cli_entry.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert _loaded() == []


@pytest.mark.parametrize("argv", RETIRED, ids=" ".join)
def test_a_retired_hook_command_writes_no_file(argv, tmp_path, monkeypatch, isolate_home_directory):
    monkeypatch.chdir(tmp_path)
    before = sorted(tmp_path.rglob("*"))
    assert cli_entry.main(argv) == 0
    assert sorted(tmp_path.rglob("*")) == before
    assert not isolate_home_directory.exists()


@pytest.mark.parametrize("argv", [
    [], ["--version"], ["settings", "show"], ["on"], ["off"], ["mcp"], ["doctor"],
    ["hook-review", "claude"], ["review", "report"], ["install", "claude"],
    # A retired name that is an argument, not the command, retires nothing.
    ["settings", "repo", "off", "--project", "usage-hook"],
    ["review", "package", "hook-gate"],
    # An option that takes a value hides only its own value.
    ["--usage-directory", "usage-hook", "settings"],
    ["--usage-directory"],
])
def test_every_other_command_is_not_retired(argv):
    assert cli_entry.dispatch_retired(argv) is None


def test_the_retired_names_are_exactly_the_three_earlier_hooks():
    assert cli_entry.RETIRED_HOOK_COMMANDS == {"hook-pending", "hook-gate", "usage-hook"}
    # No retired name is a command of this build: nothing live is silenced.
    from modelquad.cli import build_parser

    commands = build_parser()._subparsers._group_actions[0].choices
    assert not cli_entry.RETIRED_HOOK_COMMANDS & set(commands)


def test_the_review_hook_in_a_worker_exits_before_the_handler_is_imported(monkeypatch, capsys):
    _forget(monkeypatch)
    monkeypatch.setenv("MODELQUAD_WORKER", "1")
    monkeypatch.setattr(sys, "stdin", _ForbiddenInput())
    assert cli_entry.dispatch_review(["hook-review", "claude"]) == 0
    assert cli_entry.main(["hook-review", "claude"]) == 0
    assert capsys.readouterr().out == ""
    assert _loaded() == []


@pytest.mark.parametrize("argv", [
    ["hook-review"], ["hook-review", "codex"], ["hook-review", "claude", "extra"],
])
def test_a_malformed_review_hook_call_exits_zero_without_the_handler(argv, monkeypatch, capsys):
    _forget(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _ForbiddenInput())
    assert cli_entry.dispatch_review(argv) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert _loaded() == []


def test_the_review_hook_hands_stdin_and_stdout_to_the_handler(monkeypatch):
    from modelquad.adapters import review_reminder

    seen = []
    stdin = io.TextIOWrapper(io.BytesIO(b"{}"))
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(review_reminder, "run_hook",
                        lambda host, source, out, **kw: seen.append((host, source, out)) or 0)
    assert cli_entry.dispatch_review(["hook-review", "claude"]) == 0
    # Bytes in, so the handler decodes UTF-8 itself; the real stdout out.
    # (``facts`` is passed by keyword for the timing record, hook_timing.py.)
    assert seen == [("claude", stdin.buffer, sys.stdout)]


def test_a_failing_review_handler_is_silent(monkeypatch, capsys):
    from modelquad.adapters import review_reminder

    def fail(*args, **kwargs):
        raise RuntimeError("private failure text")

    monkeypatch.setattr(review_reminder, "run_hook", fail)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert cli_entry.dispatch_review(["hook-review", "claude"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


@pytest.mark.parametrize("argv", [
    [], ["--version"], ["settings", "show"], ["on"], ["mcp"], ["hook-gate"],
    ["usage-hook", "claude"], ["review", "report"], ["settings", "hook-review"],
])
def test_only_the_review_hook_is_dispatched_as_one(argv, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _ForbiddenInput())
    assert cli_entry.dispatch_review(argv) is None


def test_the_full_cli_gives_the_same_early_answers(monkeypatch, capsys):
    """``cli.main`` is reachable directly (``python -m``-style); it answers the same."""
    from modelquad import cli

    monkeypatch.setattr(sys, "stdin", _ForbiddenInput())
    for argv in RETIRED:
        assert cli.main(argv) == 0
    monkeypatch.setenv("MODELQUAD_WORKER", "1")
    assert cli.main(["hook-review", "claude"]) == 0
    assert capsys.readouterr().out == ""
