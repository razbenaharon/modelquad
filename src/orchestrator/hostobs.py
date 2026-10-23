# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Host observability: who is asking, at the moment they ask.

ModelQuad's research question is not "which worker is best" but "given the
exact agent and model the user is ALREADY working in, and the context it
already owns, is delegation worth the handoff?". Answering it needs the same
resolution on the *host* side that ``workerevidence`` gives on the worker
side -- and until this module the host side was structurally empty:
``HostContext.current_model`` was never populated by any production path.

Three concepts, deliberately separate:

``HostObserver``
    A runtime-specific probe. Two implementations, and they were worth
    having because they agree about almost nothing. Claude Code exposes a
    rich process environment and a bare MCP client name. Codex exposes a
    scrubbed environment and a client name carrying its own version.

    The ``claims()`` / ``observe()`` contract has held for both unchanged.
    What did not hold was an assumption *outside* it: the *ordering*, which
    the first version wrote for one host. See :func:`default_observers`.

``HostRuntimeObservation``
    What one observer saw, already normalized. This is the ephemeral
    boundary: raw host metadata is read here, reduced to closed-vocabulary
    tokens, buckets and fingerprints, and never returned in raw form.

``HostDecisionSnapshot``
    The immutable, persistence-safe freeze of the host at ONE routing
    decision. A session may change model between decisions, so the snapshot
    belongs to the decision, never to the installation, and a later
    observation must never rewrite an earlier decision's snapshot.

Provenance rules, enforced rather than merely documented:

- A host-*reported* model is REPORTED. A model read from the host's own
  configuration is CONFIGURED. Neither ever becomes CONFIRMED: ModelQuad
  cannot observe which model actually served a host turn without reading the
  conversation transcript, which the privacy rules refuse.
- Runtime version and model version are separate axes and never substitute
  for one another.
- A family is never promoted to an exact id. If the host only says "opus",
  the exact release stays UNKNOWN -- a trustworthy UNKNOWN is valid data.

Privacy. Host metadata is more sensitive than worker metrics
because the host owns the whole conversation, so this module reads only an
explicit ALLOW-LIST of environment names, at most two declared keys of the
host's own configuration file per runtime, and exactly one declared key of an
MCP request's vendor ``_meta``. It never reads a transcript, a prompt, a
message, source content, a path, or a credential; session identifiers are
HMAC-fingerprinted against the installation id before they are stored, so a
raw session id never reaches a snapshot.

The declared-keys rule earns its keep on the Codex path, where the host's
config file is *known* to sit beside a table of absolute workspace paths, a
path to a user program, and MCP server commands with injected credentials --
none of it reachable, because the reader names two scalars instead of walking
a document. An allow-list is the only reason none of that is reachable.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .adapters.atomicwrite import atomic_write
from .models import ModelIdentitySource, RuntimeModelIdentity, RuntimeRole

HOST_OBSERVATION_VERSION = 1
HOST_SNAPSHOT_VERSION = 1

#: Where a host push (see :func:`record_host_push`) is cached, per runtime.
#: Codex is deliberately absent: it has no push channel this module will use
#: (see :class:`CodexHostObserver`), so ``read_host_push("codex")`` is None by
#: construction rather than by a missing file.
HOST_PUSH_FILE_NAMES: dict[str, str] = {"claude_code": "host-observation-claude.json"}


class HostFactSource(str, Enum):
    """HOW a host fact was obtained -- orthogonal to how much it is believed.

    ``ModelIdentitySource`` answers "how confident are we in this model
    claim". This answers "which channel produced it", so a future analysis
    can weight channels independently of the confidence label they map to.
    """

    #: Environment of the ModelQuad process the host itself spawned. Session
    #: scoped and not fabricable by a model, but frozen at spawn time.
    PROCESS_ENV = "process_env"
    #: A structured payload the host pushed to a ModelQuad-owned hook.
    #: Session scoped AND live -- the only channel that can track a
    #: mid-session change.
    HOST_PUSH = "host_push"
    #: The host's own configuration file. Installation scoped, so it can
    #: disagree with what the current session actually runs.
    HOST_CONFIG = "host_config"
    #: MCP protocol ``clientInfo`` supplied during ``initialize``. Scoped to
    #: the CONNECTION: it describes who opened the socket, and is fixed for
    #: that connection's lifetime.
    MCP_CLIENT_INFO = "mcp_client_info"
    #: ModelQuad's own measurement of its own process.
    SELF_MEASURED = "self_measured"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------
# Normalizers. Every value crossing the boundary passes through one of these,
# so a field can only ever hold a shape this module recognizes.
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION_RE = re.compile(r"^\d{1,6}(?:\.\d{1,6}){0,3}$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_DISPLAY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,47}$")
_AI_AGENT_RE = re.compile(r"^claude-code_(\d{1,6}(?:-\d{1,6}){1,3})_agent$")

#: Closed vocabulary. An unrecognized effort value reads UNKNOWN rather than
#: passing through -- reasoning effort is a research axis, not free text.
#: ``minimal`` is Codex's floor (``execution/codex.py`` VALID_REASONING_EFFORTS)
#: and is accepted here so a real observed effort is not thrown away as
#: unrecognized. Codex does NOT validate this key itself -- ``-c
#: model_reasoning_effort=bogus`` was accepted verbatim by codex-cli 0.147.0
#: -- so normalizing against a closed set is the only thing standing between
#: the host's config file and the research axis.
_REASONING_MODES: frozenset[str] = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)


def _safe_token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _TOKEN_RE.match(value) else None


def _safe_version(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _VERSION_RE.match(value) else None


def _safe_model_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _MODEL_ID_RE.match(value) else None


def _safe_display_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _DISPLAY_NAME_RE.match(value) else None


def _safe_reasoning_mode(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    return lowered if lowered in _REASONING_MODES else None


def _safe_int(value: Any) -> int | None:
    # bool is an int subclass; a JSON true must never read as a count.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def uptime_bucket(seconds: float | None) -> str | None:
    """Coarse bucket for how long the observing process has been alive.

    Buckets, not seconds: the exact lifetime of a process is a fine-grained
    series that narrows an anonymity set for no routing benefit, while the
    order of magnitude is exactly what the "fresh session vs long session"
    hypothesis needs.
    """

    if seconds is None or seconds < 0:
        return None
    if seconds < 300:
        return "lt_5m"
    if seconds < 1800:
        return "5m_to_30m"
    if seconds < 7200:
        return "30m_to_2h"
    if seconds < 21600:
        return "2h_to_6h"
    return "gte_6h"


def payload_size_bucket(size: int | None) -> str | None:
    """Coarse bucket for a handoff payload's SIZE. The payload never travels."""

    if size is None or size < 0:
        return None
    if size == 0:
        return "empty"
    if size < 512:
        return "lt_512b"
    if size < 4096:
        return "512b_to_4k"
    if size < 32768:
        return "4k_to_32k"
    return "gte_32k"


def session_fingerprint(
    session_id: str | None, *, state_dir: Path | None = None
) -> str | None:
    """Stable, installation-scoped pseudonym for a host session id.

    The raw session id is never stored: it is an identifier minted by the
    host, and correlating it across installations is exactly the linkage the
    privacy rules refuse. HMAC-keyed on the installation id (itself random and
    user-resettable), with a purpose string so this derivation can never
    collide with another use of the same key. Resetting the installation id
    therefore also severs every session fingerprint -- the unlinkability
    lever keeps working.
    """

    if not isinstance(session_id, str) or not session_id.strip():
        return None
    try:
        from .identity import installation_id

        key = installation_id(state_dir).encode("utf-8")
    except Exception:  # noqa: BLE001 -- identity must never break routing
        return None
    message = b"modelquad:host-session-fingerprint:v1:" + session_id.strip().encode("utf-8")
    return hmac.new(key, message, sha256).hexdigest()[:16]


# --------------------------------------------------------------------------
# The observation and the snapshot
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HostContextMetrics:
    """Safe, derived facts about the host's session state.

    Every field is a count, a bucket, a fingerprint or a closed-vocabulary
    token derived WITHOUT reading conversation content. Absent means UNKNOWN,
    never zero: "we cannot see the turn count" and "the session has had zero
    turns" are different research facts and must stay distinguishable.
    """

    #: HMAC pseudonym of the host session id; groups decisions within one
    #: session without identifying it. UNKNOWN when the host exposes none.
    session_fingerprint: str | None = None
    #: How long the process observing the host has been alive, bucketed.
    #: A PROXY for session age -- deliberately named for what it measures,
    #: because a resumed session restarts the process.
    observer_uptime_bucket: str | None = None
    #: How many routing decisions this observer process has already served.
    #: ModelQuad's own count of its own work: objective, and the only
    #: "prior substantive work in this session" signal available without
    #: reading the transcript. NOT the host's turn count, which is not
    #: observable through any channel this module is willing to use.
    prior_modelquad_decisions: int | None = None
    #: Host-reported flag that the conversation exceeds a large-context
    #: threshold. Only ever populated from a host push; None means the host
    #: did not say, NOT that the context is small.
    context_exceeds_large_threshold: bool | None = None
    #: Caller-declared context ownership/richness (task capsule). Declared,
    #: never classified by ModelQuad -- there is no deterministic basis for
    #: a "richness" classifier and inventing one would poison the axis.
    context_owner: str | None = None
    context_richness: str | None = None
    #: Whether the routed target differs from the current host runtime, and
    #: how big the payload that would have to travel is. Filled in at
    #: decision time (see :func:`with_decision_context`), not by an observer.
    handoff_required: bool | None = None
    handoff_payload_bucket: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_fingerprint": self.session_fingerprint,
            "observer_uptime_bucket": self.observer_uptime_bucket,
            "prior_modelquad_decisions": self.prior_modelquad_decisions,
            "context_exceeds_large_threshold": self.context_exceeds_large_threshold,
            "context_owner": self.context_owner,
            "context_richness": self.context_richness,
            "handoff_required": self.handoff_required,
            "handoff_payload_bucket": self.handoff_payload_bucket,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "HostContextMetrics":
        if not isinstance(data, dict):
            return cls()
        exceeds = data.get("context_exceeds_large_threshold")
        handoff = data.get("handoff_required")
        return cls(
            session_fingerprint=_safe_token(data.get("session_fingerprint")),
            observer_uptime_bucket=_safe_token(data.get("observer_uptime_bucket")),
            prior_modelquad_decisions=_safe_int(data.get("prior_modelquad_decisions")),
            context_exceeds_large_threshold=(exceeds if isinstance(exceeds, bool) else None),
            context_owner=_safe_token(data.get("context_owner")),
            context_richness=_safe_token(data.get("context_richness")),
            handoff_required=(handoff if isinstance(handoff, bool) else None),
            handoff_payload_bucket=_safe_token(data.get("handoff_payload_bucket")),
        )


@dataclass(frozen=True)
class HostRuntimeObservation:
    """What one :class:`HostObserver` saw, already normalized and safe.

    Provenance is kept per fact rather than once per observation, because a
    single observation routinely mixes channels: the runtime version can come
    from the process environment while the model comes from a host push and
    the reasoning mode from configuration. Collapsing them would let the
    weakest channel describe the strongest fact.
    """

    runtime: str
    runtime_version: str | None = None
    runtime_version_source: HostFactSource = HostFactSource.UNKNOWN
    entrypoint: str | None = None
    model_family: str | None = None
    model_exact_id: str | None = None
    model_display_name: str | None = None
    model_provider: str | None = None
    model_source: ModelIdentitySource = ModelIdentitySource.UNKNOWN
    model_fact_source: HostFactSource = HostFactSource.UNKNOWN
    reasoning_mode: str | None = None
    reasoning_mode_source: HostFactSource = HostFactSource.UNKNOWN
    context: HostContextMetrics = field(default_factory=HostContextMetrics)
    #: Every channel that contributed anything, for auditability.
    fact_sources: tuple[HostFactSource, ...] = ()
    #: Channels that were consulted and had nothing to say. An honest record
    #: of "we looked and it was empty", distinct from "we never looked".
    unavailable_sources: tuple[HostFactSource, ...] = ()
    observed_at: str | None = None

    def identity(self) -> RuntimeModelIdentity:
        """The role-tagged identity this observation supports, and no more."""

        return RuntimeModelIdentity(
            runtime=self.runtime,
            runtime_version=self.runtime_version,
            model_provider=self.model_provider,
            model_family=self.model_family,
            model_exact_id=self.model_exact_id,
            model_display_name=self.model_display_name,
            model_source=self.model_source,
            role=RuntimeRole.CURRENT_HOST,
            observed_at=self.observed_at,
        )


@dataclass(frozen=True)
class HostDecisionSnapshot:
    """The host, frozen at ONE routing decision. Immutable by construction.

    This is what is persisted and what a research record reads. It answers,
    for a single decision: which host the user was in, which runtime version,
    which model and how trustworthy that identity was, what safe context
    state existed, and whether delegation would have required a handoff.
    """

    snapshot_version: int
    identity: RuntimeModelIdentity
    context: HostContextMetrics
    entrypoint: str | None = None
    runtime_version_source: HostFactSource = HostFactSource.UNKNOWN
    model_fact_source: HostFactSource = HostFactSource.UNKNOWN
    reasoning_mode: str | None = None
    reasoning_mode_source: HostFactSource = HostFactSource.UNKNOWN
    fact_sources: tuple[HostFactSource, ...] = ()
    unavailable_sources: tuple[HostFactSource, ...] = ()
    #: Host client name from MCP ``clientInfo`` -- protocol-level, and the
    #: only host fact a calling model cannot fabricate through tool arguments.
    mcp_client_name: str | None = None
    observed: bool = True
    observed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_version": self.snapshot_version,
            "observed": self.observed,
            "identity": self.identity.to_dict(),
            "context": self.context.to_dict(),
            "entrypoint": self.entrypoint,
            "runtime_version_source": self.runtime_version_source.value,
            "model_fact_source": self.model_fact_source.value,
            "reasoning_mode": self.reasoning_mode,
            "reasoning_mode_source": self.reasoning_mode_source.value,
            "fact_sources": [s.value for s in self.fact_sources],
            "unavailable_sources": [s.value for s in self.unavailable_sources],
            "mcp_client_name": self.mcp_client_name,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "HostDecisionSnapshot | None":
        """Rebuild a persisted snapshot, or None when there is not one.

        Re-validates rather than trusting the stored bytes: a snapshot read
        back from disk is input like any other, and the safe shape is cheaper
        to re-enforce than to assume.
        """

        if not isinstance(data, dict):
            return None
        identity_raw = data.get("identity")
        identity = (
            RuntimeModelIdentity.from_dict(identity_raw)
            if isinstance(identity_raw, dict)
            else RuntimeModelIdentity(role=RuntimeRole.CURRENT_HOST)
        )
        version = data.get("snapshot_version")
        return cls(
            snapshot_version=version if isinstance(version, int) else HOST_SNAPSHOT_VERSION,
            identity=identity,
            context=HostContextMetrics.from_dict(data.get("context")),
            entrypoint=_safe_token(data.get("entrypoint")),
            runtime_version_source=_fact_source(data.get("runtime_version_source")),
            model_fact_source=_fact_source(data.get("model_fact_source")),
            reasoning_mode=_safe_reasoning_mode(data.get("reasoning_mode")),
            reasoning_mode_source=_fact_source(data.get("reasoning_mode_source")),
            fact_sources=tuple(_fact_source(s) for s in data.get("fact_sources") or ()),
            unavailable_sources=tuple(
                _fact_source(s) for s in data.get("unavailable_sources") or ()
            ),
            mcp_client_name=_safe_token(data.get("mcp_client_name")),
            observed=bool(data.get("observed", True)),
            observed_at=data.get("observed_at"),
        )


def _ordered_sources(*groups: "tuple[HostFactSource, ...]") -> tuple[HostFactSource, ...]:
    """Concatenate channel lists, keeping first-seen order and no duplicates."""

    seen: dict[HostFactSource, None] = {}
    for group in groups:
        for source in group:
            seen.setdefault(source, None)
    return tuple(seen)


def _fact_source(value: Any) -> HostFactSource:
    try:
        return HostFactSource(value)
    except (ValueError, TypeError):
        return HostFactSource.UNKNOWN


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def unobserved_snapshot(mcp_client_name: str | None = None) -> HostDecisionSnapshot:
    """The honest snapshot for "no observer recognized this host".

    Routing must never depend on host observability, so an unobservable host
    yields a snapshot that says so rather than an absence the reader has to
    interpret -- and never a guessed identity.
    """

    return HostDecisionSnapshot(
        snapshot_version=HOST_SNAPSHOT_VERSION,
        identity=RuntimeModelIdentity(role=RuntimeRole.CURRENT_HOST),
        context=HostContextMetrics(),
        observed=False,
        mcp_client_name=_safe_token(mcp_client_name),
        observed_at=_now(),
    )


# --------------------------------------------------------------------------
# Observers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HostProtocolIdentity:
    """What the host said about itself in the MCP ``initialize`` handshake.

    Supplied by the connecting client's own MCP implementation, so -- like
    the process environment, and unlike a tool argument -- a calling model
    cannot fabricate it. Unlike the process environment it is *session
    scoped by construction*: it arrives on the connection this decision is
    being served on, so it cannot be a stale reading from another session.

    This exists because the original abstraction was validated against one
    runtime. Claude Code hands ModelQuad a rich environment and a bare client
    name; Codex hands it a scrubbed environment and a rich client name (see
    :class:`CodexHostObserver`). ``clientInfo.version`` was already arriving
    on every request and was being discarded at the boundary.

    Normalized on construction: a host is a data source like any other, and
    ``title`` is free text the host chooses.
    """

    name: str | None = None
    title: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _safe_token(self.name))
        object.__setattr__(self, "title", _safe_display_name(self.title))
        object.__setattr__(self, "version", _safe_version(self.version))

    def __bool__(self) -> bool:
        return any((self.name, self.title, self.version))


#: MCP reserves ``_meta`` keys of the form ``<namespace>/<name>``, where the
#: namespace is a DNS-style label owned by the vendor. Matching the shape --
#: rather than a hardcoded literal -- is what lets an unrecognized namespace
#: be *recorded as unrecognized* instead of being silently dropped or, worse,
#: mistaken for a known one.
_META_KEY_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9.-]{0,63})/([A-Za-z0-9_.-]{1,64})$")


@dataclass(frozen=True)
class HostRequestMetadata:
    """Vendor ``_meta`` from the MCP request this decision is being served for.

    A ``clientInfo`` is fixed when the socket opens; a ``_meta`` arrives with
    the call, so it is the one channel that can still be true after a
    mid-session change. Neither can be fabricated through tool *arguments*.
    No supported host is observed through it today; it is threaded through so
    an observer can claim from the request without a new parameter.

    Deliberately NOT a general ``_meta`` reader. Only the vendor namespace
    tokens present survive construction; every value is discarded.
    """

    namespaces: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.namespaces)

    def has_namespace(self, namespace: str) -> bool:
        return namespace in self.namespaces


def host_request_metadata_from_meta(meta: Any) -> HostRequestMetadata:
    """Project a raw MCP request ``_meta`` onto the namespace tokens above.

    This is the ephemeral boundary for the request channel: the caller hands
    over the raw mapping, no value is read, and the mapping is not retained. A malformed, hostile or absent payload yields an
    empty record rather than raising -- host observability is evidence, never
    a dependency.
    """

    if not isinstance(meta, dict):
        return HostRequestMetadata()
    namespaces: set[str] = set()
    for key in meta:
        if not isinstance(key, str):
            continue
        match = _META_KEY_RE.match(key)
        if match:
            namespaces.add(match.group(1))
    return HostRequestMetadata(namespaces=frozenset(namespaces))


@runtime_checkable
class HostObserver(Protocol):
    """A runtime-specific probe: "am I inside this host, and what is it?".

    ``claims()`` must be cheap and side-effect free; ``observe()`` may read
    allow-listed environment and ModelQuad-owned state, and must never raise.
    """

    runtime_id: str

    def claims(self) -> bool: ...

    def observe(self) -> HostRuntimeObservation | None: ...


#: The client-name prefix consistent with a Claude Code host. Observed
#: values: ``claude-code`` from Claude Code itself (recorded in
#: ``routing_decisions.evidence_json`` alongside ``runtime=claude_code``), and
#: ``claude desktop`` from the Desktop host. See
#: :func:`protocol_contradicts_runtime`.
CLAUDE_CLIENT_NAME_PREFIX = "claude"


def protocol_contradicts_runtime(
    protocol: "HostProtocolIdentity | None", name_prefix: str
) -> bool:
    """Does the live connection name a client this runtime cannot be?

    The generic rule, and the reason it is a *function* rather than a line
    inside one observer.

    :func:`default_observers` already orders session-scoped channels above
    inheritable ones, which resolves the case where both channels identify a
    host. It does nothing for the case where only the *inheritable* one does:
    a connection whose ``clientInfo`` names a client no observer recognizes
    falls straight through to the environment, and an environment marker is
    inherited by every descendant process. So an unrecognized client served
    from inside a Claude Code shell was recorded as Claude Code -- ordering
    intact, answer wrong.

    The rule: a named client is positive evidence about the CURRENT
    connection, and an inheritable claim must yield to it. A connection that
    names *nothing* is not evidence of anything and leaves the environment in
    charge -- which is the CLI path, and the Claude Desktop path.

    Deliberately one-directional. This can only ever *withdraw* a claim, never
    create one: a client name alone never promotes an environment-based
    observer into claiming a host it has no other evidence for.
    """

    name = protocol.name if protocol else None
    if not name:
        return False
    return not name.lower().startswith(name_prefix)


#: The ONLY environment names any observer in this module reads. Anything not
#: named here is structurally unreachable, which is what makes the adversarial
#: leak tests meaningful: they poison the environment and assert nothing lands.
CLAUDE_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "AI_AGENT",
        "CLAUDE_AGENT_SDK_VERSION",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_EFFORT",
    }
)

#: Named, audited refusals: documentation with a test behind it. All of these
#: were observed in a real Claude Code session's environment (2026-08-26) and
#: are deliberately not collected -- two are live credentials, three are
#: paths or endpoints, and the rest add identifiability with no routing
#: hypothesis behind them.
CLAUDE_ENV_REFUSED: frozenset[str] = frozenset(
    {
        "CLAUDE_CODE_MESSAGING_TOKEN",   # a live secret
        "CLAUDE_CODE_OAUTH_SCOPES",      # authorization surface
        "CLAUDE_CODE_MESSAGING_SOCKET",  # named-pipe path
        "CLAUDE_CODE_EXECPATH",          # absolute path
        "CLAUDE_CODE_HOST_SESSION_ID",   # second identifier, no added hypothesis
        "CLAUDE_PID",                    # machine-local process identity
        "ANTHROPIC_BASE_URL",            # endpoint configuration
        "BAGGAGE",                       # tracing envelope, carries app build ids
        "PWD",                           # path
    }
)


def _read_allowlisted_env(
    allowlist: frozenset[str], environ: dict[str, str] | None = None
) -> dict[str, str]:
    """Project the environment onto an allow-list, before anything reads it."""

    source = os.environ if environ is None else environ
    return {name: source[name] for name in allowlist if source.get(name)}


def _claude_settings_model(settings_path: Path) -> tuple[str | None, str | None]:
    """Read ONLY the declared model and effort keys from the host's settings.

    This is the host's own configuration file -- the same one ModelQuad's
    ``install`` command already writes its hook and statusLine entries into -- not
    private session state. Two named keys are read and everything else in the
    document is ignored, so widening this into a general settings reader
    would have to be a visible change rather than an accident.

    CONFIGURED is the strongest provenance this channel can support, and
    three separate things can make it disagree with the running session: a
    ``--model`` flag at launch, a mid-session switch, and a project-level
    settings file that overrides the user-level one (deliberately not read
    here -- a project path is exactly what the privacy rules refuse to touch).
    """

    try:
        raw = settings_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    return _safe_token(data.get("model")), _safe_reasoning_mode(data.get("effortLevel"))


def read_host_push(runtime: str, state_dir: Path | None = None) -> dict[str, Any] | None:
    """Read the newest host push for a runtime, re-validating every field.

    Written by a ModelQuad-owned host hook (see :func:`record_host_push`).
    The reader does not trust the writer: the file is state on disk, so its
    fields go through the same normalizers again.
    """

    name = HOST_PUSH_FILE_NAMES.get(runtime)
    if name is None:
        return None
    if state_dir is None:
        try:
            from . import paths

            state_dir = paths.state_root()
        except Exception:  # noqa: BLE001
            return None
    try:
        data = json.loads(Path(state_dir, name).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    exceeds = data.get("context_exceeds_large_threshold")
    return {
        "runtime": _safe_token(data.get("runtime")),
        "runtime_version": _safe_version(data.get("runtime_version")),
        "session_fingerprint": _safe_token(data.get("session_fingerprint")),
        "model_exact_id": _safe_model_id(data.get("model_exact_id")),
        "model_display_name": _safe_display_name(data.get("model_display_name")),
        "context_exceeds_large_threshold": exceeds if isinstance(exceeds, bool) else None,
        "observed_at": data.get("observed_at"),
    }


def record_host_push(
    runtime: str,
    payload: Any,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Normalize a host-pushed payload and cache it for the next decision.

    This is the ephemeral boundary for the push channel: the raw payload is
    read once, here, reduced to the handful of safe fields below, and
    dropped. Claude Code's statusLine payload is known to carry a transcript
    path, a workspace path and cost figures; none of them are read, and there
    is no code path by which they could be.

    Returns the normalized record, or None when the payload carried nothing
    usable -- in which case nothing is written, so a content-free render can
    never erase a real observation.
    """

    if not isinstance(payload, dict):
        return None
    name = HOST_PUSH_FILE_NAMES.get(runtime)
    if name is None:
        return None
    model = payload.get("model")
    model = model if isinstance(model, dict) else {}
    exceeds = payload.get("exceeds_200k_tokens")
    record = {
        "observation_version": HOST_OBSERVATION_VERSION,
        "runtime": runtime,
        "runtime_version": _safe_version(payload.get("version")),
        "session_fingerprint": session_fingerprint(
            payload.get("session_id"), state_dir=state_dir
        ),
        "model_exact_id": _safe_model_id(model.get("id")),
        "model_display_name": _safe_display_name(model.get("display_name")),
        "context_exceeds_large_threshold": exceeds if isinstance(exceeds, bool) else None,
        "observed_at": _now(),
    }
    substantive = any(
        record[key] is not None
        for key in (
            "runtime_version",
            "model_exact_id",
            "model_display_name",
            "context_exceeds_large_threshold",
        )
    )
    if not substantive:
        return None
    if state_dir is None:
        try:
            from . import paths

            state_dir = paths.state_root()
        except Exception:  # noqa: BLE001
            return record
    target = Path(state_dir, name)
    try:
        # Every running Claude Code session pushes through this hook, so two
        # sessions can cache the same file at once. ``atomic_write`` stages
        # each writer in its own exclusively created temp file; a shared
        # ``.json.tmp`` name let one writer truncate the other's staging file
        # and lose its rename (the rename race, in a milder form).
        atomic_write(target, json.dumps(record, indent=2) + "\n")
    except OSError:
        # A host hook must never fail because an observation could not be
        # cached. Losing one push costs a snapshot field, not a session.
        pass
    return record


class ClaudeCodeHostObserver:
    """Observe a Claude Code host through channels it already gives ModelQuad.

    Channel audit, in descending order of what each can support:

    ``PROCESS_ENV``
        Claude Code spawns the ModelQuad MCP server and its hooks, so their
        environment is session-scoped fact that a calling model cannot
        fabricate. Carries the runtime marker, the runtime version, the
        entrypoint, the session id and the reasoning effort -- but is frozen
        at spawn, so a mid-session effort change is NOT visible here.

    ``HOST_PUSH``
        Claude Code renders a statusLine by invoking a ModelQuad-owned
        command with a JSON payload on stdin. This is the only channel that
        is both session-scoped and live, and the only one that could carry an
        exact model id. **UNVERIFIED on the development machine**: no
        statusLine-sourced observation has ever been recorded there (every
        Claude reading came from the Desktop plan-history collector), so the
        payload's model fields are read defensively and their absence is the
        expected case, not a defect.

    ``HOST_CONFIG``
        The host's settings file yields a model FAMILY ("opus") and an effort
        level. Installation-scoped, hence CONFIGURED, hence never a
        substitute for an exact id.

    Audited and REJECTED as sources:

    - ``claude --version`` on PATH. Observed 2026-08-26 to report ``2.1.233``
      while the running session's own environment reported ``2.1.246``: the
      CLI on PATH is a different installation from the one hosting the
      session. A probe that can be confidently wrong is worse than an
      UNKNOWN, so the host runtime version is never taken from it.
    - The conversation transcript. It would answer turn count and exact model
      directly, and is refused outright: wanting metadata is not a reason to
      read a conversation.
    - Claude's private session and state directories, for the same reason.
    """

    runtime_id = "claude_code"

    def __init__(
        self,
        *,
        environ: dict[str, str] | None = None,
        settings_path: Path | None = None,
        state_dir: Path | None = None,
        protocol: HostProtocolIdentity | None = None,
    ) -> None:
        self._environ = environ
        self._settings_path = settings_path
        self._state_dir = state_dir
        # Not an observation channel for this runtime -- Claude Code's
        # ``clientInfo`` is a bare name and carries nothing the environment
        # does not already say. It is here only so a client naming somebody
        # else can withdraw an inherited claim.
        self._protocol = protocol

    def _env(self) -> dict[str, str]:
        return _read_allowlisted_env(CLAUDE_ENV_ALLOWLIST, self._environ)

    def claims(self) -> bool:
        """True when this process was spawned by Claude Code, and the live
        connection does not say otherwise.

        ``CLAUDECODE`` is set by the host itself in every process it starts,
        which is why presence is treated as fact. Nothing else in the
        allow-list is load-bearing for the claim: a process that sets only
        ``AI_AGENT`` is not evidence of a Claude Code host.

        It is fact about an *ancestor*, though, and that is the whole
        difficulty. ``CLAUDECODE`` is inherited by every descendant and by
        everything those spawn in turn, so the marker outlives the session
        that set it. When the connection being served names a client that is
        not Claude, that name is the better evidence and this claim is
        withdrawn -- see :func:`protocol_contradicts_runtime`. A connection
        that names nothing withdraws nothing.
        """

        if not self._env().get("CLAUDECODE"):
            return False
        return not protocol_contradicts_runtime(
            self._protocol, CLAUDE_CLIENT_NAME_PREFIX
        )

    def _resolved_settings_path(self) -> Path | None:
        if self._settings_path is not None:
            return self._settings_path
        try:
            from .adapters.registry import get_host_descriptor

            return get_host_descriptor("claude").default_target_dir() / "settings.json"
        except Exception:  # noqa: BLE001 -- config discovery is best-effort
            return None

    def observe(self) -> HostRuntimeObservation | None:
        if not self.claims():
            return None
        env = self._env()
        used: list[HostFactSource] = [HostFactSource.PROCESS_ENV]
        empty: list[HostFactSource] = []

        runtime_version = None
        match = _AI_AGENT_RE.match(env.get("AI_AGENT", ""))
        if match:
            runtime_version = _safe_version(match.group(1).replace("-", "."))
        version_source = (
            HostFactSource.PROCESS_ENV if runtime_version else HostFactSource.UNKNOWN
        )

        reasoning_mode = _safe_reasoning_mode(env.get("CLAUDE_EFFORT"))
        reasoning_source = (
            HostFactSource.PROCESS_ENV if reasoning_mode else HostFactSource.UNKNOWN
        )

        fingerprint = session_fingerprint(
            env.get("CLAUDE_CODE_SESSION_ID"), state_dir=self._state_dir
        )

        # -- channel: host configuration (family only, CONFIGURED) ---------
        model_family: str | None = None
        model_exact_id: str | None = None
        model_display_name: str | None = None
        model_source = ModelIdentitySource.UNKNOWN
        model_fact_source = HostFactSource.UNKNOWN
        settings_path = self._resolved_settings_path()
        configured_model, configured_effort = (
            _claude_settings_model(settings_path) if settings_path else (None, None)
        )
        if configured_model or configured_effort:
            used.append(HostFactSource.HOST_CONFIG)
        else:
            empty.append(HostFactSource.HOST_CONFIG)
        if configured_model:
            # "opus" is a family, not a release. Recording it as an exact id
            # would make Opus 4.8 and Opus 5 indistinguishable, destroying
            # the drift axis this dataset exists to preserve. A
            # configured value that IS an exact id is kept as one -- but the
            # provenance stays CONFIGURED either way.
            from .models import model_family_from_exact_id

            derived = model_family_from_exact_id(configured_model)
            if derived is not None and derived != configured_model:
                model_family, model_exact_id = derived, configured_model
            else:
                model_family = derived or configured_model
            model_source = ModelIdentitySource.CONFIGURED
            model_fact_source = HostFactSource.HOST_CONFIG
        if reasoning_mode is None and configured_effort:
            reasoning_mode = configured_effort
            reasoning_source = HostFactSource.HOST_CONFIG

        # -- channel: host push (exact model, live) ------------------------
        push = read_host_push(self.runtime_id, self._state_dir)
        # A push is believed only when it fingerprints to THIS session.
        # Without that check a push from a previous session -- possibly a
        # different model -- would silently describe this decision.
        push_matches = bool(
            push and fingerprint and push.get("session_fingerprint") == fingerprint
        )
        exceeds_large = None
        if push_matches and push is not None:
            used.append(HostFactSource.HOST_PUSH)
            exceeds_large = push.get("context_exceeds_large_threshold")
            if push.get("runtime_version"):
                runtime_version = push["runtime_version"]
                version_source = HostFactSource.HOST_PUSH
            if push.get("model_exact_id"):
                model_exact_id = push["model_exact_id"]
                model_display_name = push.get("model_display_name")
                # The host naming its own model is REPORTED, never CONFIRMED:
                # ModelQuad did not observe which model served the turn.
                model_source = ModelIdentitySource.REPORTED
                model_fact_source = HostFactSource.HOST_PUSH
                model_family = None  # re-derived from the exact id
        else:
            empty.append(HostFactSource.HOST_PUSH)

        return HostRuntimeObservation(
            runtime=self.runtime_id,
            runtime_version=runtime_version,
            runtime_version_source=version_source,
            entrypoint=_safe_token(env.get("CLAUDE_CODE_ENTRYPOINT")),
            model_family=model_family,
            model_exact_id=model_exact_id,
            model_display_name=model_display_name,
            model_provider="claude" if (model_family or model_exact_id) else None,
            model_source=model_source,
            model_fact_source=model_fact_source,
            reasoning_mode=reasoning_mode,
            reasoning_mode_source=reasoning_source,
            context=HostContextMetrics(
                session_fingerprint=fingerprint,
                context_exceeds_large_threshold=exceeds_large,
            ),
            fact_sources=tuple(used),
            unavailable_sources=tuple(empty),
            observed_at=_now(),
        )


#: Codex reads NOTHING from the environment, and that is a finding rather
#: than an omission: codex-cli 0.147.0 spawns an MCP server with a scrubbed
#: 19-variable environment (OS paths and user directories only) carrying no
#: marker, no version, no session id, no model and no effort. An empty
#: allow-list makes that structurally true -- there is no name a poisoned
#: environment could use to reach a Codex observation.
CODEX_ENV_ALLOWLIST: frozenset[str] = frozenset()

#: The ONLY keys read from the Codex host's ``config.toml``. Both are
#: top-level scalars; the reader never walks the document. Named separately
#: and then collected, so the reader binds each key by name -- a positional
#: read would break the moment the tuple gained a third entry.
CODEX_MODEL_KEY = "model"
CODEX_EFFORT_KEY = "model_reasoning_effort"
CODEX_CONFIG_KEYS: tuple[str, ...] = (CODEX_MODEL_KEY, CODEX_EFFORT_KEY)

#: Named, audited refusals in the Codex host's own state directory. Every one
#: was observed on the development machine (2026-08-26) and is deliberately
#: not read. Documentation with a test behind it.
CODEX_SOURCES_REFUSED: frozenset[str] = frozenset(
    {
        "sessions",                  # rollout transcripts -- the conversation
        "session_index.jsonl",       # per-session index, carries workspace paths
        "history.jsonl",             # prompt history
        "auth.json",                 # live credentials
        ".codex-global-state.json",  # private host state
        "state_5.sqlite",            # private host state
        "config.toml:projects",      # a table of absolute workspace paths
        "config.toml:notify",        # an absolute path to a user program
        "config.toml:mcp_servers",   # commands, args and injected env
    }
)


def _codex_config_facts(config_path: Path) -> tuple[str | None, str | None]:
    """Read ONLY the two declared top-level keys from Codex's config.

    Deliberately shaped like :func:`_claude_settings_model`: named keys, never
    a general settings reader, so widening it would have to be a visible
    change rather than an accident. That discipline earns more here than it
    did for Claude, because this file is *known* to be full of what section 20
    refuses -- the development machine's copy carries a ``[projects.'...']``
    table of 25+ absolute workspace paths, a ``notify`` entry naming an
    executable under ``%LOCALAPPDATA%``, and an ``mcp_servers`` table of
    commands. None of them are reachable from here.

    The document is parsed whole because that is what a TOML parser does; the
    parse result is local to this call and exactly two scalars survive it.

    ``tomllib`` is stdlib on every supported interpreter, so this adds no
    dependency.
    """

    from .codex_config import read_config
    return read_config(config_path, for_execution=False)


class CodexHostObserver:
    """Observe a Codex host. The channel profile is Claude Code's, inverted.

    Slice 6 built this abstraction against a single runtime, which is the
    state in which an abstraction is most likely to be quietly wrong. Codex
    is the useful second case precisely because it does not resemble the
    first. Audited against codex-cli 0.147.0 on 2026-08-26:

    ``MCP_CLIENT_INFO`` -- the primary channel, and the only session-scoped one
        ``initialize`` carries ``clientInfo = {name: "codex-mcp-client",
        title: "Codex", version: "0.147.0"}``. That version comes from the
        process actually hosting the session, over the very connection this
        decision is served on. It is the fact that was *unobtainable* for
        Claude Code, where the PATH CLI reported 2.1.233 while the hosting
        session reported 2.1.246 -- so Codex's runtime version is strong
        exactly where Claude's had to fall back to the environment.

    ``HOST_CONFIG`` -- ``~/.codex/config.toml``
        Yields ``model`` and ``model_reasoning_effort``. Note what differs
        from Claude: this is an **exact model id** (``gpt-5.6-terra``), not a
        family. It is still CONFIGURED and still never CONFIRMED --
        installation-scoped configuration cannot testify that this model
        served this turn, and ``-m/--model`` at launch, a ``--profile``
        layer, and a mid-session switch can each make it wrong.

    ``PROCESS_ENV`` -- **empty, and verified empty**
        Codex spawns MCP servers with a scrubbed 19-variable environment: no
        ``CODEX``-anything, no version, no session id, no model, no effort.
        The exact inverse of Claude Code, whose environment carries almost
        everything. Recorded in ``unavailable_sources``, because "we looked
        and it was empty" is a different fact from "we never looked".

    Two consequences, stated plainly rather than papered over:

    - **A Codex host has no session fingerprint.** No channel this module is
      willing to use exposes a session id, so decisions cannot be grouped
      into a session. That is UNKNOWN, not a synthesized id.
    - **A Codex host has no live channel.** Every fact is fixed at connection
      time or read from installation-scoped config, so a mid-session change
      is invisible. Claude has that blind spot for reasoning effort; Codex
      has it for the model as well.

    Audited and REJECTED as sources:

    - ``codex --version`` on PATH. It happened to agree with the session here
      (both 0.147.0), but agreement in one observation is not a guarantee,
      and ``clientInfo`` already answers the question from *inside* the
      session. A weaker channel is not worth consulting for a fact a stronger
      one already owns.
    - The ``notify`` hook, Codex's nearest equivalent to Claude's statusLine.
      It is a **single global slot**, and it was already occupied on the
      development machine by the user's own computer-use integration. Taking
      it would break a working integration to gain a turn-*ended* signal,
      which is the wrong moment for a decision-time snapshot anyway.
    - Codex's ``sessions/`` directory, ``session_index.jsonl`` and ``history.jsonl``.
      These would answer turn count and exact model directly, and are refused
      for the same reason as Claude's transcript: wanting metadata is not a
      reason to read a conversation.
    - ``auth.json`` (live credentials) and the private state stores.
    - Every table in ``config.toml`` beyond the two declared scalars. See
      :data:`CODEX_SOURCES_REFUSED`.
    """

    runtime_id = "codex"

    def __init__(
        self,
        *,
        protocol: HostProtocolIdentity | None = None,
        config_path: Path | None = None,
    ) -> None:
        self._protocol = protocol
        self._config_path = config_path

    def claims(self) -> bool:
        """True when the MCP client identified itself as Codex.

        Only ``clientInfo.name`` is load-bearing. ``title`` is a display
        string the host picks; letting it decide the claim would mean a
        cosmetic change upstream could silently start -- or stop --
        identifying a host.
        """

        name = self._protocol.name if self._protocol else None
        return bool(name and name.lower().startswith("codex"))

    def _resolved_config_path(self) -> Path | None:
        if self._config_path is not None:
            return self._config_path
        try:
            from .codex_config import config_path
            return config_path()
        except Exception:  # noqa: BLE001 -- config discovery is best-effort
            return None

    def observe(self) -> HostRuntimeObservation | None:
        if not self.claims():
            return None
        protocol = self._protocol or HostProtocolIdentity()
        used: list[HostFactSource] = [HostFactSource.MCP_CLIENT_INFO]
        # Audited and empty, not unexamined. See CODEX_ENV_ALLOWLIST.
        empty: list[HostFactSource] = [HostFactSource.PROCESS_ENV]

        runtime_version = protocol.version
        version_source = (
            HostFactSource.MCP_CLIENT_INFO if runtime_version else HostFactSource.UNKNOWN
        )

        config_path = self._resolved_config_path()
        configured_model, configured_effort = (
            _codex_config_facts(config_path) if config_path else (None, None)
        )
        if configured_model or configured_effort:
            used.append(HostFactSource.HOST_CONFIG)
        else:
            empty.append(HostFactSource.HOST_CONFIG)

        model_family: str | None = None
        model_exact_id: str | None = None
        model_source = ModelIdentitySource.UNKNOWN
        model_fact_source = HostFactSource.UNKNOWN
        if configured_model:
            from .models import model_family_from_exact_id

            # Codex configures an exact release, so unlike the Claude path
            # this normally IS an exact id. Deriving the family from it must
            # never raise provenance above CONFIGURED.
            model_exact_id = configured_model
            model_family = model_family_from_exact_id(configured_model)
            model_source = ModelIdentitySource.CONFIGURED
            model_fact_source = HostFactSource.HOST_CONFIG

        reasoning_mode = configured_effort
        reasoning_source = (
            HostFactSource.HOST_CONFIG if reasoning_mode else HostFactSource.UNKNOWN
        )

        return HostRuntimeObservation(
            runtime=self.runtime_id,
            runtime_version=runtime_version,
            runtime_version_source=version_source,
            # Codex exposes no entrypoint on any channel above. The field
            # stays UNKNOWN rather than being filled with the client name,
            # which is a different fact wearing the same shape.
            entrypoint=None,
            model_family=model_family,
            model_exact_id=model_exact_id,
            model_display_name=None,
            model_provider="codex" if (model_family or model_exact_id) else None,
            model_source=model_source,
            model_fact_source=model_fact_source,
            reasoning_mode=reasoning_mode,
            reasoning_mode_source=reasoning_source,
            # No channel exposes a Codex session id, so there is no
            # fingerprint to derive and none is invented.
            context=HostContextMetrics(),
            fact_sources=tuple(used),
            unavailable_sources=tuple(empty),
            observed_at=_now(),
        )


def default_observers(
    protocol: HostProtocolIdentity | None = None,
    request_meta: HostRequestMetadata | None = None,
) -> tuple[HostObserver, ...]:
    """The observers to try, in order. **The order is load-bearing.**

    Protocol-based observers come first, and that is a rule rather than a
    preference: an MCP ``clientInfo`` describes *the connection being served
    right now*, while an environment marker describes *some ancestor
    process*. ``CLAUDECODE=1`` is inherited by everything Claude Code spawns
    and by everything those spawn in turn, so a server started somewhere down
    that tree carries the marker no matter who later connects to it. Ordering
    the environment claim first therefore lets a stale ancestor outvote the
    client on the other end of the socket.

    That is not hypothetical: driving the production MCP server as a Codex
    client, from a shell inside a Claude Code session, recorded
    ``runtime=claude_code`` against a connection whose ``clientInfo`` said
    ``codex-mcp-client`` (2026-08-26). The identity was wrong in the one
    column the cross-host matrix is built on.

    So: a session-scoped channel outranks an inheritable one. Any future
    observer that claims from the protocol belongs above this line; any that
    claims from the environment belongs below it.

    ``protocol`` is the host's own ``initialize`` identity, threaded in
    rather than read from ambient state because it belongs to one connection
    -- and it is the entire reason a Codex host is observable at all
    (:class:`CodexHostObserver`).

    ``request_meta`` is the same argument one level stronger: a request
    ``_meta`` belongs to the call being served. An observer that claims from
    it would be ordered first.

    The ordering, and why each sits where it does:

    1. :class:`CodexHostObserver` -- claims from ``clientInfo`` only.
    2. :class:`ClaudeCodeHostObserver` -- claims from the process
       environment, which is inheritable and therefore last.
    """

    return (
        CodexHostObserver(protocol=protocol),
        ClaudeCodeHostObserver(protocol=protocol),
    )


# --------------------------------------------------------------------------
# Decision-time capture
# --------------------------------------------------------------------------

#: When this process started, for the observer-uptime proxy. Module import
#: happens once per MCP-server process, which the host spawns at session
#: start -- so this measures "how long ModelQuad has been serving this host",
#: which is what the field is named after.
_PROCESS_STARTED_AT = datetime.now(timezone.utc)

#: How many routing decisions this process has snapshotted. Process-local by
#: design: it counts ModelQuad's own work in this session, and must not be a
#: cross-session total.
_DECISIONS_OBSERVED = 0


def reset_process_counters(*, started_at: datetime | None = None) -> None:
    """Reset the process-local counters. Test seam; not used in production."""

    global _DECISIONS_OBSERVED, _PROCESS_STARTED_AT
    _DECISIONS_OBSERVED = 0
    if started_at is not None:
        _PROCESS_STARTED_AT = started_at


def capture_host_decision_snapshot(
    *,
    protocol: HostProtocolIdentity | None = None,
    request_meta: HostRequestMetadata | None = None,
    observers: tuple[HostObserver, ...] | None = None,
    now: datetime | None = None,
    count_decision: bool = True,
) -> HostDecisionSnapshot:
    """Freeze the current host for one routing decision. Never raises.

    ``protocol`` carries the host's MCP ``initialize`` identity, which is
    both the recorded ``mcp_client_name`` and -- for a host whose environment
    says nothing -- the observation channel itself. ``request_meta`` carries
    the vendor ``_meta`` of the single MCP request this decision is being
    served for. A caller without an MCP connection (the CLI) passes
    neither and gets exactly the observers that can work without one.

    Host observability is evidence, not a dependency: an observer that is
    absent, broken, or fed malformed metadata yields
    :func:`unobserved_snapshot` and routing proceeds unchanged (AGENTS.md
    invariant 4 -- unknown stays unknown).
    """

    global _DECISIONS_OBSERVED
    prior = _DECISIONS_OBSERVED
    if count_decision:
        _DECISIONS_OBSERVED += 1

    client_name = protocol.name if protocol else None
    observation: HostRuntimeObservation | None = None
    chain = (
        observers
        if observers is not None
        else default_observers(protocol, request_meta)
    )
    for observer in chain:
        try:
            if not observer.claims():
                continue
            observation = observer.observe()
        except Exception:  # noqa: BLE001 -- an observer may never break routing
            observation = None
        if observation is not None:
            break
    if observation is None:
        return unobserved_snapshot(client_name)

    moment = now or datetime.now(timezone.utc)
    uptime = (moment - _PROCESS_STARTED_AT).total_seconds()
    context = HostContextMetrics(
        session_fingerprint=observation.context.session_fingerprint,
        observer_uptime_bucket=uptime_bucket(uptime),
        prior_modelquad_decisions=prior,
        context_exceeds_large_threshold=observation.context.context_exceeds_large_threshold,
    )
    return HostDecisionSnapshot(
        snapshot_version=HOST_SNAPSHOT_VERSION,
        identity=observation.identity(),
        context=context,
        entrypoint=observation.entrypoint,
        runtime_version_source=observation.runtime_version_source,
        model_fact_source=observation.model_fact_source,
        reasoning_mode=observation.reasoning_mode,
        reasoning_mode_source=observation.reasoning_mode_source,
        # Deduplicated: an observer whose PRIMARY channel is clientInfo has
        # already claimed it, and a channel listed twice would read as two
        # independent contributions to one fact.
        fact_sources=_ordered_sources(
            observation.fact_sources,
            (HostFactSource.MCP_CLIENT_INFO,) if client_name else (),
        ),
        unavailable_sources=observation.unavailable_sources,
        mcp_client_name=client_name,
        observed=True,
        observed_at=observation.observed_at,
    )


def with_decision_context(
    snapshot: HostDecisionSnapshot,
    *,
    selected_runtime: str | None = None,
    context_owner: str | None = None,
    context_richness: str | None = None,
    handoff_payload_size: int | None = None,
) -> HostDecisionSnapshot:
    """Annotate a snapshot with facts only the decision itself knows.

    ``handoff_required`` is derived here rather than observed, because it is
    a property of the pairing (this host, that target), not of the host.
    Direction matters for the research question -- ``Claude -> Codex`` is not
    ``Codex -> Claude``, because what the handoff loses differs -- so the
    origin identity stays attached alongside it.

    A snapshot that never identified a host cannot say whether a handoff
    would be required, so it stays UNKNOWN rather than defaulting to True.

    Composable: an argument left as ``None`` leaves that field as it was, so
    a caller that learns the target only after annotating the context can
    layer the two without erasing the first.
    """

    from dataclasses import replace

    context = snapshot.context
    handoff_required = context.handoff_required
    host_runtime = snapshot.identity.runtime
    if host_runtime and selected_runtime:
        handoff_required = normalize_runtime_id(selected_runtime) != normalize_runtime_id(
            host_runtime
        )
    return replace(
        snapshot,
        context=replace(
            context,
            context_owner=_safe_token(context_owner) or context.context_owner,
            context_richness=_safe_token(context_richness) or context.context_richness,
            handoff_required=handoff_required,
            handoff_payload_bucket=(
                payload_size_bucket(handoff_payload_size)
                if handoff_payload_size is not None
                else context.handoff_payload_bucket
            ),
        ),
    )


def normalize_runtime_id(value: str) -> str:
    """Compare runtime ids across the two spellings in circulation.

    The registry's runtime id for Claude Code is ``claude``; the host
    observer names the runtime ``claude_code``. Both denote the same runtime,
    and treating them as different would report a handoff for a decision that
    keeps the task exactly where it is.
    """

    lowered = value.strip().lower().replace("-", "_")
    return {"claude": "claude_code", "claude_code": "claude_code"}.get(lowered, lowered)
