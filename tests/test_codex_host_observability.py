# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Codex host observability (V8 slice 7) -- the second runtime.

Slice 6 built ``hostobs`` against a single host, which is exactly the state in
which an abstraction is most likely to be quietly wrong. Codex is the useful
second case because its channel profile is Claude Code's *inverted*, audited
against codex-cli 0.147.0 on 2026-08-26:

* Claude Code hands ModelQuad a rich process environment and a bare MCP client
  name. Codex hands it a **scrubbed** environment (19 OS variables, nothing
  identifying Codex) and a **rich** client name carrying the runtime version.
* Claude Code's configuration yields a model *family* (``opus``); Codex's
  yields an *exact id* (``gpt-5.6-terra``) -- with the same weak provenance.
* Claude Code exposes a session id; Codex exposes none through any channel
  this module is willing to read, so a Codex host has no session fingerprint.

These tests pin the properties that survived the second runtime, and the
honest UNKNOWNs that the second runtime forced into the open.
"""

from __future__ import annotations

import pytest

from orchestrator import hostobs
from orchestrator.hostobs import (
    CODEX_CONFIG_KEYS,
    CODEX_ENV_ALLOWLIST,
    ClaudeCodeHostObserver,
    CodexHostObserver,
    HostDecisionSnapshot,
    HostFactSource,
    HostProtocolIdentity,
    capture_host_decision_snapshot,
    default_observers,
    with_decision_context,
)
from orchestrator.models import ModelIdentitySource, RuntimeRole

# What codex-cli 0.147.0 actually sent in `initialize`, recorded live.
REAL_CLIENT_INFO = {
    "name": "codex-mcp-client",
    "title": "Codex",
    "version": "0.147.0",
}


def _protocol(**overrides) -> HostProtocolIdentity:
    return HostProtocolIdentity(**{**REAL_CLIENT_INFO, **overrides})


def _config(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _observer(tmp_path, *, body: str | None = None, protocol=None):
    return CodexHostObserver(
        protocol=_protocol() if protocol is None else protocol,
        config_path=(
            _config(tmp_path, body) if body is not None else tmp_path / "absent.toml"
        ),
    )


def _snapshot(tmp_path, *, body=None, protocol=None, selected_runtime=None):
    hostobs.reset_process_counters()
    snapshot = capture_host_decision_snapshot(
        protocol=_protocol() if protocol is None else protocol,
        observers=(_observer(tmp_path, body=body, protocol=protocol),),
    )
    if selected_runtime is not None:
        snapshot = with_decision_context(snapshot, selected_runtime=selected_runtime)
    return snapshot


# The two scalars the real machine's config carries, in a file that also
# carries everything the reader must not touch.
REAL_CONFIG = """
model = "gpt-5.6-terra"
model_reasoning_effort = "high"
service_tier = "default"

notify = ["C:\\\\Users\\\\example\\\\AppData\\\\Local\\\\OpenAI\\\\Codex\\\\notify.exe", "turn-ended"]

[projects.'c:\\\\users\\\\example\\\\desktop\\\\python\\\\secret-client-work']
trust_level = "trusted"

[mcp_servers.something]
command = "some-command"
env = { API_KEY = "sk-not-a-real-key" }
"""


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


def test_codex_is_claimed_from_the_mcp_client_name(tmp_path):
    assert _observer(tmp_path).claims() is True


def test_no_protocol_identity_means_no_claim(tmp_path):
    """The CLI path has no MCP connection, so it can never see a Codex host."""

    assert CodexHostObserver(protocol=None).claims() is False
    assert CodexHostObserver(protocol=None).observe() is None


def test_the_display_title_alone_never_claims_the_host(tmp_path):
    """`title` is free text the host picks; only `name` is load-bearing."""

    observer = _observer(tmp_path, protocol=HostProtocolIdentity(title="Codex"))
    assert observer.claims() is False
    assert observer.observe() is None


def test_another_host_named_in_clientinfo_is_not_codex(tmp_path):
    observer = _observer(tmp_path, protocol=HostProtocolIdentity(name="some-editor"))
    assert observer.claims() is False


def test_an_inherited_environment_never_outvotes_the_live_connection(tmp_path):
    """The regression this slice actually found, in the shape it was found.

    ``CLAUDECODE=1`` is inherited by every descendant of a Claude Code
    session, so a ModelQuad MCP server started anywhere under one carries the
    marker regardless of who later connects. Driving the production server as
    a Codex client from inside a Claude Code shell recorded
    ``runtime=claude_code`` against a ``codex-mcp-client`` connection --
    wrong in the one column the cross-host matrix is built on.

    An MCP ``clientInfo`` describes the connection being served; an
    environment variable describes an ancestor process. The session-scoped
    channel has to win.
    """

    inherited_claude_env = {
        "CLAUDECODE": "1",
        "AI_AGENT": "claude-code_2-1-246_agent",
        "CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
    }
    # Both observers would claim: the environment says Claude, the socket
    # says Codex. Only one of them is describing this connection.
    claude = ClaudeCodeHostObserver(
        environ=inherited_claude_env,
        settings_path=tmp_path / "absent.json",
        state_dir=tmp_path,
    )
    codex = _observer(tmp_path, body=REAL_CONFIG)
    assert claude.claims() is True
    assert codex.claims() is True

    hostobs.reset_process_counters()
    snapshot = capture_host_decision_snapshot(
        protocol=_protocol(), observers=default_observers(_protocol())
    )
    assert snapshot.identity.runtime == "codex"
    assert snapshot.identity.runtime_version == "0.147.0"


def test_a_connection_that_names_no_runtime_leaves_the_environment_in_charge(tmp_path):
    """The ordering must not cost Claude Code its own (working) channel.

    Claude Code sends no Codex clientInfo, so the protocol-based observer
    declines and the environment-based one answers exactly as it did before.
    """

    claude_env = {"CLAUDECODE": "1", "AI_AGENT": "claude-code_2-1-246_agent"}
    observers = (
        CodexHostObserver(protocol=HostProtocolIdentity(name="claude-code")),
        ClaudeCodeHostObserver(
            environ=claude_env,
            settings_path=tmp_path / "absent.json",
            state_dir=tmp_path,
        ),
    )
    hostobs.reset_process_counters()
    snapshot = capture_host_decision_snapshot(
        protocol=HostProtocolIdentity(name="claude-code"), observers=observers
    )
    assert snapshot.identity.runtime == "claude_code"
    assert snapshot.identity.runtime_version == "2.1.246"


# ---------------------------------------------------------------------------
# Runtime identity -- the fact Claude could not get, and Codex can
# ---------------------------------------------------------------------------


def test_the_runtime_version_comes_from_the_session_not_from_path(tmp_path):
    """`clientInfo.version` is the hosting process describing itself.

    This is the inverse of the Claude finding: there, the PATH CLI reported
    2.1.233 while the hosting session reported 2.1.246, so the PATH probe was
    rejected. Here the hosting session states its own version over the
    connection the decision is served on, so no PATH probe is needed at all.
    """

    observation = _observer(tmp_path).observe()
    assert observation is not None
    assert observation.runtime == "codex"
    assert observation.runtime_version == "0.147.0"
    assert observation.runtime_version_source is HostFactSource.MCP_CLIENT_INFO


def test_a_malformed_version_is_unknown_not_garbage(tmp_path):
    observation = _observer(
        tmp_path, protocol=_protocol(version="0.147.0-nightly+deadbeef")
    ).observe()
    assert observation is not None
    assert observation.runtime == "codex"
    assert observation.runtime_version is None
    assert observation.runtime_version_source is HostFactSource.UNKNOWN


def test_the_entrypoint_is_unknown_rather_than_the_client_name(tmp_path):
    """Codex exposes no entrypoint; the client name is a different fact."""

    observation = _observer(tmp_path).observe()
    assert observation.entrypoint is None


# ---------------------------------------------------------------------------
# Model identity -- an exact id, with provenance that never rises for it
# ---------------------------------------------------------------------------


def test_a_configured_exact_id_stays_exact_and_stays_configured(tmp_path):
    observation = _observer(tmp_path, body=REAL_CONFIG).observe()
    assert observation.model_exact_id == "gpt-5.6-terra"
    assert observation.model_family == "gpt"
    assert observation.model_provider == "codex"
    assert observation.model_source is ModelIdentitySource.CONFIGURED
    assert observation.model_fact_source is HostFactSource.HOST_CONFIG


def test_no_channel_can_produce_a_confirmed_host_model(tmp_path):
    """The slice-6 invariant, re-asserted against the second runtime.

    Codex hands over an exact model id, which is more than Claude's config
    gives -- and it still does not testify that this model served this turn.
    Only the transcript could, and reading it is refused.
    """

    for body in (REAL_CONFIG, 'model = "gpt-5.6-terra"', "", "{{{ not toml"):
        observer = _observer(tmp_path, body=body)
        observation = observer.observe()
        assert observation is not None
        assert observation.model_source is not ModelIdentitySource.CONFIRMED


def test_an_unrecognized_model_id_is_dropped_not_passed_through(tmp_path):
    observation = _observer(
        tmp_path, body='model = "../../etc/passwd"'
    ).observe()
    assert observation.model_exact_id is None
    assert observation.model_source is ModelIdentitySource.UNKNOWN


def test_a_family_is_not_invented_for_an_unknown_vendor(tmp_path):
    """An exact id we do not recognize keeps its exactness and no family."""

    observation = _observer(tmp_path, body='model = "llama-4-scout"').observe()
    assert observation.model_exact_id == "llama-4-scout"
    assert observation.model_family is None
    assert observation.model_source is ModelIdentitySource.CONFIGURED


# ---------------------------------------------------------------------------
# Reasoning effort
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", ["minimal", "low", "medium", "high", "xhigh"])
def test_every_effort_codex_accepts_survives_the_vocabulary(tmp_path, effort):
    """`minimal` is Codex's floor and had to be added to the closed set.

    Mirrors execution/codex.py VALID_REASONING_EFFORTS -- if that widens and
    this does not, a real observed effort silently reads UNKNOWN.
    """

    observation = _observer(
        tmp_path, body=f'model_reasoning_effort = "{effort}"'
    ).observe()
    assert observation.reasoning_mode == effort
    assert observation.reasoning_mode_source is HostFactSource.HOST_CONFIG


def test_an_unrecognized_effort_is_unknown_not_passed_through(tmp_path):
    """Codex itself does not validate this key -- `-c model_reasoning_effort=
    bogus` was accepted verbatim by codex-cli 0.147.0 -- so the normalizer is
    the only thing between the host's config file and the research axis."""

    observation = _observer(
        tmp_path, body='model_reasoning_effort = "bogus"'
    ).observe()
    assert observation.reasoning_mode is None
    assert observation.reasoning_mode_source is HostFactSource.UNKNOWN


# ---------------------------------------------------------------------------
# The honest UNKNOWNs the second runtime forced into the open
# ---------------------------------------------------------------------------


def test_a_codex_host_has_no_session_fingerprint(tmp_path):
    """No channel exposes a Codex session id, so none is synthesized.

    Decisions in one Codex session therefore cannot be grouped. That is a
    real limit of the dataset, recorded as absence rather than papered over
    with a per-process id that would look like a session and not be one.
    """

    observation = _observer(tmp_path, body=REAL_CONFIG).observe()
    assert observation.context.session_fingerprint is None


def test_the_empty_process_environment_is_recorded_as_looked_at(tmp_path):
    """"We looked and it was empty" is a different fact from "we never looked"."""

    observation = _observer(tmp_path, body=REAL_CONFIG).observe()
    assert HostFactSource.PROCESS_ENV in observation.unavailable_sources
    assert HostFactSource.PROCESS_ENV not in observation.fact_sources


def test_an_absent_config_leaves_the_model_unknown_but_still_observes(tmp_path):
    observation = _observer(tmp_path).observe()
    assert observation is not None
    assert observation.runtime_version == "0.147.0"
    assert observation.model_exact_id is None
    assert observation.model_source is ModelIdentitySource.UNKNOWN
    assert HostFactSource.HOST_CONFIG in observation.unavailable_sources


def test_the_context_exceeds_flag_is_never_claimed(tmp_path):
    """Codex has no push channel, so it never reports a large context."""

    observation = _observer(tmp_path, body=REAL_CONFIG).observe()
    assert observation.context.context_exceeds_large_threshold is None
    assert hostobs.read_host_push("codex", tmp_path) is None


# ---------------------------------------------------------------------------
# Adversarial: nothing hostile reaches a snapshot
# ---------------------------------------------------------------------------


def test_no_environment_can_reach_a_codex_observation(monkeypatch, tmp_path):
    """The allow-list is empty, so this is structural rather than a value check."""

    assert CODEX_ENV_ALLOWLIST == frozenset()
    for name, value in {
        "CODEX_HOME": r"C:\Users\SecretPerson\.codex",
        "CODEX_API_KEY": "sk-not-a-real-key",
        "CODEX_SESSION_ID": "01a03e87-5120-7180-8fd2-3ac5d3d13348",
        "CODEX_MODEL": "gpt-9-unreleased",
        "OPENAI_API_KEY": "sk-also-not-real",
        "PWD": r"C:\Users\SecretPerson\clients\acme",
    }.items():
        monkeypatch.setenv(name, value)

    snapshot = _snapshot(tmp_path, body=REAL_CONFIG)
    blob = repr(snapshot.to_dict())
    for leaked in ("SecretPerson", "sk-not-a-real-key", "sk-also-not-real",
                   "gpt-9-unreleased", "01a03e87", "acme"):
        assert leaked not in blob


def test_a_hostile_config_file_cannot_reach_a_snapshot(tmp_path):
    """The real config is full of what section 20 refuses. Two scalars survive."""

    snapshot = _snapshot(tmp_path, body=REAL_CONFIG)
    blob = repr(snapshot.to_dict())
    for refused in (
        "secret-client-work",   # a [projects.'...'] absolute workspace path
        "notify.exe",           # an absolute path to a user program
        "some-command",         # an mcp_servers command
        "sk-not-a-real-key",    # an mcp_servers injected credential
        "trust_level",
        "service_tier",
    ):
        assert refused not in blob
    assert snapshot.identity.model_exact_id == "gpt-5.6-terra"
    assert snapshot.reasoning_mode == "high"


def test_only_the_declared_keys_are_ever_read(tmp_path):
    """A config whose refused keys are booby-trapped still reads clean."""

    assert CODEX_CONFIG_KEYS == ("model", "model_reasoning_effort")
    body = "\n".join(
        [
            'model = "gpt-5.6-terra"',
            'model_reasoning_effort = "high"',
            *(f'{name} = "POISON-{name}"' for name in
              ("notify", "projects", "mcp_servers", "history", "auth", "sessions")),
        ]
    )
    snapshot = _snapshot(tmp_path, body=body)
    assert "POISON" not in repr(snapshot.to_dict())


def test_a_corrupt_config_is_ignored_and_never_raises(tmp_path):
    observation = _observer(tmp_path, body="{{{ this is not toml").observe()
    assert observation is not None
    assert observation.model_source is ModelIdentitySource.UNKNOWN
    assert observation.runtime_version == "0.147.0"


def test_a_hostile_protocol_identity_is_normalized(tmp_path):
    """clientInfo is host-supplied data, normalized like any other input."""

    identity = HostProtocolIdentity(
        name=r"C:\Users\SecretPerson\codex",
        title="Codex " * 40,
        version="'; DROP TABLE routing_decisions; --",
    )
    assert identity.name is None
    assert identity.title is None
    assert identity.version is None
    assert not identity


# ---------------------------------------------------------------------------
# End to end, through the real capture path
# ---------------------------------------------------------------------------


def test_the_default_observers_can_see_a_codex_host(tmp_path):
    """The wiring, not the observer: default_observers must carry the channel."""

    observers = default_observers(_protocol())
    assert any(isinstance(o, CodexHostObserver) for o in observers)
    assert any(o.claims() for o in observers)


def test_the_default_observers_see_nothing_without_a_protocol_identity(tmp_path):
    assert not any(o.claims() for o in default_observers(None) if o.runtime_id == "codex")


def test_a_codex_snapshot_records_the_host_as_current_host(tmp_path):
    snapshot = _snapshot(tmp_path, body=REAL_CONFIG)
    assert snapshot.observed is True
    assert snapshot.identity.runtime == "codex"
    assert snapshot.identity.runtime_version == "0.147.0"
    assert snapshot.identity.model_exact_id == "gpt-5.6-terra"
    assert snapshot.identity.role is RuntimeRole.CURRENT_HOST
    assert snapshot.mcp_client_name == "codex-mcp-client"


def test_the_client_info_channel_is_recorded_once_not_twice(tmp_path):
    """The observer's primary channel and the recorded client name are one fact.

    Listing `mcp_client_info` twice would read as two independent
    contributions where there was only ever one connection.
    """

    snapshot = _snapshot(tmp_path, body=REAL_CONFIG)
    assert snapshot.fact_sources.count(HostFactSource.MCP_CLIENT_INFO) == 1


def test_staying_with_a_codex_host_is_not_a_handoff(tmp_path):
    snapshot = _snapshot(tmp_path, body=REAL_CONFIG, selected_runtime="codex")
    assert snapshot.context.handoff_required is False


def test_delegating_away_from_a_codex_host_is_a_handoff(tmp_path):
    """The direction the matrix could not express before this slice."""

    snapshot = _snapshot(tmp_path, body=REAL_CONFIG, selected_runtime="claude")
    assert snapshot.context.handoff_required is True
    assert snapshot.identity.runtime == "codex"


def test_a_codex_snapshot_survives_the_persistence_round_trip(tmp_path):
    restored = HostDecisionSnapshot.from_dict(_snapshot(tmp_path, body=REAL_CONFIG).to_dict())
    assert restored is not None
    assert restored.identity.runtime == "codex"
    assert restored.identity.model_exact_id == "gpt-5.6-terra"
    assert restored.identity.model_source is ModelIdentitySource.CONFIGURED
    assert restored.reasoning_mode == "high"
    assert restored.runtime_version_source is HostFactSource.MCP_CLIENT_INFO
    assert HostFactSource.PROCESS_ENV in restored.unavailable_sources
