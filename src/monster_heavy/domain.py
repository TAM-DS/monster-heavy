"""Validated immutable values and deterministic Phase 2 rules. No I/O capabilities."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID


class BoundaryError(ValueError):
    """A deterministic boundary refusal; the message is a stable reason code."""


def instant(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ExpectedAwareTimestamp")
    return value.astimezone(UTC)


def text(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ExpectedNonemptyText")


def symbol(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,31}", value):
        raise ValueError("InvalidSymbol")


def positive(value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("ExpectedPositiveDecimal")


@dataclass(frozen=True, kw_only=True)
class Terms:
    portfolio_id: UUID
    symbol: str
    side: str
    quantity: Decimal
    reference_price: Decimal
    expires_at: datetime

    def __post_init__(self):
        if not isinstance(self.portfolio_id, UUID):
            raise ValueError("ExpectedPortfolioUUID")
        symbol(self.symbol)
        if self.side not in ("BUY", "SELL"):
            raise ValueError("InvalidSide")
        positive(self.quantity)
        positive(self.reference_price)
        object.__setattr__(self, "expires_at", instant(self.expires_at))


@dataclass(frozen=True, kw_only=True)
class Provenance:
    model: str
    model_version: str
    prompt_version: str

    def __post_init__(self):
        for value in (self.model, self.model_version, self.prompt_version):
            text(value)


@dataclass(frozen=True, kw_only=True)
class Recommendation:
    terms: Terms
    grounding_evidence_id: UUID
    provenance: Provenance

    def __post_init__(self):
        if not isinstance(self.terms, Terms) or not isinstance(self.provenance, Provenance):
            raise ValueError("InvalidStructuredRecommendation")
        if not isinstance(self.grounding_evidence_id, UUID):
            raise ValueError("ExpectedEvidenceUUID")


@dataclass(frozen=True, kw_only=True)
class Actor:
    id: str
    role: str

    def __post_init__(self):
        text(self.id)
        text(self.role)

    def require(self, role: str):
        if self.role != role:
            raise BoundaryError("UnauthorizedRole")


@dataclass(frozen=True, kw_only=True)
class Observation:
    symbol: str
    price: Decimal
    currency: str
    source: str
    observed_at: datetime

    def __post_init__(self):
        symbol(self.symbol)
        positive(self.price)
        text(self.source)
        if not isinstance(self.currency, str) or not re.fullmatch("[A-Z]{3}", self.currency):
            raise ValueError("InvalidCurrency")
        object.__setattr__(self, "observed_at", instant(self.observed_at))


@dataclass(frozen=True, kw_only=True)
class _EvidenceReference:
    id: UUID
    observation: Observation

    def __post_init__(self):
        if not isinstance(self.id, UUID) or not isinstance(self.observation, Observation):
            raise ValueError("InvalidEvidenceReference")


@dataclass(frozen=True, kw_only=True)
class GroundingEvidence(_EvidenceReference):
    pass


@dataclass(frozen=True, kw_only=True)
class ExecutionEvidence(_EvidenceReference):
    pass


@dataclass(frozen=True, kw_only=True)
class PolicyRules:
    allowed_actions: tuple[str, ...]
    allowed_symbols: tuple[str, ...]
    maximum_order_notional: Decimal
    maximum_resulting_position: Decimal  # quantity of shares, not monetary notional
    maximum_execution_evidence_age_seconds: int
    maximum_price_drift: Decimal  # fractional ratio: Decimal('0.05') means 5%
    maximum_proposal_age_seconds: int

    def __post_init__(self):
        for name in ("allowed_actions", "allowed_symbols"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not values or len(set(values)) != len(values):
                raise ValueError("ExpectedUniqueNonemptyTuple")
            object.__setattr__(self, name, tuple(sorted(values)))
        if any(action not in ("BUY", "SELL") for action in self.allowed_actions):
            raise ValueError("InvalidSide")
        for value in self.allowed_symbols:
            symbol(value)
        positive(self.maximum_order_notional)
        positive(self.maximum_resulting_position)
        for value in (
            self.maximum_execution_evidence_age_seconds,
            self.maximum_proposal_age_seconds,
        ):
            if type(value) is not int or not 0 < value <= 2147483647:
                raise ValueError("ExpectedPositiveSeconds")
        drift = self.maximum_price_drift
        if not isinstance(drift, Decimal) or not drift.is_finite() or not 0 <= drift <= 1:
            raise ValueError("InvalidDriftRatio")


@dataclass(frozen=True, kw_only=True)
class PublishedPolicy:
    version: int
    digest: str
    rules: PolicyRules
    effective_at: datetime
    published_at: datetime
    publisher: Actor


def eligible(status: str, expires_at: datetime, now: datetime, *, pending_only=False) -> None:
    if status not in (("PENDING",) if pending_only else ("PENDING", "APPROVED")):
        raise BoundaryError("IneligibleProposal")
    if instant(expires_at) <= instant(now):
        raise BoundaryError("ProposalExpired")


def verify_evidence(
    terms: Terms, evidence: ExecutionEvidence, rules: PolicyRules, now: datetime
) -> None:
    """Pure preflight only. The future executor must recheck and persist its outcome."""
    if type(evidence) is not ExecutionEvidence:
        raise BoundaryError("ExecutionEvidenceRequired")
    observation = evidence.observation
    if observation.symbol != terms.symbol:
        raise BoundaryError("EvidenceSymbolMismatch")
    age = instant(now) - observation.observed_at
    if age < timedelta(0):
        raise BoundaryError("FutureEvidence")
    if age > timedelta(seconds=rules.maximum_execution_evidence_age_seconds):
        raise BoundaryError("StaleEvidence")
    # Integer-ratio arithmetic avoids dependence on the process Decimal context.
    price_n, price_d = observation.price.as_integer_ratio()
    ref_n, ref_d = terms.reference_price.as_integer_ratio()
    drift_n, drift_d = rules.maximum_price_drift.as_integer_ratio()
    if abs(price_n * ref_d - ref_n * price_d) * drift_d > drift_n * ref_n * price_d:
        raise BoundaryError("PriceDriftExceeded")


def verify_policy_terms(
    terms: Terms, rules: PolicyRules, created_at: datetime, now: datetime
) -> None:
    """Proposal-term preflight, not authorization or an execution decision.

    Execution must additionally evaluate observed-price notional, currency,
    resulting positions, cash, and all current eligibility in its transaction.
    """
    eligible("APPROVED", terms.expires_at, now)
    if terms.side not in rules.allowed_actions:
        raise BoundaryError("DisallowedAction")
    if terms.symbol not in rules.allowed_symbols:
        raise BoundaryError("DisallowedSymbol")
    quantity_n, quantity_d = terms.quantity.as_integer_ratio()
    price_n, price_d = terms.reference_price.as_integer_ratio()
    limit_n, limit_d = rules.maximum_order_notional.as_integer_ratio()
    if quantity_n * price_n * limit_d > limit_n * quantity_d * price_d:
        raise BoundaryError("OrderNotionalExceeded")
    age = instant(now) - instant(created_at)
    if age < timedelta(0):
        raise BoundaryError("FutureProposal")
    if age >= timedelta(seconds=rules.maximum_proposal_age_seconds):
        raise BoundaryError("ProposalPolicyAgeExceeded")
