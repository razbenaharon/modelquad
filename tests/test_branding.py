# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest


def test_branding_constants():
    from modelquad import branding

    assert branding.PRODUCT_NAME == "ModelQuad"
    assert branding.PRODUCT_SLUG == "modelquad"
    # The private predecessor's names are not part of the public product.
    assert not hasattr(branding, "LEGACY_PRODUCT_NAME")
    assert not hasattr(branding, "LEGACY_CLI_COMMAND")


def test_cli_banner_contains_product_name(monkeypatch, capsys, managed_service_child):
    import uvicorn
    import modelquad.cli as cli_module
    import modelquad.service.app as service_app_module
    from modelquad import windowless_diagnostics as diagnostics
    from modelquad.branding import PRODUCT_NAME

    monkeypatch.setattr(service_app_module, "create_app", lambda **kwargs: object())
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    argv, manager = managed_service_child(["serve"])
    # The managed background child discards raw output. Restore process-wide
    # streams before checking the foreground controller's console branding.
    with monkeypatch.context() as child:
        child.setattr(cli_module.sys, "stdout", cli_module.sys.stdout)
        child.setattr(cli_module.sys, "stderr", cli_module.sys.stderr)
        child.setattr(diagnostics, "_active", False)
        child.setattr(diagnostics, "_sink", None)
        child.setattr(diagnostics, "_budgets", {})
        assert cli_module.main(argv) == 0
        assert isinstance(cli_module.sys.stdout, diagnostics.NullText)
        assert isinstance(cli_module.sys.stderr, diagnostics.NullText)
    background_output = capsys.readouterr()
    assert background_output.out == "" and background_output.err == ""
    from modelquad.service.lifecycle import ProcessResult
    monkeypatch.setattr(manager, "start", lambda **kwargs: ProcessResult(
        success=True, supported=True, running=True, pid=550,
        message="Started the detached control-plane service.", action="start", ready=True))
    monkeypatch.setattr(manager, "wait_started_child", lambda: 0)
    assert cli_module.main(["serve"]) == 0
    captured = capsys.readouterr().out
    assert PRODUCT_NAME in captured
    assert "ModelQuad" in captured


def test_version_lookup_falls_back_to_source_version_without_dist_info(monkeypatch):
    """A copy with no dist-info must still report the real product version on
    /api/health and in DecisionTrace provenance, rather than 'n/a'."""

    from importlib import metadata as importlib_metadata

    import modelquad
    from modelquad.branding import installed_version

    def nothing_resolves(name):
        raise importlib_metadata.PackageNotFoundError(name)

    monkeypatch.setattr("importlib.metadata.version", nothing_resolves)
    assert installed_version("n/a") == modelquad.__version__
