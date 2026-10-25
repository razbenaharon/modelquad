# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Canonical product state: one installation, one store, one truth.

Every test here is written against the live 2026-08-26 defect rather than
against the implementation that fixes it. The bug was not that some function
returned the wrong path -- each function returned a locally correct path. It
was that:

* the host launched ModelQuad by a bare PATH name, so *which* ModelQuad it
  reached was decided by PATH order the product does not control;
* ``paths.state_root()`` then answered from packaging shape, so the
  editable runtime the host reached honestly owned development state;
* and nothing in the product compared the two, so ``doctor`` was green while
  the former Control Center said ``routing_mode=automatic`` and the host's MCP said
  ``manual``.

So the coverage is in three layers: resolution (does each surface agree?),
registration (does a host launch the runtime that owns the canonical state?),
and detection (would we notice if it happened again?).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import modelquad.paths as paths_module
from modelquad import hostruntime, statediag
from modelquad.service import db as service_db
from modelquad.statecontext import (
    STATE_MODE_ENV_VAR,
    STATE_ROOT_ENV_VAR,
    StateMode,
    StateSource,
    installed_state_root,
    resolve_state_context,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def force_installed(monkeypatch, local_appdata: Path) -> Path:
    """Make this process resolve as a non-editable installed product."""

    monkeypatch.setattr(paths_module, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    return installed_state_root()[0]


def force_development(monkeypatch) -> None:
    monkeypatch.setattr(paths_module, "is_dev_checkout", lambda: True)


def make_store(root: Path, settings: dict[str, object]) -> Path:
    """Create a minimal ModelQuad store, the way a real one looks on disk."""

    root.mkdir(parents=True, exist_ok=True)
    database = root / "modelquad.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for key, value in settings.items():
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value))
            )
        connection.execute("PRAGMA user_version=15")
        connection.commit()
    finally:
        connection.close()
    return database


# --------------------------------------------------------------------------
# 1. State resolution -- every installed surface resolves the same store
# --------------------------------------------------------------------------


def test_every_installed_surface_resolves_one_canonical_store(monkeypatch, tmp_path):
    """CLI, MCP, service and its HTTP API must agree, by construction.

    Each of these four surfaces asks a *different* module where state lives.
    Before the fix they could disagree; this asserts they cannot, by checking
    that all four resolve through the same authority rather than by comparing
    four hardcoded strings (which would pass even if all four were wrong).
    """

    expected = force_installed(monkeypatch, tmp_path / "AppData" / "Local")

    from modelquad.service import lifecycle

    context = resolve_state_context()
    assert context.mode is StateMode.INSTALLED
    assert context.state_root == expected

    # service/db.py -- the MCP server, the CLI and the HTTP API all read state
    # through this resolver.
    assert paths_module.db_path() == expected / "modelquad.db"
    assert context.database_path == expected / "modelquad.db"

    # service/lifecycle.py -- the pidfile and logs clients of the local service
    # use to find the running service.
    manager = lifecycle.ServiceProcessManager()
    assert manager.state_dir == expected

    # The HTTP API's identity handshake must claim the SAME kind it
    # actually serves. Reporting "development" while writing installed state
    # is precisely the contradiction that made the split invisible.
    from modelquad.service.api import get_health

    health = get_health()
    assert health["instance_kind"] == "installed"
    assert health["state_root"] == str(expected)


def test_a_non_editable_install_no_longer_claims_to_be_a_development_instance(
    monkeypatch, tmp_path
):
    """The latent half of the split, independent of PATH.

    ``instance_kind`` used to be ``"installed" if is_frozen() else
    "development"`` while ``state_root()`` keyed off ``is_dev_checkout()``.
    A plain wheel install satisfies neither predicate the same way: it is not
    frozen and not a checkout, so it reported *development* while writing
    *installed* state. Two answers, one process.
    """

    monkeypatch.setattr(paths_module, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

    context = resolve_state_context()
    assert context.mode is StateMode.INSTALLED
    assert context.source is StateSource.INSTALLED_DEFAULT

    from modelquad.service.api import get_health

    assert get_health()["instance_kind"] == "installed"
    assert Path(get_health()["state_root"]) == context.state_root


# --------------------------------------------------------------------------
# 2. Development isolation -- allowed, but only when asked for
# --------------------------------------------------------------------------


def test_an_explicit_state_root_isolates_without_touching_installed_state(
    monkeypatch, tmp_path
):
    """Development isolation must be available -- and must be a declaration.

    The bug was never that development state exists. It was that a production
    integration fell into it silently. An explicit root is the supported way
    to say "this instance is separate", and it must win over every packaging
    inference.
    """

    installed = force_installed(monkeypatch, tmp_path / "AppData" / "Local")
    sandbox = tmp_path / "sandbox-state"
    monkeypatch.setenv(STATE_ROOT_ENV_VAR, str(sandbox))

    context = resolve_state_context()
    assert context.source is StateSource.ENV_OVERRIDE
    assert context.mode is StateMode.DEVELOPMENT
    assert context.state_root == sandbox
    assert paths_module.db_path() == sandbox / "modelquad.db"

    # The installed store is untouched -- isolation never migrates or writes
    # over the production store it is isolating from.
    assert not installed.exists()


def test_an_explicit_mode_selects_that_modes_canonical_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    monkeypatch.setattr(paths_module, "is_dev_checkout", lambda: True)
    monkeypatch.setenv(STATE_MODE_ENV_VAR, "installed")

    context = resolve_state_context()
    assert context.mode is StateMode.INSTALLED
    assert context.source is StateSource.ENV_OVERRIDE
    assert context.state_root == installed_state_root()[0]


def test_an_unparseable_mode_override_falls_through_instead_of_breaking_startup(
    monkeypatch, tmp_path
):
    """A typo in an environment variable must not make the product unstartable."""

    force_development(monkeypatch)
    monkeypatch.setenv(STATE_MODE_ENV_VAR, "prodcution")

    context = resolve_state_context()
    assert context.mode is StateMode.DEVELOPMENT
    assert context.source is StateSource.DEV_CHECKOUT


def test_dev_mode_state_root_is_unchanged_from_pre_fix_behaviour(monkeypatch):
    """Load-bearing: this fix must not move an existing developer's data."""

    force_development(monkeypatch)
    context = resolve_state_context()
    assert context.state_root == Path.home() / ".modelquad"
    assert context.database_path == Path.home() / ".modelquad" / "modelquad.db"


# --------------------------------------------------------------------------
# 3. Host registration -- a host must launch the runtime that owns the state
# --------------------------------------------------------------------------


def test_an_installed_product_registers_the_bare_command_name(monkeypatch, tmp_path):
    """pip puts ``modelquad-mcp`` on PATH; there is no installer shim to pin.

    The shim folder of the retired installer is never consulted, even when an
    old one is still on disk.
    """

    install_root = force_installed(monkeypatch, tmp_path / "AppData" / "Local")
    old_shim = install_root / "bin" / "modelquad-mcp.cmd"
    old_shim.parent.mkdir(parents=True)
    old_shim.write_text("@echo off\n", encoding="utf-8")

    assert hostruntime.mcp_launch_command() == "modelquad-mcp"


def test_a_development_checkout_keeps_the_bare_name(monkeypatch):
    force_development(monkeypatch)
    assert hostruntime.mcp_launch_command() == "modelquad-mcp"


@pytest.mark.parametrize("host_id", ["claude"])
def test_each_adapter_writes_the_resolved_runtime_into_the_host_config(
    monkeypatch, tmp_path, host_id
):
    """End to end, per host: install, then read back what the host will launch.

    Claude Code is the only host (decision H1).
    """

    install_root = force_installed(monkeypatch, tmp_path / "AppData" / "Local")
    shim = install_root / "bin" / "modelquad-mcp.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo off\n", encoding="utf-8")

    from modelquad.adapters.claude_adapter import ClaudeAdapter

    target = tmp_path / "hosts" / ".claude"
    target.mkdir(parents=True)
    ClaudeAdapter().install(target)

    recorded = hostruntime.read_registered_command(host_id, target)
    assert recorded == "modelquad-mcp"
    assert hostruntime.classify_registration(recorded, which=_on_path).canonical is True


def test_installing_twice_is_idempotent_and_writes_no_duplicate_registration(
    monkeypatch, tmp_path
):
    """Install/repair runs more than once in real life -- and does so on repair."""

    install_root = force_installed(monkeypatch, tmp_path / "AppData" / "Local")
    shim = install_root / "bin" / "modelquad-mcp.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo off\n", encoding="utf-8")

    from modelquad.adapters.claude_adapter import ClaudeAdapter

    claude_target = tmp_path / ".claude"
    claude_target.mkdir()

    ClaudeAdapter().install(claude_target)
    first_claude = (tmp_path / ".claude.json").read_text(encoding="utf-8")

    ClaudeAdapter().install(claude_target)

    assert (tmp_path / ".claude.json").read_text(encoding="utf-8") == first_claude
    assert json.loads(first_claude)["mcpServers"].keys() == {"modelquad"}


def test_installing_preserves_unrelated_host_configuration(monkeypatch, tmp_path):
    """Only ModelQuad-owned entries may be modified -- ever."""

    install_root = force_installed(monkeypatch, tmp_path / "AppData" / "Local")
    shim = install_root / "bin" / "modelquad-mcp.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo off\n", encoding="utf-8")

    from modelquad.adapters.claude_adapter import ClaudeAdapter

    target = tmp_path / ".claude"
    target.mkdir()
    config = tmp_path / ".claude.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {"someone-elses-server": {"command": "their-thing"}},
                "unrelatedTopLevelKey": {"keep": "me"},
            }
        ),
        encoding="utf-8",
    )

    ClaudeAdapter().install(target)

    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload["mcpServers"]["someone-elses-server"] == {"command": "their-thing"}
    assert payload["unrelatedTopLevelKey"] == {"keep": "me"}
    assert payload["mcpServers"]["modelquad"]["command"] == "modelquad-mcp"


# --------------------------------------------------------------------------
# 4. Mixed-runtime detection -- would we notice next time?
# --------------------------------------------------------------------------


def test_a_bare_name_is_canonical_exactly_when_it_resolves(monkeypatch, tmp_path):
    """The bare name is the registration; only a name PATH cannot find is a finding."""

    force_installed(monkeypatch, tmp_path / "AppData" / "Local")
    scripts = tmp_path / "Python311" / "Scripts" / "modelquad-mcp.exe"
    verdict = hostruntime.classify_registration("modelquad-mcp", which=lambda _name: str(scripts))
    assert verdict.canonical is True and verdict.resolved == str(scripts)

    missing = hostruntime.classify_registration("modelquad-mcp", which=lambda _name: None)
    assert missing.canonical is False
    assert "not on this PATH" in missing.reason


def test_a_registration_pinned_to_another_installation_is_reported(monkeypatch, tmp_path):
    force_installed(monkeypatch, tmp_path / "AppData" / "Local")
    other = tmp_path / "Some" / "Other" / "modelquad-mcp.cmd"
    verdict = hostruntime.classify_registration(str(other), which=_no_lookup)
    assert verdict.canonical is False
    assert "pinned" in verdict.reason and str(other) in verdict.reason


def wire_foreign_host(tmp_path):
    """The historical split requires a host launching the other runtime.

    Store coexistence alone now describes preserved history. Give incident
    fixtures a registration pinned to another copy, so their existing store
    and Coexistence assertions still describe an active split, not mere files.
    The pin is judged structurally, so the machine's own PATH cannot hide it.
    """
    (tmp_path / ".claude.json").write_text(json.dumps({
        "mcpServers": {"modelquad": {"command": str(tmp_path / "Other" / "modelquad-mcp.exe")}},
    }), encoding="utf-8")


def test_the_live_split_is_detected_as_mixed_state(monkeypatch, tmp_path):
    """The regression test for the actual incident.

    Two populated stores, disagreeing about ``routing_mode`` and
    ``routing_paused`` -- byte-for-byte the condition observed on 2026-08-26 --
    must be reported, not silently served.
    """

    wire_foreign_host(tmp_path)
    local_appdata = tmp_path / "AppData" / "Local"
    force_installed(monkeypatch, local_appdata)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    make_store(
        installed_state_root()[0],
        {"routing_mode": "automatic", "routing_paused": True, "telemetry_consent": False},
    )
    make_store(
        tmp_path / ".modelquad",
        {"routing_mode": "manual", "routing_paused": False, "telemetry_consent": False},
    )

    survey = statediag.survey_state(which=lambda _name: None)
    assert survey.consistent is False
    assert len(survey.populated_stores) == 2
    joined = " ".join(survey.findings)
    assert "More than one ModelQuad database" in joined
    assert "routing_mode" in joined
    assert "routing_paused" in joined
    # telemetry_consent agrees in both stores and must NOT be reported.
    assert "telemetry_consent" not in joined


def test_a_single_store_is_reported_consistent(monkeypatch, tmp_path):
    local_appdata = tmp_path / "AppData" / "Local"
    force_installed(monkeypatch, local_appdata)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    make_store(installed_state_root()[0], {"routing_mode": "automatic"})

    survey = statediag.survey_state(which=lambda _name: None)
    assert survey.consistent is True
    assert survey.findings == ()


def test_a_lazily_unwritten_setting_is_not_a_divergence(monkeypatch, tmp_path):
    """Settings are written on first change, so absence means "default".

    The two live stores held 7 and 3 keys respectively. Judging divergence on
    row counts -- or treating an absent key as a conflict -- would report a
    split on every fresh install, and the check would stop being believed.
    """

    wire_foreign_host(tmp_path)
    local_appdata = tmp_path / "AppData" / "Local"
    force_installed(monkeypatch, local_appdata)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    make_store(installed_state_root()[0], {"routing_mode": "automatic"})
    make_store(tmp_path / ".modelquad", {"decision_mode": "deterministic"})

    survey = statediag.survey_state(which=lambda _name: None)
    # Two stores is still a finding; disagreeing settings is not.
    assert any("More than one ModelQuad database" in f for f in survey.findings)
    assert not any("disagree about settings" in f for f in survey.findings)


def test_surveying_never_creates_or_modifies_a_store(monkeypatch, tmp_path):
    """Diagnosis must not be a write. Nothing here may migrate or initialise."""

    local_appdata = tmp_path / "AppData" / "Local"
    force_installed(monkeypatch, local_appdata)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    database = make_store(installed_state_root()[0], {"routing_mode": "automatic"})
    before_bytes = database.read_bytes()
    dev_root = tmp_path / ".modelquad"

    statediag.survey_state(which=lambda _name: None)

    assert database.read_bytes() == before_bytes
    # The absent store stays absent: inspecting it must not bring it into being.
    assert not (dev_root / "modelquad.db").exists()


def test_doctor_reports_mixed_state_instead_of_passing_green(monkeypatch, tmp_path):
    """``doctor`` returned ok=True on the machine with the live split.

    That is the reason the split survived: the product's own diagnostic could
    not see it. This asserts the check exists and fires.
    """

    wire_foreign_host(tmp_path)
    local_appdata = tmp_path / "AppData" / "Local"
    force_installed(monkeypatch, local_appdata)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    make_store(installed_state_root()[0], {"routing_mode": "automatic"})
    make_store(tmp_path / ".modelquad", {"routing_mode": "manual"})

    from modelquad.doctor import CheckStatus, _state_consistency_check

    check = _state_consistency_check()
    assert check.status is CheckStatus.WARN
    assert "More than one ModelQuad database" in check.detail


def test_doctor_state_check_is_ok_for_a_single_store(monkeypatch, tmp_path):
    local_appdata = tmp_path / "AppData" / "Local"
    force_installed(monkeypatch, local_appdata)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    make_store(installed_state_root()[0], {"routing_mode": "automatic"})

    from modelquad.doctor import CheckStatus, _state_consistency_check

    check = _state_consistency_check()
    assert check.status is CheckStatus.OK
    assert "installed" in check.detail


def test_a_non_canonical_registration_is_routed_to_repair(monkeypatch, tmp_path):
    """A finding nobody can act on is only half a diagnostic.

    ``repair`` is doctor-driven: it repairs hosts whose checks WARN. The new
    check has to be in the set it recognises, or the Repair button would be
    offered for a problem it does not fix.
    """

    from modelquad.repair import _map_check_to_host

    assert _map_check_to_host("claude MCP runtime") == "claude"
    assert _map_check_to_host("codex MCP runtime") == "codex"
    assert _map_check_to_host("state consistency") is None


def test_doctor_mcp_runtime_check_flags_the_live_registration(monkeypatch, tmp_path):
    install_root = force_installed(monkeypatch, tmp_path / "AppData" / "Local")
    shim = install_root / "bin" / "modelquad-mcp.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("@echo off\n", encoding="utf-8")

    target = tmp_path / ".claude"
    target.mkdir()
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"modelquad": {"command": "modelquad-mcp", "args": []}}}),
        encoding="utf-8",
    )

    from modelquad.doctor import CheckStatus, _mcp_runtime_check

    check = _mcp_runtime_check("claude", target, which=lambda _name: None)
    assert check is not None
    assert check.status is CheckStatus.WARN
    assert "not on this PATH" in check.detail
    assert "repair" in check.detail


def test_doctor_mcp_runtime_check_is_silent_when_there_is_no_registration(tmp_path):
    """Absence is the neighbouring check's finding; do not report it twice."""

    from modelquad.doctor import _mcp_runtime_check

    target = tmp_path / ".claude"
    target.mkdir()
    assert _mcp_runtime_check("claude", target, which=lambda _name: None) is None


# --------------------------------------------------------------------------
# 5. Settings round trips -- one store, seen by every surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setter", "getter", "value"),
    [
        ("put_routing_mode", "get_routing_mode", "automatic"),
        ("put_routing_paused", "get_routing_paused", True),
        # decision_mode no longer round-trips a planner mode (every stored value
        # now reads as deterministic); the shared user switch is the plan one.
        ("put_plan_advice_enabled", "get_plan_advice_enabled", True),
    ],
)
def test_a_setting_written_by_one_surface_is_read_by_every_other(
    monkeypatch, tmp_path, setter, getter, value
):
    """The invariant, stated as a test.

    The surfaces do not share an object -- they share a resolved path. So the
    round trip is asserted through the resolver every surface uses, with the
    module-level ``DEFAULT_DB_PATH`` constant deliberately NOT patched: if a
    surface ever stops resolving through ``paths``, this fails.
    """

    from modelquad.service import history as service_history

    force_installed(monkeypatch, tmp_path / "AppData" / "Local")
    monkeypatch.setattr(service_db, "DEFAULT_DB_PATH", paths_module.db_path())

    # Written the way the HTTP API writes it...
    getattr(service_history, setter)(paths_module.db_path(), value)

    # ...and read the way the MCP server reads it: through the module constant
    # every MCP tool uses, which must resolve to the same file.
    assert getattr(service_history, getter)(service_db.DEFAULT_DB_PATH) == value
    assert service_db.DEFAULT_DB_PATH == resolve_state_context().database_path


# --------------------------------------------------------------------------
# 6. Pre-existing layouts -- every supported case has a stated behaviour
# --------------------------------------------------------------------------


def _survey_with(monkeypatch, tmp_path, *, installed=None, development=None):
    local_appdata = tmp_path / "AppData" / "Local"
    force_installed(monkeypatch, local_appdata)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    if installed is not None:
        make_store(installed_state_root()[0], installed)
    if development is not None:
        make_store(tmp_path / ".modelquad", development)
    if installed is not None and development is not None:
        wire_foreign_host(tmp_path)
    return statediag.survey_state(which=lambda _name: None)


def test_no_store_yet_is_not_a_finding(monkeypatch, tmp_path):
    survey = _survey_with(monkeypatch, tmp_path)
    assert survey.coexistence is statediag.Coexistence.NONE
    assert survey.consistent is True


def test_only_the_owned_store_is_the_healthy_case(monkeypatch, tmp_path):
    survey = _survey_with(monkeypatch, tmp_path, installed={"routing_mode": "automatic"})
    assert survey.coexistence is statediag.Coexistence.SINGLE
    assert survey.consistent is True


def test_only_a_foreign_store_is_reported_and_never_adopted(monkeypatch, tmp_path):
    """A runtime silently adopting a store it does not own is the bug, inverted.

    An installed product finding only a dev store must not start serving it --
    that is how one product becomes another. It reports, and leaves the store
    exactly as it found it.
    """

    survey = _survey_with(monkeypatch, tmp_path, development={"routing_mode": "manual"})
    assert survey.coexistence is statediag.Coexistence.FOREIGN_ONLY
    # One store is not a split, so it is not a finding -- but the survey still
    # says which store this runtime owns, and it is not that one.
    assert survey.context.state_root == installed_state_root()[0]
    assert (tmp_path / ".modelquad" / "modelquad.db").is_file()


def test_two_agreeing_stores_are_still_reported(monkeypatch, tmp_path):
    """Agreement today is not one product.

    History, installation identity and future writes are still split; settings
    matching is a coincidence of the moment, not a merge.
    """

    survey = _survey_with(
        monkeypatch,
        tmp_path,
        installed={"routing_mode": "automatic"},
        development={"routing_mode": "automatic"},
    )
    assert survey.coexistence is statediag.Coexistence.EQUIVALENT
    assert survey.consistent is False
    assert not any("disagree about settings" in f for f in survey.findings)


def test_two_conflicting_stores_are_classified_as_conflict(monkeypatch, tmp_path):
    survey = _survey_with(
        monkeypatch,
        tmp_path,
        installed={"routing_mode": "automatic", "routing_paused": True},
        development={"routing_mode": "manual", "routing_paused": False},
    )
    assert survey.coexistence is statediag.Coexistence.CONFLICT
    assert any("disagree about settings" in f for f in survey.findings)


@pytest.mark.parametrize("foreign", [False, True])
def test_preserved_stores_and_active_control_plane(monkeypatch, tmp_path, capsys, foreign):
    """Preserved history must go green without deletion; a foreign host must still expose the live split."""
    from modelquad.cli import render_state_survey
    from modelquad.doctor import CheckStatus, _state_consistency_check

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = force_installed(monkeypatch, tmp_path / "Local")
    databases = (
        make_store(root, {"routing_mode": "automatic"}),
        make_store(tmp_path / ".modelquad", {"routing_mode": "manual"}),
    )
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in databases}
    shim = root / "bin" / "modelquad-mcp.cmd"
    shim.parent.mkdir(parents=True)
    shim.touch()
    command = str(tmp_path / "foreign" / "modelquad-mcp.cmd") if foreign else "modelquad-mcp"
    (tmp_path / ".claude.json").write_text(json.dumps({
        "mcpServers": {"modelquad": {"command": command}},
    }), encoding="utf-8")
    survey = statediag.survey_state(which=_on_path)
    assert len(survey.hosts) == 1
    assert survey.active_split_brain is foreign
    assert survey.consistent is (not foreign)
    assert survey.coexistence is statediag.Coexistence.CONFLICT
    assert survey.findings == survey.store_findings + survey.host_findings
    assert bool(survey.store_findings) is foreign
    assert bool(survey.host_findings) is foreign
    assert "routing_mode" in " ".join(survey.findings if foreign else survey.notes)
    if not foreign:
        assert survey.findings == ()
        assert "neither migrates nor deletes" in " ".join(survey.notes)
    check = _state_consistency_check()
    assert check.status is (CheckStatus.WARN if foreign else CheckStatus.OK)
    assert "routing_mode" in check.detail
    render_state_survey(survey)
    output = capsys.readouterr().out
    assert f"State consistency: {'MIXED' if foreign else 'OK'}" in output
    assert "Surface" in output and "mcp" in output
    if foreign:
        assert "Run `modelquad repair`" in output
    else:
        assert "Preserved state notes:" in output
        assert "routing_mode" in output
        assert "not the active host control plane" in output
        assert "Run `modelquad repair`" not in output
    payload = survey.to_dict()
    assert payload["active_split_brain"] is foreign
    assert payload["notes"] == list(survey.notes)
    assert payload["store_findings"] == list(survey.store_findings)
    assert payload["host_findings"] == list(survey.host_findings)
    # Diagnosis, doctor and rendering must leave BOTH databases present and
    # byte-identical, including the legacy store which makes the report green.
    assert all(path.is_file() for path in databases)
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in databases} == before


# --------------------------------------------------------------------------
# 5. A service already running against the other store
# --------------------------------------------------------------------------
#
# An earlier change made a preserved store go green when no managed host launches it. That
# reading is structural and cannot see a service that is *already running*
# against the other store -- a development service from a checkout serving
# port 8765 while every host config is pinned to the installed launcher. The
# only honest signal is process liveness with identity: the pidfile's pid is
# alive AND its image is the executable the pidfile recorded. A pidfile alone
# is a file, not a process (a crashed service leaves one behind), and a time
# threshold would be a number the product cannot justify.

import os
import subprocess
import sys

from conftest import no_window_kwargs


def _two_stores_hosts_pinned(monkeypatch, tmp_path):
    """The two-store layout: installed store owned, dev store preserved, hosts canonical."""

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = force_installed(monkeypatch, tmp_path / "Local")
    databases = (
        make_store(root, {"routing_mode": "automatic"}),
        make_store(tmp_path / ".modelquad", {"routing_mode": "manual"}),
    )
    shim = root / "bin" / "modelquad-mcp.cmd"
    shim.parent.mkdir(parents=True)
    shim.touch()
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"modelquad": {"command": "modelquad-mcp"}}}), encoding="utf-8"
    )
    return root, tmp_path / ".modelquad", databases


def _write_pidfile(store_root: Path, pid: int, argv: list[str], *, started_at: str | None = None) -> None:
    """A pidfile the way lifecycle writes one: started_at is taken after the spawn."""

    from datetime import datetime, timezone

    (store_root / "service.pid").write_text(
        json.dumps(
            {"argv": argv, "pid": pid, "started_at": started_at or datetime.now(timezone.utc).isoformat()},
            indent=2,
        ),
        encoding="utf-8",
    )


def _snapshot(databases) -> dict:
    return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in databases}


def _no_lookup(_name):
    pytest.fail("An explicit registration must not consult the machine PATH")


def _on_path(name):
    """A PATH on which pip installed both console commands."""
    return f"C:/Python311/Scripts/{name}.exe"


def test_a_live_identity_matched_service_on_the_other_store_is_a_finding(monkeypatch, tmp_path, capsys):
    from modelquad.cli import render_state_survey
    from modelquad.doctor import CheckStatus, _state_consistency_check

    root, foreign_root, databases = _two_stores_hosts_pinned(monkeypatch, tmp_path)
    before = _snapshot(databases)
    # This very process is alive and its image is sys.executable's basename.
    _write_pidfile(foreign_root, os.getpid(), [sys.executable, "-m", "modelquad", "service", "run"])

    survey = statediag.survey_state(which=_on_path)

    foreign = next(store for store in survey.stores if store.root == foreign_root)
    assert foreign.service_pid == os.getpid()
    assert foreign.service_process == "alive"
    assert foreign.service_image_verified is True
    assert foreign.service_live is True
    assert survey.live_foreign_stores == (foreign,)
    assert survey.active_split_brain is True
    assert survey.consistent is False
    assert survey.host_findings == ()  # the hosts are fine; the process is the split
    text = " ".join(survey.store_findings)
    assert f"pid {os.getpid()}" in text
    assert str(foreign_root) in text
    assert f"started {foreign.service_started_at}" in text
    assert "Stop it from the ModelQuad that started it" in text
    assert "kill" not in text.lower()
    assert "repair" not in text
    assert "More than one ModelQuad database exists" in text
    assert "routing_mode" in text
    assert survey.notes == ()

    check = _state_consistency_check()
    assert check.status is CheckStatus.WARN
    assert f"pid {os.getpid()}" in check.detail

    render_state_survey(survey)
    output = capsys.readouterr().out
    assert "State consistency: MIXED" in output
    assert "Stop it from the ModelQuad that started it" in output
    assert "Run `modelquad repair`" not in output

    payload = survey.to_dict()
    store_payload = next(s for s in payload["stores"] if s["root"] == str(foreign_root))
    assert store_payload["service_pid"] == os.getpid()
    assert store_payload["service_started_at"] == foreign.service_started_at
    assert store_payload["service_process"] == "alive"
    assert store_payload["service_image_verified"] is True
    assert store_payload["service_live"] is True
    assert payload["live_foreign_stores"] == [str(foreign_root)]
    assert payload["active_split_brain"] is True
    # Read-only, still: both databases untouched, the pidfile untouched.
    assert _snapshot(databases) == before
    assert (foreign_root / "service.pid").is_file()


def test_a_dead_pid_in_the_other_stores_pidfile_keeps_the_preserved_state_green(monkeypatch, tmp_path, capsys):
    """The preserved-store case: a crashed dev service left its pidfile; nothing runs."""

    from modelquad.cli import render_state_survey
    from modelquad.doctor import CheckStatus, _state_consistency_check

    root, foreign_root, databases = _two_stores_hosts_pinned(monkeypatch, tmp_path)
    before = _snapshot(databases)
    # A pid proven dead: our own child, exited, its handle still held by the
    # Popen object so the OS cannot reuse the number while the test runs.
    child = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL,
                             **no_window_kwargs())
    child.wait(timeout=60)
    try:
        _write_pidfile(foreign_root, child.pid, [sys.executable, "-m", "modelquad", "service", "run"])

        survey = statediag.survey_state(which=_on_path)

        foreign = next(store for store in survey.stores if store.root == foreign_root)
        assert foreign.service_pid == child.pid
        assert foreign.service_process == "dead"
        assert foreign.service_image_verified is None
        assert foreign.service_live is False
        assert survey.live_foreign_stores == ()
        assert survey.active_split_brain is False
        assert survey.consistent is True
        assert survey.findings == ()
        assert "neither migrates nor deletes" in " ".join(survey.notes)
        assert "not established" not in " ".join(survey.notes)
        check = _state_consistency_check()
        assert check.status is CheckStatus.OK
        render_state_survey(survey)
        output = capsys.readouterr().out
        assert "State consistency: OK" in output
        assert "Preserved state notes:" in output
        assert _snapshot(databases) == before
        assert (foreign_root / "service.pid").is_file()
    finally:
        del child


@pytest.mark.parametrize(
    ("probe", "verified", "expected_words"),
    [
        ("unknown", None, "could not be probed"),
        ("alive", False, "not the recorded service"),
        ("alive", None, "process image could not be read"),
        ("alive", "creation-unknown", "creation time or the record's start time could not be read"),
    ],
)
def test_an_unestablished_service_pid_is_a_note_that_says_so_never_ok_by_omission(
    monkeypatch, tmp_path, capsys, probe, verified, expected_words
):
    """Neither a finding nor silence: the product says what it could not establish."""

    from modelquad.cli import render_state_survey
    from modelquad.doctor import CheckStatus, _state_consistency_check

    root, foreign_root, databases = _two_stores_hosts_pinned(monkeypatch, tmp_path)
    before = _snapshot(databases)
    _write_pidfile(foreign_root, os.getpid(), ["C:\\other\\modelquad.exe", "service", "run"])
    monkeypatch.setattr(statediag, "pid_state", lambda pid: probe)
    if verified is None:
        monkeypatch.setattr(statediag, "process_image_name", lambda pid: None)
    elif verified == "creation-unknown":
        # The image matches but the creation time cannot be read: not established.
        monkeypatch.setattr(statediag, "process_image_name", lambda pid: "modelquad.exe")
        monkeypatch.setattr(statediag, "process_creation_epoch", lambda pid: None)
        verified = None
    else:
        monkeypatch.setattr(statediag, "process_image_name", lambda pid: "python.exe")

    survey = statediag.survey_state(which=_on_path)

    foreign = next(store for store in survey.stores if store.root == foreign_root)
    assert foreign.service_process == probe
    assert foreign.service_image_verified is verified
    assert foreign.service_live is False
    assert survey.active_split_brain is False
    assert survey.consistent is True
    notes = " ".join(survey.notes)
    assert f"records service pid {os.getpid()}" in notes
    assert expected_words in notes
    assert "neither migrates nor deletes" in notes
    check = _state_consistency_check()
    assert check.status is CheckStatus.OK
    assert expected_words in check.detail
    render_state_survey(survey)
    output = capsys.readouterr().out
    assert "State consistency: OK" in output
    assert expected_words in output
    assert _snapshot(databases) == before


def test_a_live_service_on_the_owned_store_is_not_a_split(monkeypatch, tmp_path):
    """Our own service running against our own store is the healthy case."""

    root, foreign_root, databases = _two_stores_hosts_pinned(monkeypatch, tmp_path)
    _write_pidfile(root, os.getpid(), [sys.executable, "-m", "modelquad", "service", "run"])

    survey = statediag.survey_state(which=_on_path)

    owned = next(store for store in survey.stores if store.root == root)
    assert owned.service_live is True
    assert survey.live_foreign_stores == ()
    assert survey.active_split_brain is False
    assert survey.consistent is True


def test_service_liveness_never_consults_time(monkeypatch, tmp_path):
    """No threshold: the decision reads the process, not the clock or the rows."""

    import inspect

    source = inspect.getsource(statediag._service_process_facts) + inspect.getsource(statediag.survey_state)
    for forbidden in ("last_activity", "st_mtime", "time.time", "datetime.now", "timedelta"):
        assert forbidden not in source, forbidden


def test_a_pidfile_without_a_pid_records_nothing(monkeypatch, tmp_path):
    root, foreign_root, databases = _two_stores_hosts_pinned(monkeypatch, tmp_path)
    (foreign_root / "service.pid").write_text(json.dumps({"argv": ["x"], "pid": True}), encoding="utf-8")

    survey = statediag.survey_state(which=_on_path)

    foreign = next(store for store in survey.stores if store.root == foreign_root)
    assert (foreign.service_pid, foreign.service_process, foreign.service_image_verified) == (None, None, None)
    assert survey.consistent is True
    assert "not established" not in " ".join(survey.notes)


def test_a_recycled_pid_with_the_same_image_is_not_the_recorded_service(monkeypatch, tmp_path):
    """Every development service is a Python executable, so the image alone cannot
    tell a recycled pid from the recorded one. A process created after the
    pidfile was written cannot be the process it names."""

    from datetime import datetime, timedelta, timezone

    root, foreign_root, databases = _two_stores_hosts_pinned(monkeypatch, tmp_path)
    # A record written an hour ago; this process (same image) was created later.
    earlier = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _write_pidfile(foreign_root, os.getpid(), [sys.executable, "-m", "modelquad", "service", "run"], started_at=earlier)

    survey = statediag.survey_state(which=_on_path)

    foreign = next(store for store in survey.stores if store.root == foreign_root)
    assert foreign.service_process == "alive"
    assert foreign.service_image_verified is False
    assert foreign.service_live is False
    assert survey.active_split_brain is False
    assert survey.consistent is True
    notes = " ".join(survey.notes)
    assert "not the recorded service" in notes
    assert "created after that record" in notes


def test_an_unparseable_started_at_leaves_identity_unestablished(monkeypatch, tmp_path):
    root, foreign_root, databases = _two_stores_hosts_pinned(monkeypatch, tmp_path)
    _write_pidfile(foreign_root, os.getpid(), [sys.executable, "-m", "modelquad"], started_at="yesterday-ish")

    survey = statediag.survey_state(which=_on_path)

    foreign = next(store for store in survey.stores if store.root == foreign_root)
    assert foreign.service_process == "alive"
    assert foreign.service_image_verified is None
    assert survey.consistent is True
    assert "not established" in " ".join(survey.notes)


def test_an_alias_of_the_owned_root_is_never_foreign(monkeypatch, tmp_path):
    """`..`, case and junctions reach the owned store all the same."""

    root, foreign_root, databases = _two_stores_hosts_pinned(monkeypatch, tmp_path)
    _write_pidfile(root, os.getpid(), [sys.executable, "-m", "modelquad", "service", "run"])
    (root.parent / "detour").mkdir()
    spellings = [root.parent / "detour" / ".." / root.name]
    if os.name == "nt":
        # Case is only an alias where the filesystem ignores it.
        spellings.append(Path(str(root).upper()))
    real_roots = statediag.known_state_roots()
    monkeypatch.setattr(
        statediag,
        "known_state_roots",
        lambda: real_roots + [(spelling, statediag.StateMode.INSTALLED) for spelling in spellings],
    )

    survey = statediag.survey_state(which=_on_path)

    # Every spelling of the one directory has the owned service live on it.
    live = [store for store in survey.stores if store.service_live]
    assert len(live) == 1 + len(spellings)
    assert {str(store.root) for store in live} == {str(root), *(str(s) for s in spellings)}
    assert survey.live_foreign_stores == ()
    assert survey.active_split_brain is False
    assert survey.consistent is True


def test_creation_slack_is_measurement_tolerance_per_platform(monkeypatch):
    """One second against Windows's absolute instant; two where the reading is
    elapsed time at one-second resolution. Never an age."""

    monkeypatch.setattr(statediag.os, "name", "nt")
    assert statediag._creation_slack_seconds() == 1.0
    monkeypatch.setattr(statediag.os, "name", "posix")
    assert statediag._creation_slack_seconds() == 2.0
