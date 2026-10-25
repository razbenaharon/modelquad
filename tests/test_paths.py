# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sys
from pathlib import Path

import modelquad.paths as paths_module
from modelquad.paths import (
    db_path,
    is_dev_checkout,
    logs_dir,
    sessions_path,
    state_root,
)
from modelquad.service import db as service_db


def test_dev_checkout_detection_follows_the_src_layout_rule(tmp_path, monkeypatch):
    """Detection is by layout, not by ambient install mode.

    This used to assert ``is_dev_checkout() is True`` on the grounds that the
    suite only ever runs from a checkout. That stopped being true the moment
    CI began installing the package non-editable on purpose: there the package
    lives in site-packages and the correct answer is False. Asserting the
    ambient value tested the environment; asserting the rule tests the code,
    and holds under both installation modes.
    """

    checkout = tmp_path / "checkout"
    package = checkout / "src" / "modelquad"
    package.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(paths_module, "__file__", str(package / "paths.py"))
    assert is_dev_checkout() is True

    # site-packages shape: no ``src`` parent, no sibling pyproject.toml.
    installed = tmp_path / "site-packages" / "modelquad"
    installed.mkdir(parents=True)
    monkeypatch.setattr(paths_module, "__file__", str(installed / "paths.py"))
    assert is_dev_checkout() is False


def test_dev_mode_state_root_uses_the_identical_construction_as_pre_v08_defaults(monkeypatch):
    """Load-bearing backward-compat constraint: dev mode must not move anyone's data.

    Dev mode is forced rather than assumed. Whether the ambient install is
    editable is not a property of this test, and under a non-editable install
    (CI) the resolver correctly returns installed-mode paths instead -- which
    would fail this test for the wrong reason. The installed-mode counterpart
    is covered by ``test_simulated_installed_windows_db_path_resolves_under_localappdata``.

    Compares against the *construction rule* -- ``Path.home() / ".modelquad"``
    -- evaluated at test time, not against config.py's/service/db.py's own
    ``DEFAULT_SESSIONS_PATH``/``DEFAULT_DB_PATH`` module constants: those are
    resolved once at import time (a pre-existing gotcha this repo's own
    tests/conftest.py already has to work around for DEFAULT_DB_PATH
    specifically), so comparing against them directly under this file's
    autouse home-isolation fixture would be comparing against whatever
    Path.home() happened to be at collection time, not this test's fake one.
    """

    monkeypatch.setattr(paths_module, "is_dev_checkout", lambda: True)

    assert db_path() == Path.home() / ".modelquad" / "modelquad.db"
    assert sessions_path() == Path.home() / ".modelquad" / "sessions.json"
    assert state_root() == Path.home() / ".modelquad"


def test_logs_dir_is_under_state_root():
    assert logs_dir() == state_root() / "logs"


def test_installed_copy_keeps_its_state_outside_appdata_on_windows(monkeypatch):
    """Windows redirects what the Claude desktop app's programs write under
    AppData to a private copy, so an installed copy's state lives in the home
    folder instead, where the desktop app and a terminal see the same files."""
    monkeypatch.setattr(paths_module, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Someone\AppData\Local")

    assert state_root() == Path.home() / ".local" / "share" / "modelquad"
    assert db_path() == Path.home() / ".local" / "share" / "modelquad" / "modelquad.db"
    assert "AppData" not in state_root().parts


def test_non_editable_install_is_not_treated_as_dev_checkout(monkeypatch, tmp_path):
    """Simulate a package copied into site-packages (no src/ layout, no sibling pyproject.toml)."""

    fake_site_packages = tmp_path / "site-packages" / "modelquad"
    fake_site_packages.mkdir(parents=True)
    fake_file = fake_site_packages / "paths.py"
    fake_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(paths_module, "__file__", str(fake_file))

    assert is_dev_checkout() is False


def test_installed_copy_does_not_need_localappdata(monkeypatch):
    monkeypatch.setattr(paths_module, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert state_root() == Path.home() / ".local" / "share" / "modelquad"


def test_service_db_default_db_path_matches_paths_db_path():
    assert service_db.DEFAULT_DB_PATH == db_path()


def test_installed_and_development_state_never_share_a_folder(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(paths_module, "is_dev_checkout", lambda: False)
    installed = state_root()
    monkeypatch.setattr(paths_module, "is_dev_checkout", lambda: True)
    assert state_root() != installed
