# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""User presentation adapter for one routing / delegation decision.

The canonical decision object (``RoutingResult.to_dict`` /
``ExecutionResult.to_dict``) is the source of truth and is never trimmed to
make a screen tidier. This module is a *reader* of that object: it derives
what a person should see, in the product's user-facing vocabulary, and
leaves the canonical object completely intact for diagnostics,
history and programmatic consumers.

    Router / Evidence / History
            |
      canonical decision object
            |-- internal diagnostics / history     (unchanged)
            '-- build_route_presentation()  <-- this module
                        |
                  MCP App widget + concise text fallback

Three rules the rest of this file exists to enforce:

1. **Routing is not delegation.** A recommendation is ``selected``; only an
   execution that actually ran may say ``completed``. The lifecycle
   vocabularies are separate and the route surface cannot express an
   execution state.
2. **Unknown is a state, never a zero.** A missing quota reading renders as
   "Capacity unknown", never as ``0%`` or ``null``; a stale reading is never
   presented as a current fact.
3. **Internals stay internal.** ``fit``, ``usage_score``, ``quota_penalty``,
   ``decision_id``, ``resource_pool``, ``exclusion_stage`` and friends are
   assembled only under :func:`build_technical_details`, which callers attach
   exclusively in developer mode.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

#: 2 -- the decision presentation gained ``capacity``, ``capabilities`` and the
#: lifecycle's ``progress`` / ``summary``, and the route headlines now name
#: ModelQuad as the decider. Additive: every v1 key is still present and still
#: means what it meant.
PRESENTATION_SCHEMA_VERSION = 2

#: What a *route* decision may say about itself. Deliberately disjoint from
#: the execution vocabulary below: nothing has run, so nothing here may claim
#: a result.
ROUTE_STATES: frozenset[str] = frozenset({"selected", "stayed_with_host", "blocked"})

#: What an *execution* may say about itself, mapped from the objective
#: backend state (``WorkerTaskStatus`` plus the timeout/refusal overlays).
EXECUTION_STATES: frozenset[str] = frozenset(
    {"completed", "partial", "blocked", "failed", "timeout", "refused"}
)

_STATE_LABELS: dict[str, str] = {
    "selected": "Selected",
    "stayed_with_host": "Stayed here",
    "blocked": "Blocked",
    "completed": "Completed",
    "partial": "Partly done",
    "failed": "Failed",
    "timeout": "Timed out",
    "refused": "Not started",
    # Activity-history states (see ACTIVITY_STATES). They share this table so
    # one state can never be spelled two ways across the two surfaces.
    "not_routed": "No provider",
    "delegating": "Delegating",
    # The caller stopped waiting; the worker did not stop. "Still running" is
    # about the WORKER -- the note beside it says nobody is waiting for it.
    "unattended": "Still running",
    # ModelQuad ended the worker and confirmed the ending.
    "stopped": "Stopped",
    "running": "Running",
    "no_response": "No response",
    "no_result": "No result reported",
    "unknown": "Unknown",
}

#: The four tones the shared Badge component implements -- no fifth tone is
#: introduced for this surface. "Selected" is deliberately `muted`: nothing
#: has happened yet, and a success-coloured chip on a recommendation would be
#: the UI overstating what the evidence supports.
_STATE_TONES: dict[str, str] = {
    "selected": "muted",
    "stayed_with_host": "muted",
    "blocked": "warning",
    "completed": "good",
    "partial": "warning",
    "failed": "critical",
    "timeout": "critical",
    "refused": "muted",
    # `not_routed` and `no_response` are `warning`, not `critical`: nothing
    # crashed, but there is something a person may want to look at. Both
    # UNKNOWN-shaped states are `muted` -- invariant 4's UI half is that an
    # absence of evidence is not an alarm.
    "not_routed": "warning",
    "delegating": "muted",
    # `warning`, not `critical`: nothing has gone wrong yet, but paid quota is
    # being spent on work nobody is waiting for, and that deserves a look.
    "unattended": "warning",
    # `critical`: work the user asked for was ended before it finished.
    "stopped": "critical",
    "running": "muted",
    "no_response": "warning",
    "no_result": "muted",
    "unknown": "muted",
}

_CONSUMPTION_LABELS: dict[str, str] = {"low": "Low", "medium": "Medium", "high": "High"}

_EFFORT_LABELS: dict[str, str] = {"low": "Low", "medium": "Medium", "high": "High"}

_CAPABILITY_LABELS: dict[str, str] = {
    "read": "read files",
    "write": "change files",
    "command": "run commands",
}

_INVOCATION_LABELS: dict[str, str] = {
    "automatic": "Auto",
    "explicit_user_request": "Requested",
    "direct_manual": "Manual",
    "unknown": "",
}

_TASK_LABELS: dict[str, str] = {
    "architecture": "Architecture",
    "implementation": "Implementation",
    "exploration": "Exploration",
    "debugging": "Debugging",
    "testing": "Testing",
    "review": "Review",
    "trivial": "Quick task",
}

# Freshness values that mean "this reading describes right now". Everything
# else -- including an unrecognised value -- is treated as not current, which
# is the safe direction: over-claiming freshness is the failure that matters.
_CURRENT_FRESHNESS: frozenset[str] = frozenset({"fresh"})

#: The ``Freshness`` vocabulary, as the *category* a person reads. Deliberately
#: separate from :func:`freshness_phrase`, which is the sentence: a percentage
#: and how old it is are two facts, and merging them into "99% left at the last
#: check" made a reading look worse or better than the evidence supports.
_FRESHNESS_LABELS: dict[str, str] = {
    "fresh": "Fresh",
    "stale": "Stale",
    "very_stale": "Very stale",
    "unknown": "Unknown",
}

#: A merely stale reading is normal operation, not an alarm -- `muted`, like
#: unknown. Only `very_stale` is worth a person's attention,
#: and nothing here is ever `critical`: an old quota number is not a failure.
_FRESHNESS_TONES: dict[str, str] = {
    "fresh": "good",
    "stale": "muted",
    "very_stale": "warning",
    "unknown": "muted",
}

#: How far a lifecycle state has got, as three machine-readable buckets. This
#: is what lets a surface style "nothing has run yet" differently from "it is
#: running" without re-deriving the rule from the state name -- and it is why
#: every route state is `not_started` no matter which provider won.
_STATE_PROGRESS: dict[str, str] = {
    "selected": "not_started",
    "stayed_with_host": "not_started",
    # `blocked` is the one state both vocabularies spell. On the execution
    # surface a worker started and could not proceed; on the route surface
    # nothing was ever chosen. `route_lifecycle` overrides accordingly -- one
    # word, two facts, and neither may be presented as the other.
    "blocked": "finished",
    "not_routed": "not_started",
    "refused": "not_started",
    "delegating": "in_progress",
    "running": "in_progress",
    # Still in progress: the caller leaving did not stop the work.
    "unattended": "in_progress",
    "stopped": "finished",
    "completed": "finished",
    "partial": "finished",
    "failed": "finished",
    "timeout": "finished",
    "no_response": "finished",
    "no_result": "finished",
    "unknown": "unknown",
}

#: The primary card's one-line truth about where this stands. Short on purpose
#: -- the longer sentence stays in ``note``, one level down, so the collapsed
#: card keeps its few-lines budget.
_STATE_SUMMARIES: dict[str, str] = {
    "selected": "Not started yet",
    "stayed_with_host": "No handoff",
    "blocked": "Could not proceed",
    "not_routed": "Nothing to run",
    "refused": "Never started",
    "delegating": "Handing over",
    "running": "Running now",
    "unattended": "Running unattended",
    "stopped": "Stopped",
    "completed": "Finished",
    "partial": "Partly finished",
    "failed": "Did not finish",
    "timeout": "No answer in time",
    "no_response": "Never reported back",
    "no_result": "No result reported",
    "unknown": "Not enough evidence",
}


# --------------------------------------------------------------- formatting


def format_percent(value: Any) -> str | None:
    """A percentage a person can read, or ``None`` when there is no reading.

    Floating point routing arithmetic produces values like
    ``99.65999999999999``; rendering that verbatim is the single most
    obvious "this is a debugging API" tell. Two decimals, trailing zeros
    trimmed, so ``36.0 -> "36%"``, ``99.7 -> "99.7%"``,
    ``99.65999999999999 -> "99.66%"``.
    """

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    rounded = round(float(value), 2)
    text = f"{rounded:.2f}".rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def _title(value: str | None) -> str | None:
    if not value:
        return None
    return value[:1].upper() + value[1:]


def _humanize_id(value: str | None) -> str | None:
    """``claude_code`` -> ``Claude Code``. Never invents a name it was not given."""

    if not value:
        return None
    words = [word for word in value.replace("-", " ").replace("_", " ").split() if word]
    if not words:
        return None
    return " ".join(word[:1].upper() + word[1:] for word in words)


def provider_label(provider: str | None) -> str | None:
    """The provider's own display name from the registry, never a guess."""

    if not provider:
        return None
    try:
        from ..registry import default_registry

        plugin = default_registry().get(provider)
    except Exception:  # noqa: BLE001 -- a label is never worth failing a tool over
        plugin = None
    if plugin is not None:
        return plugin.display_name
    # `_humanize_id`, not `_title`: a runtime id like ``claude_code`` reaching
    # here (the registry keys providers, not runtimes) must read "Claude Code"
    # rather than "Claude_code". Single-word ids are unaffected.
    return _humanize_id(provider)


def state_label(state: str | None) -> str:
    """The one display name for a lifecycle/activity state.

    Public accessor over ``_STATE_LABELS`` so surfaces outside this module
    (the service's current-task route) spell a state exactly the way the MCP
    surfaces spell it, instead of keeping a second table that can drift.
    """

    if not state:
        return "Unknown"
    return _STATE_LABELS.get(state, _title(state.replace("_", " ")) or "Unknown")


def task_label(task_type: str | None) -> str:
    if not task_type:
        return "Task"
    return _TASK_LABELS.get(task_type, _title(task_type.replace("_", " ")) or "Task")


def _capability_phrase(capabilities: list[str] | tuple[str, ...]) -> str | None:
    named = [_CAPABILITY_LABELS.get(item, item) for item in capabilities if item]
    if not named:
        return None
    if len(named) == 1:
        return named[0]
    return ", ".join(named[:-1]) + " and " + named[-1]


#: What a person is told when ModelQuad's network policy, not the provider,
#: is the reason. Saying "not available" instead would blame a provider that
#: is working perfectly well.
_NETWORK_POLICY_NOTE = (
    "ModelQuad's network policy does not allow calling this provider's API"
)


def _capability_gap_note(gaps: list[dict[str, Any]]) -> str:
    """Plain-language "why not", in the user's vocabulary rather than ours.

    Each capability state gets its own sentence, because they mean genuinely
    different things to the person reading:

    * restricted   -- the agent can; ModelQuad does not let it here
    * unsupported  -- the agent genuinely cannot
    * needs_repair -- it should work and did not; something is fixable
    * unverified   -- we have not checked

    Rendering all four as "Cannot ..." was the user-visible half of the defect:
    it told people their agent was less capable than it is.
    """

    phrase = _capability_phrase([gap.get("capability") for gap in gaps])
    if not phrase:
        return "Not eligible for this task"
    states = {gap.get("state") for gap in gaps}
    if states == {"restricted"}:
        return f"ModelQuad does not allow this agent to {phrase} here"
    if states == {"unsupported"}:
        return f"Cannot {phrase}"
    if states == {"needs_repair"}:
        return f"Needs repair before it can {phrase}"
    if states == {"unverified"}:
        return f"Not yet verified to {phrase}"
    # Mixed reasons: say the neutral thing rather than picking one and
    # implying it explains all of them.
    return f"Not able to {phrase} for this task"


def _capability_items(names: Any) -> list[dict[str, str]]:
    """Capability ids paired with their plain-language names, in payload order."""

    if not isinstance(names, (list, tuple)):
        return []
    return [
        {"key": str(name), "label": _CAPABILITY_LABELS.get(str(name), str(name))}
        for name in names
        if isinstance(name, str) and name
    ]


def capability_view(score: dict[str, Any] | None) -> dict[str, Any]:
    """What this agent is actually allowed to do here, from recorded facts only.

    Read from exactly three normalized fields and nothing else:

    * ``available_capabilities`` -- the OPERATIONAL set: supported, permitted
      on this machine, and not known-broken. It is what a routing surface must
      answer with, because "could in principle" is a different question.
    * ``required_capabilities`` -- what this task needs.
    * ``capability_gaps`` -- per capability, the STATE that blocked it, which
      is what keeps "ModelQuad does not allow this" distinguishable from "the
      runtime cannot".

    Absence is handled in the two genuinely different ways the payload
    supports. The key missing entirely means nobody reported permissions. The
    key present and empty means no capability profile is configured for this
    provider -- which the router treats as "not excluded", never as "can do
    nothing" -- so neither case may be rendered as a denial, and neither may
    be widened into a claim about a capability that was not listed.
    """

    score = score if isinstance(score, dict) else {}
    reported = "available_capabilities" in score
    allowed = _capability_items(score.get("available_capabilities"))
    required = _capability_items(score.get("required_capabilities"))
    gaps = [gap for gap in (score.get("capability_gaps") or []) if isinstance(gap, dict)]
    blocked = [
        {
            "key": str(gap.get("capability") or ""),
            "label": _CAPABILITY_LABELS.get(str(gap.get("capability") or ""), str(gap.get("capability") or "")),
            "state": str(gap.get("state") or "unknown"),
            # Whose decision it was. `restricted` is ModelQuad's own policy;
            # everything else is a fact about the runtime.
            "by_policy": bool(gap.get("is_policy_restriction")),
        }
        for gap in gaps
        if gap.get("capability")
    ]

    if blocked:
        summary = _capability_gap_note(gaps)
        known = True
    elif allowed:
        summary = f"Allowed here: {_capability_phrase([item['key'] for item in allowed])}"
        known = True
    elif reported:
        # An empty list is a real, recorded answer: no capability profile is
        # configured for this provider. It is not a denial and must not read
        # as one.
        summary = "No permissions are configured for this agent"
        known = False
    else:
        summary = "Permissions were not reported"
        known = False

    return {
        "known": known,
        "reported": reported,
        "allowed": allowed,
        "required": required,
        "blocked": blocked,
        "summary": summary,
    }


# ------------------------------------------------------------ host identity


#: What the origin node says when nothing identified the calling host.
#: Neutral and true: something is calling, and we do not know what.
UNKNOWN_HOST_LABEL = "Current host"


def _identifies_modelquad(value: str | None) -> bool:
    """True when this identity is ModelQuad itself rather than a host.

    A client that reports itself as some flavour of "ModelQuad" is not the
    ORIGIN of the task -- ModelQuad is the hub in the middle of the rail. Left
    unfiltered it produced the lineage this function exists to remove:

        Local Agent Mode Modelquad -> ModelQuad -> Codex

    which names the hub twice and tells the reader nothing about where the
    task actually came from.
    """

    if not value:
        return False
    squashed = "".join(character for character in value.lower() if character.isalnum())
    return "modelquad" in squashed


def _host_label(value: str | None) -> str | None:
    """A display name for one host identity, or ``None`` when it is not one."""

    if _identifies_modelquad(value):
        return None
    return _humanize_id(value)


def host_node(host_name: str | None, host_runtime: str | None) -> dict[str, Any]:
    """The "where the task is right now" node of the flow.

    Both inputs are evidence, not assertions: ``host_name`` is the MCP
    ``clientInfo`` name and ``host_runtime`` is whatever the host observers
    concluded. When neither identifies anything, the node says so rather
    than inventing a host.
    """

    # The observer names the runtime ("claude_code"), the registry names the
    # provider ("claude"): two spellings of one thing, and the runtime is the
    # more precise of the two. Humanize it rather than trading it for a
    # coarser registry display name, and never show a raw id.
    label = _host_label(host_runtime)
    key = host_runtime if label else None
    if not label:
        label = _host_label(host_name)
        key = host_name if label else None
    return {
        "role": "host",
        "key": key,
        "label": label or UNKNOWN_HOST_LABEL,
        "known": bool(label),
    }


# ------------------------------------------------------------------ reasons


def _fit_reason(fit: Any) -> dict[str, str] | None:
    if not isinstance(fit, (int, float)) or isinstance(fit, bool):
        return None
    if fit >= 0.9:
        text = "Strong fit for this task"
    elif fit >= 0.7:
        text = "Good fit for this task"
    elif fit >= 0.5:
        text = "Reasonable fit for this task"
    else:
        text = "Workable fit for this task"
    return {"kind": "fit", "tone": "good", "text": text}


def _capacity_reason(remaining: Any, freshness: str | None) -> dict[str, str]:
    percent = format_percent(remaining)
    if percent is None:
        # Invariant 4: absent evidence reads UNKNOWN, never a confident zero.
        return {
            "kind": "capacity",
            "tone": "muted",
            "text": "Remaining capacity is not reported by this provider",
        }
    if freshness in _CURRENT_FRESHNESS:
        return {"kind": "capacity", "tone": "good", "text": f"{percent} capacity available"}
    return {
        "kind": "capacity",
        "tone": "muted",
        "text": f"{percent} capacity at the last check (may be outdated)",
    }


def _strategy_reason(strategy: str | None, label: str | None) -> dict[str, str] | None:
    """The routing preference, named by what the user set rather than by us.

    "Spend Codex capacity" is the internal vocabulary of the resource
    strategy -- accurate inside the router, and inside-out on a card: it
    describes ModelQuad's accounting rather than the person's own choice, and
    it reads as though the product is about to burn their quota. Both
    sentences below say the same thing as a preference the user owns, and
    neither exposes a weight or a score.
    """

    if strategy == "spend":
        target = label or "this provider"
        return {
            "kind": "strategy",
            "tone": "good",
            "text": f"Your settings prefer {target} for work like this",
        }
    if strategy == "preserve":
        return {
            "kind": "strategy",
            "tone": "good",
            "text": "Your settings prefer to save the capacity you have left",
        }
    return None


def strategy_objective(strategy: str | None) -> str | None:
    """The same preference as a two-word objective, for the details list."""

    return {
        "spend": "Use available capacity",
        "preserve": "Save remaining capacity",
    }.get(strategy or "")


def _capability_reason(score: dict[str, Any]) -> dict[str, str] | None:
    phrase = _capability_phrase(score.get("required_capabilities") or [])
    if phrase is None or not score.get("capable", True):
        return None
    return {"kind": "capability", "tone": "good", "text": f"Can {phrase} for this task"}


#: Reason kinds the decision card renders somewhere else. They stay in
#: ``reasons`` because the text fallback has no capacity strip and no
#: capability line -- there, these sentences ARE the answer. On the card they
#: would be the same fact twice, and a collapsed automatic card that no longer
#: fits in a few lines has stopped being quiet.
_NON_PRIMARY_REASON_KINDS: frozenset[str] = frozenset({"capacity", "capability"})


def build_reasons(score: dict[str, Any] | None, label: str | None) -> list[dict[str, Any]]:
    """Two to four plain-language reasons, in the user's vocabulary.

    Explicitly not the arithmetic: stating a score without stating what would
    change it is less honest, not more.

    Each reason carries ``primary``: whether the compact card should show it.
    That call is made here rather than in React, so which sentences a surface
    shows stays one decision in one place.
    """

    if not score:
        return []
    reasons: list[dict[str, Any]] = []
    fit = _fit_reason(score.get("fit"))
    if fit:
        reasons.append(fit)
    reasons.append(_capacity_reason(score.get("remaining_percentage"), score.get("freshness")))
    strategy = _strategy_reason(score.get("strategy"), label)
    if strategy:
        reasons.append(strategy)
    capability = _capability_reason(score)
    if capability:
        reasons.append(capability)
    for reason in reasons:
        reason["primary"] = reason["kind"] not in _NON_PRIMARY_REASON_KINDS
    return reasons


# ---------------------------------------------------------------- freshness


def freshness_phrase(freshness: str | None) -> str:
    return {
        "fresh": "Checked just now",
        "stale": "May be outdated",
        "very_stale": "Outdated — refresh to be sure",
        "unknown": "Never checked",
    }.get(freshness or "unknown", "Never checked")


def freshness_key(freshness: Any) -> str:
    """One of the four known freshness categories, or ``unknown``.

    An unrecognised value is `unknown`, never silently promoted: over-claiming
    freshness is the failure that matters (AGENTS.md invariant 4).
    """

    return freshness if freshness in _FRESHNESS_LABELS else "unknown"


def capacity_phrase(remaining: Any, freshness: str | None = None) -> str:
    """How much is left, and *only* that.

    ``freshness`` is accepted and ignored: it used to be spliced into this
    string ("99% left at the last check"), which made one line answer two
    questions badly. Age is now its own field -- see :func:`capacity_view`.
    """

    percent = format_percent(remaining)
    return "Capacity unknown" if percent is None else f"{percent} left"


def _age_label(observed_at: Any, *, now: datetime | None = None) -> str | None:
    """"3 minutes ago", but only when a real timestamp says so.

    Returns ``None`` for anything unparseable, absent, or in the future. A
    routing payload does not carry ``observed_at`` today, so in practice the
    widget falls back to the categorical freshness -- which is the point: a
    manufactured age would be worse than no age at all.
    """

    if not isinstance(observed_at, str) or not observed_at.strip():
        return None
    text = observed_at.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    seconds = (reference - moment).total_seconds()
    if seconds < 0:
        # A reading from the future is a clock disagreement, not evidence of
        # freshness. Say nothing rather than "in 3 minutes".
        return None
    if seconds < 60:
        return "just now"
    for limit, size, unit in ((3600, 60, "minute"), (86400, 3600, "hour"), (604800, 86400, "day")):
        if seconds < limit:
            count = max(1, int(seconds // size))
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    weeks = max(1, int(seconds // 604800))
    return f"{weeks} week{'s' if weeks != 1 else ''} ago"


def capacity_view(score: dict[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    """Remaining capacity and how old that reading is, as two separate facts.

    The percentage answers "how much is left"; the freshness answers "how much
    should I trust that". Merging them let a stale reading read as a current
    one and an unknown one read as empty. ``age`` is populated only when the payload carries
    a timestamp -- otherwise the categorical label is the whole answer.
    """

    score = score if isinstance(score, dict) else {}
    percent = format_percent(score.get("remaining_percentage"))
    freshness = freshness_key(score.get("freshness"))
    age = _age_label(score.get("observed_at"), now=now)
    return {
        "provider": score.get("provider"),
        "known": percent is not None,
        "percent": percent,
        "label": percent if percent is not None else "Not reported",
        "freshness": freshness,
        "freshness_label": _FRESHNESS_LABELS[freshness],
        "freshness_tone": _FRESHNESS_TONES[freshness],
        # The sentence a person reads under the chip. A real age wins over the
        # category when one exists, because "3 minutes ago" is strictly more
        # information than "Fresh".
        "freshness_detail": f"Checked {age}" if age else freshness_phrase(freshness),
        "age": age,
    }


# ------------------------------------------------------------------- model


def model_fact(target: dict[str, Any] | None, provider_display: str | None) -> dict[str, Any] | None:
    """What to say about the model, or ``None`` when there is nothing true to say.

    Three genuinely different situations, and only two of them produce a
    line. A named model is stated. No model with ``model_source="unset"``
    means the runtime uses its own default, which is a true statement worth
    making. Anything else is omitted rather than rendered as ``null``.
    """

    if not target:
        return None
    model = target.get("model")
    if isinstance(model, str) and model.strip():
        return {"known": True, "label": model.strip(), "value": model.strip()}
    if provider_display:
        # No model was selected -- by the router, by a configured prior, or by
        # the caller -- so whatever runs uses its own default. That is a true
        # statement about the decision, and the only honest thing to say in
        # place of `model: null`.
        return {"known": False, "label": f"Chosen by {provider_display}", "value": None}
    return None


# --------------------------------------------------------------- lifecycle


def _stages(states: list[str], current: str) -> list[dict[str, str]]:
    stages: list[dict[str, str]] = []
    reached = False
    for state in states:
        if state == current:
            status = "current"
            reached = True
        else:
            status = "pending" if reached else "done"
        stages.append({"key": state, "label": _STATE_LABELS.get(state, _title(state) or state), "status": status})
    return stages


def route_lifecycle(state: str) -> dict[str, Any]:
    """The route surface's own lifecycle. It can never say "completed".

    ``executed`` is the machine-readable half of the same promise: a route
    decision is advice, and the widget must not let a reader believe work
    happened because a provider won a comparison.
    """

    if state not in ROUTE_STATES:
        raise ValueError(f"advice lifecycle state must be one of {sorted(ROUTE_STATES)}, got {state!r}")
    notes = {
        "selected": "Nothing has run yet. This is advice only.",
        "stayed_with_host": "No handoff — the task stays in this session.",
        "blocked": "No provider could take this task.",
    }
    summaries = {
        "selected": "Not started yet",
        "stayed_with_host": "No handoff",
        "blocked": "Nothing to run",
    }
    return {
        "phase": "route",
        "state": state,
        "label": _STATE_LABELS[state],
        "tone": _STATE_TONES[state],
        "executed": False,
        # `not_started` for every route state, including the one that picked a
        # winner: choosing is not running, and this is the field a surface
        # styles from so it never has to infer that from the state name.
        "progress": "not_started",
        "summary": summaries[state],
        "note": notes[state],
        "stages": _stages(["routing", state] if state != "blocked" else ["routing", "blocked"], state),
    }


#: The execution states that describe work still in flight. They are part of
#: the vocabulary because history already records them (``ACTIVITY_STATES``)
#: and a live surface must be able to say them -- but no adapter here invents
#: one: ``execution_state_from_result`` only ever returns a settled state, so
#: a route or a finished delegation can never render as ``running``.
_IN_FLIGHT_STATES: frozenset[str] = frozenset({"delegating", "running"})


def execution_lifecycle(state: str) -> dict[str, Any]:
    """The delegation surface's lifecycle, mapped from objective backend state.

    Accepts the in-flight states too, for a caller holding live evidence that
    a handoff is under way. ``executed`` stays False for ``delegating``:
    ModelQuad is handing the task over and no worker is confirmed running, so
    claiming execution there would be the same over-claim as calling a route
    a result.
    """

    if state not in EXECUTION_STATES and state not in _IN_FLIGHT_STATES:
        raise ValueError(
            f"execution lifecycle state must be one of "
            f"{sorted(EXECUTION_STATES | _IN_FLIGHT_STATES)}, got {state!r}"
        )
    notes = {
        "completed": "Codex reported the task done.",
        "partial": "Codex reported only part of the task done.",
        "blocked": "Codex could not proceed.",
        "failed": "The task did not succeed.",
        "timeout": "Codex did not return in time.",
        "refused": "Nothing was sent to Codex.",
        "delegating": _ACTIVITY_NOTES["delegating"],
        "running": _ACTIVITY_NOTES["running"],
    }
    if state == "refused":
        stages = _stages(["selected", "refused"], state)
    elif state in _IN_FLIGHT_STATES:
        stages = _stages(["selected", state], state)
    else:
        stages = _stages(["selected", "running", state], state)
    return {
        "phase": "execution",
        "state": state,
        "label": _STATE_LABELS[state],
        "tone": _STATE_TONES[state],
        "executed": state not in {"refused", "delegating"},
        "progress": _STATE_PROGRESS[state],
        "summary": _STATE_SUMMARIES[state],
        "note": notes[state],
        "stages": stages,
    }


# ----------------------------------------------------------------- helpers


def _score_for(decision: dict[str, Any], provider: str | None) -> dict[str, Any] | None:
    if not provider:
        return None
    for score in decision.get("providers") or []:
        if isinstance(score, dict) and score.get("provider") == provider:
            return score
    return None


def _invocation(mode: str | None) -> dict[str, Any]:
    resolved = mode if mode in _INVOCATION_LABELS else "unknown"
    return {
        "mode": resolved,
        "label": _INVOCATION_LABELS[resolved],
        "automatic": resolved == "automatic",
        # "unknown" is honest: the caller did not report why this ran. It
        # produces no badge rather than a guess.
        "reported": resolved != "unknown",
    }


def _alternatives(decision: dict[str, Any], chosen: str | None) -> list[dict[str, Any]]:
    """Every other candidate, as a comparison a person can read.

    Eligibility and preference stay separate here exactly as they are in the
    router: a candidate that could not run at all is not "ranked lower", it
    is "cannot run this task", with the reason attached.
    """

    rows: list[dict[str, Any]] = []
    for score in decision.get("providers") or []:
        if not isinstance(score, dict):
            continue
        provider = score.get("provider")
        if not provider or provider == chosen:
            continue
        eligible = bool(score.get("eligible", True))
        gaps = score.get("capability_gaps") or []
        if eligible:
            note = "Available for this task"
        elif score.get("exclusion_stage") == "network_policy":
            # ModelQuad's own network policy, so say so in ModelQuad's name.
            # "Not available right now" would be a statement about the
            # provider, and the provider is fine.
            note = _NETWORK_POLICY_NOTE
        elif gaps:
            note = _capability_gap_note(gaps)
        elif score.get("missing_capabilities"):
            # A decision recorded before capability states existed: we know
            # WHICH capability excluded it but not WHY. Say only that much --
            # rendering it as "Cannot ..." would restate the old falsehood over
            # history that cannot answer for itself.
            phrase = _capability_phrase(score.get("missing_capabilities") or [])
            note = f"Not able to {phrase} for this task" if phrase else "Not eligible for this task"
        elif not score.get("available", True):
            note = "Not available right now"
        else:
            note = "Not eligible for this task"
        # "Cannot run this task" is only true when the RUNTIME is the obstacle.
        # Where ModelQuad's own policy is, saying "cannot" blames the agent for
        # our decision -- the defect this milestone exists to remove.
        if eligible:
            status = "Eligible"
        elif score.get("exclusion_stage") == "network_policy" or (
            gaps and all(gap.get("is_policy_restriction") for gap in gaps)
        ):
            status = "Restricted by ModelQuad"
        else:
            status = "Cannot run this task"
        capacity = capacity_view(score)
        rows.append(
            {
                "provider": provider,
                "label": provider_label(provider) or provider,
                "eligible": eligible,
                "status": status,
                "note": note,
                "capacity": capacity_phrase(score.get("remaining_percentage")),
                "capacity_known": capacity["known"],
                # The sentence stays for continuity; the category beside it is
                # what makes "99% left" and "how old is that" two facts rather
                # than one crowded string.
                "freshness": freshness_phrase(score.get("freshness")),
                "freshness_label": capacity["freshness_label"],
                "freshness_tone": capacity["freshness_tone"],
            }
        )
    return rows


def _routing_method(decision_source: str | None) -> str:
    return {
        # Advice, never an automatic choice: quota is display-only and a
        # worker runs only when the user asked for it.
        "deterministic": "Advice (task fit)",
        "host_reasoned": "Advice (with reasoning)",
        "delegated_reasoned": "Advice (with reasoning)",
        "user_override": "You chose the provider",
        # Rows recorded before step 5 may still carry this source.
        "fallback": "Older record",
        "learned_future": "Advice",
    }.get(decision_source or "", "Advice")


# ------------------------------------------------------ the two adapters


def build_route_presentation(
    decision: dict[str, Any],
    *,
    invocation_mode: str | None = None,
    host_name: str | None = None,
    host_runtime: str | None = None,
    developer_mode: bool = False,
) -> dict[str, Any]:
    """Derive the user-facing view of one routing decision.

    ``decision`` is the canonical ``RoutingResult.to_dict()`` payload (plus
    the ``stay_with_host`` / ``decision_id`` keys the MCP layer adds). It is
    read, never modified.
    """

    recommendation = decision.get("recommendation")
    stay_with_host = bool(decision.get("stay_with_host"))
    chosen_label = provider_label(recommendation)
    score = _score_for(decision, recommendation)
    target = decision.get("target") if isinstance(decision.get("target"), dict) else None

    if recommendation is None:
        state = "blocked"
    elif stay_with_host:
        state = "stayed_with_host"
    else:
        state = "selected"

    host = host_node(host_name, host_runtime)
    if state == "stayed_with_host":
        # NOT a copy of the host node. Repeating the same label at both ends
        # ("Claude Code -> ModelQuad -> Claude Code") reads as a round trip
        # through a worker; naming the outcome says what actually happens.
        target_node = {
            **host,
            "role": "target",
            "label": f"Stay with {host['label']}" if host["known"] else "Stay here",
            "state": state,
        }
    elif recommendation:
        target_node = {
            "role": "target",
            "key": recommendation,
            "label": chosen_label or recommendation,
            "known": True,
            "state": state,
        }
    else:
        target_node = {"role": "target", "key": None, "label": "No provider", "known": False, "state": state}

    task_type = decision.get("task_type")
    consumption = decision.get("expected_consumption") or {}
    bucket = consumption.get("bucket") if isinstance(consumption, dict) else None
    effort = target.get("reasoning_effort") if target else None
    model = model_fact(target, chosen_label)

    # The headline names the ACTOR and the CHOICE. "Selected Codex" read
    # as a past-tense action a reader could take for a delegation that had
    # already happened; it also never said who selected. Saying "ModelQuad
    # chose" answers who decided -- that this is
    # ModelQuad's decision -- while `lifecycle.summary` beside it says nothing
    # has run.
    if state == "blocked":
        headline = "ModelQuad found no provider for this task"
    elif state == "stayed_with_host":
        headline = (
            f"ModelQuad kept this task in {host['label']}"
            if host["known"]
            else "ModelQuad kept this task here"
        )
    else:
        headline = f"ModelQuad chose {chosen_label or recommendation}"

    subline_parts = [task_label(task_type)]
    if bucket in _CONSUMPTION_LABELS:
        subline_parts.append(f"{_CONSUMPTION_LABELS[bucket]} expected usage")

    facts: list[dict[str, str]] = []
    if bucket in _CONSUMPTION_LABELS:
        # Expected consumption and reasoning effort describe different
        # dimensions (how much capacity vs how hard to think). Both are
        # labelled in full so neither can be read as the other.
        facts.append({"key": "expected_consumption", "label": "Expected task usage", "value": _CONSUMPTION_LABELS[bucket]})
    if effort in _EFFORT_LABELS:
        facts.append({"key": "reasoning_effort", "label": "Reasoning level", "value": _EFFORT_LABELS[effort]})
    if model and model["known"]:
        facts.append({"key": "model", "label": "Model", "value": model["label"]})

    detail_rows: list[dict[str, str]] = [
        {"label": "How this was decided", "value": _routing_method(decision.get("decision_source"))},
        {
            "label": "Where the task runs",
            "value": "Stayed with Claude" if stay_with_host else f"Asked {chosen_label or 'Codex'}",
        },
    ]
    if model and not model["known"]:
        detail_rows.append({"label": "Model", "value": model["label"]})
    if effort in _EFFORT_LABELS:
        # The level is always shown as a primary fact. A rationale is added
        # here only when the payload already carries one -- a known level with
        # an unknown reason shows the level and stops.
        rationale = (target or {}).get("reasoning_effort_reason")
        if isinstance(rationale, str) and rationale.strip():
            detail_rows.append({"label": "Reasoning level", "value": rationale.strip()})
    required = _capability_phrase((score or {}).get("required_capabilities") or [])
    if required:
        detail_rows.append({"label": "This task needs to", "value": _title(required) or required})
    capabilities = capability_view(score)
    detail_rows.append({"label": "What this agent may do", "value": capabilities["summary"]})
    capacity = capacity_view(score)
    if score is not None:
        detail_rows.append({"label": "Capacity left", "value": capacity["label"]})
        detail_rows.append(
            {"label": "Capacity reading", "value": freshness_phrase(score.get("freshness"))}
        )
    objective = strategy_objective((score or {}).get("strategy"))
    if objective:
        detail_rows.append({"label": "Your preference", "value": objective})
    detail_rows.append(
        {
            "label": "Handoff",
            "value": "None — the task stays here"
            if stay_with_host
            else (f"To {chosen_label}" if chosen_label else "None — nothing was chosen"),
        }
    )
    # Lifecycle truth is NOT repeated as a row here: the disclosure already
    # opens with `lifecycle.note`, and saying it twice is how two copies of
    # one sentence start disagreeing.

    presentation: dict[str, Any] = {
        "schema_version": PRESENTATION_SCHEMA_VERSION,
        "surface": "route",
        "product": "ModelQuad",
        "invocation": _invocation(invocation_mode),
        "lifecycle": route_lifecycle(state),
        "flow": {
            "stay_with_host": stay_with_host,
            "nodes": [host, {"role": "hub", "key": "modelquad", "label": "ModelQuad", "known": True}, target_node],
        },
        "task": {"type": task_type, "label": task_label(task_type)},
        "selection": {
            "provider": recommendation,
            "label": chosen_label,
            "stay_with_host": stay_with_host,
            "model": model,
        },
        "headline": headline,
        "subline": " · ".join(subline_parts),
        "reasons": build_reasons(score, chosen_label),
        "facts": facts,
        # Capacity and capabilities are their own normalized blocks rather than
        # more prose in `facts`: a percentage, its freshness, and what an agent
        # is allowed to do are the three things a reader scans for, and each
        # has to be able to say "unknown" in its own right.
        "capacity": capacity,
        "capabilities": capabilities,
        "details": {"rows": detail_rows, "alternatives": _alternatives(decision, recommendation)},
    }
    if developer_mode:
        presentation["technical"] = build_technical_details(decision)
    return presentation


def execution_state_from_result(result: dict[str, Any]) -> str:
    """Map an execution payload onto the execution lifecycle, objectively.

    Timeout is a coarse overlay: a worker can time out whatever it last
    reported, so it is checked before the reported status.
    """

    if result.get("refused") or result.get("delegated") is False:
        return "refused"
    metadata = result.get("metadata")
    if isinstance(metadata, dict) and metadata.get("timed_out"):
        return "timeout"
    status = result.get("task_status")
    if status in EXECUTION_STATES:
        return status
    # UNKNOWN means the worker never produced a completion envelope. That is
    # not success, and it is not a crash either -- "failed" would overstate.
    return "blocked" if status == "unknown" else "failed"


def build_delegation_presentation(
    result: dict[str, Any],
    *,
    task_type: str | None = None,
    invocation_mode: str | None = None,
    host_name: str | None = None,
    host_runtime: str | None = None,
    developer_mode: bool = False,
) -> dict[str, Any]:
    """Derive the user-facing view of one delegation that actually ran.

    Linked to its routing decision by ``decision_id``: the Claude host cannot
    push a new result into an already-rendered MCP App instance, so the route
    widget and this one are two truthful surfaces joined by that id rather
    than one widget pretending to update.
    """

    state = execution_state_from_result(result)
    provider = result.get("provider")
    label = provider_label(provider)
    host = host_node(host_name, host_runtime)
    stayed = bool(provider) and provider == host.get("key")

    target_node = {
        "role": "target",
        "key": provider,
        "label": label or provider or "No tool",
        "known": bool(provider),
        "state": state,
    }

    reason = result.get("reason") or result.get("error")
    headline = {
        "completed": f"{label or 'Codex'} finished this task",
        "partial": f"{label or 'Codex'} finished part of this task",
        "blocked": f"{label or 'Codex'} could not proceed",
        "failed": f"{label or 'Codex'} did not finish this task",
        "timeout": f"{label or 'Codex'} did not return in time",
        "refused": "Codex was not started",
    }[state]

    facts: list[dict[str, str]] = []
    evidence = result.get("evidence")
    if isinstance(evidence, dict) and state != "refused":
        # "Verified" is a claim only the modelquad's own re-run can make;
        # a worker's self-report is recorded as exactly that. A call that
        # never started Codex has no report from it to label.
        facts.append(
            {
                "key": "evidence",
                "label": "Result",
                "value": "Checked by ModelQuad" if evidence.get("verified") else "Codex's own report (not checked by ModelQuad)",
            }
        )
    elif state in {"completed", "partial"}:
        facts.append({"key": "evidence", "label": "Result", "value": "Codex's own report (not checked by ModelQuad)"})

    detail_rows: list[dict[str, str]] = []
    if reason:
        detail_rows.append({"label": "What happened", "value": str(reason)})
    if result.get("safe_to_retry"):
        detail_rows.append({"label": "Can be retried", "value": "Yes"})

    presentation: dict[str, Any] = {
        "schema_version": PRESENTATION_SCHEMA_VERSION,
        "surface": "delegation",
        "product": "ModelQuad",
        "invocation": _invocation(invocation_mode),
        "lifecycle": execution_lifecycle(state),
        "flow": {
            "stay_with_host": stayed,
            "nodes": [host, {"role": "hub", "key": "modelquad", "label": "ModelQuad", "known": True}, target_node],
        },
        "task": {"type": task_type, "label": task_label(task_type)},
        "selection": {"provider": provider, "label": label, "stay_with_host": stayed, "model": None},
        "headline": headline,
        "subline": task_label(task_type),
        "reasons": [],
        "facts": facts,
        "details": {"rows": detail_rows, "alternatives": []},
    }
    if developer_mode:
        presentation["technical"] = build_technical_details(result)
    return presentation


# -------------------------------------------------------------- activity

#: What one row of the *recorded activity history* may say about itself.
#:
#: A third vocabulary alongside ROUTE_STATES and EXECUTION_STATES, because
#: history can express two things neither of them can:
#:
#: - ``running`` / ``no_response`` -- a delegation attempt that was written
#:   before anything could fail and then either has not come back yet, or
#:   never did. That open row IS the evidence (AGENTS.md, schema v12); it is
#:   never rounded up to "completed" and never left running forever.
#: - ``no_result`` -- a worker process that ended without ever producing a
#:   completion envelope. A clean exit is not success, so this is neither
#:   ``completed`` nor ``failed``.
ACTIVITY_STATES: frozenset[str] = frozenset(
    {
        "selected",
        "not_routed",
        "delegating",
        "running",
        # The caller timed out or went away while the worker kept running.
        "unattended",
        # The worker was stopped afterwards and the ending was confirmed.
        "stopped",
        "no_response",
        "refused",
        "completed",
        "partial",
        "blocked",
        "failed",
        "timeout",
        "no_result",
        "unknown",
    }
)

#: The task-status values a worker may report about itself. Anything outside
#: this set -- including NULL and the literal "unknown" -- means no completion
#: envelope arrived, which is ``no_result``, never a success.
_REPORTED_TASK_STATUSES: frozenset[str] = frozenset({"completed", "partial", "blocked", "failed"})

_ACTIVITY_NOTES: dict[str, str] = {
    "selected": "ModelQuad gave advice on which tool fits this task.",
    "not_routed": "No provider could take this task.",
    "delegating": "Codex has not started yet.",
    "running": "Codex is working on this now.",
    "unattended": (
        "Your request timed out, but Codex is still running. ModelQuad will "
        "record when it ends."
    ),
    "stopped": (
        "Codex was stopped after the request timed out, and ModelQuad "
        "confirmed the process ended."
    ),
    "no_response": (
        "This handoff started and never reported back, so ModelQuad cannot say "
        "whether the work happened."
    ),
    "refused": "ModelQuad did not start this Codex call.",
    "completed": "Codex reported the task done.",
    "partial": "Codex reported only part of the task done.",
    "blocked": "Codex could not proceed.",
    "failed": "The task did not succeed.",
    "timeout": "Codex did not return in time.",
    "no_result": (
        "Codex stopped without reporting a result, so ModelQuad cannot say "
        "whether the task was done."
    ),
    "unknown": "There is not enough recorded evidence to say what happened.",
}

#: Why ModelQuad declined to start a handoff, in the user's vocabulary. An
#: unrecognised kind produces no sentence at all rather than a raw token.
_REFUSAL_REASONS: dict[str, str] = {
    "policy_refused": "ModelQuad's rules or your settings did not allow this Codex call.",
    "duplicate_in_flight": "The same task was already running.",
    "no_viable_worker": "No provider could take this task.",
    "provider_unavailable": "The chosen provider was not available.",
    "failed_to_start": "Codex could not be started.",
    "invalid_request": "The request was not valid.",
    "orphaned": "Codex was gone, so ModelQuad closed this handoff.",
    "timeout": "Codex did not return in time.",
}


#: One plain sentence per refusal a reply can carry, keyed by the closed code
#: (or the fixed start of a policy reason). The code itself stays in the
#: structured fields; a person reads only the sentence. An unknown code gets
#: the general sentence, never the raw token.
_REFUSAL_LINES: dict[str, str] = {
    "duplicate_in_flight": "The same request is already running, so ModelQuad started nothing new.",
    "task_type_not_read_only": ("Codex through ModelQuad takes only read-only review, exploration "
                                "or architecture tasks."),
    "capsule_action_authority_rejected": "The request tried to approve itself, which ModelQuad does not allow. Ask again without it.",
    "launch_state_unknown": "ModelQuad could not read its own settings. Run: modelquad doctor",
    "routing is paused": ("Codex calls are paused in ModelQuad's saved settings, so Codex was not started. "
                          "No command in this version resumes them; please report it at "
                          "https://github.com/razbenaharon/modelquad/issues"),
    "host_unidentified": ("ModelQuad could not tell which app sent this call, and at least one app is "
                          "disconnected, so nothing started. Run: modelquad host status"),
    "host_disconnected": "Claude Code is disconnected in ModelQuad. Run: modelquad host status",
    "MODELQUAD_WORKER=1": "This session was started by ModelQuad for Codex, so it cannot ask Codex again.",
    "worker ancestry match": "This session was started by ModelQuad for Codex, so it cannot ask Codex again.",
    "user_repo_codex_off": ("You turned Codex off for this repository. To allow it again, run: "
                            "modelquad settings repo inherit"),
}
REFUSAL_FALLBACK = "ModelQuad's rules did not allow this Codex call."
#: A request that could not be used at all: one sentence that names the
#: problem. These are not rules that refused the call; nothing was started.
_INPUT_LINES: dict[str, str] = {
    "working_directory_missing": ("That working folder does not exist, so nothing was started. "
                                  "Pass the absolute path of an existing project folder."),
    "working_directory_not_a_folder": ("That working_directory is a file, not a folder, so nothing "
                                       "was started. Pass the project folder and name the file in files."),
    "model_not_valid": ("That model name is not a valid Codex model id, so nothing was started. "
                        "Leave model out to use Codex's default, or pass a model id Codex lists."),
    "reasoning_effort_not_valid": ("That reasoning_effort is not a value ModelQuad passes to Codex, so "
                                   "nothing was started. Leave it out to use Codex's default."),
    "prompt_empty": "The task text (prompt) is empty, so nothing was started. Say what Codex should do.",
    "invalid_request": ("The request could not be used (the working folder, the model or another "
                        "argument is not valid), so nothing was started."),
}


def refusal_line(payload: Any) -> str:
    """The one sentence a person reads about why Codex was not started.

    A fixed ``message`` the refusing gate wrote (the repository rules do)
    comes first; otherwise the sentence for the closed code. Never the code.
    """
    if not isinstance(payload, dict):
        return REFUSAL_FALLBACK
    message = payload.get("message")
    if isinstance(message, str) and message.strip() and payload.get("reason") != message:
        return message
    for reason in (payload.get("reason"), payload.get("task_status_reason")):
        if not isinstance(reason, str):
            continue
        if reason in _REFUSAL_LINES:
            return _REFUSAL_LINES[reason]
        for start, line in _REFUSAL_LINES.items():
            if reason.startswith(start):
                return line
        if reason in _INPUT_LINES:
            return _INPUT_LINES[reason]
        # What stopped the files or the task text from being sent, or the
        # Codex CLI from being used: the packer's own sentence for that cause.
        from ..execution import noshell

        if reason in noshell.REFUSAL_MESSAGES:
            return noshell.REFUSAL_MESSAGES[reason]
    return REFUSAL_FALLBACK


def failure_sentence(failure_kind: Any) -> str | None:
    """The user-facing sentence for one recorded ``failure_kind``, or None.

    The one reading of ``_REFUSAL_REASONS``, shared so the native Current
    Task surface and the MCP activity feed say the same thing about the same
    row instead of each keeping its own wording. An unrecognised kind -- and
    a row that has none -- produces no sentence rather than a raw token.

    The attempt's free-text ``detail`` is deliberately NOT a fallback here.
    It is written from sites that interpolate a live exception and it names
    pids and kill commands; it is a diagnostic, not a sentence for a user.
    """

    return _REFUSAL_REASONS.get(str(failure_kind or ""))


def _runtime_node(key: Any, *, unknown_label: str = "Unknown") -> dict[str, Any]:
    """One runtime, named or honestly unnamed.

    Never invents an identity: a key we were not given produces
    ``known: False`` and the word "Unknown", not a blank or a plausible guess
    (AGENTS.md safety invariant 4).
    """

    named = isinstance(key, str) and bool(key.strip())
    label = provider_label(key) if named else None
    return {
        "key": key if named else None,
        "label": label or unknown_label,
        "known": bool(label),
    }


def _handoff(entry: dict[str, Any]) -> dict[str, Any]:
    """Where the task came from, and what ran it -- as two records.

    ``origin`` and ``ran_on`` are never merged. For a handoff they are two
    runtimes; for a task kept in the calling session they are one runtime at
    two moments, and ``moved`` says which of those it was. ``moved`` is None
    -- not False -- when the origin could not be identified: "it did not
    move" and "we do not know whether it moved" are different facts, and the
    execution row already records that distinction for us as
    ``host_handoff.handoff_required``.
    """

    metadata = entry.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    recorded = metadata.get("host_handoff")
    recorded = recorded if isinstance(recorded, dict) else {}

    kind = entry.get("kind")
    origin_key = recorded.get("origin_runtime") or entry.get("host")
    if kind == "execution":
        ran_key = (
            recorded.get("target_runtime")
            or metadata.get("agent_runtime")
            or entry.get("agent_runtime")
            or entry.get("provider")
        )
    elif kind == "attempt":
        ran_key = entry.get("routed_provider") or entry.get("requested_provider")
    elif kind == "routing":
        # A routing row records a CHOICE. Nothing ran, so the node names what
        # was chosen and the caller's lifecycle state says it has not run.
        ran_key = entry.get("selected_provider") or entry.get("recommendation")
    else:
        ran_key = None

    moved = recorded.get("handoff_required")
    if not isinstance(moved, bool):
        moved = None
    if moved is None and metadata.get("host_outcome") is True:
        # A recorded host outcome is direct evidence that the task never left
        # the calling session -- not an inference from comparing two names.
        moved = False

    return {
        "origin": _runtime_node(origin_key),
        "ran_on": _runtime_node(ran_key) if ran_key else None,
        "moved": moved,
    }


def user_stop_sentence(
    stop_requested: bool, confirmed_dead: bool | None, before_start: bool = False,
    planner_ran: bool | None = False,
) -> str | None:
    """One stop explanation; planner_ran=None retains a stopped planner's fate."""
    if stop_requested is not True:
        return None
    if planner_ran is True or (planner_ran is None and confirmed_dead is True):
        return "You stopped this work before Codex answered. Nothing else started."
    if planner_ran is None:
        return (
            "You asked to stop this work, but ModelQuad could not confirm Codex ended. "
            "Nothing else started."
        )
    if before_start is True:
        return "You stopped this work before Codex started. Nothing ran."
    if confirmed_dead is True:
        return (
            "You stopped this work. ModelQuad confirmed Codex ended. "
            "Codex through ModelQuad cannot change your files."
        )
    return "You asked to stop this work, but ModelQuad could not confirm Codex ended."


def user_stop_facts(entry: dict[str, Any]) -> tuple[bool, bool | None, bool, bool | None]:
    """Recognize the core's stop marker; never render its diagnostic suffix."""
    if entry.get("kind") == "execution":
        metadata = entry.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        confirmed = metadata.get("worker_confirmed_dead")
        planner_ran = entry.get("task_status_reason") in (
            "stop_requested_after_planner_completed", "stop_requested_after_planner_ran",
        )
        # None retains a stopped planner's observed fate in both views.
        if (planner_ran and metadata.get("stop_requested") is True
                and entry.get("task_status_reason") != "stop_requested_after_planner_completed"):
            planner_ran = None
        return (
            metadata.get("stop_requested") is True or planner_ran is True,
            confirmed if type(confirmed) is bool else None,
            metadata.get("stopped_before_start") is True,
            planner_ran,
        )
    detail = entry.get("detail")
    planning_worker = False
    if isinstance(detail, str):
        for role in ("planning_worker", "plan_step_worker"):
            if detail.startswith(role + ":"):
                planning_worker = role == "planning_worker"
                detail = detail[len(role) + 1:].strip()
                break
    requested = (
        entry.get("resolved_at") is not None
        and isinstance(detail, str)
        and detail.split(";", 1)[0].strip() in ("stop_requested", "stop_requested_after_planner_completed", "stop_requested_after_planner_ran")
    )
    planner_ran = requested and (
        detail.split(";", 1)[0].strip() in (
            "stop_requested_after_planner_completed", "stop_requested_after_planner_ran",
        )
    )
    if planner_ran and entry.get("failure_kind") != "failed_to_start" and (
        detail.split(";", 1)[0].strip() == "stop_requested_after_planner_ran"
    ):
        planner_ran = None
    # A planner's own stop marker does not say what the outer delegation did.
    # Read its recorded outer execution when the caller supplies that context.
    execution = entry.get("execution")
    if (requested and planning_worker and isinstance(execution, dict)
            and execution.get("task_status_reason") in (
                "stop_requested_after_planner_completed", "stop_requested_after_planner_ran",
            )):
        return user_stop_facts({**execution, "kind": "execution"})
    return (
        requested, entry.get("failure_kind") == "terminated" if requested else None,
        requested and entry.get("failure_kind") == "failed_to_start" and planner_ran is False,
        planner_ran,
    )


def activity_state_from_execution(entry: dict[str, Any]) -> str:
    """Map a recorded execution row onto the activity vocabulary.

    Reads bounded stop intent/fate before ``result`` and ``task_status``. In
    particular it never reads ``success`` or ``exit_code``: those are
    process-channel facts, and presenting a clean exit as a finished task is
    the exact claim the evidence chain exists to refuse.
    """

    requested, confirmed, before_start, planner_ran = user_stop_facts({**entry, "kind": "execution"})
    if requested:
        if before_start or planner_ran:
            return "failed"
        return "stopped" if confirmed is True else "unknown"
    if entry.get("result") == "timeout":
        # A worker can time out whatever it last reported, so the coarse
        # result overlay is checked before the reported status.
        return "timeout"
    status = entry.get("task_status")
    if isinstance(status, str) and status in _REPORTED_TASK_STATUSES:
        return status
    return "no_result"


def activity_state_from_attempt(entry: dict[str, Any]) -> str:
    """Map an open (or refused) delegation attempt onto the activity vocabulary.

    ``liveness`` is derived upstream by
    ``service.history.annotate_attempt_liveness`` from the recorded worker
    pid, or from the row's age when no pid was recorded. An attempt whose
    liveness could not be derived at all reads ``unknown`` rather than being
    guessed into either direction.

    Two facts recorded beside the worker's are read here too, and only
    through the closed vocabularies that carry them:
    ``caller_abandoned`` -- the caller stopped waiting -- separates
    ``unattended`` from ``running``, and ``failure_kind`` separates a
    confirmed ``stopped`` from a stop that was only attempted. The row's
    free-text ``detail`` is never shown; only its exact stop-intent marker
    is recognized by ``user_stop_facts``.
    """

    requested, confirmed, before_start, planner_ran = user_stop_facts(entry)
    if requested:
        if before_start or planner_ran:
            return "failed"
        return "stopped" if confirmed is True else "unknown"
    if entry.get("status") == "refused":
        return "refused"
    if entry.get("liveness") == "abandoned":
        # An attempt nothing came back to keeps its own distinct history
        # state: "never reported back" says more than a bare unknown.
        return "no_response"

    from .livelifecycle import derive_live_state, facts_from_attempt

    # `liveness` alone cannot separate "dispatching" from "a worker is
    # genuinely executing": it reports in_flight for a pid-less row purely
    # because that row is young. Running therefore requires a REAL pid probe,
    # and everything short of one is honestly Delegating.
    pid = entry.get("worker_pid")
    worker_alive: bool | None = None
    if pid:
        try:
            from ..procutil import pid_alive

            worker_alive = pid_alive(int(pid))
        except Exception:  # noqa: BLE001 -- an unprobeable pid is UNKNOWN, not dead
            worker_alive = None

    state = derive_live_state(
        facts_from_attempt(entry, worker_alive=worker_alive)
    ).state
    return state if state in ACTIVITY_STATES else "unknown"


def activity_state(entry: dict[str, Any]) -> str:
    """The activity state of any recorded row, by kind."""

    kind = entry.get("kind")
    if kind == "execution":
        return activity_state_from_execution(entry)
    if kind == "attempt":
        return activity_state_from_attempt(entry)
    if kind == "routing":
        # Only the selection says something was chosen. A row that records
        # nothing selected beside a recommendation is a delegation that
        # reached no executor, and the recommendation is advice, not a choice.
        # Every row that did select records it: the writer has
        # filled `selected_provider` from the recommendation since the column
        # was added, unless told nothing was selected.
        return "selected" if entry.get("selected_provider") else "not_routed"
    return "unknown"


def _activity_headline(state: str, where: dict[str, Any]) -> str:
    ran = where.get("ran_on") or {}
    named = ran.get("label") if ran.get("known") else None
    worker = named or "Codex"
    return {
        "selected": f"Chose {named}" if named else "Chose where to run this",
        "not_routed": "No provider could take this",
        "delegating": f"Handing this to {named}" if named else "Handing this over",
        "running": f"Running on {named}" if named else "Running now",
        "unattended": f"{worker} is still running this",
        "stopped": f"{worker} was stopped",
        "no_response": f"{worker} never reported back",
        "refused": "Codex was not started",
        "completed": f"{worker} finished this task",
        "partial": f"{worker} finished part of this task",
        "blocked": f"{worker} could not proceed",
        "failed": f"{worker} did not finish this task",
        "timeout": f"{worker} did not return in time",
        "no_result": f"{worker} stopped without reporting a result",
        "unknown": "Not enough recorded evidence to say",
    }[state]


def _outcome_basis(entry: dict[str, Any], state: str) -> str | None:
    """Who says the task is done -- and it is never ModelQuad by default.

    "Checked by ModelQuad" is a claim only the modelquad's own objective
    re-run earns (``OutcomeEvidence.verified``). Everything else, including a
    row carrying no outcome evidence at all, is the worker's own account and
    is labelled as exactly that.
    """

    if state not in {"completed", "partial"}:
        return None
    return "Checked by ModelQuad" if entry.get("outcome_verified") is True else "Codex's own report (not checked by ModelQuad)"


def _activity_why(entry: dict[str, Any], decision_sources: dict[str, str] | None) -> str | None:
    """How this decision was made, in words -- or nothing when it was not recorded.

    A row that predates ``decision_source`` genuinely does not record whether
    a person or the router chose, so no sentence is produced. Saying
    "Advice (task fit)" there would be the UI asserting something the
    evidence does not carry.
    """

    if entry.get("kind") == "routing" and entry.get("overridden"):
        return _routing_method("user_override")
    source = entry.get("decision_source")
    if not source:
        # An execution or attempt row carries no decision_source of its own;
        # the routing row that caused it does, and one decision_id joins
        # them. This is a lookup over recorded rows, never a reconstruction.
        decision_id = entry.get("decision_id")
        if decision_id and decision_sources:
            source = decision_sources.get(decision_id)
    if not source:
        return None
    return _routing_method(source)


def _activity_facts(entry: dict[str, Any], state: str) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []

    basis = _outcome_basis(entry, state)
    if basis:
        facts.append({"key": "result", "label": "Result", "value": basis})

    if entry.get("kind") == "execution" and entry.get("fallback") and entry.get("fallback_from"):
        first = provider_label(entry.get("fallback_from"))
        if first:
            facts.append({"key": "fallback", "label": "First tried", "value": first})

    if state == "unattended":
        # The one thing this state adds beyond "Running": nobody is on the
        # other end of it. Said as a fact, not as an alarm.
        facts.append(
            {"key": "waiting", "label": "Waiting", "value": "Nothing is waiting for the result"}
        )

    if state == "unknown" and entry.get("failure_kind") == "timeout":
        # A stop was attempted and never confirmed (that case stays
        # `timeout`). The row's free-text `detail` carries the pid and
        # the kill command and is NEVER echoed; the closed-vocabulary kind is
        # all that is read, and this sentence is all that is said.
        facts.append(
            {
                "key": "worker_fate",
                "label": "Codex",
                "value": "ModelQuad could not confirm Codex stopped",
            }
        )

    if state == "refused":
        sentence = failure_sentence(entry.get("failure_kind"))
        if sentence:
            facts.append({"key": "refusal", "label": "Why not", "value": sentence})

    if entry.get("kind") == "routing" and entry.get("overridden") and entry.get("override_reason"):
        facts.append({"key": "override", "label": "You said", "value": str(entry["override_reason"])})

    duration = entry.get("duration_seconds")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        facts.append({"key": "duration", "label": "Took", "value": f"{round(float(duration), 1):g}s"})

    return facts


def build_activity_entry(
    entry: dict[str, Any],
    *,
    decision_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Derive the user-facing view of one recorded activity row.

    ``entry`` is a row as ``service.history.fetch_activity`` (kinds
    ``routing`` / ``execution``) or ``fetch_open_delegation_attempts`` (kind
    ``attempt``) produced it. It is read, never modified, and nothing that
    identifies the storage -- ``decision_id``, ``source``, ``task_id``,
    ``exit_code``, raw status tokens, schema words -- is carried across.
    """

    state = activity_state(entry)
    where = _handoff(entry)
    return {
        "kind": entry.get("kind"),
        "id": entry.get("id"),
        "created_at": entry.get("created_at"),
        "state": state,
        "label": _STATE_LABELS[state],
        "tone": _STATE_TONES[state],
        "note": user_stop_sentence(*user_stop_facts(entry)) or _ACTIVITY_NOTES[state],
        "headline": _activity_headline(state, where),
        "task": {"type": entry.get("task_type"), "label": task_label(entry.get("task_type"))},
        "where": where,
        "why": _activity_why(entry, decision_sources),
        "facts": _activity_facts(entry, state),
    }


def build_activity_feed(
    entries: Any = (),
    *,
    attempts: Any = (),
    decision_sources: dict[str, str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """The whole recent-activity list, in plain language.

    ``attempts`` carries the delegation attempts that will never produce an
    execution row -- one still open, or one refused before it began. They are
    merged into the same time-ordered list rather than shown apart, because
    "started and never came back" belongs beside the runs that did come back;
    hiding it because it is untidy is itself a failure.
    """

    rows = [
        build_activity_entry(row, decision_sources=decision_sources)
        for row in (*(entries or ()), *(attempts or ()))
        if isinstance(row, dict)
    ]
    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    if limit is not None:
        rows = rows[: max(0, int(limit))]
    return {
        "schema_version": PRESENTATION_SCHEMA_VERSION,
        "surface": "activity",
        "entries": rows,
        # Empty states invite action rather than reporting an absence.
        "empty": "No activity yet — ModelQuad will record what it decides and what runs as you work.",
    }


# ------------------------------------------------------ developer details


def build_technical_details(decision: dict[str, Any]) -> dict[str, Any]:
    """The complete routing internals, for developer mode only.

    Every value is read straight off the canonical object -- this is a view
    of the decision, not a second copy of it, so it cannot drift from what
    the router actually produced. Numbers keep full precision here on
    purpose: diagnostics and tests rely on the raw value.
    """

    rows: list[dict[str, Any]] = []
    for key in ("decision_id", "decision_source", "task_type", "recommendation", "reason"):
        if key in decision:
            rows.append({"label": key, "value": decision[key]})
    target = decision.get("target")
    if isinstance(target, dict):
        for key, value in target.items():
            rows.append({"label": f"target.{key}", "value": value})
    consumption = decision.get("expected_consumption")
    if isinstance(consumption, dict):
        for key, value in consumption.items():
            rows.append({"label": f"expected_consumption.{key}", "value": value})
    return {
        "rows": rows,
        "candidates": [score for score in (decision.get("providers") or []) if isinstance(score, dict)],
    }


# ------------------------------------------------------------ text fallback


def render_presentation_text(presentation: dict[str, Any]) -> str:
    """The concise fallback for hosts with no MCP Apps support.

    Two or three lines that answer the question a person actually has. It is
    deliberately NOT a serialization of the decision: the canonical object
    travels in ``structuredContent``, where a machine reads it, and a
    multi-page JSON dump is never the human-facing text.
    """

    lines = [f"{presentation['product']}: {presentation['headline']}."]
    lifecycle = presentation.get("lifecycle") or {}
    task = (presentation.get("task") or {}).get("label")
    # Deliberately NOT the subline: the facts line below already carries
    # expected usage, and repeating it is the kind of noise an automatic
    # invocation cannot afford.
    detail = f"{task} task." if task else ""
    if lifecycle.get("note"):
        detail = f"{detail} {lifecycle['note']}".strip()
    if detail:
        lines.append(detail)
    reasons = [reason["text"] for reason in presentation.get("reasons") or []]
    if reasons:
        lines.append("Why: " + "; ".join(reasons) + ".")
    facts = presentation.get("facts") or []
    if facts:
        lines.append(" · ".join(f"{fact['label']}: {fact['value']}" for fact in facts))
    return "\n".join(lines)
