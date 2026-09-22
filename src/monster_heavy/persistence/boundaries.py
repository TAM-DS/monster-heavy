"""Narrow PostgreSQL adapters. Each operation owns one short transaction.

Wire these adapters in trusted composition code; never hand a connection/DSN or
an aggregate repository to model code. Python object privacy is not an IAM sandbox.
"""

from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from monster_heavy.domain import (
    Actor,
    BoundaryError,
    ExecutionEvidence,
    GroundingEvidence,
    Observation,
    PolicyRules,
    PublishedPolicy,
    Recommendation,
    eligible,
    instant,
)
from monster_heavy.persistence.database import connect
from monster_heavy.persistence.models import Approval, Proposal


def decimal_string(value: Decimal) -> str:
    """Canonical decimal text without rounding under the ambient decimal context."""
    if value == 0:
        return "0"
    value = format(value, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _insert(conn, table, **values):
    from psycopg import sql

    return conn.execute(
        sql.SQL("INSERT INTO monster_heavy.{} ({}) VALUES ({}) RETURNING *").format(
            sql.Identifier(table),
            sql.SQL(",").join(map(sql.Identifier, values)),
            sql.SQL(",").join(sql.Placeholder() for _ in values),
        ),
        list(values.values()),
    ).fetchone()


def _now(conn):
    # Sample after acquiring locks; transaction start time can be stale after a wait.
    return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]


def _locked(conn, proposal_id):
    row = conn.execute(
        "SELECT * FROM monster_heavy.proposals WHERE id=%s FOR UPDATE", (proposal_id,)
    ).fetchone()
    if row is None:
        raise BoundaryError("ProposalNotFound")
    return row


def _status(conn, proposal_id, status):
    return Proposal(
        **conn.execute(
            "UPDATE monster_heavy.proposals SET status=%s WHERE id=%s RETURNING *",
            (status, proposal_id),
        ).fetchone()
    )


def _observation(row):
    payload = row["payload"]
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise BoundaryError("UnsupportedEvidenceSchema")
    if set(payload) != {"schema_version", "symbol", "price", "currency"} or not isinstance(
        payload.get("price"), str
    ):
        raise BoundaryError("InvalidEvidencePayload")
    return Observation(
        symbol=payload["symbol"],
        price=Decimal(payload["price"]),
        currency=payload["currency"],
        source=row["source"],
        observed_at=row["observed_at"],
    )


def _evidence(conn, kind, observation):
    if not isinstance(observation, Observation):
        raise ValueError("InvalidObservation")
    now = _now(conn)
    if observation.observed_at > now:
        raise BoundaryError("FutureEvidence")
    return _insert(
        conn,
        "evidence",
        kind=kind,
        source=observation.source,
        payload=Jsonb(
            {
                "schema_version": 1,
                "symbol": observation.symbol,
                "price": decimal_string(observation.price),
                "currency": observation.currency,
            }
        ),
        observed_at=observation.observed_at,
        recorded_at=now,
    )


class _Store:
    def __init__(self, dsn: str):
        self._dsn = dsn


class GroundingStore(_Store):
    def grounding(self, observation: Observation) -> GroundingEvidence:
        with connect(self._dsn) as conn:
            row = _evidence(conn, "GROUNDING", observation)
            return GroundingEvidence(id=row["id"], observation=_observation(row))


class ExecutionEvidenceStore(_Store):
    def execution(self, observation: Observation) -> ExecutionEvidence:
        with connect(self._dsn) as conn:
            row = _evidence(conn, "EXECUTION", observation)
            return ExecutionEvidence(id=row["id"], observation=_observation(row))

    def get(self, evidence_id: UUID) -> ExecutionEvidence:
        with connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT * FROM monster_heavy.evidence WHERE id=%s AND kind='EXECUTION'",
                (evidence_id,),
            ).fetchone()
            if row is None:
                raise BoundaryError("ExecutionEvidenceRequired")
            return ExecutionEvidence(id=row["id"], observation=_observation(row))


class ProposalStore(_Store):
    def create(self, recommendation: Recommendation, supersedes_id: UUID | None = None):
        if not isinstance(recommendation, Recommendation):
            raise ValueError("InvalidStructuredRecommendation")
        terms = recommendation.terms
        with connect(self._dsn) as conn:
            old = _locked(conn, supersedes_id) if supersedes_id is not None else None
            now = _now(conn)
            if old is not None:
                eligible(old["status"], old["expires_at"], now)
                if old["portfolio_id"] != terms.portfolio_id:
                    raise BoundaryError("ReplacementPortfolioMismatch")
            if terms.expires_at <= now:
                raise BoundaryError("ProposalExpired")
            grounding = conn.execute(
                "SELECT * FROM monster_heavy.evidence WHERE id=%s AND kind='GROUNDING'",
                (recommendation.grounding_evidence_id,),
            ).fetchone()
            if grounding is None:
                raise BoundaryError("GroundingEvidenceRequired")
            observation = _observation(grounding)
            if observation.symbol != terms.symbol or observation.price != terms.reference_price:
                raise BoundaryError("GroundingTermsMismatch")
            portfolio = conn.execute(
                "SELECT currency FROM monster_heavy.portfolios WHERE id=%s", (terms.portfolio_id,)
            ).fetchone()
            if portfolio is None:
                raise BoundaryError("PortfolioNotFound")
            if portfolio["currency"] != observation.currency:
                raise BoundaryError("EvidenceCurrencyMismatch")
            values = asdict(terms)
            # Keep the Phase 1 database hash contract; canonicalize equivalent input scales.
            values["quantity"] = Decimal(decimal_string(terms.quantity))
            values["reference_price"] = Decimal(decimal_string(terms.reference_price))
            row = _insert(
                conn,
                "proposals",
                **values,
                supersedes_id=supersedes_id,
                grounding_evidence_id=grounding["id"],
                model_provenance=Jsonb(asdict(recommendation.provenance)),
                created_at=now,
            )
            if old is not None:
                _status(conn, old["id"], "SUPERSEDED")
            return Proposal(**row)


class ApprovalStore(_Store):
    def decide(
        self, proposal_id: UUID, terms_hash: str, actor: Actor, rationale: str, decision: str
    ):
        actor.require("approver")
        if decision not in ("APPROVED", "REJECTED") or not isinstance(rationale, str):
            raise ValueError("InvalidHumanDecision")
        with connect(self._dsn) as conn:
            proposal = _locked(conn, proposal_id)
            now = _now(conn)
            eligible(proposal["status"], proposal["expires_at"], now, pending_only=True)
            if terms_hash != proposal["terms_hash"]:
                raise BoundaryError("TermsHashMismatch")
            evidence = _insert(
                conn,
                "evidence",
                kind="APPROVAL",
                source="human_decision",
                payload=Jsonb(
                    {
                        "schema_version": 1,
                        "proposal_id": str(proposal_id),
                        "terms_hash": terms_hash,
                        "decision": decision,
                        "actor_id": actor.id,
                        "actor_role": actor.role,
                        "rationale": rationale,
                    }
                ),
                observed_at=now,
                recorded_at=now,
            )
            row = _insert(
                conn,
                "approvals",
                proposal_id=proposal_id,
                terms_hash=terms_hash,
                decision=decision,
                actor_id=actor.id,
                actor_role=actor.role,
                rationale=rationale,
                evidence_id=evidence["id"],
                created_at=now,
            )
            _status(conn, proposal_id, decision)
            return Approval(**row)


class LifecycleStore(_Store):
    def expire(self, proposal_id: UUID):
        with connect(self._dsn) as conn:
            proposal = _locked(conn, proposal_id)
            if proposal["status"] == "EXPIRED":
                return Proposal(**proposal)
            if proposal["status"] not in ("PENDING", "APPROVED"):
                raise BoundaryError("IneligibleProposal")
            if proposal["expires_at"] > _now(conn):
                raise BoundaryError("ProposalNotExpired")
            return _status(conn, proposal_id, "EXPIRED")


def _policy(row):
    values = dict(row["rules"])
    schema_version = values.pop("schema_version", None)
    if type(schema_version) is not int or schema_version != 1:
        raise BoundaryError("UnsupportedPolicySchema")
    for name in ("maximum_order_notional", "maximum_resulting_position", "maximum_price_drift"):
        if not isinstance(values.get(name), str):
            raise BoundaryError("InvalidPolicyPayload")
        values[name] = Decimal(values[name])
    for name in ("allowed_actions", "allowed_symbols"):
        values[name] = tuple(values[name])
    return PublishedPolicy(
        version=row["version"],
        digest=row["digest"],
        rules=PolicyRules(**values),
        effective_at=row["effective_at"],
        published_at=row["published_at"],
        publisher=Actor(id=row["publisher_id"], role=row["publisher_role"]),
    )


class PolicyPublicationStore(_Store):
    def publish(self, rules: PolicyRules, effective_at: datetime | None, actor: Actor):
        actor.require("policy_publisher")
        effective_at = instant(effective_at) if effective_at is not None else None
        if not isinstance(rules, PolicyRules):
            raise ValueError("InvalidPolicyRules")
        values = asdict(rules)
        for name, value in values.items():
            if isinstance(value, Decimal):
                values[name] = decimal_string(value)
        values["schema_version"] = 1
        try:
            with connect(self._dsn) as conn:
                # Serialize allocation and publication across independent connections.
                conn.execute("SELECT pg_advisory_xact_lock(728431002)")
                now = _now(conn)
                if effective_at is not None and effective_at < now:
                    raise BoundaryError("RetroactivePolicyPublication")
                version = conn.execute(
                    "SELECT coalesce(max(version),0)+1 AS version "
                    "FROM monster_heavy.policy_versions"
                ).fetchone()["version"]
                return _policy(
                    _insert(
                        conn,
                        "policy_versions",
                        version=version,
                        rules=Jsonb(values),
                        effective_at=effective_at or now,
                        published_at=now,
                        publisher_id=actor.id,
                        publisher_role=actor.role,
                    )
                )
        except UniqueViolation as exc:
            raise BoundaryError("PolicyEffectiveTimeConflict") from exc


class PolicyReadStore(_Store):
    def active(self) -> PublishedPolicy:
        with connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT * FROM monster_heavy.policy_versions "
                "WHERE effective_at <= statement_timestamp() "
                "ORDER BY effective_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise BoundaryError("NoActivePolicy")
            return _policy(row)

    def at(self, evaluated_at: datetime) -> PublishedPolicy:
        """Historical/scheduled resolution for inspection, never an execution authority."""
        with connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT * FROM monster_heavy.policy_versions "
                "WHERE effective_at <= %s ORDER BY effective_at DESC LIMIT 1",
                (instant(evaluated_at),),
            ).fetchone()
            if row is None:
                raise BoundaryError("NoActivePolicy")
            return _policy(row)

    def get(self, version: int) -> PublishedPolicy:
        with connect(self._dsn) as conn:
            row = conn.execute(
                "SELECT * FROM monster_heavy.policy_versions WHERE version=%s", (version,)
            ).fetchone()
            if row is None:
                raise BoundaryError("PolicyNotFound")
            return _policy(row)
