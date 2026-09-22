"""Phase 2 services: inject only the capability named by each service's port."""

from datetime import datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from monster_heavy.domain import (
    Actor,
    ExecutionEvidence,
    GroundingEvidence,
    Observation,
    PolicyRules,
    PublishedPolicy,
    Recommendation,
)

if TYPE_CHECKING:
    from monster_heavy.persistence.models import Approval, Proposal


class ProposalWriter(Protocol):
    def create(
        self, recommendation: Recommendation, supersedes_id: UUID | None = None
    ) -> "Proposal": ...


class ApprovalWriter(Protocol):
    def decide(
        self, proposal_id: UUID, terms_hash: str, actor: Actor, rationale: str, decision: str
    ) -> "Approval": ...


class LifecycleWriter(Protocol):
    def expire(self, proposal_id: UUID) -> "Proposal": ...


class PolicyPublisher(Protocol):
    def publish(
        self, rules: PolicyRules, effective_at: datetime | None, actor: Actor
    ) -> PublishedPolicy: ...


class PolicyReader(Protocol):
    def active(self) -> PublishedPolicy: ...


class GroundingWriter(Protocol):
    def grounding(self, observation: Observation) -> GroundingEvidence: ...


class ExecutionEvidenceWriter(Protocol):
    def execution(self, observation: Observation) -> ExecutionEvidence: ...


class ProposalService:
    def __init__(self, store: ProposalWriter):
        self._store = store

    def create(
        self, recommendation: Recommendation, *, supersedes_id: UUID | None = None
    ) -> "Proposal":
        if not isinstance(recommendation, Recommendation):
            raise ValueError("InvalidStructuredRecommendation")
        return self._store.create(recommendation, supersedes_id)


class ApprovalService:
    def __init__(self, store: ApprovalWriter):
        self._store = store

    def approve(
        self, proposal_id: UUID, terms_hash: str, actor: Actor, rationale: str
    ) -> "Approval":
        actor.require("approver")
        return self._store.decide(proposal_id, terms_hash, actor, rationale, "APPROVED")

    def reject(
        self, proposal_id: UUID, terms_hash: str, actor: Actor, rationale: str
    ) -> "Approval":
        actor.require("approver")
        return self._store.decide(proposal_id, terms_hash, actor, rationale, "REJECTED")


class LifecycleService:
    def __init__(self, store: LifecycleWriter):
        self._store = store

    def expire(self, proposal_id: UUID) -> "Proposal":
        return self._store.expire(proposal_id)


class PolicyService:
    def __init__(self, publisher: PolicyPublisher):
        self._publisher = publisher

    def publish(
        self, rules: PolicyRules, effective_at: datetime | None, actor: Actor
    ) -> PublishedPolicy:
        actor.require("policy_publisher")
        if not isinstance(rules, PolicyRules):
            raise ValueError("InvalidPolicyRules")
        return self._publisher.publish(rules, effective_at, actor)


class GroundingService:
    def __init__(self, store: GroundingWriter):
        self._store = store

    def create(self, observation: Observation) -> GroundingEvidence:
        if not isinstance(observation, Observation):
            raise ValueError("InvalidObservation")
        return self._store.grounding(observation)


class ExecutionEvidenceService:
    def __init__(self, store: ExecutionEvidenceWriter):
        self._store = store

    def create(self, observation: Observation) -> ExecutionEvidence:
        if not isinstance(observation, Observation):
            raise ValueError("InvalidObservation")
        return self._store.execution(observation)


class PolicyResolver:
    def __init__(self, reader: PolicyReader):
        self._reader = reader

    def active(self) -> PublishedPolicy:
        return self._reader.active()
