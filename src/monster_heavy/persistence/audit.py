"""Read-only, snapshot-consistent durable metrics and decision reconstruction."""

from contextlib import contextmanager
from hashlib import sha256
from importlib.resources import files
from uuid import UUID

from monster_heavy.persistence.database import connect

POLICY_REASONS = (
    "NoActivePolicy",
    "InvalidActivePolicy",
    "DisallowedAction",
    "DisallowedSymbol",
    "OrderNotionalExceeded",
    "ResultingPositionExceeded",
    "ProposalPolicyAgeExceeded",
    "PriceDriftExceeded",
)


class AuditStore:
    def __init__(self, dsn: str):
        self._dsn = dsn

    @contextmanager
    def _snapshot(self):
        with connect(self._dsn) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL ROLE monster_heavy_audit")
            conn.execute("SET LOCAL statement_timeout = '5s'")
            yield conn

    def ready(self):
        expected = {
            path.name: sha256(path.read_bytes()).hexdigest()
            for path in files("monster_heavy.persistence").joinpath("migrations").iterdir()
            if path.name.endswith(".sql")
        }
        with self._snapshot() as conn:
            actual = {
                row["name"]: row["checksum"]
                for row in conn.execute("SELECT name,checksum FROM public.schema_migrations")
            }
            if expected != actual:
                raise RuntimeError("MigrationHistoryMismatch")
            conn.execute("SELECT id FROM monster_heavy.control_events LIMIT 1")
        return {"status": "ready"}

    def metrics(self):
        with self._snapshot() as conn:

            def count(table, condition="true"):
                return conn.execute(
                    f"SELECT count(*) AS n FROM monster_heavy.{table} WHERE {condition}"
                ).fetchone()["n"]

            reasons = {
                row["reason_code"]: row["n"]
                for row in conn.execute(
                    "SELECT reason_code,count(*) AS n FROM monster_heavy.execution_attempts "
                    "WHERE status='REJECTED' GROUP BY reason_code ORDER BY reason_code"
                )
            }
            events = {
                row["kind"]: row["n"]
                for row in conn.execute(
                    "SELECT kind,count(*) AS n FROM monster_heavy.control_events GROUP BY kind"
                )
            }
            latency = conn.execute(
                "SELECT count(*) AS samples, min(seconds) AS minimum_seconds, "
                "max(seconds) AS maximum_seconds, avg(seconds) AS mean_seconds FROM ("
                "SELECT extract(epoch FROM (a.finished_at-h.created_at)) AS seconds "
                "FROM monster_heavy.execution_attempts a "
                "JOIN monster_heavy.approvals h ON h.id=a.approval_id "
                "WHERE a.status='ACCEPTED') measurement"
            ).fetchone()
            rejected = sum(reasons.values())
            stale = reasons.get("StaleEvidence", 0)
            policy = sum(reasons.get(reason, 0) for reason in POLICY_REASONS)
            return {
                "proposals": count("proposals"),
                "model_proposals": count("proposals", "origin='MODEL'"),
                "approvals": count("approvals", "decision='APPROVED'"),
                "human_rejections": count("approvals", "decision='REJECTED'"),
                "accepted_executions": count("execution_attempts", "status='ACCEPTED'"),
                "rejected_executions": rejected,
                "rejected_by_reason": reasons,
                "stale_evidence_rejections": stale,
                "policy_related_rejections": policy,
                "rejection_rate_denominator": rejected,
                "submission_replays": events.get("SUBMISSION_REPLAY", 0),
                "execution_replays": events.get("EXECUTION_REPLAY", 0),
                "duplicate_proposal_suppressions": reasons.get("AlreadyExecuted", 0),
                "worker_claims": events.get("CLAIM", 0) + events.get("RECLAIM", 0),
                "worker_retries_reclaims": events.get("RECLAIM", 0),
                "compensations_requested": count("compensation_requests"),
                "compensations_accepted": count(
                    "execution_attempts a",
                    "a.status='ACCEPTED' AND EXISTS (SELECT FROM "
                    "monster_heavy.compensation_requests c WHERE c.proposal_id=a.proposal_id)",
                ),
                "approval_to_execution_latency": latency,
            }

    @staticmethod
    def _decision(conn, attempt_id):
        attempt = conn.execute(
            "SELECT * FROM monster_heavy.execution_attempts WHERE id=%s", (attempt_id,)
        ).fetchone()
        if attempt is None:
            return None

        def row(table, key):
            return conn.execute(
                f"SELECT * FROM monster_heavy.{table} WHERE id=%s", (key,)
            ).fetchone()

        proposal = row("proposals", attempt["proposal_id"])
        policy = conn.execute(
            "SELECT * FROM monster_heavy.policy_versions WHERE version=%s",
            (attempt["policy_version"],),
        ).fetchone()
        approval = row("approvals", attempt["approval_id"])
        return {
            "attempt": attempt,
            "request": row("execution_requests", attempt["request_id"]),
            "proposal": proposal,
            "approval": approval,
            "approval_evidence": row("evidence", approval["evidence_id"]) if approval else None,
            "grounding_evidence": row("evidence", proposal["grounding_evidence_id"]),
            "execution_evidence": row("evidence", attempt["execution_evidence_id"]),
            "consequence_evidence": row("evidence", attempt["consequence_evidence_id"]),
            "policy": policy,
            "ledger": conn.execute(
                "SELECT * FROM monster_heavy.decision_ledger WHERE attempt_id=%s", (attempt_id,)
            ).fetchone(),
        }

    def reconstruct(self, attempt_id: UUID):
        with self._snapshot() as conn:
            result = self._decision(conn, attempt_id)
            if result is None:
                return None
            links = conn.execute(
                "SELECT * FROM monster_heavy.compensation_requests "
                "WHERE original_attempt_id=%s OR proposal_id=%s ORDER BY requested_at,proposal_id",
                (attempt_id, result["proposal"]["id"]),
            ).fetchall()
            result["compensations"] = []
            for link in links:
                proposal = conn.execute(
                    "SELECT * FROM monster_heavy.proposals WHERE id=%s", (link["proposal_id"],)
                ).fetchone()
                attempts = conn.execute(
                    "SELECT id FROM monster_heavy.execution_attempts "
                    "WHERE proposal_id=%s ORDER BY started_at,id",
                    (link["proposal_id"],),
                ).fetchall()
                result["compensations"].append(
                    {
                        "request": link,
                        "proposal": proposal,
                        "original": self._decision(conn, link["original_attempt_id"]),
                        "decisions": [self._decision(conn, row["id"]) for row in attempts],
                    }
                )
            return result
