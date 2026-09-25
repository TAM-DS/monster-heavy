"""Operator-requested deterministic proposals. No approval or execution capability."""

from datetime import timedelta
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from monster_heavy.domain import Actor, BoundaryError, Observation, instant
from monster_heavy.persistence.boundaries import _evidence, _insert, _now
from monster_heavy.persistence.database import connect
from monster_heavy.persistence.models import Proposal


class CompensationStore:
    def __init__(self, dsn: str):
        self._dsn = dsn

    def request(
        self, original_attempt_id: UUID, actor: Actor, observation: Observation, expires_at
    ):
        if not isinstance(actor, Actor):
            raise ValueError("ExpectedActor")
        actor.require("operator")
        if not isinstance(original_attempt_id, UUID):
            raise ValueError("ExpectedUUID")
        if not isinstance(observation, Observation):
            raise ValueError("InvalidObservation")
        expires_at = instant(expires_at)
        try:
            with connect(self._dsn) as conn:
                original = conn.execute(
                    "SELECT p.*, f.currency FROM monster_heavy.execution_attempts a "
                    "JOIN monster_heavy.proposals p ON p.id=a.proposal_id "
                    "JOIN monster_heavy.portfolios f ON f.id=p.portfolio_id "
                    "WHERE a.id=%s AND a.status='ACCEPTED'",
                    (original_attempt_id,),
                ).fetchone()
                if original is None:
                    raise BoundaryError("AcceptedAttemptRequired")
                now = _now(conn)
                if expires_at <= now:
                    raise BoundaryError("ProposalExpired")
                if not timedelta(0) <= now - observation.observed_at <= timedelta(seconds=60):
                    raise BoundaryError("FreshGroundingRequired")
                if observation.symbol != original["symbol"]:
                    raise BoundaryError("GroundingTermsMismatch")
                if observation.currency != original["currency"]:
                    raise BoundaryError("EvidenceCurrencyMismatch")
                grounding = _evidence(conn, "GROUNDING", observation)
                proposal = _insert(
                    conn,
                    "proposals",
                    portfolio_id=original["portfolio_id"],
                    symbol=original["symbol"],
                    side="SELL" if original["side"] == "BUY" else "BUY",
                    quantity=original["quantity"],
                    reference_price=observation.price,
                    grounding_evidence_id=grounding["id"],
                    model_provenance=Jsonb({}),
                    origin="COMPENSATION",
                    created_at=now,
                    expires_at=expires_at,
                )
                _insert(
                    conn,
                    "compensation_requests",
                    proposal_id=proposal["id"],
                    original_attempt_id=original_attempt_id,
                    requester_id=actor.id,
                    requester_role=actor.role,
                    requested_at=now,
                )
                return Proposal(**proposal)
        except UniqueViolation as exc:
            raise BoundaryError("CompensationAlreadyRequested") from exc
