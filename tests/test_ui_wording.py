# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Every surface a person reads uses the same plain words, and none of the banned ones.

The surfaces: the terminal status line (both forms), the desktop quota band's
source, the reply text of modelquad_status, modelquad_delegate (refusals),
modelquad_handoff, modelquad_split and modelquad_activity, the first-call
notice, the CLI output of status, doctor's person-only checks, settings plan
and repo and split summary, and the skills. They are scanned over a grid of
quota states (fresh, old, unknown; below and above the near-limit marks) and
repository states (no file, valid, Codex off, broken, unknown keys, the
user's own off) for the phrases the legal self-review (3.5) rules out, also
in the negative. The text a person reads also carries no internal code
(a snake_case token such as ``repo_policy_codex_off``): codes stay in the
structured fields.
"""

from __future__ import annotations

import itertools
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modelquad import assistance, cli, plans, repo_policy, repo_settings
from modelquad.doctor import plans_check, repository_checks
from modelquad.mcp import presentation
from modelquad.mcp import server as mcp
from modelquad.models import Freshness, FreshnessThresholds, ProviderStatus

ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = FreshnessThresholds()

#: Wording the legal self-review (3.5) rules out, also in the negative.
BANNED = (
    "save quota", "save usage", "save your", "waste", "every last percent", "stretch", "extend your",
    "maximise", "maximize", "get more out of", "beat ", "bypass", "get around", "escape",
    "skip the limit", "skip a limit", "never hit", "keep coding", "runs out", "continue after",
    "failover", "more usage", "double your", "unlimited", "combine", "pool", "merge", "route",
    "routing", "router", "load balancing", "auto-switch", "switch", "arbitrage", "cheaper",
    "official", "partner", "endorsed", "integrated with", "safe", "secure", "guarantee",
    "locked-down", "locked down", "nothing leaves", "autonomous", "take over", "hand off to",
)
#: An internal code: two or more lowercase words joined by underscores.
CODE = re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b")


#: Names, not claims: Claude Code's own mode name, the providers' "safety review"
#: name, and the keys of the repository file.
NAMES = ("bypass-permissions", "safety review")
FILE_KEYS = frozenset({"task_kinds", "max_files", "max_total_kib"})


def banned_in(text: str) -> list[str]:
    folded = text.casefold()
    for name in NAMES:
        folded = folded.replace(name, "")
    return [phrase for phrase in BANNED if phrase in folded]


def check_person_text(text: str) -> None:
    assert banned_in(text) == [], text
    assert [code for code in CODE.findall(text) if code not in FILE_KEYS] == [], text


def person_text(result) -> str:
    """The block a person reads: the first one when the data follows it."""
    blocks = [block.text for block in result.content]
    assert blocks, result
    assert not blocks[0].lstrip().startswith("{"), "the first block is data, not a sentence"
    return blocks[0]


# ------------------------------------------------------------------ the grid

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
FRESH, OLD = timedelta(0), timedelta(hours=2)
CLAUDE = {
    "unknown": None,
    "low": {"five": 40, "seven": 30, "age": FRESH},
    "5h-near": {"five": 85, "seven": 40, "age": FRESH},
    "5h-near-resets-soon": {"five": 92, "seven": 40, "age": FRESH, "five_reset": 10},
    "week-near": {"five": 50, "seven": 95, "age": FRESH},
    "old": {"five": 88, "seven": 20, "age": OLD, "five_reset": 40},
}
CODEX = {
    "absent": "absent",
    "unknown": None,
    "signed-out": "signed-out",
    "low": {"five": 30, "seven": 20, "age": FRESH},
    "high": {"five": 95, "seven": 60, "age": FRESH},
    "old": {"five": 30, "seven": 20, "age": OLD, "five_reset": 40},
}
GRID = list(itertools.product(CLAUDE, CODEX))


def status(name, spec):
    metadata = {"source": "claude_statusline" if name == "claude" else "codex_app_server"}
    if spec == "signed-out":
        return ProviderStatus(name=name, available=False, metadata=metadata,
                              reason="codex app-server rate-limit read failed: Not logged in.")
    if spec is None:
        return ProviderStatus(name=name, available=True, metadata=metadata)
    observed = NOW - spec["age"]
    metadata["five_hour_used_percentage"] = spec["five"]
    metadata["five_hour_resets_at"] = (NOW + timedelta(minutes=spec.get("five_reset", 120))).isoformat()
    metadata["seven_day_used_percentage"] = spec["seven"]
    metadata["seven_day_resets_at"] = (NOW + timedelta(days=3)).isoformat()
    if spec["age"]:
        metadata["stale"] = True
    return ProviderStatus(name=name, available=True, observed_at=observed,
                          remaining_percentage=100.0 - max(spec["five"], spec["seven"]),
                          freshness=Freshness.STALE if spec["age"] else Freshness.FRESH,
                          metadata=metadata)


def statuses(claude, codex):
    found = [status("claude", CLAUDE[claude])]
    if CODEX[codex] != "absent":
        found.append(status("codex", CODEX[codex]))
    return found


# A repository for every state the repository line can be in.
REPOS = {
    "no-file": None,
    "valid": 'schema = 1\n[codex]\ntask_kinds = ["review"]\naccount = "workspace"\n'
             '[offers]\ncodex = "on_request"\n',
    "off": "schema = 1\n[codex]\nallowed = false\n",
    "broken": "schema = 1\n[codex\n",
    "wrong-type": 'schema = 1\n[codex]\nmax_files = "lots"\n',
    "unknown-keys": "schema = 1\nauto_approve = true\n[codex]\ntrusted = true\n",
    "user-off": None,
}


@pytest.fixture
def repos(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELQUAD_STATE_ROOT", str(tmp_path / "state"))
    made = {}
    for name, text in REPOS.items():
        folder = tmp_path / "repos" / name
        (folder / ".git").mkdir(parents=True)
        if text is not None:
            (folder / repo_policy.FILE_NAME).write_text(text, encoding="utf-8")
        made[name] = folder
    repo_settings.write_mode(made["user-off"], "off")
    return made


# ------------------------------------------------------------- status line


@pytest.mark.parametrize("claude,codex", GRID)
def test_the_status_line_reads_the_same_for_both_tools(claude, codex):
    found = statuses(claude, codex)
    line = assistance.statusline_text(found, THRESHOLDS, now=NOW)
    segment = assistance.statusline_segment(found, THRESHOLDS, now=NOW)
    for text in (line, segment):
        check_person_text(text)
        assert text.isascii()
        assert text.startswith("Claude"), text
        # No figure is invented: nothing in the grid has 100% used.
        assert "stale" not in text and re.search(r"(?<!\d)0% left", text) is None
    # Claude first, then Codex, in both forms; a whole-tool state reads "<Tool>: <state>".
    assert line.index("Claude") < line.index("| Codex") if "| Codex" in line else True
    if codex == "signed-out":
        assert "Codex: not signed in (run codex login)" in line
    if "old" in (claude, codex):
        assert "(as of " in line
    note = assistance.handoff_suggestion(found, THRESHOLDS, now=NOW)
    if note is not None:
        check_person_text(note["message"])
        assert "near its limit" in line


# ------------------------------------------------------------------ the band


def test_the_band_source_carries_no_banned_phrase():
    for name in ("quota-band.tsx", "quota-band.test.tsx"):
        text = (ROOT / "plugin" / "hooks" / name).read_text(encoding="utf-8")
        assert banned_in(text) == [], name


# ------------------------------------------------------------- tool replies


@pytest.fixture
def quiet_server(tmp_path, monkeypatch):
    from modelquad.config import Config
    from modelquad.service import db

    path = tmp_path / "state.db"
    db.connect_and_init(path).close()
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(mcp, "load_config", lambda *a: Config())

    def tripwire(*args, **kwargs):
        raise AssertionError("a reply started work")

    monkeypatch.setattr(mcp, "_delegate_payload", tripwire)
    monkeypatch.setattr(mcp, "start_job", tripwire)
    monkeypatch.setattr(mcp, "_build_execution_manager", tripwire)
    return monkeypatch


@pytest.mark.parametrize("claude,codex", GRID)
def test_the_status_reply_says_the_quota_first_over_every_repository(claude, codex, quiet_server, repos):
    from types import SimpleNamespace

    found = statuses(claude, codex)
    providers = [SimpleNamespace(name=item.name, get_status=lambda item=item: item) for item in found]
    quiet_server.setattr(mcp, "build_providers", lambda *a, **k: providers)
    for folder in (None, *repos.values()):
        result = mcp.modelquad_status(None if folder is None else str(folder))
        text = person_text(result)
        check_person_text(text)
        assert text.splitlines()[0].startswith("Quota now: Claude")
        view = result.structured_content["repo_policy"]
        assert view["state"] == ("unknown" if folder is None else "known")
        if view["state"] == "known":
            assert view["refusal"] is None or view["refusal"] in repo_policy.REASONS | repo_settings.REASONS
            assert str(folder) not in json.dumps(result.structured_content)


def test_each_repository_state_has_its_own_plain_line(quiet_server, repos):
    lines = {name: mcp._repo_policy_line(mcp._repo_policy_view(str(folder)))
             for name, folder in repos.items()}
    assert lines["no-file"] == "This repository: Codex allowed (no .modelquad.toml)."
    assert lines["off"] == "This repository: Codex is off (its .modelquad.toml)."
    assert lines["user-off"] == "This repository: Codex is off (your setting: modelquad settings repo)."
    assert lines["broken"].startswith("This repository: Codex is refused until its .modelquad.toml is fixed.")
    assert lines["valid"] == ("This repository: Codex allowed (only review; work account only; "
                              "Claude calls Codex only when you ask; .modelquad.toml).")
    view = mcp._repo_policy_view(str(repos["unknown-keys"]))
    assert view["unknown_key_count"] == 2 and "auto_approve" not in json.dumps(view)


REFUSALS = [
    {"refused": True, "reason": code, "message": repo_policy.message(code)} for code in sorted(repo_policy.REASONS)
] + [
    {"refused": True, "reason": repo_settings.USER_CODEX_OFF, "message": repo_settings.USER_OFF_MESSAGE},
    {"refused": True, "reason": "user_repo_codex_off"},
    {"refused": True, "reason": "duplicate_in_flight"},
    {"refused": True, "reason": "task_type_not_read_only"},
    {"refused": True, "reason": "capsule_action_authority_rejected"},
    {"refused": True, "reason": "launch_state_unknown: routing controls are unavailable or invalid"},
    {"refused": True, "reason": "routing is paused; no new provider work may start until routing is resumed"},
    {"refused": True, "reason": "host_disconnected: this tool cannot start new work through ModelQuad"},
    {"refused": True, "reason": "host_unidentified: a tool is disconnected"},
    {"refused": True, "reason": "MODELQUAD_WORKER=1: a delegated worker must never hand off work"},
    {"refused": True, "reason": "worker ancestry match: a delegated worker must never hand off work"},
    {"refused": True, "reason": "something_new_and_unlisted"},
    {"refused": True},
]


@pytest.mark.parametrize("payload", REFUSALS, ids=lambda payload: str(payload.get("reason"))[:40])
def test_a_refusal_says_codex_was_not_started_then_why_in_words(payload):
    handoff = person_text(mcp._refused_result(dict(payload)))
    assert handoff.splitlines()[0] == "Codex was not started."
    check_person_text(handoff)

    delegate = mcp._decision_result(dict(payload), presentation.build_delegation_presentation(
        dict(payload), task_type="review"))
    text = person_text(delegate)
    assert text.splitlines()[0] == "ModelQuad: Codex was not started."
    assert text.splitlines()[1] == presentation.refusal_line(payload)
    check_person_text(text)
    assert delegate.structured_content.get("reason") == payload.get("reason")


def test_handoff_answers_and_waits_read_as_outcomes():
    for line in mcp._ANSWER_LINES.values():
        assert line.startswith("Codex was not started: ")
        check_person_text(line)


def test_the_first_call_notice_starts_with_the_outcome(monkeypatch):
    monkeypatch.setattr("modelquad.disclosure.notice_shown", lambda: False)
    monkeypatch.setattr("modelquad.disclosure.record_notice_shown", lambda *a: None)
    result = mcp._notice_first()
    text = person_text(result)
    assert text.startswith("Codex was not started.")
    check_person_text(text)
    assert result.structured_content["codex_started"] is False


def test_the_activity_lines_name_outcomes_not_codes():
    rows = [presentation.build_activity_entry(
        {"kind": "attempt", "id": 1, "status": "refused", "failure_kind": "policy_refused",
         "task_type": "review", "created_at": "2026-11-04T10:00:00+00:00"})]
    payload = {"presentation": {"entries": rows},
               "split": {"known": True, "codex": {"requests": 1, "outcomes": {"answered": 0},
                                                  "refused": {"refused_repo_file": 1}}}}
    for scoped in (False, True):
        lines = mcp._activity_lines(payload, scoped=scoped)
        for line in lines:
            check_person_text(line)
    assert mcp._activity_lines({"presentation": {"entries": []}}, scoped=True) == [
        "No ModelQuad activity recorded yet for this project."]
    for sentence in (*presentation._ACTIVITY_NOTES.values(), *presentation._REFUSAL_REASONS.values()):
        assert banned_in(sentence) == [], sentence


# ------------------------------------------------------------------- the CLI


def test_doctor_person_only_checks_show_the_repository_file_and_the_plans(repos, tmp_path, monkeypatch):
    checks = {name: {check.name: check for check in repository_checks(folder)}
              for name, folder in repos.items()}
    assert checks["no-file"]["repository file"].status.value == "ok"
    assert checks["valid"]["repository file"].detail.startswith("Valid .modelquad.toml.")
    broken = checks["broken"]["repository file"]
    assert broken.status.value == "warn" and broken.detail.startswith("Invalid: ")
    assert "remove the file" in broken.detail
    unknown = checks["unknown-keys"]["repository file"]
    assert unknown.status.value == "warn" and "auto_approve" in unknown.detail
    assert "off (no Codex here)" in checks["user-off"]["repository setting"].detail
    for found in checks.values():
        for check in found.values():
            assert banned_in(check.detail) == [], check

    usage = tmp_path / "usage"
    usage.mkdir(exist_ok=True)
    (usage / "usage-codex.json").write_text('{"plan_type": "plus"}', encoding="utf-8")
    plans.write_declared("codex", "pro")
    check = plans_check(usage)
    assert check.status.value == "warn"
    assert "Codex reports plus; you set pro." in check.detail and "modelquad settings plan" in check.detail
    plans.write_declared("codex", None)
    (usage / "usage-codex.json").unlink()
    assert plans_check(usage).status.value == "unknown"


def test_cli_outputs_carry_no_banned_phrase(repos, tmp_path, monkeypatch, capsys):
    usage = tmp_path / "usage"
    usage.mkdir(exist_ok=True)
    monkeypatch.setattr(cli, "read_status", lambda provider, refresh=False: provider.get_status())
    outputs = []
    commands = [["settings", "plan"], ["settings", "plan", "codex"], ["settings", "plan", "claude", "pro"],
                ["settings", "proactive", "off"]]
    for folder in repos.values():
        commands += [["settings", "repo", "--project", str(folder)],
                     ["split", "summary", "--project", str(folder)]]
    for command in commands:
        cli.main(["--usage-directory", str(usage), "--history-db", str(tmp_path / "h.db"), *command])
        outputs.append(capsys.readouterr().out)
    for folder in repos.values():
        monkeypatch.chdir(folder)
        cli.main(["--usage-directory", str(usage), "--no-history", "status"])
        outputs.append(capsys.readouterr().out)
    assert any(text.startswith("Plans:\n") for text in outputs)
    assert any("Quota now: Claude" in text for text in outputs)
    for text in outputs:
        assert banned_in(text) == [], text


def test_cli_help_texts_carry_no_banned_phrase(capsys):
    for command in (["status"], ["doctor"], ["activity"], ["split", "summary"], ["settings", "plan"],
                    ["settings", "repo"], ["settings", "proactive"]):
        with pytest.raises(SystemExit):
            cli.main([*command, "--help"])
        assert banned_in(capsys.readouterr().out) == [], command


# ----------------------------------------------------------------- the skills


def test_skills_have_one_line_descriptions_and_no_banned_phrase():
    skills = [*(ROOT / "plugin" / "skills").glob("*/SKILL.md"),
              *(ROOT / "src" / "modelquad" / "adapters" / "templates" / "claude" / "skills").glob("*/SKILL.md")]
    assert len(skills) == 12
    for path in skills:
        text = path.read_text(encoding="utf-8")
        assert banned_in(text) == [], path
        (description,) = [line for line in text.splitlines() if line.startswith("description: ")]
        assert len(description) <= 160 and description.endswith("."), path
        assert "worker" not in description.casefold(), path


def test_one_name_per_concept_in_the_skills():
    """Claude Code's prompt is the "approval prompt"; Codex through ModelQuad is read-only."""
    for path in (ROOT / "plugin" / "skills").glob("*/SKILL.md"):
        text = path.read_text(encoding="utf-8")
        assert "Claude Code asks them again" not in text and "ModelQuad's Codex worker" not in text, path
