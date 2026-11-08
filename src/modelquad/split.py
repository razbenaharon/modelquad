# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A proposed division of a task of several steps between Claude and Codex.

``modelquad_split`` hands this module up to 12 steps, each a short title, a
task kind, what the step needs (``edit``, ``run`` or ``none``), whether its
value is an opinion independent of Claude's own work, and its size (how much
reading and thinking it needs). For each step it returns an owner and one
closed reason code:

* ``claude_only``: Codex cannot take this step here. A step that edits files
  or runs commands, or is not a review, exploration or architecture task, is
  Claude's: Codex through ModelQuad is read-only and tool-less (invariant 8).
  The user's ``modelquad settings repo off``, the repository's
  ``.modelquad.toml`` and a missing Codex also land here.
* ``codex``: proposed for Codex, read-only, and ``asks_user`` is True: each
  such step is its own ``modelquad_delegate`` call with its own approval
  prompt. Nothing here approves or starts any of them.
* ``claude``: Codex could take it, and the task fit says Claude.
* ``codex`` with ``claude_near_limit``: a step the task fit gave Claude,
  proposed for Codex because of the two quota readings (below).

Task fit decides first (``Router.route`` with the step's capabilities, and
Claude excluded for an independent opinion). Only whether each tool is there
at all reaches the router: no percentage, window, reset or plan.

Then one quota rule, :func:`quota_lean`, may move a step from Claude to
Codex. It applies only to a step that is already a candidate for Codex in
every other way (reading and thinking only, a review, exploration or
architecture kind, nothing refusing Codex, Codex available) and that is
medium or large: a small step is not worth a separate Codex call. And only
when all of these readings hold at once:

* Claude's own reading is fresh and at or over the near-limit threshold of
  ``assistance.HANDOFF_USED_PERCENT`` (80% of the 5-hour window, or 90% of
  the weekly window);
* Codex's 5-hour and weekly readings are both known, fresh and under
  ``assistance.HANDOFF_CODEX_EXHAUSTED_USED`` (90% used), and Codex reports
  no limit reached.

A missing, old or unknown reading on either side moves nothing: unknown is
not room (invariant 4). A step that edits files or runs commands never
moves; Claude stays the writer. A moved step is still its own
``modelquad_delegate`` call with its own approval (or the user's standing
grant), and the reply asks Claude to pass the trigger ``split_quota``, so the
review ledger records why that call was made and ``modelquad review report``
and the split summary count those calls. Nothing here claims that a move
used less of anything; the summary reports only what was measured.

The plan kind words, the quota windows and one factual caution when Codex
reports a limit reached are returned beside the owners as facts for the
person; a plan never moves an owner.

Pure: no process is started, nothing is stored or logged, and the titles
leave only in the reply, after the privacy module's context sanitizer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from . import assistance, plans, repo_policy
from .models import ProviderStatus, TaskType
from .repo_settings import ON_REQUEST, USER_CODEX_OFF

MAX_STEPS = 12
MAX_TITLE_CHARS = 120
NEEDS = ("edit", "run", "none")
TASK_TYPES = tuple(item.value for item in TaskType)
#: The kinds Codex takes through ModelQuad (``execution.base.READ_ONLY_TASK_TYPES``;
#: a test checks they agree).
READ_ONLY_KINDS = ("review", "exploration", "architecture")
_CAPABILITIES = {"edit": ("write",), "run": ("command",), "none": None}

CLAUDE, CODEX, CLAUDE_ONLY = "claude", "codex", "claude_only"
OWNERS = (CLAUDE, CODEX, CLAUDE_ONLY)

EDITS_OR_COMMANDS = "edits_or_commands"
KIND_NOT_READ_ONLY = "kind_not_read_only"
INDEPENDENT_REVIEW = "independent_review"
SECOND_OPINION = "second_opinion"
TASK_FIT = "task_fit"
CODEX_UNAVAILABLE = "codex_unavailable"
#: ModelQuad's own launch policy refuses every Codex call now (paused, or
#: Claude Code disconnected with ``modelquad host``).
CODEX_PAUSED = "codex_paused"
#: A step the task fit gave Claude, proposed for Codex because Claude's fresh
#: reading is near its limit and Codex's fresh readings show room (:func:`quota_lean`).
CLAUDE_NEAR_LIMIT = "claude_near_limit"
#: Every reason a step can carry. The account rule of a repository file is
#: checked when a Codex step runs, not here: it depends on the plan Codex
#: reports, and a plan never moves an owner.
REASONS = frozenset({
    EDITS_OR_COMMANDS, KIND_NOT_READ_ONLY, INDEPENDENT_REVIEW, SECOND_OPINION, TASK_FIT,
    CODEX_UNAVAILABLE, CODEX_PAUSED, USER_CODEX_OFF, repo_policy.CODEX_OFF, repo_policy.KIND_REMOVED,
    repo_policy.INVALID, repo_policy.UNREADABLE, CLAUDE_NEAR_LIMIT,
})

#: How much reading and thinking a step needs. A small step is not worth a
#: separate Codex call, so quota never moves one.
SIZES = ("small", "medium", "large")
LEAN_SIZES = frozenset({"medium", "large"})
#: The ``modelquad_delegate`` trigger for a step moved by :func:`quota_lean`
#: (``review_ledger.TRIGGERS``), so the ledger can count those calls.
QUOTA_TRIGGER = "split_quota"

APPROVAL = "Each Codex step shows its own approval prompt when it runs."

_WORDS = {
    EDITS_OR_COMMANDS: "edits or commands",
    KIND_NOT_READ_ONLY: "not a review, exploration or architecture task",
    TASK_FIT: "task fit",
    INDEPENDENT_REVIEW: "independent review",
    SECOND_OPINION: "second opinion",
    CODEX_UNAVAILABLE: "Codex is not available",
    CODEX_PAUSED: "Codex calls are paused in ModelQuad (modelquad host status)",
    CLAUDE_NEAR_LIMIT: "Claude near its limit, Codex has room",
    USER_CODEX_OFF: "you turned Codex off for this repository",
    repo_policy.CODEX_OFF: f"this repository's {repo_policy.FILE_NAME} turns Codex off",
    repo_policy.KIND_REMOVED: f"this repository's {repo_policy.FILE_NAME} does not allow that kind",
    repo_policy.INVALID: f"this repository's {repo_policy.FILE_NAME} needs fixing",
    repo_policy.UNREADABLE: f"this repository's {repo_policy.FILE_NAME} needs fixing",
}
_LIMIT_NAMES = {"five_hour": "5-hour", "seven_day": "weekly"}


@dataclass(frozen=True)
class Step:
    """One step as the tool received it; ``title`` is already sanitized (or None)."""

    title: str | None
    task_type: str
    needs: str
    independent: bool = False
    size: str = "medium"


def clean_title(title) -> str | None:
    """The title as the reply may carry it, or None when the sanitizer refuses it."""
    from .privacy import ContextTextRefused, sanitize_context_text

    if type(title) is not str:
        return None
    text = " ".join(title.split())[:MAX_TITLE_CHARS]
    try:
        return sanitize_context_text(text)
    except ContextTextRefused:
        return None


def router_statuses(statuses: Iterable[ProviderStatus]) -> list[ProviderStatus]:
    """What the router sees: only whether Codex is there. Claude is the caller.

    No percentage, window, reset, freshness or plan is copied: the task fit is
    decided without quota. Quota acts only afterwards, through :func:`quota_lean`.
    """
    codex = next((status for status in statuses if status.name == "codex"), None)
    seen = [ProviderStatus(name="claude", available=True)]
    if codex is not None:
        seen.append(ProviderStatus(name="codex", available=codex.available))
    return seen


def owner_for(step: Step, router, statuses, *, refusal: str | None, policy) -> tuple[str, str]:
    """The owner and the closed reason for one step."""
    read_only = step.task_type in READ_ONLY_KINDS
    excluded = (CLAUDE,) if step.independent and read_only else ()
    result = router.route(step.task_type, statuses,
                          required_capabilities=_CAPABILITIES[step.needs],
                          independence_excluded=excluded)
    # The hard gate, whatever the router says: Codex never edits or runs.
    if step.needs != "none":
        return CLAUDE_ONLY, EDITS_OR_COMMANDS
    if not read_only:
        return CLAUDE_ONLY, KIND_NOT_READ_ONLY
    if refusal is not None:
        return CLAUDE_ONLY, refusal
    if policy is not None:
        code = policy.refusal_for(step.task_type, check_account=False)
        if code is not None:
            return CLAUDE_ONLY, code
    codex = next((item for item in result.providers if item.provider == CODEX), None)
    if codex is None or not codex.eligible or result.recommendation is None:
        return CLAUDE_ONLY, CODEX_UNAVAILABLE
    if result.recommendation == CODEX:
        if step.independent:
            return CODEX, INDEPENDENT_REVIEW if step.task_type == "review" else SECOND_OPINION
        return CODEX, TASK_FIT
    return CLAUDE, TASK_FIT


def _current(window, now) -> bool:
    """A known window, observed recently, that has not reset since."""
    return (not window.get("stale") and not window.get("unknown")
            and assistance._readable(window, now))


def quota_lean(statuses, thresholds, now: datetime) -> dict | None:
    """The readings that let a medium or large read-only step move to Codex, or None.

    All of these must hold: Claude's fresh 5-hour or weekly reading is at or
    over its ``HANDOFF_USED_PERCENT``; Codex's 5-hour and weekly readings are
    both known, fresh and under ``HANDOFF_CODEX_EXHAUSTED_USED``; Codex
    reports no limit reached. Anything missing, old or unknown returns None.
    """
    by_name = {status.name: status for status in statuses}
    claude, codex = by_name.get(CLAUDE), by_name.get(CODEX)
    if claude is None or codex is None or not codex.available:
        return None
    limits = assistance.HANDOFF_USED_PERCENT
    over = [window for window in assistance.quota_windows(claude, thresholds, now=now)
            if window["window"] in limits and _current(window, now)
            and assistance._used(window) >= limits[window["window"]]]
    if not over:
        return None
    codex_windows = {window["window"]: window
                     for window in assistance.quota_windows(codex, thresholds, now=now)
                     if window["window"] in assistance.WINDOW_MINUTES}
    if set(codex_windows) != set(assistance.WINDOW_MINUTES):
        return None
    if not all(_current(window, now)
               and assistance._used(window) < assistance.HANDOFF_CODEX_EXHAUSTED_USED
               for window in codex_windows.values()):
        return None
    if codex_caution(codex, thresholds, now) is not None:
        return None
    claude_window = max(over, key=lambda window: (assistance._used(window),
                                                  window["window"] == "five_hour"))
    return {
        "claude_window": assistance._window_view(claude_window),
        "claude_threshold_used_percent": limits[claude_window["window"]],
        "codex_windows": [assistance._window_view(codex_windows[name])
                          for name in assistance.WINDOW_MINUTES],
        "codex_under_used_percent": assistance.HANDOFF_CODEX_EXHAUSTED_USED,
        "sizes": sorted(LEAN_SIZES),
        "trigger": QUOTA_TRIGGER,
        "line": (f"Claude has used {assistance._window_phrase(claude_window, now)}; "
                 "Codex has room."),
    }


def leaned(step: Step, owner: str, reason: str, lean: dict | None) -> tuple[str, str]:
    """The owner after the quota rule: only a Claude task-fit step that Codex could take moves."""
    if (lean is not None and owner == CLAUDE and reason == TASK_FIT and step.needs == "none"
            and step.task_type in READ_ONLY_KINDS and step.size in LEAN_SIZES):
        return CODEX, CLAUDE_NEAR_LIMIT
    return owner, reason


def codex_caution(status: ProviderStatus | None, thresholds, now: datetime) -> str | None:
    """One factual line when Codex reports a limit reached, or a current window is full."""
    if status is None:
        return None
    current = [window for window in assistance.quota_windows(status, thresholds, now=now)
               if assistance._readable(window, now) and not window.get("stale")]
    full = [window for window in current if assistance._used(window) >= 100.0]
    metadata = status.metadata if isinstance(status.metadata, Mapping) else {}
    reached = metadata.get("rate_limit_reached_type")
    reported = type(reached) is str and bool(reached.strip()) and bool(current)
    if not full and not reported:
        return None
    if full:
        window = max(full, key=lambda item: (assistance._moment(item.get("resets_at")) or now))
        name = _LIMIT_NAMES.get(window["window"])
        what = f"its {name} limit reached" if name else "a limit reached"
        resets = assistance.local_clock(window.get("resets_at"), now)
        until = f" until {resets}" if resets else ""
    else:
        what, until = "a limit reached", ""
    return f"Codex reports {what}{until}; a Codex step would likely be refused."


def facts(statuses, thresholds, now: datetime, plan_views, policy, offer: str,
          lean: dict | None = None, moved: bool = False) -> dict:
    """The facts shown beside the owners. ``changes_owners`` is True only when quota moved a step."""
    by_name = {status.name: status for status in statuses}
    quota = {name: (assistance.quota_windows(by_name[name], thresholds, now=now) if name in by_name else [])
             for name in (CLAUDE, CODEX)}
    return {
        "quota": quota,
        "plans": plans.public_plans(plan_views),
        "caution": codex_caution(by_name.get(CODEX), thresholds, now),
        "repository": {
            "offer_mode": offer,
            "codex_account_rule": policy.account if policy is not None else "any",
        },
        "quota_lean": ({key: value for key, value in lean.items() if key != "line"}
                       if moved and lean is not None else None),
        "changes_owners": moved,
    }


def _numbers(indexes: list[int]) -> str:
    label = "step" if len(indexes) == 1 else "steps"
    return f"{label} {', '.join(str(index) for index in indexes)}"


def _reasons(rows: list[dict]) -> str:
    seen = []
    for row in rows:
        words = _WORDS[row["reason"]]
        if words not in seen:
            seen.append(words)
    return "; ".join(seen)


def content_lines(rows: list[dict], fact_block: dict, *, quota_line: str, account_line: str | None,
                  lean_line: str | None = None) -> list[str]:
    """The 3 to 8 lines the person reads.

    The line about a step quota moved and the caution never appear together:
    a move needs Codex to report no limit reached, and the caution says one is.
    """
    claude = [row for row in rows if row["owner"] != CODEX]
    codex = [row for row in rows if row["owner"] == CODEX]
    count = len(rows)
    lines = [f"Proposed split of {count} step{'s' if count != 1 else ''}; nothing has started."]
    if claude:
        lines.append(f"Claude: {_numbers([row['step'] for row in claude])} ({_reasons(claude)}).")
    if codex:
        lines.append(f"Codex (read-only): {_numbers([row['step'] for row in codex])} "
                     f"({_reasons(codex)}). {APPROVAL}")
        moved = [row["step"] for row in codex if row["reason"] == CLAUDE_NEAR_LIMIT]
        if moved and lean_line:
            lines.append(f"{lean_line} Proposed for Codex (read-only): {_numbers(moved)}. "
                         f"Claude passes trigger {QUOTA_TRIGGER} when it asks Codex, so the "
                         "review report counts these calls.")
        repository = fact_block["repository"]
        if repository["offer_mode"] == ON_REQUEST:
            lines.append("In this repository, Claude calls Codex only when you ask.")
        if repository["codex_account_rule"] == "workspace":
            lines.append(f"This repository's {repo_policy.FILE_NAME} allows Codex only on a work "
                         "account; ModelQuad checks that when a Codex step runs.")
    else:
        lines.append("No step is proposed for Codex.")
    if fact_block["caution"]:
        lines.append(fact_block["caution"])
    if account_line:
        lines.append(account_line)
    lines.append(f"Quota now: {quota_line}")
    return lines


def propose(steps: Iterable[Step], *, router, statuses, thresholds, now: datetime,
            plan_views, policy, user_mode: str | None, global_proactive: bool,
            codex_paused: bool = False) -> dict:
    """The whole reply: owners, facts, ``launches`` False and the human lines.

    ``policy`` is the repository file's ``RepoPolicy`` or None; ``user_mode``
    the user's per-repo word. ``codex_paused``: ModelQuad's own launch policy
    refuses every Codex call now, so no step goes to Codex. Neither the
    statuses' quota nor the plans reach :func:`owner_for`; quota acts only
    through :func:`quota_lean`, after it, and a plan never does.
    """
    from .repo_settings import effective

    steps = list(steps)
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"Give between 1 and {MAX_STEPS} steps.")
    statuses = list(statuses)
    applied = effective(global_proactive=global_proactive, user_mode=user_mode, policy=policy)
    refusal = applied.refusal or (CODEX_PAUSED if codex_paused else None)
    seen = router_statuses(statuses)
    try:
        lean = quota_lean(statuses, thresholds, now)
    except Exception:  # noqa: BLE001 -- a reading that cannot be read moves nothing
        lean = None
    rows = []
    for index, step in enumerate(steps, start=1):
        owner, reason = owner_for(step, router, seen, refusal=refusal, policy=policy)
        owner, reason = leaned(step, owner, reason, lean)
        rows.append({"step": index, "title": step.title, "task_type": step.task_type,
                     "needs": step.needs, "independent": step.independent, "size": step.size,
                     "owner": owner, "reason": reason, "asks_user": owner == CODEX,
                     "trigger": QUOTA_TRIGGER if reason == CLAUDE_NEAR_LIMIT else None})
    moved = any(row["reason"] == CLAUDE_NEAR_LIMIT for row in rows)
    fact_block = facts(statuses, thresholds, now, plan_views, policy, applied.offer,
                       lean=lean, moved=moved)
    try:
        quota_line = assistance.statusline_text(statuses, thresholds, now=now)
    except Exception:  # noqa: BLE001 -- a reading that cannot be shown reads unknown
        quota_line = "unknown."
    account = plans.account_line(plan_views) if plans.accounts_differ(plan_views) else None
    lines = content_lines(rows, fact_block, quota_line=quota_line, account_line=account,
                          lean_line=lean["line"] if moved and lean is not None else None)
    return {
        "steps": rows,
        "launches": False,
        "approval": APPROVAL,
        "facts": fact_block,
        "content": "\n".join(lines),
    }

