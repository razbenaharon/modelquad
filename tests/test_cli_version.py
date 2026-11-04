# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""The version flag.

`--version` is the first thing anyone types at an unfamiliar CLI. It used to
error out with `unrecognized arguments: --version` and a usage dump, leaving
the version reachable only by reading the tail of `doctor` -- which a beta
tester reporting a problem is unlikely to find.
"""

from __future__ import annotations

import subprocess

import pytest

from modelquad import buildinfo
from modelquad.branding import PRODUCT_NAME, installed_version
from modelquad.cli import build_parser

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize("flag", ["--version", "-V"])
def test_version_flag_prints_product_and_version_then_exits(flag, capsys):
    parser = build_parser()

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args([flag])

    assert excinfo.value.code == 0
    printed = capsys.readouterr().out.strip()
    assert printed.startswith(f"{PRODUCT_NAME} {installed_version('unknown')}")
    assert printed.endswith(("(source)", "(unknown build)"))


def test_version_flag_names_the_product_a_user_recognizes(capsys):
    """Not the module or entry-point name -- the product name."""

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--version"])

    printed = capsys.readouterr().out
    assert printed.startswith(PRODUCT_NAME)
    assert "modelquad " not in printed


def test_version_flag_does_not_displace_subcommands():
    """The new top-level flag must not shadow normal parsing."""

    args = build_parser().parse_args(["route", "trivial"])
    assert args.command == "route"


# ----------------------------------------------------------------
#
# The version flag used to print the package version alone, so every build
# answered `ModelQuad 0.2.0`. It now names the build: the checkout's commit,
# and the channel.


def _version_line(capsys) -> str:
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--version"])
    assert excinfo.value.code == 0
    return capsys.readouterr().out.strip()


def test_a_source_checkout_names_its_commit(monkeypatch, capsys, tmp_path):
    # Pinned to a checkout root so the test holds when the suite runs against
    # an installed (non-editable) copy, as CI does.
    monkeypatch.setattr(buildinfo, "_source_checkout_root", lambda: tmp_path)
    monkeypatch.setattr(buildinfo, "_git_head", lambda root: SHA)
    assert _version_line(capsys) == f"{PRODUCT_NAME} {installed_version('unknown')}+{SHA[:7]} (source)"


def test_a_build_git_cannot_name_says_it_is_unknown(monkeypatch, capsys):
    monkeypatch.setattr(buildinfo, "_git_head", lambda root: None)
    assert _version_line(capsys) == f"{PRODUCT_NAME} {installed_version('unknown')} (unknown build)"


def test_other_commands_do_not_pay_for_the_build_identity(monkeypatch):
    """Resolved only when --version is used: building the parser asks git nothing."""

    def refuse(*args, **kwargs):
        raise AssertionError("git was run to build the parser")

    monkeypatch.setattr(subprocess, "run", refuse)
    build_parser().parse_args(["route", "trivial"])


def test_advise_is_the_command_and_route_still_works_unlisted(capsys):
    parser = build_parser()
    assert parser.parse_args(["advise", "review"]).command == "advise"
    assert parser.parse_args(["route", "review"]).command == "route"
    text = parser.format_help()
    assert "advise" in text and "route" not in text
