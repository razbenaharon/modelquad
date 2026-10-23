# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Claude host observability and decision-time identity (V8 slice 6).

The worker side of the dataset has been strong since slice 4/5; the host side
was structurally empty. These tests pin the properties that make the host side
worth having:

* runtime version, model family and model exact id are THREE separate axes,
  and none of them may be reconstructed from another;
* a model claim never gains provenance it did not earn;
* the host is frozen at the decision, and a later observation -- including the
  host's own outcome report -- can never rewrite it;
* nothing from the conversation, the filesystem or the credential store can
  reach a snapshot, however hostile the environment;
* every failure mode degrades to UNKNOWN rather than blocking routing.
"""

from __future__ import annotations

import json

import pytest

from orchestrator import hostobs
from orchestrator.hostobs import (
    CLAUDE_ENV_ALLOWLIST,
    CLAUDE_ENV_REFUSED,
    ClaudeCodeHostObserver,
    HostContextMetrics,
    HostDecisionSnapshot,
    HostFactSource,
    HostProtocolIdentity,
    capture_host_decision_snapshot,
    payload_size_bucket,
    read_host_push,
    record_host_push,
    session_fingerprint,
    unobserved_snapshot,
    uptime_bucket,
    with_decision_context,
)
from orchestrator.models import ModelIdentitySource, RuntimeRole

# The SHAPE of a real Claude Code session environment, observed 2026-08-26.
# The variable NAMES and the format of the values we parse are real; every
# value that stands in for a secret or an identifier is a canary.
#
# Canary values are deliberately LOW-ENTROPY and carry no credential prefix,
# for the reason recorded in a9b299b: a realistic-looking fake key trips
# secret scanners, and every false positive makes a future real leak easier
# to miss. The assertions only need a distinctive token to prove absent, so
# keep them boring -- do not "helpfully" make them look realistic again, and
# never paste a value out of a live session.
REAL_ENV = {
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
    "AI_AGENT": "claude-code_2-1-246_agent",
    "CLAUDE_AGENT_SDK_VERSION": "0.3.246",
    "CLAUDE_CODE_SESSION_ID": "canary-session-id-must-never-be-persisted",
    "CLAUDE_EFFORT": "high",
    "CLAUDE_CODE_MESSAGING_TOKEN": "canary-messaging-token-must-never-be-persisted",
    "CLAUDE_CODE_OAUTH_SCOPES": "canary-oauth-scopes-must-never-be-persisted",
    "CLAUDE_CODE_EXECPATH": r"C:\Users\SecretPerson\AppData\Roaming\Claude\cli.js",
    "CLAUDE_PID": "14184",
    "ANTHROPIC_BASE_URL": "https://api.internal.invalid",
    "BAGGAGE": "canary-tracing-baggage-must-never-be-persisted",
    "PWD": "/c/Users/SecretPerson/acme-internal-payments",
}


def _settings(tmp_path, **keys):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(keys), encoding="utf-8")
    return path


def _observer(tmp_path, env=None, **keys):
    return ClaudeCodeHostObserver(
        environ=dict(env if env is not None else REAL_ENV),
        settings_path=_settings(tmp_path, **keys) if keys else tmp_path / "absent.json",
        state_dir=tmp_path,
    )


def _snapshot(tmp_path, env=None, mcp_client_name=None, **keys):
    hostobs.reset_process_counters()
    return capture_host_decision_snapshot(
        protocol=HostProtocolIdentity(name=mcp_client_name),
        observers=(_observer(tmp_path, env=env, **keys),),
    )


# ---------------------------------------------------------------------------
# Host identity
# ---------------------------------------------------------------------------


def test_runtime_version_comes_from_the_session_environment(tmp_path):
    snapshot = _snapshot(tmp_path)
    assert snapshot.observed is True
    assert snapshot.identity.runtime == "claude_code"
    assert snapshot.identity.runtime_version == "2.1.246"
    assert snapshot.runtime_version_source is HostFactSource.PROCESS_ENV
    assert snapshot.entrypoint == "claude-desktop"


def test_runtime_version_is_separate_from_model_version(tmp_path):
    """The two drift axes must never be readable off one another."""

    push = {
        "session_id": REAL_ENV["CLAUDE_CODE_SESSION_ID"],
        "model": {"id": "claude-opus-5", "display_name": "Opus 5"},
    }
    record_host_push("claude_code", push, state_dir=tmp_path)
    snapshot = _snapshot(tmp_path, model="opus")

    identity = snapshot.identity
    assert identity.runtime_version == "2.1.246"     # harness axis
    assert identity.model_exact_id == "claude-opus-5"  # model axis
    # Same harness, different model, and vice versa, must both be expressible.
    assert identity.runtime_version != identity.model_exact_id
    assert identity.model_family == "opus"
    assert identity.model_family != identity.model_exact_id


def test_configured_family_never_becomes_an_exact_model(tmp_path):
    """`"model": "opus"` says nothing about which Opus release."""

    snapshot = _snapshot(tmp_path, model="opus")
    identity = snapshot.identity
    assert identity.model_family == "opus"
    assert identity.model_exact_id is None
    assert identity.model_source is ModelIdentitySource.CONFIGURED
    assert identity.is_confirmed is False
    assert snapshot.model_fact_source is HostFactSource.HOST_CONFIG


def test_a_configured_exact_id_keeps_its_exactness_and_its_weak_provenance(tmp_path):
    snapshot = _snapshot(tmp_path, model="claude-opus-4-5")
    identity = snapshot.identity
    assert identity.model_exact_id == "claude-opus-4-5"
    assert identity.model_family == "opus"
    # Reading a precise string out of a config file is still only CONFIGURED.
    assert identity.model_source is ModelIdentitySource.CONFIGURED
    assert identity.is_confirmed is False


def test_a_host_reported_model_is_reported_never_confirmed(tmp_path):
    record_host_push(
        "claude_code",
        {
            "session_id": REAL_ENV["CLAUDE_CODE_SESSION_ID"],
            "model": {"id": "claude-opus-5", "display_name": "Opus 5"},
            "version": "2.1.250",
        },
        state_dir=tmp_path,
    )
    snapshot = _snapshot(tmp_path, model="opus")
    identity = snapshot.identity
    assert identity.model_exact_id == "claude-opus-5"
    assert identity.model_source is ModelIdentitySource.REPORTED
    assert identity.is_confirmed is False
    assert snapshot.model_fact_source is HostFactSource.HOST_PUSH
    # A live push also supersedes the spawn-time runtime version.
    assert identity.runtime_version == "2.1.250"
    assert snapshot.runtime_version_source is HostFactSource.HOST_PUSH


def test_no_channel_can_produce_a_confirmed_host_model(tmp_path):
    """There is deliberately no path to CONFIRMED for a host model.

    Confirming it would require reading the conversation transcript, which
    the design refuses. This test exists so adding such a path has to be a
    deliberate act that breaks a named assertion.
    """

    record_host_push(
        "claude_code",
        {
            "session_id": REAL_ENV["CLAUDE_CODE_SESSION_ID"],
            "model": {"id": "claude-opus-5"},
        },
        state_dir=tmp_path,
    )
    for keys in ({}, {"model": "opus"}, {"model": "claude-opus-5"}):
        snapshot = _snapshot(tmp_path, **keys)
        assert snapshot.identity.model_source is not ModelIdentitySource.CONFIRMED


def test_role_is_recorded_as_current_host_not_inferred(tmp_path):
    assert _snapshot(tmp_path).identity.role is RuntimeRole.CURRENT_HOST


def test_unknown_model_stays_unknown(tmp_path):
    snapshot = _snapshot(tmp_path)  # no settings file, no push
    identity = snapshot.identity
    assert identity.model_family is None
    assert identity.model_exact_id is None
    assert identity.model_source is ModelIdentitySource.UNKNOWN
    assert snapshot.model_fact_source is HostFactSource.UNKNOWN
    # And it says so explicitly rather than leaving the reader to guess.
    assert HostFactSource.HOST_CONFIG in snapshot.unavailable_sources
    assert HostFactSource.HOST_PUSH in snapshot.unavailable_sources


def test_a_snapshot_is_immutable(tmp_path):
    snapshot = _snapshot(tmp_path, model="opus")
    with pytest.raises(Exception):
        snapshot.identity.model_source = ModelIdentitySource.CONFIRMED  # type: ignore[misc]
    with pytest.raises(Exception):
        snapshot.context.session_fingerprint = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Reasoning / effort state
# ---------------------------------------------------------------------------


def test_reasoning_mode_prefers_the_session_environment_over_configuration(tmp_path):
    snapshot = _snapshot(tmp_path, effortLevel="low")
    assert snapshot.reasoning_mode == "high"  # CLAUDE_EFFORT
    assert snapshot.reasoning_mode_source is HostFactSource.PROCESS_ENV


def test_reasoning_mode_falls_back_to_configuration_with_weaker_provenance(tmp_path):
    env = {k: v for k, v in REAL_ENV.items() if k != "CLAUDE_EFFORT"}
    snapshot = _snapshot(tmp_path, env=env, effortLevel="low")
    assert snapshot.reasoning_mode == "low"
    assert snapshot.reasoning_mode_source is HostFactSource.HOST_CONFIG


def test_an_unrecognized_reasoning_mode_is_unknown_not_passed_through(tmp_path):
    env = dict(REAL_ENV, CLAUDE_EFFORT="turbo-ultra")
    snapshot = _snapshot(tmp_path, env=env)
    assert snapshot.reasoning_mode is None
    assert snapshot.reasoning_mode_source is HostFactSource.UNKNOWN


# ---------------------------------------------------------------------------
# Session / decision semantics
# ---------------------------------------------------------------------------


def test_a_session_fingerprint_is_stable_and_never_the_raw_session_id(tmp_path):
    raw = REAL_ENV["CLAUDE_CODE_SESSION_ID"]
    first = session_fingerprint(raw, state_dir=tmp_path)
    second = session_fingerprint(raw, state_dir=tmp_path)
    assert first == second
    assert first is not None
    assert raw not in first
    assert session_fingerprint("a-different-session", state_dir=tmp_path) != first


def test_resetting_the_installation_id_severs_the_fingerprint(tmp_path):
    from orchestrator.identity import reset_installation_id

    raw = REAL_ENV["CLAUDE_CODE_SESSION_ID"]
    before = session_fingerprint(raw, state_dir=tmp_path)
    reset_installation_id(tmp_path)
    assert session_fingerprint(raw, state_dir=tmp_path) != before


def test_the_model_may_differ_between_two_decisions_in_one_session(tmp_path):
    """A mid-session model switch must produce two distinguishable snapshots."""

    session = {"session_id": REAL_ENV["CLAUDE_CODE_SESSION_ID"]}
    record_host_push("claude_code", dict(session, model={"id": "claude-opus-5"}), state_dir=tmp_path)
    first = _snapshot(tmp_path)

    record_host_push(
        "claude_code", dict(session, model={"id": "claude-sonnet-5"}), state_dir=tmp_path
    )
    second = _snapshot(tmp_path)

    assert first.identity.model_exact_id == "claude-opus-5"
    assert second.identity.model_exact_id == "claude-sonnet-5"
    # Same session, two different models -- and the fingerprint proves they
    # belong to the same session rather than two unrelated ones.
    assert first.context.session_fingerprint == second.context.session_fingerprint


def test_a_push_from_another_session_is_ignored(tmp_path):
    """Otherwise a previous session's model would silently describe this one."""

    record_host_push(
        "claude_code",
        {"session_id": "some-other-session", "model": {"id": "claude-haiku-4-5"}},
        state_dir=tmp_path,
    )
    snapshot = _snapshot(tmp_path, model="opus")
    assert snapshot.identity.model_exact_id is None
    assert snapshot.identity.model_source is ModelIdentitySource.CONFIGURED
    assert HostFactSource.HOST_PUSH in snapshot.unavailable_sources


def test_prior_decision_count_advances_only_for_recorded_decisions(tmp_path):
    hostobs.reset_process_counters()
    observers = (_observer(tmp_path),)
    first = capture_host_decision_snapshot(observers=observers)
    preview = capture_host_decision_snapshot(observers=observers, count_decision=False)
    second = capture_host_decision_snapshot(observers=observers)

    assert first.context.prior_modelquad_decisions == 0
    assert preview.context.prior_modelquad_decisions == 1
    assert second.context.prior_modelquad_decisions == 1


# ---------------------------------------------------------------------------
# Handoff direction
# ---------------------------------------------------------------------------


def test_staying_with_the_host_is_not_a_handoff(tmp_path):
    snapshot = with_decision_context(_snapshot(tmp_path), selected_runtime="claude")
    # The registry spells the runtime "claude" and the observer "claude_code";
    # treating those as different would report a handoff for a task that never
    # moved.
    assert snapshot.context.handoff_required is False


def test_delegating_to_another_runtime_is_a_handoff(tmp_path):
    snapshot = with_decision_context(
        _snapshot(tmp_path), selected_runtime="codex", handoff_payload_size=5000
    )
    assert snapshot.context.handoff_required is True
    assert snapshot.context.handoff_payload_bucket == "4k_to_32k"


def test_an_unidentified_host_cannot_claim_a_handoff_direction():
    snapshot = with_decision_context(unobserved_snapshot(), selected_runtime="codex")
    assert snapshot.context.handoff_required is None


def test_annotations_layer_rather_than_overwrite(tmp_path):
    snapshot = with_decision_context(
        _snapshot(tmp_path), context_richness="rich", handoff_payload_size=100
    )
    snapshot = with_decision_context(snapshot, selected_runtime="codex")
    assert snapshot.context.context_richness == "rich"
    assert snapshot.context.handoff_payload_bucket == "lt_512b"
    assert snapshot.context.handoff_required is True


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [(None, None), (-1, None), (0, "lt_5m"), (299, "lt_5m"), (600, "5m_to_30m"),
     (3600, "30m_to_2h"), (10000, "2h_to_6h"), (100000, "gte_6h")],
)
def test_uptime_buckets(seconds, expected):
    assert uptime_bucket(seconds) == expected


@pytest.mark.parametrize(
    "size,expected",
    [(None, None), (-1, None), (0, "empty"), (10, "lt_512b"), (1000, "512b_to_4k"),
     (10000, "4k_to_32k"), (100000, "gte_32k")],
)
def test_payload_size_buckets(size, expected):
    assert payload_size_bucket(size) == expected


# ---------------------------------------------------------------------------
# Privacy: nothing from the conversation, filesystem or credential store
# ---------------------------------------------------------------------------

#: Every one of these appears in the hostile fixtures below. None may appear
#: anywhere in a serialized snapshot, a cached push, or a research record.
#: All are low-entropy canaries -- see the note on REAL_ENV.
FORBIDDEN = (
    "canary-messaging-token-must-never-be-persisted",   # a credential
    "canary-apikey-must-never-be-persisted",            # a credential
    "canary-password-must-never-be-persisted",          # a credential
    "canary-oauth-scopes-must-never-be-persisted",      # authorization surface
    r"C:\Users\SecretPerson",                           # absolute path
    "/c/Users/SecretPerson/acme-internal-payments",     # absolute path
    "acme-internal-payments",                           # repository name
    "def charge_card(pan, cvv):",                       # source code
    "canary-user-prompt-must-never-be-persisted",       # user prompt
    "canary-assistant-message-must-never-be-persisted", # assistant message
    "customer_ssns.csv",                                # file name
    "canary-session-id-must-never-be-persisted",        # RAW session id
    "canary-user-email-must-never-be-persisted",        # user identity
)


def _assert_clean(blob: str) -> None:
    for secret in FORBIDDEN:
        assert secret not in blob, f"leaked {secret!r}"


def test_a_hostile_environment_cannot_reach_a_snapshot(tmp_path):
    hostile = dict(
        REAL_ENV,
        ANTHROPIC_API_KEY="canary-apikey-must-never-be-persisted",
        MY_PASSWORD="canary-password-must-never-be-persisted",
        USER_PROMPT="canary-user-prompt-must-never-be-persisted",
        ASSISTANT_MESSAGE="canary-assistant-message-must-never-be-persisted",
        SOURCE="def charge_card(pan, cvv):",
        REPO="acme-internal-payments",
        EMAIL="canary-user-email-must-never-be-persisted",
    )
    snapshot = _snapshot(tmp_path, env=hostile)
    _assert_clean(json.dumps(snapshot.to_dict()))
    # The useful facts still came through -- the boundary is an allow-list,
    # not a blanket refusal.
    assert snapshot.identity.runtime_version == "2.1.246"
    assert snapshot.context.session_fingerprint is not None


def test_a_hostile_host_push_cannot_reach_a_snapshot(tmp_path):
    record_host_push(
        "claude_code",
        {
            "session_id": REAL_ENV["CLAUDE_CODE_SESSION_ID"],
            "version": "2.1.250",
            "model": {"id": "claude-opus-5", "display_name": "Opus 5"},
            # Everything Claude Code's statusLine payload is known to carry
            # beyond the fields above:
            "transcript_path": r"C:\Users\SecretPerson\.claude\projects\customer_ssns.csv",
            "workspace": {"current_dir": "/c/Users/SecretPerson/acme-internal-payments"},
            "cwd": "/c/Users/SecretPerson/acme-internal-payments",
            "cost": {"total_cost_usd": 12.5, "total_lines_added": 400},
            "last_message": "canary-user-prompt-must-never-be-persisted",
            "api_key": "canary-apikey-must-never-be-persisted",
        },
        state_dir=tmp_path,
    )
    cached = (tmp_path / "host-observation-claude.json").read_text(encoding="utf-8")
    _assert_clean(cached)

    snapshot = _snapshot(tmp_path)
    _assert_clean(json.dumps(snapshot.to_dict()))
    assert snapshot.identity.model_exact_id == "claude-opus-5"


def test_a_hostile_settings_file_cannot_reach_a_snapshot(tmp_path):
    snapshot = _snapshot(
        tmp_path,
        model="opus",
        apiKey="canary-apikey-must-never-be-persisted",
        password="canary-password-must-never-be-persisted",
        projectPath=r"C:\Users\SecretPerson",
        lastPrompt="canary-user-prompt-must-never-be-persisted",
    )
    _assert_clean(json.dumps(snapshot.to_dict()))
    assert snapshot.identity.model_family == "opus"


def test_the_allowlist_and_the_refusal_list_do_not_overlap():
    assert not (CLAUDE_ENV_ALLOWLIST & CLAUDE_ENV_REFUSED)
    # Every refusal is a name that really occurs next to the ones we read.
    assert CLAUDE_ENV_REFUSED <= set(REAL_ENV) | {"CLAUDE_CODE_MESSAGING_SOCKET",
                                                  "CLAUDE_CODE_HOST_SESSION_ID"}


def test_only_allowlisted_names_are_ever_read(tmp_path):
    """Structural, not a value check: the reader must touch nothing else."""

    seen: list[str] = []

    class _Recording(dict):
        def get(self, key, default=None):  # noqa: D102
            seen.append(key)
            return super().get(key, default)

        def __getitem__(self, key):
            seen.append(key)
            return super().__getitem__(key)

    env = _Recording(REAL_ENV)
    ClaudeCodeHostObserver(environ=env, state_dir=tmp_path, settings_path=tmp_path / "x").observe()
    assert set(seen) <= CLAUDE_ENV_ALLOWLIST


def test_a_content_free_push_never_overwrites_a_real_observation(tmp_path):
    record_host_push(
        "claude_code",
        {"session_id": REAL_ENV["CLAUDE_CODE_SESSION_ID"], "model": {"id": "claude-opus-5"}},
        state_dir=tmp_path,
    )
    assert record_host_push("claude_code", {"cost": {"total_cost_usd": 1}}, state_dir=tmp_path) is None
    assert read_host_push("claude_code", tmp_path)["model_exact_id"] == "claude-opus-5"


# ---------------------------------------------------------------------------
# Failure behavior: everything degrades, nothing blocks
# ---------------------------------------------------------------------------


def test_no_observer_claims_the_host(tmp_path):
    hostobs.reset_process_counters()
    snapshot = capture_host_decision_snapshot(
        observers=(_observer(tmp_path, env={"SHELL": "/bin/bash"}),),
        protocol=HostProtocolIdentity(name="some-editor"),
    )
    assert snapshot.observed is False
    assert snapshot.identity.runtime is None
    assert snapshot.identity.model_source is ModelIdentitySource.UNKNOWN
    assert snapshot.identity.role is RuntimeRole.CURRENT_HOST
    assert snapshot.mcp_client_name == "some-editor"


def test_an_exploding_observer_does_not_break_routing(tmp_path):
    class _Broken:
        runtime_id = "broken"

        def claims(self):
            return True

        def observe(self):
            raise RuntimeError("host probe blew up")

    hostobs.reset_process_counters()
    snapshot = capture_host_decision_snapshot(observers=(_Broken(),))
    assert snapshot.observed is False


def test_malformed_runtime_version_is_unknown_not_garbage(tmp_path):
    for bogus in ("claude-code_not-a-version_agent", "claude-code_agent", "", "'; DROP TABLE"):
        snapshot = _snapshot(tmp_path, env=dict(REAL_ENV, AI_AGENT=bogus))
        assert snapshot.identity.runtime_version is None
        assert snapshot.runtime_version_source is HostFactSource.UNKNOWN
        # The host is still identified -- a missing version is not a missing host.
        assert snapshot.identity.runtime == "claude_code"


def test_partial_metadata_yields_a_partial_but_valid_snapshot(tmp_path):
    snapshot = _snapshot(tmp_path, env={"CLAUDECODE": "1"})
    assert snapshot.observed is True
    assert snapshot.identity.runtime == "claude_code"
    assert snapshot.identity.runtime_version is None
    assert snapshot.entrypoint is None
    assert snapshot.context.session_fingerprint is None
    assert snapshot.reasoning_mode is None


def test_a_corrupt_host_push_file_is_ignored(tmp_path):
    (tmp_path / "host-observation-claude.json").write_text("{not json", encoding="utf-8")
    assert read_host_push("claude_code", tmp_path) is None
    assert _snapshot(tmp_path).identity.model_exact_id is None


def test_a_corrupt_settings_file_is_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{{{", encoding="utf-8")
    observer = ClaudeCodeHostObserver(
        environ=dict(REAL_ENV), settings_path=path, state_dir=tmp_path
    )
    observation = observer.observe()
    assert observation is not None
    assert observation.model_source is ModelIdentitySource.UNKNOWN


def test_a_persisted_snapshot_is_revalidated_on_the_way_back(tmp_path):
    """Stored bytes are input, not truth."""

    tampered = {
        "snapshot_version": 1,
        "identity": {"runtime": "claude_code", "model_exact_id": "claude-opus-5",
                     "model_source": "confirmed", "role": "current_host"},
        "context": {"session_fingerprint": r"C:\Users\SecretPerson\secret",
                    "prior_modelquad_decisions": "lots"},
        "entrypoint": "/absolute/path/injected",
        "runtime_version_source": "telepathy",
        "reasoning_mode": "definitely-not-a-mode",
        "fact_sources": ["process_env", "made_up"],
    }
    restored = HostDecisionSnapshot.from_dict(tampered)
    assert restored is not None
    assert restored.context.session_fingerprint is None   # rejected shape
    assert restored.context.prior_modelquad_decisions is None
    assert restored.entrypoint is None
    assert restored.runtime_version_source is HostFactSource.UNKNOWN
    assert restored.reasoning_mode is None
    assert restored.fact_sources == (HostFactSource.PROCESS_ENV, HostFactSource.UNKNOWN)


def test_from_dict_of_nothing_is_none():
    assert HostDecisionSnapshot.from_dict(None) is None
    assert HostDecisionSnapshot.from_dict("not a dict") is None
    assert HostContextMetrics.from_dict(None) == HostContextMetrics()


def test_concurrent_host_pushes_never_corrupt_the_cache_or_leave_staging_files(tmp_path, monkeypatch):
    """Every running Claude Code session pushes through the same hook.

    Two sessions caching the same file at once must each stage in their own
    temp file: a shared ``.json.tmp`` name let one truncate the other's
    staging file mid-write and lose its rename.
    """

    import threading

    from orchestrator.adapters import atomicwrite

    staged_through_atomic_write: list[str] = []
    real_atomic_write = atomicwrite.atomic_write

    def spy(path, content, **kwargs):
        staged_through_atomic_write.append(path.name)
        real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(hostobs, "atomic_write", spy)

    versions = [f"2.1.{n}" for n in range(200, 216)]
    barrier = threading.Barrier(len(versions))
    outcomes: list[dict | None] = []
    failures: list[BaseException] = []

    def push(version: str) -> None:
        try:
            barrier.wait(timeout=5)
            outcomes.append(
                record_host_push(
                    "claude_code",
                    {"session_id": REAL_ENV["CLAUDE_CODE_SESSION_ID"], "version": version},
                    state_dir=tmp_path,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            failures.append(exc)

    threads = [threading.Thread(target=push, args=(version,)) for version in versions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert all(outcome is not None for outcome in outcomes)
    # Every push staged through atomic_write -- the old shared `.json.tmp` path did not.
    assert staged_through_atomic_write == [hostobs.HOST_PUSH_FILE_NAMES["claude_code"]] * len(versions)
    cached = json.loads((tmp_path / hostobs.HOST_PUSH_FILE_NAMES["claude_code"]).read_text(encoding="utf-8"))
    assert cached["runtime_version"] in versions
    # The fingerprint mints identity.json beside the cache; nothing else may remain.
    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == sorted([hostobs.HOST_PUSH_FILE_NAMES["claude_code"], "identity.json"]), (
        "no writer's staging file survives its own replace"
    )
