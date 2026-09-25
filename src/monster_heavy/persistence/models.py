"""Immutable row models, usable with psycopg.rows.class_row.

NUMERIC columns map to Decimal. JSON evidence is opaque; monetary snapshot
values must be decimal strings, never JSON floats. These are records, not services.
"""

from dataclasses import dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


def _validate(value: Any) -> None:
    if isinstance(value, float):
        raise TypeError("Binary floating point is not permitted in persistence records")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Persistence timestamps must be timezone-aware")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("Persistence decimals must be finite")
    if isinstance(value, dict):
        for item in value.values():
            _validate(item)
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate(item)


@dataclass(frozen=True, kw_only=True)
class Record:
    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            _validate(value)
            if field.type is Decimal and not isinstance(value, Decimal):
                raise TypeError(f"{field.name} must be Decimal")
            if isinstance(value, datetime):
                object.__setattr__(self, field.name, value.astimezone(UTC))


@dataclass(frozen=True, kw_only=True)
class Evidence(Record):
    id: UUID
    kind: str
    source: str
    payload: dict[str, Any]
    observed_at: datetime
    recorded_at: datetime


@dataclass(frozen=True, kw_only=True)
class PolicyVersion(Record):
    version: int
    rules: dict[str, Any]
    digest: str
    effective_at: datetime
    published_at: datetime
    publisher_id: str
    publisher_role: str


@dataclass(frozen=True, kw_only=True)
class Portfolio(Record):
    id: UUID
    cash: Decimal
    currency: str
    revision: int
    updated_at: datetime


@dataclass(frozen=True, kw_only=True)
class Position(Record):
    portfolio_id: UUID
    symbol: str
    quantity: Decimal


@dataclass(frozen=True, kw_only=True)
class Proposal(Record):
    id: UUID
    supersedes_id: UUID | None
    portfolio_id: UUID
    symbol: str
    side: str
    quantity: Decimal
    reference_price: Decimal
    terms_hash: str
    status: str
    grounding_evidence_id: UUID
    grounding_kind: str
    model_provenance: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    origin: str = "MODEL"


@dataclass(frozen=True, kw_only=True)
class Approval(Record):
    id: UUID
    proposal_id: UUID
    terms_hash: str
    decision: str
    actor_id: str
    actor_role: str
    rationale: str
    evidence_id: UUID
    evidence_kind: str
    created_at: datetime


@dataclass(frozen=True, kw_only=True)
class ExecutionRequest(Record):
    id: UUID
    idempotency_key: str
    proposal_id: UUID
    actor_id: str
    actor_role: str
    created_at: datetime
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True, kw_only=True)
class ExecutionAttempt(Record):
    id: UUID
    request_id: UUID
    proposal_id: UUID
    status: str
    approval_id: UUID | None
    approval_decision: str
    execution_evidence_id: UUID | None
    execution_kind: str
    policy_version: int | None
    policy_digest: str | None
    consequence_evidence_id: UUID | None
    consequence_kind: str
    reason_code: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, kw_only=True)
class DecisionLedger(Record):
    id: UUID
    attempt_id: UUID
    outcome: str
    recorded_at: datetime


@dataclass(frozen=True, kw_only=True)
class OutboxRecord(Record):
    id: UUID
    ledger_id: UUID
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    delivered_at: datetime | None
    delivery_attempts: int
