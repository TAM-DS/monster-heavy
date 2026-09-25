from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from test_execution_core import ACTOR, HUMAN, snapshot, submit
from test_execution_core import case as execution_case  # noqa: F401
from test_recovery_release import compensation

from monster_heavy.domain import Actor, BoundaryError
from monster_heavy.persistence.audit import AuditStore
from monster_heavy.persistence.boundaries import ApprovalStore, _insert
from monster_heavy.persistence.compensation import CompensationStore
from monster_heavy.persistence.database import connect
from monster_heavy.persistence.execution import ExecutionStore
from monster_heavy.persistence.worker import WorkerStore

pytestmark = pytest.mark.integration


@pytest.fixture
def case(execution_case):  # noqa: F811
    return execution_case


@pytest.mark.parametrize(
    "problem", ["unknown", "rejected", "role", "stale", "future", "symbol", "currency", "expiry"]
)
def test_invalid_compensation_request_is_atomic(dsn, case, problem):
    store = ExecutionStore(dsn)
    original = store.execute(submit(dsn, case).id, None if problem == "rejected" else case[2].id)
    actor = Actor(id="model", role="proposer") if problem == "role" else ACTOR
    observation = replace(case[2].observation, observed_at=datetime.now(UTC))
    modifications = {
        "stale": {"observed_at": datetime.now(UTC) - timedelta(seconds=61)},
        "future": {"observed_at": datetime.now(UTC) + timedelta(hours=1)},
        "symbol": {"symbol": "OTHER"},
        "currency": {"currency": "EUR"},
    }
    observation = replace(observation, **modifications.get(problem, {}))
    before = AuditStore(dsn).metrics()
    state = snapshot(dsn, case)
    with pytest.raises(BoundaryError):
        CompensationStore(dsn).request(
            uuid4() if problem == "unknown" else original.id,
            actor,
            observation,
            datetime.now(UTC) + timedelta(hours=-1 if problem == "expiry" else 1),
        )
    assert AuditStore(dsn).metrics() == before
    assert snapshot(dsn, case) == state


@pytest.mark.concurrency
def test_concurrent_compensation_requests_create_one_pending_proposal(dsn, case):
    original = ExecutionStore(dsn).execute(submit(dsn, case).id, case[2].id)
    barrier = Barrier(2)
    before = AuditStore(dsn).metrics()

    def run(_):
        barrier.wait(timeout=5)
        try:
            return compensation(dsn, case, original)[1]
        except BoundaryError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sum(isinstance(result, str) for result in results) == 1
    assert "CompensationAlreadyRequested" in results
    after = AuditStore(dsn).metrics()
    assert after["proposals"] - before["proposals"] == 1
    assert after["compensations_requested"] - before["compensations_requested"] == 1


@pytest.mark.parametrize(
    "tamper",
    ["rejected_original", "side", "quantity", "portfolio", "model", "unlinked", "currency"],
)
def test_database_enforces_compensation_link_and_terms(dsn, case, tamper):
    original = ExecutionStore(dsn).execute(
        submit(dsn, case).id, None if tamper == "rejected_original" else case[2].id
    )
    from monster_heavy.persistence.boundaries import _evidence

    with (
        pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation)),
        connect(dsn) as conn,
    ):
        now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        observation = replace(case[2].observation, observed_at=now)
        if tamper == "currency":
            observation = replace(observation, currency="EUR")
        grounding = _evidence(conn, "GROUNDING", observation)
        portfolio_id = case[0]["id"]
        if tamper == "portfolio":
            portfolio_id = _insert(conn, "portfolios", cash=1000, currency="USD")["id"]
        proposal = _insert(
            conn,
            "proposals",
            portfolio_id=portfolio_id,
            symbol="TEST",
            side="BUY" if tamper == "side" else "SELL",
            quantity=Decimal(3 if tamper == "quantity" else 2),
            reference_price=Decimal(10),
            grounding_evidence_id=grounding["id"],
            origin="COMPENSATION",
            model_provenance=Jsonb({"model": "pretend"} if tamper == "model" else {}),
            created_at=now,
            expires_at=now + timedelta(hours=1),
        )
        if tamper != "unlinked":
            _insert(
                conn,
                "compensation_requests",
                proposal_id=proposal["id"],
                original_attempt_id=original.id,
                requester_id=ACTOR.id,
                requester_role=ACTOR.role,
            )


@pytest.mark.parametrize("table", ["control_events", "compensation_requests"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
@pytest.mark.parametrize("runtime", [True, False])
def test_new_durable_history_cannot_be_mutated(dsn, case, table, operation, runtime):
    request = submit(dsn, case)
    WorkerStore(dsn).claim("fixture", request_id=request.id)
    original = ExecutionStore(dsn).execute(request.id, case[2].id)
    compensation(dsn, case, original)
    with (
        pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.InsufficientPrivilege)),
        connect(dsn) as conn,
    ):
        if runtime:
            conn.execute("SET LOCAL ROLE monster_heavy_app")
        identifier = sql.Identifier("monster_heavy", table)
        statement = {
            "UPDATE": sql.SQL("UPDATE {} SET request_id=request_id")
            if table == "control_events"
            else sql.SQL("UPDATE {} SET requester_id=requester_id"),
            "DELETE": sql.SQL("DELETE FROM {}"),
            "TRUNCATE": sql.SQL("TRUNCATE {}"),
        }[operation]
        conn.execute(statement.format(identifier))


def test_runtime_cannot_fabricate_claim_events(dsn, case):
    request = submit(dsn, case)
    with pytest.raises(psycopg.errors.InsufficientPrivilege), connect(dsn) as conn:
        conn.execute("SET LOCAL ROLE monster_heavy_app")
        conn.execute(
            "INSERT INTO monster_heavy.control_events(request_id,kind,worker_owner) "
            "VALUES (%s,'CLAIM','fake')",
            (request.id,),
        )
    with pytest.raises(psycopg.errors.CheckViolation), connect(dsn) as conn:
        conn.execute("SET LOCAL ROLE monster_heavy_app")
        conn.execute("SELECT monster_heavy.record_replay(%s,'CLAIM')", (request.id,))
    with pytest.raises(psycopg.errors.CheckViolation), connect(dsn) as conn:
        conn.execute("SET LOCAL ROLE monster_heavy_app")
        conn.execute("SELECT monster_heavy.record_replay(%s,'EXECUTION_REPLAY')", (request.id,))


@pytest.mark.concurrency
def test_independent_claimers_get_one_lease(dsn, case):
    request = submit(dsn, case)
    barrier = Barrier(2)

    def run(owner):
        barrier.wait(timeout=5)
        return WorkerStore(dsn).claim(owner, request_id=request.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(run, ("one", "two")))
    assert sum(claim is not None for claim in claims) == 1


def test_claim_and_compensation_work_as_runtime_role(dsn, case, monkeypatch):
    import monster_heavy.persistence.compensation as compensation_module
    import monster_heavy.persistence.worker as worker_module

    def runtime(dsn):
        conn = connect(dsn)
        conn.execute("SET ROLE monster_heavy_app")
        return conn

    monkeypatch.setattr(worker_module, "connect", runtime)
    monkeypatch.setattr(compensation_module, "connect", runtime)
    request = submit(dsn, case)
    assert WorkerStore(dsn).claim("runtime", request_id=request.id)
    original = ExecutionStore(dsn).execute(request.id, case[2].id)
    _, proposal, _ = compensation(dsn, case, original)
    ApprovalStore(dsn).decide(proposal.id, proposal.terms_hash, HUMAN, "review", "APPROVED")
    assert proposal.origin == "COMPENSATION"


def test_compensation_of_sell_deterministically_buys(dsn, case):
    _, first, _ = compensation(dsn, case)
    ApprovalStore(dsn).decide(first.id, first.terms_hash, HUMAN, "review", "APPROVED")
    store = ExecutionStore(dsn)
    sold = store.execute(store.submit(str(uuid4()), first.id, ACTOR).id, case[2].id)
    assert sold.status == "ACCEPTED"
    _, second, _ = compensation(dsn, case, sold)
    assert second.side == "BUY" and second.quantity == first.quantity
