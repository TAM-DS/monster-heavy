from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from conftest import accepted, insert, new_request, open_db, seed

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]


@pytest.mark.parametrize("race", ["idempotency", "acceptance"])
def test_independent_connections_race(dsn, race):
    with open_db(dsn) as conn:
        data = seed(conn)
        requests = [data["request"], new_request(conn, data["proposal"]["id"])]
    barrier = Barrier(2)
    key = str(uuid4())

    def contender(index):
        try:
            with open_db(dsn, app=True) as conn:
                pid = conn.info.backend_pid
                barrier.wait(timeout=10)
                if race == "idempotency":
                    new_request(conn, data["proposal"]["id"], key)
                else:
                    attempt = accepted(conn, data, requests[index])
                    insert(conn, "decision_ledger", attempt_id=attempt["id"], outcome="ACCEPTED")
            return "committed", pid
        except psycopg.errors.UniqueViolation:
            return "conflict", pid

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(contender, range(2)))
    assert sorted(r[0] for r in results) == ["committed", "conflict"]
    assert len({r[1] for r in results}) == 2
    with open_db(dsn) as conn:
        if race == "idempotency":
            count = conn.execute(
                "SELECT count(*) AS n FROM execution_requests WHERE idempotency_key=%s", (key,)
            ).fetchone()["n"]
        else:
            count = conn.execute(
                """SELECT count(*) AS n FROM execution_attempts a
                JOIN decision_ledger l ON l.attempt_id=a.id
                WHERE a.proposal_id=%s AND a.status='ACCEPTED'""",
                (data["proposal"]["id"],),
            ).fetchone()["n"]
        assert count == 1
