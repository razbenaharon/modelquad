# SPDX-FileCopyrightText: 2026 Raz Ben Aharon
# SPDX-License-Identifier: Apache-2.0
"""Canonical, provider-neutral capability truth.

Four different questions were previously answered by one boolean per
provider/capability pair, and collapsing them is what made ModelQuad lie about
its own agents:

1. **supported**   -- can this runtime do it *at all*?
2. **allowed**     -- does ModelQuad's policy permit it *here*?
3. **verified**    -- have we *observed* it work on this machine?
4. **operational** -- does it work *right now*?

The failure this module exists to end: a worker may be denied a capability
because ModelQuad cannot bound the grant (``AGENTS.md`` safety invariant 3),
not because the runtime cannot do it. With one boolean, "ModelQuad forbids this" and "the runtime cannot do this" are
the same value, so the router said the worker "lacks required capability
(command)" and the former Control Center said "Cannot run commands". Both statements
were false, and a false statement about an agent's ability is not cosmetic: it
is surfaced to the user, recorded as evidence, and learned from.

Keeping the four facts apart is therefore a correctness requirement, not a
modelling preference. **A restriction ModelQuad chose must always be
attributable to ModelQuad.**

This module is also the single home of the coarse capability vocabulary
(``read`` / ``write`` / ``command``) and of the fine-to-coarse mapping.
``config.py`` re-exports both so existing call sites read unchanged; nothing
else may keep a private copy, because two copies of one mapping are free to
disagree -- and already had.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# --- the coarse vocabulary ---------------------------------------------------
# Deliberately coarse and provider-neutral: what a delegated worker can DO
# inside its workspace, not which product it is. Provider-specific facts belong
# in the evidence attached to a fact, never in a new capability name.

CAP_READ = "read"
CAP_WRITE = "write"
CAP_COMMAND = "command"
ALL_CAPABILITIES = frozenset({CAP_READ, CAP_WRITE, CAP_COMMAND})

# read < write < command: possessing a higher capability implies the lower ones.
_CAPABILITY_IMPLICATIONS: dict[str, set[str]] = {
    CAP_WRITE: {CAP_READ},
    CAP_COMMAND: {CAP_READ, CAP_WRITE},
}


def expand_capabilities(caps: Iterable[str]) -> frozenset[str]:
    """Close a capability set under the read<write<command implication chain."""

    expanded = set(caps)
    for cap in list(expanded):
        expanded |= _CAPABILITY_IMPLICATIONS.get(cap, set())
    return frozenset(expanded)


# --- the four facts ----------------------------------------------------------


class VerificationState(Enum):
    """Whether we have *observed* the capability work in this configuration.

    Never populated from a model's self-report. A worker saying it ran the
    tests is not evidence that it can; only an observed side effect is.
    """

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"


class CapabilityState(Enum):
    """The single state a user-facing surface should render.

    Derived, never stored: a stored state is a fifth source of truth, free to
    drift from the four facts it claims to summarise.
    """

    AVAILABLE = "available"
    RESTRICTED = "restricted"
    UNVERIFIED = "unverified"
    UNSUPPORTED = "unsupported"
    NEEDS_REPAIR = "needs_repair"


@dataclass(frozen=True)
class RestrictionReason:
    """Why ModelQuad denies a capability it agrees the runtime supports.

    ``code`` is machine-stable (history, tests, outcome classification);
    ``detail`` is what a person reads. Both are required: a denial with no
    stated reason is indistinguishable from an incapability, which is the whole
    defect.
    """

    code: str
    detail: str

    def __post_init__(self) -> None:
        if not self.code or not self.detail:
            raise ValueError("a restriction reason needs both a code and a detail")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class VerificationEvidence:
    """What was observed, when, and how -- or nothing at all.

    ``observed_at`` being None is the honest answer for a fact nobody has
    checked. Absent evidence reads UNKNOWN (safety invariant 4), never a
    confident zero, so this type never invents a timestamp.
    """

    method: str | None = None
    detail: str | None = None
    observed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "detail": self.detail,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


@dataclass(frozen=True)
class CapabilityFact:
    """One capability answer for one runtime, with the four facts kept apart."""

    capability: str
    #: None means UNKNOWN -- nobody has established whether the runtime can do
    #: this. Invariant 4: unknown is never rendered or routed as False.
    supported: bool | None = None
    allowed: bool = False
    restriction_reason: RestrictionReason | None = None
    verification: VerificationState = VerificationState.UNVERIFIED
    evidence: VerificationEvidence = field(default_factory=VerificationEvidence)
    #: Why we believe ``supported`` -- the provenance of a declaration that is
    #: otherwise indistinguishable from a guess.
    support_basis: str | None = None

    def __post_init__(self) -> None:
        if self.capability not in ALL_CAPABILITIES:
            raise ValueError(
                f"unknown capability {self.capability!r}; expected one of "
                f"{sorted(ALL_CAPABILITIES)}"
            )
        # The point of the whole model: a denial ModelQuad issues must name
        # itself. Without this, a policy restriction is once again
        # indistinguishable from an incapability -- the defect, reintroduced.
        if not self.allowed and self.supported is not False and self.restriction_reason is None:
            raise ValueError(
                f"capability {self.capability!r} is denied while supported is "
                f"{self.supported!r}; a policy denial must carry a restriction_reason "
                "so it can never be reported as an incapability"
            )
        if self.verification is VerificationState.VERIFIED and self.supported is not True:
            raise ValueError(
                f"capability {self.capability!r} cannot be VERIFIED while supported is "
                f"{self.supported!r}: observing it work IS evidence of support"
            )

    @property
    def operational(self) -> bool:
        """Usable right now: not known-unsupported, permitted, and not broken.

        Two deliberate non-blockers, both following from invariant 4:

        * ``supported is None`` does not block. Unknown is not False, so a
          provider nobody has characterised stays routable rather than being
          silently declared incapable of everything -- which is also what keeps
          a newly added provider working before anyone writes its declaration.
        * ``UNVERIFIED`` does not block. Absent evidence is not evidence of
          absence; requiring proof first would make every capability on a fresh
          install unusable until something had already used it.

        Only an explicit ``supported is False``, an explicit denial, or an
        observed failure make a capability non-operational.
        """

        return (
            self.supported is not False
            and self.allowed
            and self.verification is not VerificationState.FAILED
        )

    @property
    def state(self) -> CapabilityState:
        if self.supported is False:
            return CapabilityState.UNSUPPORTED
        # Unknown support is UNVERIFIED, never UNSUPPORTED. This one line is the
        # difference between "we have not checked" and "it cannot".
        if self.supported is None:
            return CapabilityState.UNVERIFIED
        if not self.allowed:
            return CapabilityState.RESTRICTED
        if self.verification is VerificationState.FAILED:
            return CapabilityState.NEEDS_REPAIR
        if self.verification is VerificationState.UNVERIFIED:
            return CapabilityState.UNVERIFIED
        return CapabilityState.AVAILABLE

    def with_verification(self, evidence: VerificationEvidence) -> CapabilityFact:
        """Record objective evidence that this capability works here.

        Verification upgrades *knowledge*, never *permission*: an unknown
        support becomes known, but a policy restriction is untouched. Proving
        a runtime can run a command does not grant it permission to run commands.
        """

        return CapabilityFact(
            capability=self.capability,
            supported=True,
            allowed=self.allowed,
            restriction_reason=self.restriction_reason,
            verification=VerificationState.VERIFIED,
            evidence=evidence,
            support_basis=self.support_basis,
        )

    def with_verification_failure(self, evidence: VerificationEvidence) -> CapabilityFact:
        """Record that a capability we believe in did not work here.

        Deliberately does NOT set ``supported=False``. A failed check on this
        machine is a configuration/health signal (NEEDS_REPAIR), not a discovery
        about the runtime -- conflating them is how a broken PATH becomes a
        permanent "this agent cannot".
        """

        return CapabilityFact(
            capability=self.capability,
            supported=self.supported,
            allowed=self.allowed,
            restriction_reason=self.restriction_reason,
            verification=VerificationState.FAILED,
            evidence=evidence,
            support_basis=self.support_basis,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "supported": self.supported,
            "allowed": self.allowed,
            "restriction_reason": (
                self.restriction_reason.to_dict() if self.restriction_reason else None
            ),
            "verification": self.verification.value,
            "evidence": self.evidence.to_dict(),
            "support_basis": self.support_basis,
            "operational": self.operational,
            "state": self.state.value,
        }


# --- why a candidate was refused ---------------------------------------------


@dataclass(frozen=True)
class CapabilityGap:
    """One required capability a runtime is not operational for, and why.

    ``reason_code`` is what callers phrase a refusal from. It exists so no
    caller ever has to fall back on the word "lacks": a RESTRICTED gap and an
    UNSUPPORTED gap produce different sentences because they are different
    facts.
    """

    capability: str
    state: CapabilityState
    restriction_reason: RestrictionReason | None = None

    @property
    def reason_code(self) -> str:
        return f"{self.state.value}_capability:{self.capability}"

    @property
    def is_policy_restriction(self) -> bool:
        """True when ModelQuad, not the runtime, is the reason.

        Outcome classification reads this: a stage refused for a policy reason
        must never be attributed to the provider's performance.
        """

        return self.state is CapabilityState.RESTRICTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "is_policy_restriction": self.is_policy_restriction,
            "restriction_reason": (
                self.restriction_reason.to_dict() if self.restriction_reason else None
            ),
        }


@dataclass(frozen=True)
class CapabilityProfile:
    """Every capability fact known about one runtime.

    A profile with no fact for a capability answers UNKNOWN for it rather than
    inventing one, which is what keeps an unconfigured or newly added provider
    from being silently declared incapable of everything.
    """

    provider: str
    facts: Mapping[str, CapabilityFact] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, fact in self.facts.items():
            if name != fact.capability:
                raise ValueError(
                    f"capability profile for {self.provider!r} maps {name!r} to a fact "
                    f"about {fact.capability!r}"
                )

    def fact(self, capability: str) -> CapabilityFact:
        """The fact for a capability, or an honest UNKNOWN placeholder."""

        known = self.facts.get(capability)
        if known is not None:
            return known
        return CapabilityFact(
            capability=capability,
            supported=None,
            allowed=True,
            support_basis="no capability declaration for this provider",
        )

    @property
    def operational_capabilities(self) -> frozenset[str]:
        """What this runtime can actually be routed for right now."""

        return frozenset(
            name for name, fact in self.facts.items() if fact.operational
        )

    @property
    def supported_capabilities(self) -> frozenset[str]:
        """What the runtime can do in principle -- policy ignored.

        Distinct from ``operational_capabilities`` on purpose: the difference
        between the two sets is exactly ModelQuad's own policy surface, and a
        user is entitled to see it.
        """

        return frozenset(
            name for name, fact in self.facts.items() if fact.supported is True
        )

    def gaps(self, required: Iterable[str]) -> tuple[CapabilityGap, ...]:
        """Which required capabilities this runtime is not operational for.

        An UNVERIFIED capability is NOT a gap -- see ``CapabilityFact.operational``.
        Returned in a stable order so refusal text and tests are deterministic.
        """

        found: list[CapabilityGap] = []
        for capability in sorted(set(required)):
            fact = self.fact(capability)
            if fact.operational:
                continue
            found.append(
                CapabilityGap(
                    capability=capability,
                    state=fact.state,
                    restriction_reason=fact.restriction_reason,
                )
            )
        return tuple(found)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "facts": {name: fact.to_dict() for name, fact in sorted(self.facts.items())},
            "operational": sorted(self.operational_capabilities),
            "supported": sorted(self.supported_capabilities),
        }


def describe_gaps(gaps: Iterable[CapabilityGap]) -> str:
    """One truthful sentence explaining why a runtime was refused.

    The old text was "worker lacks required capabilities (command)" regardless
    of why, which was false whenever the answer was policy. Each state gets its
    own wording, and RESTRICTED names ModelQuad as the actor.
    """

    listed = list(gaps)
    if not listed:
        return ""
    clauses: list[str] = []
    for gap in listed:
        if gap.state is CapabilityState.RESTRICTED:
            # Prefer the restriction's own words: they name ModelQuad as the
            # actor and say why, which a generic clause cannot.
            detail = gap.restriction_reason.detail if gap.restriction_reason else None
            clause = detail or f"ModelQuad restricts {gap.capability} for this worker"
        elif gap.state is CapabilityState.UNSUPPORTED:
            clause = f"this runtime does not support {gap.capability}"
        elif gap.state is CapabilityState.NEEDS_REPAIR:
            clause = f"{gap.capability} is configured but did not work here"
        else:
            clause = f"{gap.capability} has not been verified here"
        clauses.append(clause)
    if len(clauses) == 1:
        return clauses[0]
    return "; ".join(clauses[:-1]) + "; and " + clauses[-1]


# --- the declaration layer ---------------------------------------------------
# What a provider plugin DECLARES, in fine-grained terms a human can audit
# against the runtime's real behaviour, plus the one mapping from those terms to
# the coarse routing vocabulary. There is exactly one such mapping in this
# repository; `devtools/scenario.py` used to keep a second copy of it.

CAPABILITY_PROFILE_KEYS: tuple[str, ...] = (
    "workspace_read",
    "workspace_write",
    "run_tests",
    "run_builds",
    "git_read",
    "external_network",
    "external_write",
    "push",
    "release",
)

#: Which fine-grained declarations contribute to each coarse routing capability.
#: Keys absent here (push, release, external_*) are deliberately NOT routing
#: capabilities: they are out-of-workspace authority, denied to every worker by
#: policy, and promoting them to routing capabilities would invite exactly the
#: "broad capability inside a workspace implies machine-wide permission"
#: confusion the security model forbids.
FINE_TO_COARSE: dict[str, str] = {
    "workspace_read": CAP_READ,
    "workspace_write": CAP_WRITE,
    "run_tests": CAP_COMMAND,
    "run_builds": CAP_COMMAND,
}


@dataclass(frozen=True)
class CapabilityGrant:
    """A provider plugin's declaration about one fine-grained capability."""

    supported: bool | None = None
    allowed: bool = False
    restriction_reason: RestrictionReason | None = None
    support_basis: str | None = None

    def __post_init__(self) -> None:
        if not self.allowed and self.supported is not False and self.restriction_reason is None:
            raise ValueError(
                "a grant that is supported (or of unknown support) but not allowed must "
                "carry a restriction_reason naming ModelQuad's own policy"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "allowed": self.allowed,
            "restriction_reason": (
                self.restriction_reason.to_dict() if self.restriction_reason else None
            ),
            "support_basis": self.support_basis,
        }


def granted(basis: str | None = None) -> CapabilityGrant:
    """The runtime supports it and ModelQuad permits it."""

    return CapabilityGrant(supported=True, allowed=True, support_basis=basis)


def restricted(code: str, detail: str, basis: str | None = None) -> CapabilityGrant:
    """The runtime supports it; ModelQuad withholds it, and says why.

    This is the constructor that did not exist before, and whose absence forced
    every policy decision to be recorded as an incapability.
    """

    return CapabilityGrant(
        supported=True,
        allowed=False,
        restriction_reason=RestrictionReason(code=code, detail=detail),
        support_basis=basis,
    )


def unsupported(basis: str) -> CapabilityGrant:
    """The runtime genuinely cannot do it. Requires stating how we know."""

    return CapabilityGrant(supported=False, allowed=False, support_basis=basis)


def unknown_support(code: str, detail: str) -> CapabilityGrant:
    """Nobody has established whether the runtime can do it, and it is withheld.

    Distinct from ``unsupported``: this one becomes AVAILABLE if verification
    ever observes it work, whereas ``unsupported`` is a claim we are making.
    """

    return CapabilityGrant(
        supported=None,
        allowed=False,
        restriction_reason=RestrictionReason(code=code, detail=detail),
    )


def _validate_declaration(declaration: Mapping[str, CapabilityGrant]) -> None:
    unknown = set(declaration) - set(CAPABILITY_PROFILE_KEYS)
    if unknown:
        raise ValueError(
            f"capability profile contains unknown keys: {', '.join(sorted(unknown))}"
        )


def _combine(grants: list[CapabilityGrant]) -> CapabilityGrant | None:
    """Fold several fine grants into the coarse capability they contribute to.

    A coarse capability is supported if ANY contributing declaration is
    supported (running tests and running builds are both "command"), and allowed
    if ANY is allowed. Unknown is preserved rather than resolved: a mix of
    unknown and False is still unknown, because one negative declaration about
    builds is not a finding about commands in general.
    """

    if not grants:
        return None
    if any(g.supported is True for g in grants):
        supported: bool | None = True
    elif all(g.supported is False for g in grants):
        supported = False
    else:
        supported = None
    allowed = any(g.allowed for g in grants)
    reason = None
    if not allowed:
        # Prefer a stated policy reason over silence, so the coarse fact can
        # still explain itself.
        reason = next((g.restriction_reason for g in grants if g.restriction_reason), None)
    basis = next((g.support_basis for g in grants if g.support_basis), None)
    return CapabilityGrant(
        supported=supported,
        allowed=allowed,
        restriction_reason=reason,
        support_basis=basis,
    )


def profile_from_declaration(
    provider: str,
    declaration: Mapping[str, CapabilityGrant],
) -> CapabilityProfile:
    """Build the canonical coarse profile from a plugin's fine declaration.

    This is the runtime path that did not exist before: the registry's declared
    profile and the router's effective gate were two independent facts, free to
    drift, and they had.
    """

    _validate_declaration(declaration)
    grouped: dict[str, list[CapabilityGrant]] = {}
    for key, grant in declaration.items():
        coarse = FINE_TO_COARSE.get(key)
        if coarse is None:
            continue
        grouped.setdefault(coarse, []).append(grant)

    facts: dict[str, CapabilityFact] = {}
    for coarse, grants in grouped.items():
        combined = _combine(grants)
        if combined is None:
            continue
        facts[coarse] = CapabilityFact(
            capability=coarse,
            supported=combined.supported,
            allowed=combined.allowed,
            restriction_reason=combined.restriction_reason,
            support_basis=combined.support_basis,
        )

    # Implication closure applies to what is OPERATIONAL: a worker that may run
    # commands may necessarily also read and write. It only fills gaps -- it
    # must never manufacture a fact that contradicts an explicit declaration.
    for cap in expand_capabilities(
        name for name, fact in facts.items() if fact.operational
    ):
        if cap not in facts:
            facts[cap] = CapabilityFact(
                capability=cap,
                supported=True,
                allowed=True,
                support_basis="implied by a higher declared capability",
            )
    return CapabilityProfile(provider=provider, facts=facts)


def coarse_capabilities_from_profile(
    profile: Mapping[str, Any],
) -> frozenset[str]:
    """Coarse capabilities a declaration makes OPERATIONAL.

    Accepts the current grant shape and the legacy ``dict[str, bool]`` shape. A
    legacy ``True`` means granted; a legacy ``False`` is read as a POLICY DENIAL
    of unknown support, never as a finding that the runtime is incapable --
    reading it the other way is precisely the migration bug this milestone
    exists to prevent.
    """

    normalised: dict[str, CapabilityGrant] = {}
    for key, value in profile.items():
        if isinstance(value, CapabilityGrant):
            normalised[key] = value
        elif value:
            normalised[key] = granted("legacy boolean capability profile")
        else:
            normalised[key] = unknown_support(
                "legacy_profile_denial",
                "withheld by a legacy boolean capability profile; support was never established",
            )
    return profile_from_declaration("<declaration>", normalised).operational_capabilities
