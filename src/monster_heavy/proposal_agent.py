"""The sole AI agent: deterministic binding around a capability-free model adapter."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from monster_heavy.application import ProposalService
from monster_heavy.domain import (
    BoundaryError,
    GroundingEvidence,
    Observation,
    Provenance,
    Recommendation,
    Terms,
    instant,
    positive,
)
from monster_heavy.proposal_model import Candidate


@dataclass(frozen=True, kw_only=True)
class ProposalContext:
    """Supplied by trusted composition, never deserialized from model output."""

    portfolio_id: UUID
    grounding: GroundingEvidence
    expires_at: datetime

    def __post_init__(self):
        if not isinstance(self.portfolio_id, UUID):
            raise ValueError("ExpectedPortfolioUUID")
        if type(self.grounding) is not GroundingEvidence:
            raise ValueError("GroundingEvidenceRequired")
        object.__setattr__(self, "expires_at", instant(self.expires_at))


class ProposalModel(Protocol):
    @property
    def provenance(self) -> Provenance: ...

    def propose(self, observation: Observation) -> Candidate: ...


class ProposalAgent:
    def __init__(self, model: ProposalModel, proposals: ProposalService):
        self._model = model
        self._proposals = proposals
        # Capture trusted configuration before any model call.
        self._provenance = model.provenance
        if type(self._provenance) is not Provenance:
            raise ValueError("InvalidModelProvenance")

    def propose(self, context: ProposalContext):
        if type(context) is not ProposalContext:
            raise ValueError("InvalidProposalContext")
        self._check_time(context)
        candidate = self._model.propose(context.grounding.observation)
        try:
            if type(candidate) is not Candidate:
                raise ValueError("InvalidCandidate")
            candidate = Candidate.model_validate(candidate.model_dump(warnings=False))
            if candidate.symbol != context.grounding.observation.symbol:
                raise ValueError("UngroundedSymbol")
            quantity = Decimal(candidate.quantity)
            positive(quantity)
        except ValueError as exc:
            raise BoundaryError("InvalidModelCandidate") from exc
        self._check_time(context)
        recommendation = Recommendation(
            terms=Terms(
                portfolio_id=context.portfolio_id,
                symbol=candidate.symbol,
                side=candidate.side,
                quantity=quantity,
                reference_price=context.grounding.observation.price,
                expires_at=context.expires_at,
            ),
            grounding_evidence_id=context.grounding.id,
            provenance=self._provenance,
        )
        return self._proposals.create(recommendation)

    @staticmethod
    def _check_time(context: ProposalContext):
        now = datetime.now(UTC)
        if context.expires_at <= now:
            raise BoundaryError("ProposalExpired")
        if context.grounding.observation.observed_at > now:
            raise BoundaryError("FutureEvidence")
