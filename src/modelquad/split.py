# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""A proposed division of a task of several steps between Claude and Codex.

``modelquad_split`` hands this module up to 12 steps, each a short title, a
task kind, what the step needs (``edit``, ``run`` or ``none``) and whether its
value is an opinion independent of Claude's own work. For each step it
returns an owner and one closed reason code:

* ``claude_only``: Codex cannot take this step here. A step that edits files
  or runs commands, or is not a review, exploration or architecture task, is
  Claude's: Codex through ModelQuad is read-only and tool-less (invariant 8).
  The user's ``modelquad settings repo off``, the repository's
  ``.modelquad.toml`` and a missing Codex also land here.
* ``codex``: proposed for Codex, read-only, and ``asks_user`` is True: each
  such step is its own ``modelquad_delegate`` call with its own approval
  prompt. Nothing here approves or starts any of them.
* ``claude``: Codex could take it, and the task fit says Claude.

Task fit decides (``Router.route`` with the step's capabilities, and Claude
excluded for an independent opinion). Only whether each tool is there at all
reaches the router: no percentage, window, reset or plan, so no quota or plan
reading can move an owner. The quota windows, the plan kind words and one
factual caution when Codex reports a limit reached are returned beside the
owners as facts for the person.

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
#: Every reason a step can carry. The account rule of a repository file is
#: checked when a Codex step runs, not here: it depends on the plan Codex
#: reports, and a plan never moves an owner.
REASONS = frozenset({
    EDITS_OR_COMMANDS, KIND_NOT_READ_ONLY, INDEPENDENT_REVIEW, SECOND_OPINION, TASK_FIT,
    CODEX_UNAVAILABLE, CODEX_PAUSED, USER_CODEX_OFF, repo_policy.CODEX_OFF, repo_policy.KIND_REMOVED,
    repo_policy.INVALID, repo_policy.UNREADABLE,
})

APPROVAL = "Each Codex step shows its own approval prompt when it runs."

_WORDS = {
    EDITS_OR_COMMANDS: "edits or commands",
    KIND_NOT_READ_ONLY: "not a review, exploration or architecture task",
    TASK_FIT: "task fit",
    INDEPENDENT_REVIEW: "independent review",
    SECOND_OPINION: "second opinion",
    CODEX_UNAVAILABLE: "Codex is not available",
    CODEX_PAUSED: "Codex calls are paused in ModelQuad (modelquad host status)",
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

    No percentage, window, reset, freshness or plan is copied, so no reading
    can change an owner.
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


def facts(statuses, thresholds, now: datetime, plan_views, policy, offer: str) -> dict:
    """The facts shown beside the owners. They never change an owner."""
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
        "changes_owners": False,
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


def content_lines(rows: list[dict], fact_block: dict, *, quota_line: str, account_line: str | None) -> list[str]:
    """The 3 to 8 lines the person reads."""
    claude = [row for row in rows if row["owner"] != CODEX]
    codex = [row for row in rows if row["owner"] == CODEX]
    count = len(rows)
    lines = [f"Proposed split of {count} step{'s' if count != 1 else ''}; nothing has started."]
    if claude:
        lines.append(f"Claude: {_numbers([row['step'] for row in claude])} ({_reasons(claude)}).")
    if codex:
        lines.append(f"Codex (read-only): {_numbers([row['step'] for row in codex])} "
                     f"({_reasons(codex)}). {APPROVAL}")
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
    statuses' quota nor the plans reach :func:`owner_for`.
    """
    from .repo_settings import effective

    steps = list(steps)
    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f"Give between 1 and {MAX_STEPS} steps.")
    statuses = list(statuses)
    applied = effective(global_proactive=global_proactive, user_mode=user_mode, policy=policy)
    refusal = applied.refusal or (CODEX_PAUSED if codex_paused else None)
    seen = router_statuses(statuses)
    rows = []
    for index, step in enumerate(steps, start=1):
        owner, reason = owner_for(step, router, seen, refusal=refusal, policy=policy)
        rows.append({"step": index, "title": step.title, "task_type": step.task_type,
                     "needs": step.needs, "independent": step.independent,
                     "owner": owner, "reason": reason, "asks_user": owner == CODEX})
    fact_block = facts(statuses, thresholds, now, plan_views, policy, applied.offer)
    try:
        quota_line = assistance.statusline_text(statuses, thresholds, now=now)
    except Exception:  # noqa: BLE001 -- a reading that cannot be shown reads unknown
        quota_line = "unknown."
    account = plans.account_line(plan_views) if plans.accounts_differ(plan_views) else None
    lines = content_lines(rows, fact_block, quota_line=quota_line, account_line=account)
    return {
        "steps": rows,
        "launches": False,
        "approval": APPROVAL,
        "facts": fact_block,
        "content": "\n".join(lines),
    }

