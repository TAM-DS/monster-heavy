"""Phase 5 recovery with independent OS processes and PostgreSQL sessions."""

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from test_boundaries import policy_rules
from test_execution_core import ACTOR, HUMAN, PUBLISHER, snapshot, submit
from test_execution_core import case as execution_case  # noqa: F401

from monster_heavy.persistence.audit import AuditStore
from monster_heavy.persistence.boundaries import (
    ApprovalStore,
    ExecutionEvidenceStore,
    PolicyPublicationStore,
)
from monster_heavy.persistence.compensation import CompensationStore
from monster_heavy.persistence.database import connect
from monster_heavy.persistence.execution import ExecutionStore
from monster_heavy.persistence.worker import WorkerStore

pytestmark = pytest.mark.integration


@pytest.fixture
def case(execution_case):  # noqa: F811
    return execution_case


def _crash_worker(dsn, request_id, evidence_id, phase, pipe):
    """Runs in a spawned interpreter; parent kills it at an actual transaction boundary."""
    import monster_heavy.persistence.execution as module

    claim = WorkerStore(dsn).claim("doomed-process", 1, request_id=request_id)
    assert claim is not None
    original = module.ExecutionStore._finish

    if phase == "before_commit":

        def held(*args):
            result = original(*args)
            pipe.send(("uncommitted", args[0].info.backend_pid, str(result.id)))
            pipe.recv()
            return result

        module.ExecutionStore._finish = staticmethod(held)
    result = ExecutionStore(dsn).execute(request_id, evidence_id)
    pipe.send(("committed", None, str(result.id)))
    pipe.recv()


def expire_lease(dsn, request_id):
    # Let PostgreSQL measure and wait for actual expiry; never rewrite the lease for recovery.
    with connect(dsn) as conn:
        conn.execute(
            "SELECT pg_sleep(greatest(0, extract(epoch FROM "
            "(lease_expires_at-clock_timestamp())))::double precision + 0.02) "
            "FROM monster_heavy.execution_requests WHERE id=%s",
            (request_id,),
        )


def events(dsn, request_id):
    with connect(dsn) as conn:
        return conn.execute(
            "SELECT kind,worker_owner FROM monster_heavy.control_events "
            "WHERE request_id=%s ORDER BY recorded_at,id",
            (request_id,),
        ).fetchall()


@pytest.mark.concurrency
@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
def test_os_worker_death_and_recovery(dsn, case, phase):
    request = submit(dsn, case)
    before = snapshot(dsn, case)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_crash_worker, args=(dsn, request.id, case[2].id, phase, child)
    )
    process.start()
    child.close()
    try:
        assert parent.poll(15), "Child failed to reach transaction boundary"
        stage, backend_pid, attempt_id = parent.recv()
        assert stage == ("uncommitted" if phase == "before_commit" else "committed")
        if phase == "before_commit":
            with connect(dsn) as independent:
                assert independent.info.backend_pid != backend_pid
                assert (
                    independent.execute(
                        "SELECT id FROM monster_heavy.execution_attempts WHERE id=%s", (attempt_id,)
                    ).fetchone()
                    is None
                )
            assert snapshot(dsn, case) == before
        # SIGKILL: no finally block, transaction cleanup, return or application acknowledgement.
        process.kill()
        process.join(10)
        assert process.exitcode is not None and process.exitcode < 0
        expire_lease(dsn, request.id)
        recovered = WorkerStore(dsn).claim("recovery-process", request_id=request.id)
        if phase == "before_commit":
            assert recovered and recovered.lease_owner == "recovery-process"
            assert ExecutionStore(dsn).result(request.id) is None
            assert [e["kind"] for e in events(dsn, request.id)] == ["CLAIM", "RECLAIM"]
            with connect(dsn) as conn:
                assert (
                    conn.execute(
                        "SELECT count(*) AS n FROM monster_heavy.evidence WHERE kind='CONSEQUENCE' "
                        "AND payload->>'portfolio_id'=%s",
                        (str(case[0]["id"]),),
                    ).fetchone()["n"]
                    == 0
                )
        else:
            assert recovered is None
        result = ExecutionStore(dsn).execute(request.id, case[2].id)
        assert result.status == "ACCEPTED"
        if phase == "after_commit":
            assert str(result.id) == attempt_id
            assert events(dsn, request.id)[-1]["kind"] == "EXECUTION_REPLAY"
        assert snapshot(dsn, case)[0]["revision"] == 1
        assert snapshot(dsn, case)[0]["cash"] == 980
        assert ExecutionStore(dsn).execute(request.id) == result
        assert WorkerStore(dsn).claim("later", request_id=request.id) is None
    finally:
        if process.is_alive():
            process.kill()
        process.join(10)
        parent.close()


@pytest.mark.concurrency
def test_expired_stale_worker_cannot_duplicate_consequence(dsn, case):
    request = submit(dsn, case)
    first = WorkerStore(dsn).claim("first", 1, request_id=request.id)
    assert first is not None
    assert WorkerStore(dsn).claim("second", request_id=request.id) is None
    expire_lease(dsn, request.id)
    second = WorkerStore(dsn).claim("second", request_id=request.id)
    assert second and second.lease_expires_at > first.lease_expires_at
    barrier = Barrier(2)

    def execute(_):
        barrier.wait(timeout=10)
        return ExecutionStore(dsn).execute(request.id, case[2].id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    assert results[0] == results[1] and results[0].status == "ACCEPTED"
    assert snapshot(dsn, case)[0]["revision"] == 1
    assert [e["kind"] for e in events(dsn, request.id)] == ["CLAIM", "RECLAIM", "EXECUTION_REPLAY"]


@pytest.mark.concurrency
def test_claim_skip_locked_and_database_time(dsn, case):
    request = submit(dsn, case)
    with connect(dsn) as locker:
        locker.execute(
            "SELECT id FROM monster_heavy.execution_requests WHERE id=%s FOR UPDATE", (request.id,)
        )
        assert WorkerStore(dsn).claim("independent", request_id=request.id) is None
    with connect(dsn) as conn:
        conn.execute(
            "UPDATE monster_heavy.execution_requests SET available_at=clock_timestamp() + "
            "interval '1 hour' WHERE id=%s",
            (request.id,),
        )
    assert WorkerStore(dsn).claim("not-ready", request_id=request.id) is None
    with connect(dsn) as conn:
        conn.execute(
            "UPDATE monster_heavy.execution_requests SET available_at=clock_timestamp() "
            "WHERE id=%s",
            (request.id,),
        )
        start = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
    claimed = WorkerStore(dsn).claim("ready", 30, request_id=request.id)
    with connect(dsn) as conn:
        end = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
    assert start + timedelta(seconds=30) <= claimed.lease_expires_at <= end + timedelta(seconds=30)


@pytest.mark.parametrize("status", ["ACCEPTED", "REJECTED", "FAILED"])
def test_terminal_attempt_without_completion_marker_never_claimed(dsn, case, status):
    request = submit(dsn, case)
    with connect(dsn) as conn:
        if status == "FAILED":
            conn.execute(
                "INSERT INTO monster_heavy.execution_attempts "
                "(request_id,proposal_id,status,reason_code,finished_at) "
                "VALUES (%s,%s,'FAILED','fixture',clock_timestamp())",
                (request.id, case[1].id),
            )
        else:
            ExecutionStore(dsn).execute(request.id, case[2].id if status == "ACCEPTED" else None)
            conn.execute(
                "UPDATE monster_heavy.execution_requests SET completed_at=NULL WHERE id=%s",
                (request.id,),
            )
    assert WorkerStore(dsn).claim("cannot-claim", request_id=request.id) is None


def compensation(dsn, case, original=None):
    if original is None:
        original = ExecutionStore(dsn).execute(submit(dsn, case).id, case[2].id)
    observation = replace(
        case[2].observation, observed_at=datetime.now(UTC), price=Decimal("10.25")
    )
    proposal = CompensationStore(dsn).request(
        original.id, ACTOR, observation, datetime.now(UTC) + timedelta(hours=1)
    )
    return original, proposal, observation


def test_compensation_full_governance_and_reconstruction(dsn, case):
    original, proposal, observation = compensation(dsn, case)
    audit = AuditStore(dsn)
    original_before = audit.reconstruct(original.id)
    state = snapshot(dsn, case)
    assert (proposal.status, proposal.origin, proposal.model_provenance) == (
        "PENDING",
        "COMPENSATION",
        {},
    )
    assert (proposal.portfolio_id, proposal.symbol, proposal.quantity, proposal.side) == (
        case[1].portfolio_id,
        case[1].symbol,
        case[1].quantity,
        "SELL",
    )
    assert proposal.reference_price == Decimal("10.25")
    assert proposal.grounding_evidence_id != case[1].grounding_evidence_id
    store = ExecutionStore(dsn)
    pending = store.submit(str(uuid4()), proposal.id, ACTOR)
    assert store.execute(pending.id, case[2].id).reason_code == "IneligibleProposal"
    assert snapshot(dsn, case) == state
    ApprovalStore(dsn).decide(proposal.id, proposal.terms_hash, HUMAN, "compensate", "APPROVED")
    missing = store.submit(str(uuid4()), proposal.id, ACTOR)
    assert (
        store.execute(missing.id, proposal.grounding_evidence_id).reason_code
        == "ExecutionEvidenceRequired"
    )
    fresh = ExecutionEvidenceStore(dsn).execution(
        replace(observation, observed_at=datetime.now(UTC))
    )
    accepted = store.execute(store.submit(str(uuid4()), proposal.id, ACTOR).id, fresh.id)
    assert accepted.status == "ACCEPTED"
    history = audit.reconstruct(original.id)
    for key in (
        "attempt",
        "proposal",
        "approval",
        "grounding_evidence",
        "execution_evidence",
        "consequence_evidence",
        "ledger",
    ):
        assert history[key] == original_before[key]
    link = history["compensations"][0]
    assert link["request"]["requester_id"] == ACTOR.id
    assert link["original"]["attempt"]["id"] == original.id
    assert [x["attempt"]["status"] for x in link["decisions"]] == [
        "REJECTED",
        "REJECTED",
        "ACCEPTED",
    ]
    reverse = audit.reconstruct(accepted.id)
    assert reverse["compensations"][0]["original"]["attempt"]["id"] == original.id
    assert snapshot(dsn, case)[0]["cash"] == Decimal("1000.50")
    assert snapshot(dsn, case)[0]["revision"] == 2
    assert snapshot(dsn, case)[1][0]["quantity"] == 0


@pytest.mark.parametrize("block", ["policy", "portfolio", "stale"])
def test_compensation_revalidates_current_controls(dsn, case, block):
    _, proposal, observation = compensation(dsn, case)
    ApprovalStore(dsn).decide(proposal.id, proposal.terms_hash, HUMAN, "reviewed", "APPROVED")
    if block == "policy":
        PolicyPublicationStore(dsn).publish(policy_rules(allowed_actions=("BUY",)), None, PUBLISHER)
    elif block == "portfolio":
        with connect(dsn) as conn:
            conn.execute(
                "UPDATE monster_heavy.positions SET quantity=0 WHERE portfolio_id=%s",
                (proposal.portfolio_id,),
            )
    else:
        observation = replace(observation, observed_at=datetime.now(UTC) - timedelta(minutes=5))
    evidence = ExecutionEvidenceStore(dsn).execution(observation)
    before = snapshot(dsn, case)
    request = ExecutionStore(dsn).submit(str(uuid4()), proposal.id, ACTOR)
    result = ExecutionStore(dsn).execute(request.id, evidence.id)
    assert (
        result.reason_code
        == {
            "policy": "DisallowedAction",
            "portfolio": "InsufficientPosition",
            "stale": "StaleEvidence",
        }[block]
    )
    assert snapshot(dsn, case) == before


def test_durable_metrics_and_log_independent_audit(dsn, case, tmp_path):
    audit = AuditStore(dsn)
    before = audit.metrics()
    request = submit(dsn, case)
    WorkerStore(dsn).claim("first", 1, request_id=request.id)
    expire_lease(dsn, request.id)
    WorkerStore(dsn).claim("second", request_id=request.id)
    store = ExecutionStore(dsn)
    original = store.execute(request.id, case[2].id)
    assert store.submit(request.idempotency_key, case[1].id, ACTOR).id == request.id
    assert store.execute(request.id) == original
    duplicate = store.execute(submit(dsn, case).id)
    assert duplicate.reason_code == "AlreadyExecuted"
    _, proposal, observation = compensation(dsn, case, original)
    ApprovalStore(dsn).decide(proposal.id, proposal.terms_hash, HUMAN, "review", "APPROVED")
    stale = ExecutionEvidenceStore(dsn).execution(
        replace(observation, observed_at=datetime.now(UTC) - timedelta(minutes=5))
    )
    assert (
        store.execute(store.submit(str(uuid4()), proposal.id, ACTOR).id, stale.id).reason_code
        == "StaleEvidence"
    )
    PolicyPublicationStore(dsn).publish(policy_rules(allowed_actions=("BUY",)), None, PUBLISHER)
    fresh = ExecutionEvidenceStore(dsn).execution(
        replace(observation, observed_at=datetime.now(UTC))
    )
    assert (
        store.execute(store.submit(str(uuid4()), proposal.id, ACTOR).id, fresh.id).reason_code
        == "DisallowedAction"
    )
    PolicyPublicationStore(dsn).publish(policy_rules(), None, PUBLISHER)
    assert (
        store.execute(store.submit(str(uuid4()), proposal.id, ACTOR).id, fresh.id).status
        == "ACCEPTED"
    )
    after = audit.metrics()
    for key, delta in {
        "proposals": 1,
        "approvals": 1,
        "accepted_executions": 2,
        "rejected_executions": 3,
        "stale_evidence_rejections": 1,
        "policy_related_rejections": 1,
        "submission_replays": 1,
        "execution_replays": 1,
        "duplicate_proposal_suppressions": 1,
        "worker_claims": 2,
        "worker_retries_reclaims": 1,
        "compensations_requested": 1,
        "compensations_accepted": 1,
    }.items():
        assert after[key] - before[key] == delta, key
    assert (
        after["approval_to_execution_latency"]["samples"]
        - before["approval_to_execution_latency"]["samples"]
        == 2
    )
    with connect(dsn) as conn:
        samples = conn.execute(
            "SELECT extract(epoch FROM (a.finished_at-h.created_at)) AS seconds "
            "FROM monster_heavy.execution_attempts a JOIN monster_heavy.approvals h "
            "ON h.id=a.approval_id WHERE a.status='ACCEPTED'"
        ).fetchall()
    values = [row["seconds"] for row in samples]
    assert after["approval_to_execution_latency"]["minimum_seconds"] == min(values)
    assert after["approval_to_execution_latency"]["maximum_seconds"] == max(values)
    history = audit.reconstruct(original.id)
    log = tmp_path / "application.log"
    log.write_text("ephemeral process output")
    log.unlink()
    assert AuditStore(dsn).metrics() == after
    assert AuditStore(dsn).reconstruct(original.id) == history
