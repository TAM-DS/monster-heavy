import socket
import subprocess
import sys
import time
from uuid import uuid4

import httpx2 as httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from test_execution_core import case as execution_case  # noqa: F401
from test_execution_core import submit

from monster_heavy.api import create_app
from monster_heavy.persistence.audit import AuditStore
from monster_heavy.persistence.database import connect
from monster_heavy.persistence.execution import ExecutionStore

pytestmark = pytest.mark.integration


@pytest.fixture
def case(execution_case):  # noqa: F811
    return execution_case


def test_api_readiness_metrics_exact_money_and_reconstruction(dsn, case):
    store = ExecutionStore(dsn)
    accepted = store.execute(submit(dsn, case).id, case[2].id)
    rejected = store.execute(submit(dsn, case).id)
    with TestClient(create_app(dsn)) as client:
        assert client.get("/health").json() == {"status": "alive"}
        assert client.get("/ready").json() == {"status": "ready"}
        assert client.get("/metrics").json()["accepted_executions"] >= 1
        for attempt in (accepted, rejected):
            response = client.get(f"/attempts/{attempt.id}")
            assert response.status_code == 200
            result = response.json()
            assert result["attempt"]["status"] == attempt.status
            assert result["proposal"]["quantity"] == "2"
            assert result["consequence_evidence"]["payload"]["before"]["cash"] in ("1000", "980")
        assert client.get(f"/attempts/{uuid4()}").status_code == 404
        assert client.get("/attempts/not-a-uuid").status_code == 422


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_api_has_no_mutation_routes(dsn, method):
    app = create_app(dsn)
    assert {route.path for route in app.routes} == {
        "/health",
        "/ready",
        "/metrics",
        "/attempts/{attempt_id}",
    }
    assert all(route.methods == {"GET"} for route in app.routes)
    with TestClient(app) as client:
        for path in ("/health", "/ready", "/metrics", f"/attempts/{uuid4()}"):
            assert client.request(method, path).status_code == 405
        for path in ("/approvals", "/proposals", "/executions", "/compensations"):
            assert client.request(method, path).status_code == 404


def test_readiness_fails_closed_without_database():
    with TestClient(
        create_app("host=127.0.0.1 port=1 dbname=unavailable connect_timeout=1")
    ) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json() == {"detail": "Database not ready"}


def test_readiness_verifies_migration_checksums(dsn):
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT name,checksum FROM public.schema_migrations ORDER BY name DESC LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE public.schema_migrations SET checksum='invalid' WHERE name=%s", (row["name"],)
        )
    try:
        with TestClient(create_app(dsn)) as client:
            assert client.get("/ready").status_code == 503
    finally:
        with connect(dsn) as conn:
            conn.execute(
                "UPDATE public.schema_migrations SET checksum=%s WHERE name=%s",
                (row["checksum"], row["name"]),
            )
    assert AuditStore(dsn).ready() == {"status": "ready"}


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO monster_heavy.portfolios(cash,currency) VALUES (1,'USD')",
        "UPDATE monster_heavy.execution_requests SET lease_owner=NULL,lease_expires_at=NULL",
        "SELECT monster_heavy.record_replay(gen_random_uuid(),'SUBMISSION_REPLAY')",
        "DELETE FROM monster_heavy.decision_ledger",
    ],
)
def test_audit_role_has_no_write_capability(dsn, statement):
    # Even without the additional READ ONLY transaction, the API's role cannot mutate.
    with pytest.raises(psycopg.errors.InsufficientPrivilege), connect(dsn) as conn:
        conn.execute("SET LOCAL ROLE monster_heavy_audit")
        conn.execute(statement)


def test_audit_transactions_are_read_only(dsn):
    with AuditStore(dsn)._snapshot() as conn:
        assert (
            conn.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"] == "on"
        )
        assert (
            conn.execute("SELECT current_user AS role").fetchone()["role"] == "monster_heavy_audit"
        )


def test_production_asgi_runtime_health(dsn):
    import os

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "monster_heavy.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=os.environ | {"DATABASE_URL": dsn},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 10
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            while True:
                assert process.poll() is None, process.stdout.read().decode()
                try:
                    response = client.get("/ready")
                    break
                except httpx.ConnectError:
                    if time.monotonic() >= deadline:
                        pytest.fail("ASGI server did not become ready")
                    time.sleep(0.05)
            assert response.status_code == 200
            assert client.get("/health").status_code == 200
            assert client.get("/metrics").status_code == 200
            assert client.post("/executions").status_code == 404
    finally:
        process.terminate()
        process.communicate(timeout=10)
