# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""One local, scalar-only reader for Codex model and reasoning effort."""

import os
from pathlib import Path
import tomllib

from .execution.closed import CODEX_REASONING_EFFORTS, safe_model_id  # noqa: F401


def config_path(codex_home=None):
    home = codex_home if codex_home is not None else os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "config.toml"


#: Codex's built-in provider. A worker runs with --ignore-user-config, which
#: drops any other provider (and the profile that names it), so a model chosen
#: for another provider must not be passed to the default one.
DEFAULT_MODEL_PROVIDER = "openai"


def _load(path=None):
    """The parsed config.toml; {} when there is none, None when unreadable."""
    target = Path(path) if path is not None else config_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        return tomllib.loads(text)
    except ValueError:
        return None


def _effective(data):
    """Top-level settings with the active profile's model choices applied."""
    profile = data.get('profile')
    profiles = data.get('profiles')
    active = profiles.get(profile) if isinstance(profile, str) and isinstance(profiles, dict) else None
    if isinstance(active, dict):
        data = {**data, **{key: active[key] for key in ('model', 'model_reasoning_effort', 'model_provider')
                           if key in active}}
    return data


def read_config(path=None, *, for_execution=True):
    """Only model/effort scalars escape; execution honors the active profile.

    For execution, a model configured for a provider other than Codex's
    default is not returned: the worker ignores user configuration, so that
    provider is not loaded and Codex picks its own default model instead.
    """
    from .hostobs import _safe_model_id, _safe_reasoning_mode
    data = _load(path)
    if not data:
        return None, None
    if for_execution:
        data = _effective(data)
    model = (safe_model_id if for_execution else _safe_model_id)(data.get("model"))
    if for_execution and data.get("model_provider", DEFAULT_MODEL_PROVIDER) != DEFAULT_MODEL_PROVIDER:
        model = None
    effort = _safe_reasoning_mode(data.get("model_reasoning_effort"))
    return model, effort if not for_execution or effort in CODEX_REASONING_EFFORTS else None


def review_settings(config):
    """Model and effort for a second opinion; no model means Codex's default.

    Refused only when config.toml cannot be read or names a model that is not
    a usable model id. A config without a model, or whose model belongs to
    another provider, leaves the choice to Codex (no ``-m``).
    """
    from .routing_policy import LaunchRefused
    data = _load()
    model, effort = read_config()
    if data is None or (model is None and "model" in _effective(data)
                        and _effective(data).get("model_provider", DEFAULT_MODEL_PROVIDER) == DEFAULT_MODEL_PROVIDER):
        raise LaunchRefused("codex_model_unreadable")
    source = "user_config" if effort is not None else "default_policy"
    if effort is None:
        effort = config.model_policy.get("codex", {}).get("review", {}).get("reasoning_effort")
    if effort not in CODEX_REASONING_EFFORTS:
        raise LaunchRefused("invalid_reasoning_effort")
    sources = {"reasoning_effort_source": source}
    if model is not None:
        sources["model_source"] = "user_config"
    return model, effort, sources
