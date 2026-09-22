import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from monster_heavy.persistence.database import connect
from monster_heavy.persistence.migrate import migrate


@pytest.fixture(scope="session")
def dsn():
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.fail(
            "TEST_DATABASE_URL must point to a disposable PostgreSQL database; no mock fallback"
        )
    migrate(value)
    return value


def open_db(dsn, *, app=False):
    conn = connect(dsn)
    conn.execute("SET search_path = monster_heavy, pg_catalog")
    conn.execute("SET statement_timeout = '10s'")
    if app:
        conn.execute("SET ROLE monster_heavy_app")
    return conn


@pytest.fixture
def db(dsn):
    with open_db(dsn) as conn:
        yield conn
        conn.rollback()


def insert(conn, table, **values):
    from psycopg import sql

    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, values)),
        sql.SQL(", ").join(sql.Placeholder() for _ in values),
    )
    return conn.execute(statement, list(values.values())).fetchone()


def seed(conn):
    now = datetime.now(UTC)
    evidence = {
        kind: insert(conn, "evidence", kind=kind, source="test", payload=Jsonb({}), observed_at=now)
        for kind in ("GROUNDING", "APPROVAL", "EXECUTION", "CONSEQUENCE")
    }
    policy = insert(
        conn,
        "policy_versions",
        version=uuid4().int % (2**63 - 1) + 1,
        rules=Jsonb({"maximum_order_notional": "1000.00"}),
        effective_at=now,
        publisher_id="operator",
        publisher_role="policy_publisher",
    )
    portfolio = insert(conn, "portfolios", cash="1000.00", currency="USD")
    insert(conn, "positions", portfolio_id=portfolio["id"], symbol="TEST", quantity="0")
    proposal = insert(
        conn,
        "proposals",
        portfolio_id=portfolio["id"],
        symbol="TEST",
        side="BUY",
        quantity="2",
        reference_price="10.25",
        grounding_evidence_id=evidence["GROUNDING"]["id"],
        model_provenance=Jsonb({"model": "fixture-only"}),
        expires_at=now + timedelta(days=1),
    )
    approval = insert(
        conn,
        "approvals",
        proposal_id=proposal["id"],
        terms_hash=proposal["terms_hash"],
        decision="APPROVED",
        actor_id="human",
        actor_role="approver",
        rationale="test",
        evidence_id=evidence["APPROVAL"]["id"],
    )
    conn.execute("UPDATE proposals SET status='APPROVED' WHERE id=%s", (proposal["id"],))
    request = new_request(conn, proposal["id"])
    return dict(
        evidence=evidence,
        policy=policy,
        portfolio=portfolio,
        proposal=proposal,
        approval=approval,
        request=request,
    )


def new_request(conn, proposal_id, key=None):
    return insert(
        conn,
        "execution_requests",
        proposal_id=proposal_id,
        idempotency_key=key or str(uuid4()),
        actor_id="human",
        actor_role="operator",
    )


def accepted(conn, data, request=None):
    return insert(
        conn,
        "execution_attempts",
        request_id=(request or data["request"])["id"],
        proposal_id=data["proposal"]["id"],
        status="ACCEPTED",
        approval_id=data["approval"]["id"],
        execution_evidence_id=data["evidence"]["EXECUTION"]["id"],
        policy_version=data["policy"]["version"],
        policy_digest=data["policy"]["digest"],
        consequence_evidence_id=data["evidence"]["CONSEQUENCE"]["id"],
        finished_at=datetime.now(UTC),
    )


@pytest.fixture
def data(db):
    return seed(db)
