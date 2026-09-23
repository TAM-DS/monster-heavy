"""Authoritative synchronous execution. Submission and consequence commit separately."""

from decimal import Decimal, InvalidOperation
from uuid import UUID

from psycopg.types.json import Jsonb

from monster_heavy.domain import Actor, BoundaryError, ExecutionEvidence, Terms, text
from monster_heavy.execution import evaluate
from monster_heavy.persistence.boundaries import (
    _insert,
    _locked,
    _now,
    _observation,
    _policy,
    _status,
    decimal_string,
)
from monster_heavy.persistence.database import connect
from monster_heavy.persistence.models import ExecutionAttempt, ExecutionRequest


class ExecutionStore:
    def __init__(self, dsn: str):
        self._dsn = dsn

    def submit(self, key: str, proposal_id: UUID, actor: Actor):
        text(key)
        if not isinstance(proposal_id, UUID):
            raise ValueError("ExpectedUUID")
        if not isinstance(actor, Actor):
            raise ValueError("ExpectedActor")
        with connect(self._dsn) as conn:
            row = conn.execute(
                "INSERT INTO monster_heavy.execution_requests "
                "(idempotency_key,proposal_id,actor_id,actor_role) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
                (key, proposal_id, actor.id, actor.role),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM monster_heavy.execution_requests WHERE idempotency_key=%s",
                    (key,),
                ).fetchone()
                if (row["proposal_id"], row["actor_id"], row["actor_role"]) != (
                    proposal_id,
                    actor.id,
                    actor.role,
                ):
                    raise BoundaryError("IdempotencyConflict")
            return ExecutionRequest(**row)

    def result(self, request_id: UUID):
        with connect(self._dsn) as conn:
            return self._result(conn, request_id)

    @staticmethod
    def _result(conn, request_id):
        row = conn.execute(
            "SELECT * FROM monster_heavy.execution_attempts "
            "WHERE request_id=%s AND status IN ('ACCEPTED','REJECTED')",
            (request_id,),
        ).fetchone()
        return ExecutionAttempt(**row) if row else None

    def execute(self, request_id: UUID, evidence_id: UUID | None = None):
        if not isinstance(request_id, UUID) or (
            evidence_id is not None and not isinstance(evidence_id, UUID)
        ):
            raise ValueError("ExpectedUUID")
        with connect(self._dsn) as conn:
            request = conn.execute(
                "SELECT * FROM monster_heavy.execution_requests WHERE id=%s FOR UPDATE",
                (request_id,),
            ).fetchone()
            if request is None:
                raise BoundaryError("ExecutionRequestNotFound")
            result = self._result(conn, request_id)
            if result is not None:
                return result
            proposal = _locked(conn, request["proposal_id"])
            portfolio = conn.execute(
                "SELECT * FROM monster_heavy.portfolios WHERE id=%s FOR UPDATE",
                (proposal["portfolio_id"],),
            ).fetchone()
            position = conn.execute(
                "SELECT quantity FROM monster_heavy.positions "
                "WHERE portfolio_id=%s AND symbol=%s FOR UPDATE",
                (portfolio["id"], proposal["symbol"]),
            ).fetchone()
            quantity = position["quantity"] if position else Decimal(0)
            # Shared with publication's exclusive lock. Evaluation linearizes here,
            # after all potentially blocking state locks, using fresh database time.
            conn.execute("SELECT pg_advisory_xact_lock_shared(728431002)")
            now = _now(conn)
            policy = conn.execute(
                "SELECT * FROM monster_heavy.policy_versions WHERE effective_at<=%s "
                "ORDER BY effective_at DESC LIMIT 1",
                (now,),
            ).fetchone()
            approval = conn.execute(
                "SELECT * FROM monster_heavy.approvals WHERE proposal_id=%s "
                "AND decision='APPROVED' AND terms_hash=%s",
                (proposal["id"], proposal["terms_hash"]),
            ).fetchone()
            evidence = conn.execute(
                "SELECT * FROM monster_heavy.evidence WHERE id=%s AND kind='EXECUTION'",
                (evidence_id,),
            ).fetchone()
            accepted = conn.execute(
                "SELECT id FROM monster_heavy.execution_attempts "
                "WHERE proposal_id=%s AND status='ACCEPTED'",
                (proposal["id"],),
            ).fetchone()
            reason = None
            change = None
            try:
                if accepted or proposal["status"] == "EXECUTED":
                    raise BoundaryError("AlreadyExecuted")
                if proposal["status"] == "EXPIRED" or proposal["expires_at"] <= now:
                    raise BoundaryError("ProposalExpired")
                if proposal["status"] != "APPROVED" or approval is None:
                    raise BoundaryError("IneligibleProposal")
                if evidence is None:
                    raise BoundaryError("ExecutionEvidenceRequired")
                if policy is None:
                    raise BoundaryError("NoActivePolicy")
                terms = Terms(**{name: proposal[name] for name in Terms.__dataclass_fields__})
                try:
                    observation = _observation(evidence)
                except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
                    raise BoundaryError("InvalidExecutionEvidence") from exc
                try:
                    rules = _policy(policy).rules
                except (ValueError, KeyError, TypeError, InvalidOperation) as exc:
                    raise BoundaryError("InvalidActivePolicy") from exc
                change = evaluate(
                    terms,
                    ExecutionEvidence(id=evidence["id"], observation=observation),
                    rules,
                    proposal["created_at"],
                    now,
                    portfolio["currency"],
                    portfolio["cash"],
                    quantity,
                )
            except BoundaryError as exc:
                reason = str(exc)
            return self._finish(
                conn,
                request,
                proposal,
                portfolio,
                quantity,
                evidence,
                policy,
                approval,
                now,
                change,
                reason,
                evidence_id,
            )

    @staticmethod
    def _finish(
        conn,
        request,
        proposal,
        portfolio,
        quantity,
        evidence,
        policy,
        approval,
        now,
        change,
        reason,
        evidence_id,
    ):
        before = {
            "cash": decimal_string(portfolio["cash"]),
            "quantity": decimal_string(quantity),
            "revision": portfolio["revision"],
        }
        after = dict(before)
        outcome = "REJECTED" if reason else "ACCEPTED"
        if change is not None:
            after = {
                "cash": decimal_string(change.cash),
                "quantity": decimal_string(change.quantity),
                "revision": portfolio["revision"] + 1,
            }
            conn.execute(
                "UPDATE monster_heavy.portfolios SET cash=%s,revision=revision+1,updated_at=%s "
                "WHERE id=%s",
                (change.cash, now, portfolio["id"]),
            )
            conn.execute(
                "INSERT INTO monster_heavy.positions(portfolio_id,symbol,quantity) "
                "VALUES (%s,%s,%s) ON CONFLICT (portfolio_id,symbol) "
                "DO UPDATE SET quantity=EXCLUDED.quantity",
                (portfolio["id"], proposal["symbol"], change.quantity),
            )
            _status(conn, proposal["id"], "EXECUTED")
        consequence = _insert(
            conn,
            "evidence",
            kind="CONSEQUENCE",
            source="deterministic_executor",
            observed_at=now,
            recorded_at=now,
            payload=Jsonb(
                {
                    "schema_version": 1,
                    "supplied_execution_evidence_id": str(evidence_id) if evidence_id else None,
                    "portfolio_id": str(portfolio["id"]),
                    "symbol": proposal["symbol"],
                    "currency": portfolio["currency"],
                    "before": before,
                    "after": after,
                    "outcome": outcome,
                    "notional": decimal_string(change.notional) if change else None,
                }
            ),
        )
        attempt = _insert(
            conn,
            "execution_attempts",
            request_id=request["id"],
            proposal_id=proposal["id"],
            status=outcome,
            approval_id=approval["id"] if approval else None,
            execution_evidence_id=evidence["id"] if evidence else None,
            policy_version=policy["version"] if policy else None,
            policy_digest=policy["digest"] if policy else None,
            consequence_evidence_id=consequence["id"],
            reason_code=reason,
            started_at=now,
            finished_at=now,
        )
        ledger = _insert(
            conn, "decision_ledger", attempt_id=attempt["id"], outcome=outcome, recorded_at=now
        )
        _insert(
            conn,
            "outbox",
            ledger_id=ledger["id"],
            event_type="Execution" + outcome.title(),
            payload=Jsonb({"attempt_id": str(attempt["id"]), "outcome": outcome}),
            created_at=now,
            available_at=now,
        )
        conn.execute(
            "UPDATE monster_heavy.execution_requests SET completed_at=%s WHERE id=%s",
            (now, request["id"]),
        )
        return ExecutionAttempt(**attempt)
