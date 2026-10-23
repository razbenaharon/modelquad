# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import subprocess

import pytest

from modelquad.catalog import (
    AvailabilityState,
    ModelCatalog,
    ModelDescriptor,
    discover_claude_models,
    discover_codex_models,
)
from modelquad.config import Config, load_config
from modelquad.registry import ProviderPluginContext


def test_model_descriptor_rejects_invalid_availability():
    with pytest.raises(ValueError):
        ModelDescriptor(provider="codex", model_id="x", display_name="X", availability="made_up")


def test_model_descriptor_rejects_out_of_range_relative_strength():
    with pytest.raises(ValueError):
        ModelDescriptor(provider="codex", model_id="x", display_name="X", relative_strength=1.5)


def test_model_catalog_for_provider_filters_correctly():
    catalog = ModelCatalog(
        descriptors=(
            ModelDescriptor(provider="codex", model_id="a", display_name="A"),
            ModelDescriptor(provider="claude", model_id="b", display_name="B"),
        )
    )
    assert [d.model_id for d in catalog.for_provider("codex")] == ["a"]
    assert [d.model_id for d in catalog.for_provider("zeta")] == []


def test_discover_codex_models_reads_local_config_toml(tmp_path):
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        'model = "gpt-5.6-terra"\nmodel_reasoning_effort = "high"\n\n[projects.foo]\ntrust_level = "trusted"\n',
        encoding="utf-8",
    )
    config = Config()
    ctx = ProviderPluginContext(config=config, provider_config=None)
    descriptors = discover_codex_models(ctx, codex_home=codex_home)
    assert len(descriptors) == 1
    assert descriptors[0].model_id == "gpt-5.6-terra"
    assert descriptors[0].availability == AvailabilityState.CONFIGURED
    assert descriptors[0].source == str(codex_home / "config.toml")


def test_discover_codex_models_falls_back_to_static_catalog_when_no_local_config(tmp_path):
    config = Config(model_catalog_static={"codex": ({"model_id": "curated-x", "display_name": "Curated X"},)})
    ctx = ProviderPluginContext(config=config, provider_config=None)
    descriptors = discover_codex_models(ctx, codex_home=tmp_path / "does-not-exist")
    assert len(descriptors) == 1
    assert descriptors[0].model_id == "curated-x"
    assert descriptors[0].availability == AvailabilityState.CONFIGURED


def test_discover_codex_models_honestly_empty_when_nothing_available(tmp_path):
    config = Config()
    ctx = ProviderPluginContext(config=config, provider_config=None)
    descriptors = discover_codex_models(ctx, codex_home=tmp_path / "does-not-exist")
    assert descriptors == ()


def test_discover_claude_models_uses_static_catalog():
    config = load_config()
    ctx = ProviderPluginContext(config=config, provider_config=None)
    descriptors = discover_claude_models(ctx)
    assert len(descriptors) >= 1
    assert all(d.provider == "claude" for d in descriptors)
    assert all(d.availability == AvailabilityState.CONFIGURED for d in descriptors)


def test_model_discovery_failure_does_not_affect_provider_availability(tmp_path, *, synthetic_worker_binary):
    """Discovery is independent of provider execution readiness (point 21)."""

    from modelquad.execution.codex import CodexExecutor

    executor = CodexExecutor(which=lambda _: synthetic_worker_binary('codex.exe'))
    assert executor.is_available() is True
    # Simulate a discovery failure (no local config, no static catalog) --
    # execution readiness above is computed by a completely separate path.
    config = Config()
    ctx = ProviderPluginContext(config=config, provider_config=None)
    assert discover_codex_models(ctx, codex_home=tmp_path / "missing") == ()
    assert executor.is_available() is True


# --- the read-only catalog routing uses --------------


def test_cached_catalog_reads_configured_entries_without_spawning(tmp_path, monkeypatch):
    from modelquad import catalog as catalog_module

    def _forbidden(*args, **kwargs):  # pragma: no cover - only on regression
        raise AssertionError("cached_catalog spawned a process")

    monkeypatch.setattr(subprocess, "run", _forbidden)

    config = Config(
        model_catalog_static={
            "codex": (
                {
                    "model_id": "configured-test-model",
                    "display_name": "Configured test model",
                    "supported_reasoning_levels": ["low", "medium", "high"],
                },
            )
        }
    )

    catalog = catalog_module.cached_catalog(config, codex_home=tmp_path / "absent")

    assert [d.model_id for d in catalog.for_provider("codex")] == ["configured-test-model"]


def test_cached_catalog_prefers_the_users_local_codex_model_over_the_static_list(tmp_path):
    from modelquad import catalog as catalog_module

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5.5-codex"\n', encoding="utf-8")

    config = Config(
        model_catalog_static={"codex": ({"model_id": "packaged-prior"},)}
    )

    catalog = catalog_module.cached_catalog(config, codex_home=codex_home)

    assert [d.model_id for d in catalog.for_provider("codex")] == ["gpt-5.5-codex"]


# --- a bad catalog source or entry may never reach routing ------------


def test_f10_undecodable_local_codex_config_is_skipped_not_raised(tmp_path):
    """A `config.toml` that is not UTF-8 raises `UnicodeDecodeError` on read.

    Routing now calls `cached_catalog()` on every consultation, so an
    unreadable local file used to be an exception out of ROUTING rather than
    one unusable source.
    """
    from modelquad import catalog as catalog_module

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_bytes(b'model = "\xff\xfe not utf-8"\n')

    config = Config(
        model_catalog_static={
            "codex": ({"model_id": "packaged-prior", "supported_reasoning_levels": ["high"]},)
        }
    )

    catalog = catalog_module.cached_catalog(config, codex_home=codex_home)

    # The bad source contributes nothing; the good one still does.
    assert [d.model_id for d in catalog.for_provider("codex")] == ["packaged-prior"]


def test_f10_a_missing_local_codex_config_is_still_simply_absent(tmp_path):
    from modelquad import catalog as catalog_module

    config = Config(model_catalog_static={"codex": ({"model_id": "packaged-prior"},)})

    catalog = catalog_module.cached_catalog(config, codex_home=tmp_path / "nowhere")

    assert [d.model_id for d in catalog.for_provider("codex")] == ["packaged-prior"]


def test_f10_malformed_entries_are_isolated_and_the_good_ones_survive(tmp_path):
    """`supported_reasoning_levels: null` and a bare string are authoring slips.

    `tuple(None)` is a `TypeError`; `tuple("high")` is four nonsense levels.
    Neither may cost the catalog, and neither may be silently mangled.
    """
    from modelquad import catalog as catalog_module

    config = Config(
        model_catalog_static={
            "codex": (
                {"model_id": "null-levels", "supported_reasoning_levels": None},
                {"model_id": "string-levels", "supported_reasoning_levels": "high"},
                "not-even-a-mapping",
                {"display_name": "no model id at all"},
                {
                    "model_id": "configured-test-model",
                    "supported_reasoning_levels": ["low", "medium", "high"],
                },
            )
        }
    )

    catalog = catalog_module.cached_catalog(config, codex_home=tmp_path / "absent")
    by_id = {d.model_id: d for d in catalog.for_provider("codex")}

    assert by_id["configured-test-model"].supported_reasoning_levels == ("low", "medium", "high")
    # No invented vocabulary: not `('h', 'i', 'g', 'h')`, and not an exception.
    assert by_id["null-levels"].supported_reasoning_levels == ()
    assert by_id["string-levels"].supported_reasoning_levels == ()


def test_f10_an_unusable_static_section_leaves_routing_running(tmp_path, monkeypatch):
    """Even a catalog that cannot be enumerated at all is a missing catalog.

    The truthful fallback is the one routing already has: no model claim, the
    runtime's own default -- never an exception out of `Router.route`.
    """
    from modelquad import catalog as catalog_module
    from modelquad.models import Freshness, ProviderStatus
    from modelquad.router import Router

    class Hostile:
        model_catalog_static = {"claude": ({"model_id": "ok"},)}

        def __getattr__(self, name):  # pragma: no cover - defensive
            raise AttributeError(name)

    def _explode(*args, **kwargs):
        raise RuntimeError("this source is broken")

    monkeypatch.setattr(catalog_module, "_static_from_config", _explode)

    catalog = catalog_module.cached_catalog(Hostile(), codex_home=tmp_path / "absent")

    assert catalog.descriptors == ()

    statuses = [
        ProviderStatus(name="codex", available=True, remaining_percentage=90, freshness=Freshness.FRESH),
    ]
    result = Router(Config()).route("review", statuses, catalog=catalog)

    assert result.recommendation == "codex"
