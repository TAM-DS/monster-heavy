"""Short durable claims. Leases coordinate scheduling; execute() alone authorizes consequences."""

from uuid import UUID

from monster_heavy.domain import text
from monster_heavy.persistence.database import connect
from monster_heavy.persistence.models import ExecutionRequest


class WorkerStore:
    def __init__(self, dsn: str):
        self._dsn = dsn

    def claim(self, owner: str, lease_seconds: int = 30, *, request_id: UUID | None = None):
        text(owner)
        if type(lease_seconds) is not int or not 0 < lease_seconds <= 3600:
            raise ValueError("InvalidLeaseSeconds")
        if request_id is not None and not isinstance(request_id, UUID):
            raise ValueError("ExpectedUUID")
        with connect(self._dsn) as conn:
            row = conn.execute(
                """
                WITH candidate AS (
                    SELECT r.id FROM monster_heavy.execution_requests r
                    WHERE r.completed_at IS NULL AND r.available_at <= clock_timestamp()
                      AND (r.lease_expires_at IS NULL OR r.lease_expires_at <= clock_timestamp())
                      AND (%s::uuid IS NULL OR r.id = %s)
                      AND NOT EXISTS (
                          SELECT FROM monster_heavy.execution_attempts a
                          WHERE a.request_id=r.id AND a.status IN ('ACCEPTED','REJECTED','FAILED'))
                    ORDER BY r.available_at, r.id
                    FOR UPDATE OF r SKIP LOCKED LIMIT 1
                )
                UPDATE monster_heavy.execution_requests r
                SET lease_owner=%s, lease_expires_at=clock_timestamp() + %s * interval '1 second'
                FROM candidate c WHERE r.id=c.id RETURNING r.*
                """,
                (request_id, request_id, owner, lease_seconds),
            ).fetchone()
            return ExecutionRequest(**row) if row else None
