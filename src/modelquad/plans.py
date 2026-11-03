# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Which plan the user is on with each provider, and what kind of account that is.

A plan is known in one of two ways, or not at all:

* **Reported by Codex.** The app-server quota read (``account/rateLimits/read``)
  carries a ``planType``; ``usage/codex_appserver.py`` already stores it in
  ``usage-codex.json`` as ``plan_type``. Nothing else is asked to learn a plan:
  no credential or token file is opened and no other app-server method is
  called (invariant 5).
* **You set it.** ``modelquad settings plan claude|codex <value>`` stores the
  value in ``preferences.json`` under ``plans``. The values are a closed list
  (below); anything else is refused.
* Otherwise the plan is **unknown** (invariant 4).

What a plan is used for: showing it to the person (CLI), and the account-kind
notice -- personal, workspace or unknown -- on the calls that send code to
Codex. When Codex reports a plan, that is the one the notice uses. The model
only ever gets the kind word, never the plan name. A plan is never an input
to choosing a worker, to a threshold or to a capacity figure, and it never
starts or stops work (``tests/test_declared_plans.py`` checks the worker
choice takes no plan).

Imports stay light: the MCP server reads this on every Codex call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, NamedTuple

#: The values ``modelquad settings plan`` accepts, by provider. Claude's
#: individual plans (Free, Pro, Max, Ultra) and Team, Enterprise and Education
#: were checked on claude.com/pricing on 2026-11-03; Codex's are the ChatGPT
#: plans. ``api-key`` and ``other`` say "not one of these" without guessing.
DECLARABLE_PLANS: dict[str, tuple[str, ...]] = {
    "claude": ("free", "pro", "max", "ultra", "team", "enterprise", "education", "api-key", "other"),
    "codex": ("free", "go", "plus", "pro", "team", "business", "enterprise", "edu", "api-key", "other"),
}
PROVIDERS = tuple(DECLARABLE_PLANS)

PERSONAL, WORKSPACE, UNKNOWN = "personal", "workspace", "unknown"
#: Declared plan -> account kind. ``api-key`` and ``other`` are unknown.
_DECLARED_KIND = {
    "free": PERSONAL, "go": PERSONAL, "plus": PERSONAL, "pro": PERSONAL,
    "max": PERSONAL, "ultra": PERSONAL,
    "team": WORKSPACE, "business": WORKSPACE, "enterprise": WORKSPACE,
    "edu": WORKSPACE, "education": WORKSPACE,
}
#: Codex ``planType`` values as the app-server reports them, by account kind.
_OBSERVED_PERSONAL = frozenset({"free", "go", "plus", "pro", "prolite"})
_OBSERVED_WORKSPACE = frozenset({"team", "business", "enterprise", "edu", "education", "k12"})

SOURCE_OBSERVED = "reported by Codex"
SOURCE_DECLARED = "you set"
SOURCE_UNKNOWN = "unknown"

_PLANS_KEY = "plans"
_MAX_USAGE_FILE_BYTES = 1024 * 1024
_SHOWABLE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class PlanView(NamedTuple):
    """One side's plan as ModelQuad knows it.

    ``kind`` and ``source`` may reach the model; ``plan``, ``declared`` and
    ``observed`` are for human-only surfaces.
    """

    kind: str
    source: str
    plan: str | None = None
    declared: str | None = None
    observed: str | None = None

    def public(self) -> dict[str, str]:
        """What a model-facing reply may carry: the kind word and its source."""
        return {"kind": self.kind, "source": self.source}


def declared_kind(plan: str | None) -> str:
    return _DECLARED_KIND.get(plan or "", UNKNOWN)


def observed_codex_kind(plan_type: Any) -> str:
    """The account kind of a ``planType`` Codex reported; unknown when unrecognised."""
    if type(plan_type) is not str:
        return UNKNOWN
    plan = plan_type.strip().casefold()
    if plan in _OBSERVED_PERSONAL:
        return PERSONAL
    if plan in _OBSERVED_WORKSPACE or any(word in plan for word in ("enterprise", "business", "team", "edu")):
        return WORKSPACE
    return UNKNOWN


def normalise(provider: str, value: str) -> str:
    """The stored form of a declared plan, or ValueError naming the allowed values."""
    if provider not in DECLARABLE_PLANS:
        raise ValueError(f"Unknown provider {provider!r}. Use one of: {', '.join(PROVIDERS)}.")
    plan = value.strip().casefold()
    allowed = DECLARABLE_PLANS[provider]
    if plan not in allowed:
        raise ValueError(
            f"{value!r} is not a {provider.capitalize()} plan ModelQuad knows. "
            f"Use one of: {', '.join(allowed)}, or unset."
        )
    return plan


def _preferences_path(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    from .adapters.server_instructions import preferences_path

    return preferences_path()


def read_declared(path: Path | None = None) -> dict[str, str | None]:
    """The plans the user set, by provider; None where unset or not a listed value."""
    from .adapters.server_instructions import _read_preferences

    try:
        stored = _read_preferences(_preferences_path(path)).get(_PLANS_KEY)
    except Exception:  # noqa: BLE001 -- an unreadable state folder means nothing was set
        stored = None
    stored = stored if isinstance(stored, dict) else {}
    declared: dict[str, str | None] = {}
    for provider, allowed in DECLARABLE_PLANS.items():
        value = stored.get(provider)
        declared[provider] = value if type(value) is str and value in allowed else None
    return declared


def write_declared(provider: str, value: str | None, path: Path | None = None) -> Path:
    """Set (or, with None, unset) one provider's plan, keeping every other preference."""
    from .adapters.atomicwrite import atomic_write
    from .adapters.server_instructions import _read_preferences

    plan = None if value is None else normalise(provider, value)
    if provider not in DECLARABLE_PLANS:
        raise ValueError(f"Unknown provider {provider!r}. Use one of: {', '.join(PROVIDERS)}.")
    target = _preferences_path(path)
    payload = _read_preferences(target)
    stored = payload.get(_PLANS_KEY)
    plans = {key: item for key, item in stored.items()
             if key in DECLARABLE_PLANS and type(item) is str and item in DECLARABLE_PLANS[key]} \
        if isinstance(stored, dict) else {}
    if plan is None:
        plans.pop(provider, None)
    else:
        plans[provider] = plan
    if plans:
        payload[_PLANS_KEY] = plans
    else:
        payload.pop(_PLANS_KEY, None)
    atomic_write(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return target


def read_observed_codex_plan(usage_directory: Path) -> str | None:
    """The ``plan_type`` of the stored Codex quota read, or None.

    Reads only ModelQuad's own ``usage-codex.json``.
    """
    try:
        path = Path(usage_directory) / "usage-codex.json"
        if path.stat().st_size > _MAX_USAGE_FILE_BYTES:
            return None
        plan = json.loads(path.read_text(encoding="utf-8-sig")).get("plan_type")
    except (OSError, ValueError, AttributeError, RecursionError):
        return None
    return plan if type(plan) is str else None


def merged(observed_codex: str | None, declared: dict[str, str | None]) -> dict[str, PlanView]:
    """Each side's plan view. A plan Codex reports beats the one you set."""
    claude = declared.get("claude")
    views = {"claude": PlanView(declared_kind(claude), SOURCE_DECLARED, claude, claude)
             if claude else PlanView(UNKNOWN, SOURCE_UNKNOWN)}
    codex = declared.get("codex")
    seen_kind = observed_codex_kind(observed_codex)
    if seen_kind != UNKNOWN:
        views["codex"] = PlanView(seen_kind, SOURCE_OBSERVED, observed_codex, codex, observed_codex)
    elif codex:
        views["codex"] = PlanView(declared_kind(codex), SOURCE_DECLARED, codex, codex, observed_codex)
    else:
        views["codex"] = PlanView(UNKNOWN, SOURCE_UNKNOWN, None, None, observed_codex)
    return views


def current(usage_directory: Path, preferences: Path | None = None) -> dict[str, PlanView]:
    return merged(read_observed_codex_plan(usage_directory), read_declared(preferences))


def public_plans(views: dict[str, PlanView]) -> dict[str, dict[str, str]]:
    """The ``plans`` block of a model-facing reply: kind words and sources only."""
    return {provider: views[provider].public() for provider in PROVIDERS}


_CODEX_LINES = {
    PERSONAL: ("Codex account: personal ChatGPT plan{source}. "
               "If this is work code, check you may send it to a personal account."),
    WORKSPACE: "Codex account: workspace plan{source}.",
    UNKNOWN: "Codex account: unknown.",
}
_KIND_WORDS = {PERSONAL: "personal account", WORKSPACE: "work account"}
_SOURCE_WORDS = {SOURCE_OBSERVED: "reported by Codex", SOURCE_DECLARED: "you set it"}


def account_line(views: dict[str, PlanView]) -> str:
    """One plain line about the account kinds, for replies the model sees.

    When one side is a work account and the other a personal one, the line
    names both and says which way the code goes. Otherwise it is the Codex
    account line. Kind words only; never a plan name.
    """
    claude, codex = views["claude"], views["codex"]
    if {claude.kind, codex.kind} == {PERSONAL, WORKSPACE}:
        direction = ("work code from a work account to a personal one" if claude.kind == WORKSPACE
                     else "code from a personal account to a work one")
        return (f"Claude: {_KIND_WORDS[claude.kind]} ({_SOURCE_WORDS[claude.source]}). "
                f"Codex: {_KIND_WORDS[codex.kind]} ({_SOURCE_WORDS[codex.source]}). "
                f"This sends {direction}.")
    source = " (you set it)" if codex.source == SOURCE_DECLARED else ""
    return _CODEX_LINES[codex.kind].format(source=source)


def accounts_differ(views: dict[str, PlanView]) -> bool:
    return {views["claude"].kind, views["codex"].kind} == {PERSONAL, WORKSPACE}


def _shown(plan: str | None) -> str:
    if plan is None:
        return "unknown"
    plan = plan.strip().casefold()
    return plan if _SHOWABLE.fullmatch(plan) else "not recognised"


def human_lines(views: dict[str, PlanView]) -> list[str]:
    """The plans as the CLI shows them, plan names included. Never sent to the model."""
    lines = []
    claude = views["claude"]
    if claude.declared:
        lines.append(f"Claude: {claude.declared} (you set it). Account: {claude.kind}.")
    else:
        lines.append("Claude: unknown. Set it with: modelquad settings plan claude <plan>")
    codex = views["codex"]
    if codex.source == SOURCE_OBSERVED and codex.declared and codex.declared != _shown(codex.observed):
        lines.append(f"Codex: Codex reports {_shown(codex.observed)}; you set {codex.declared}. "
                     f"Account: {codex.kind} (what Codex reports counts).")
    elif codex.source == SOURCE_OBSERVED:
        lines.append(f"Codex: {_shown(codex.observed)} (reported by Codex). Account: {codex.kind}.")
    elif codex.declared:
        seen = (f" Codex reports {_shown(codex.observed)}, which ModelQuad does not recognise."
                if codex.observed else "")
        lines.append(f"Codex: {codex.declared} (you set it). Account: {codex.kind}.{seen}")
    else:
        lines.append("Codex: unknown. Set it with: modelquad settings plan codex <plan>")
    if accounts_differ(views):
        lines.append(account_line(views))
    return lines
