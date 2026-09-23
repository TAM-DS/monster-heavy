from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from test_boundaries import policy_rules

from monster_heavy.domain import (
    Actor,
    BoundaryError,
    Observation,
    Provenance,
    Recommendation,
    Terms,
)
from monster_heavy.persistence.boundaries import (
    ApprovalStore,
    ExecutionEvidenceStore,
    GroundingStore,
    PolicyPublicationStore,
    ProposalStore,
)
from monster_heavy.persistence.database import connect
from monster_heavy.persistence.execution import ExecutionStore

pytestmark = pytest.mark.integration
D = Decimal
ACTOR = Actor(id="operator", role="operator")
HUMAN = Actor(id="Thomas", role="approver")
PUBLISHER = Actor(id="publisher", role="policy_publisher")


@pytest.fixture
def case(dsn):
    with connect(dsn) as conn:
        portfolio = conn.execute(
            "INSERT INTO monster_heavy.portfolios(cash,currency) VALUES (1000,'USD') RETURNING *"
        ).fetchone()
    observation = Observation(
        symbol="TEST", price=D(10), currency="USD", source="fixture", observed_at=datetime.now(UTC)
    )
    grounding = GroundingStore(dsn).grounding(observation)
    recommendation = Recommendation(
        terms=Terms(
            portfolio_id=portfolio["id"],
            symbol="TEST",
            side="BUY",
            quantity=D(2),
            reference_price=D(10),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
        grounding_evidence_id=grounding.id,
        provenance=Provenance(model="fixture", model_version="1", prompt_version="1"),
    )
    proposal = ProposalStore(dsn).create(recommendation)
    ApprovalStore(dsn).decide(proposal.id, proposal.terms_hash, HUMAN, "reviewed", "APPROVED")
    policy = PolicyPublicationStore(dsn).publish(policy_rules(), None, PUBLISHER)
    evidence = ExecutionEvidenceStore(dsn).execution(observation)
    return portfolio, proposal, evidence, policy, recommendation


def submit(dsn, case, key=None):
    return ExecutionStore(dsn).submit(key or str(uuid4()), case[1].id, ACTOR)


def snapshot(dsn, case):
    with connect(dsn) as conn:
        portfolio = conn.execute(
            "SELECT * FROM monster_heavy.portfolios WHERE id=%s", (case[0]["id"],)
        ).fetchone()
        positions = conn.execute(
            "SELECT * FROM monster_heavy.positions WHERE portfolio_id=%s", (case[0]["id"],)
        ).fetchall()
        return portfolio, positions


def audit(dsn, result):
    with connect(dsn) as conn:
        row = conn.execute(
            "SELECT e.payload,l.id AS ledger_id,o.id AS outbox_id,r.completed_at,p.status "
            "FROM monster_heavy.execution_attempts a "
            "JOIN monster_heavy.evidence e ON e.id=a.consequence_evidence_id "
            "JOIN monster_heavy.decision_ledger l ON l.attempt_id=a.id "
            "JOIN monster_heavy.outbox o ON o.ledger_id=l.id "
            "JOIN monster_heavy.execution_requests r ON r.id=a.request_id "
            "JOIN monster_heavy.proposals p ON p.id=a.proposal_id WHERE a.id=%s",
            (result.id,),
        ).fetchone()
        assert row and row["completed_at"]
        return row


def test_acceptance_reconstruction_and_replay(dsn, case):
    request = submit(dsn, case)
    store = ExecutionStore(dsn)
    result = store.execute(request.id, case[2].id)
    assert result.status == "ACCEPTED"
    assert result.policy_version == case[3].version and result.policy_digest == case[3].digest
    assert result.execution_evidence_id == case[2].id
    row = audit(dsn, result)
    assert row["status"] == "EXECUTED"
    assert row["payload"]["before"] == {"cash": "1000", "quantity": "0", "revision": 0}
    assert row["payload"]["after"] == {"cash": "980", "quantity": "2", "revision": 1}
    state = snapshot(dsn, case)
    assert store.result(request.id) == store.execute(request.id, case[2].id) == result
    assert submit(dsn, case, key=request.idempotency_key).id == request.id
    assert snapshot(dsn, case) == state
    duplicate = store.execute(submit(dsn, case).id)
    assert duplicate.reason_code == "AlreadyExecuted"
    assert snapshot(dsn, case) == state
    assert audit(dsn, duplicate)["payload"]["before"] == row["payload"]["after"]


@pytest.mark.parametrize(
    "scenario,reason",
    [
        ("missing", "ExecutionEvidenceRequired"),
        ("unknown", "ExecutionEvidenceRequired"),
        ("grounding", "ExecutionEvidenceRequired"),
        ("stale", "StaleEvidence"),
        ("drift", "PriceDriftExceeded"),
        ("symbol", "EvidenceSymbolMismatch"),
        ("currency", "EvidenceCurrencyMismatch"),
        ("cash", "InsufficientCash"),
        ("position", "ResultingPositionExceeded"),
        ("action", "DisallowedAction"),
        ("allowed_symbol", "DisallowedSymbol"),
        ("notional", "OrderNotionalExceeded"),
        ("age", "ProposalPolicyAgeExceeded"),
        ("pending", "IneligibleProposal"),
        ("superseded", "IneligibleProposal"),
        ("malformed", "InvalidExecutionEvidence"),
        ("invalid_policy", "InvalidActivePolicy"),
    ],
)
def test_durable_refusals(dsn, case, scenario, reason):
    evidence_id = case[2].id
    changes = {
        "stale": {"observed_at": datetime.now(UTC) - timedelta(minutes=2)},
        "drift": {"price": D(11)},
        "symbol": {"symbol": "OTHER"},
        "currency": {"currency": "EUR"},
    }
    if scenario in changes:
        evidence_id = (
            ExecutionEvidenceStore(dsn)
            .execution(replace(case[2].observation, **changes[scenario]))
            .id
        )
    if scenario in ("missing", "unknown", "grounding"):
        evidence_id = {
            "missing": None,
            "unknown": uuid4(),
            "grounding": case[1].grounding_evidence_id,
        }[scenario]
    policy_changes = {
        "position": {"maximum_resulting_position": D(1)},
        "action": {"allowed_actions": ("SELL",)},
        "allowed_symbol": {"allowed_symbols": ("OTHER",)},
        "notional": {"maximum_order_notional": D(19)},
        "age": {"maximum_proposal_age_seconds": 1},
    }
    if scenario in policy_changes:
        if scenario == "age":
            with connect(dsn) as conn:
                conn.execute("SELECT pg_sleep(1.05)")
        PolicyPublicationStore(dsn).publish(
            policy_rules(**policy_changes[scenario]), None, PUBLISHER
        )
    with connect(dsn) as conn:
        if scenario == "cash":
            conn.execute(
                "UPDATE monster_heavy.portfolios SET cash=19 WHERE id=%s", (case[0]["id"],)
            )
        if scenario == "pending":
            conn.execute(
                "UPDATE monster_heavy.proposals SET status='PENDING' WHERE id=%s", (case[1].id,)
            )
        if scenario == "malformed":
            evidence_id = conn.execute(
                "INSERT INTO monster_heavy.evidence "
                "(kind,source,payload,observed_at) "
                "VALUES ('EXECUTION','bad','{}',clock_timestamp()) "
                "RETURNING id"
            ).fetchone()["id"]
        if scenario == "invalid_policy":
            conn.execute(
                "INSERT INTO monster_heavy.policy_versions "
                "(version,rules,effective_at,publisher_id,publisher_role) "
                "SELECT max(version)+1,'{}',clock_timestamp(),'fixture','policy_publisher' "
                "FROM monster_heavy.policy_versions"
            )
    if scenario == "superseded":
        ProposalStore(dsn).create(case[4], supersedes_id=case[1].id)
    before = snapshot(dsn, case)
    request = submit(dsn, case)
    result = ExecutionStore(dsn).execute(request.id, evidence_id)
    assert result.status == "REJECTED" and result.reason_code == reason
    assert snapshot(dsn, case) == before
    row = audit(dsn, result)
    assert row["payload"]["before"] == row["payload"]["after"]
    assert ExecutionStore(dsn).execute(request.id, case[2].id) == result
    assert result.execution_evidence_id == (
        None if scenario in ("missing", "unknown", "grounding") else evidence_id
    )


@pytest.mark.parametrize(
    "side,initial,reason,cash,quantity",
    [
        ("SELL", "1", "InsufficientPosition", "1000", "1"),
        ("SELL", "2", None, "1020", "0"),
        ("SELL", "3", None, "1020", "1"),
        ("BUY", "3", None, "980", "5"),
    ],
)
def test_existing_positions(dsn, case, side, initial, reason, cash, quantity):
    with connect(dsn) as conn:
        conn.execute(
            "INSERT INTO monster_heavy.positions VALUES (%s,'TEST',%s)", (case[0]["id"], D(initial))
        )
    proposal = ProposalStore(dsn).create(replace(case[4], terms=replace(case[4].terms, side=side)))
    ApprovalStore(dsn).decide(proposal.id, proposal.terms_hash, HUMAN, "review", "APPROVED")
    request = ExecutionStore(dsn).submit(str(uuid4()), proposal.id, ACTOR)
    result = ExecutionStore(dsn).execute(request.id, case[2].id)
    assert result.reason_code == reason
    state = snapshot(dsn, case)
    assert state[0]["cash"] == D(cash) and state[1][0]["quantity"] == D(quantity)


@pytest.mark.parametrize("field", ["proposal", "actor"])
def test_idempotency_conflict(dsn, case, field):
    request = submit(dsn, case)
    with pytest.raises(BoundaryError, match="IdempotencyConflict"):
        ExecutionStore(dsn).submit(
            request.idempotency_key,
            uuid4() if field == "proposal" else case[1].id,
            Actor(id="other", role="operator") if field == "actor" else ACTOR,
        )
    assert ExecutionStore(dsn).execute(request.id, case[2].id).status == "ACCEPTED"


@pytest.mark.concurrency
@pytest.mark.parametrize("same_key", [True, False])
def test_independent_execution_connections_race(dsn, case, same_key, monkeypatch):
    import monster_heavy.persistence.execution as module

    barrier = Barrier(2)
    pids = set()

    def independent(value):
        conn = connect(value)
        pids.add(conn.info.backend_pid)
        return conn

    monkeypatch.setattr(module, "connect", independent)
    key = str(uuid4())

    def run(index):
        barrier.wait(timeout=10)
        request = submit(dsn, case, key=key if same_key else key + str(index))
        return ExecutionStore(dsn).execute(request.id, case[2].id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert len(pids) >= 2
    if same_key:
        assert results[0] == results[1]
    else:
        assert sorted(r.status for r in results) == ["ACCEPTED", "REJECTED"]
        assert next(r for r in results if r.status == "REJECTED").reason_code == "AlreadyExecuted"
    state = snapshot(dsn, case)
    assert state[0]["cash"] == 980 and state[0]["revision"] == 1
    with connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM monster_heavy.execution_attempts a "
                "JOIN monster_heavy.decision_ledger l ON l.attempt_id=a.id "
                "WHERE a.proposal_id=%s AND a.status='ACCEPTED'",
                (case[1].id,),
            ).fetchone()["n"]
            == 1
        )


@pytest.mark.concurrency
def test_different_proposals_serialize_portfolio_capacity(dsn, case):
    with connect(dsn) as conn:
        conn.execute("UPDATE monster_heavy.portfolios SET cash=20 WHERE id=%s", (case[0]["id"],))
    second = ProposalStore(dsn).create(case[4])
    ApprovalStore(dsn).decide(second.id, second.terms_hash, HUMAN, "review", "APPROVED")
    store = ExecutionStore(dsn)
    requests = [submit(dsn, case), store.submit(str(uuid4()), second.id, ACTOR)]
    barrier = Barrier(2)

    def run(request):
        barrier.wait(timeout=10)
        return store.execute(request.id, case[2].id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, requests))
    assert sorted(r.status for r in results) == ["ACCEPTED", "REJECTED"]
    assert next(r for r in results if r.reason_code).reason_code == "InsufficientCash"
    assert snapshot(dsn, case)[0]["cash"] == 0


def test_failure_after_all_writes_rolls_back_and_request_retries(dsn, case, monkeypatch):
    import monster_heavy.persistence.execution as module

    request = submit(dsn, case)
    before = snapshot(dsn, case)
    original = module.ExecutionStore._finish

    def fail(*args):
        original(*args)
        args[0].execute("SELECT 1/0")

    with monkeypatch.context() as patch:
        patch.setattr(module.ExecutionStore, "_finish", staticmethod(fail))
        with pytest.raises(psycopg.errors.DivisionByZero):
            ExecutionStore(dsn).execute(request.id, case[2].id)
    assert snapshot(dsn, case) == before
    assert ExecutionStore(dsn).result(request.id) is None
    with connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT status FROM monster_heavy.proposals WHERE id=%s", (case[1].id,)
            ).fetchone()["status"]
            == "APPROVED"
        )
        assert (
            conn.execute(
                "SELECT completed_at FROM monster_heavy.execution_requests WHERE id=%s",
                (request.id,),
            ).fetchone()["completed_at"]
            is None
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM monster_heavy.evidence "
                "WHERE kind='CONSEQUENCE' AND payload->>'portfolio_id'=%s",
                (str(case[0]["id"]),),
            ).fetchone()["n"]
            == 0
        )
        # No attempt means no ledger or outbox can survive their mandatory FK chain.
    assert ExecutionStore(dsn).execute(request.id, case[2].id).status == "ACCEPTED"


def test_runtime_role_and_terminal_immutability(dsn, case, monkeypatch):
    import monster_heavy.persistence.execution as module

    def runtime(value):
        conn = connect(value)
        conn.execute("SET ROLE monster_heavy_app")
        return conn

    monkeypatch.setattr(module, "connect", runtime)
    request = submit(dsn, case)
    assert ExecutionStore(dsn).execute(request.id, case[2].id).status == "ACCEPTED"
    with pytest.raises(psycopg.errors.CheckViolation), connect(dsn) as conn:
        conn.execute(
            "UPDATE monster_heavy.execution_attempts "
            "SET reason_code='tampered' WHERE request_id=%s",
            (request.id,),
        )


def test_new_key_can_retry_rejection_with_fresh_evidence(dsn, case):
    request = submit(dsn, case)
    store = ExecutionStore(dsn)
    rejected = store.execute(request.id)
    assert rejected.reason_code == "ExecutionEvidenceRequired"
    assert store.execute(submit(dsn, case).id, case[2].id).status == "ACCEPTED"
    assert store.execute(request.id, case[2].id) == rejected


@pytest.mark.concurrency
@pytest.mark.parametrize(
    "change", ["expiry", "stale", "policy", "scheduled_policy", "supersession"]
)
def test_rechecks_after_independent_lock_wait(dsn, case, change, monkeypatch):
    import time
    from queue import Queue

    import monster_heavy.persistence.execution as module

    proposal = case[1]
    if change == "expiry":
        proposal = ProposalStore(dsn).create(
            replace(
                case[4],
                terms=replace(case[4].terms, expires_at=datetime.now(UTC) + timedelta(seconds=1)),
            )
        )
        ApprovalStore(dsn).decide(proposal.id, proposal.terms_hash, HUMAN, "review", "APPROVED")
    if change == "stale":
        PolicyPublicationStore(dsn).publish(
            policy_rules(maximum_execution_evidence_age_seconds=1), None, PUBLISHER
        )
    scheduled = None
    if change == "scheduled_policy":
        scheduled = PolicyPublicationStore(dsn).publish(
            policy_rules(maximum_order_notional=D(1)),
            datetime.now(UTC) + timedelta(seconds=1),
            PUBLISHER,
        )
    request = ExecutionStore(dsn).submit(str(uuid4()), proposal.id, ACTOR)
    pids = Queue()

    def monitored(value):
        conn = connect(value)
        pids.put(conn.info.backend_pid)
        return conn

    monkeypatch.setattr(module, "connect", monitored)
    with connect(dsn) as locker, ThreadPoolExecutor(max_workers=1) as pool:
        locker.execute(
            "SELECT id FROM monster_heavy.proposals WHERE id=%s FOR UPDATE", (proposal.id,)
        )
        future = pool.submit(ExecutionStore(dsn).execute, request.id, case[2].id)
        pid = pids.get(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            waiting = locker.execute(
                "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (pid,)
            ).fetchone()
            if waiting and waiting["wait_event_type"] == "Lock":
                break
            if time.monotonic() > deadline:
                pytest.fail("Executor did not wait on independent proposal lock")
            time.sleep(0.01)
            locker.execute("SELECT pg_stat_clear_snapshot()")
        current = None
        if change == "policy":
            current = PolicyPublicationStore(dsn).publish(
                policy_rules(maximum_order_notional=D(1)), None, PUBLISHER
            )
        elif change == "supersession":
            # Same status transition as Lifecycle/ProposalStore under its proposal lock.
            locker.execute(
                "UPDATE monster_heavy.proposals SET status='SUPERSEDED' WHERE id=%s", (proposal.id,)
            )
        else:
            target = (
                proposal.expires_at
                if change == "expiry"
                else scheduled.effective_at
                if scheduled
                else case[2].observation.observed_at + timedelta(seconds=1)
            )
            while datetime.now(UTC) <= target:
                time.sleep(0.01)
        locker.commit()
        result = future.result(timeout=5)
    expected = {
        "expiry": "ProposalExpired",
        "stale": "StaleEvidence",
        "policy": "OrderNotionalExceeded",
        "scheduled_policy": "OrderNotionalExceeded",
        "supersession": "IneligibleProposal",
    }
    assert result.reason_code == expected[change]
    if current or scheduled:
        active = current or scheduled
        assert (result.policy_version, result.policy_digest) == (active.version, active.digest)
    assert snapshot(dsn, case)[0] == case[0]
    assert audit(dsn, result)["payload"]["before"] == audit(dsn, result)["payload"]["after"]


def test_database_unique_slot_survives_application_status_tampering(dsn, case):
    store = ExecutionStore(dsn)
    accepted = store.execute(submit(dsn, case).id, case[2].id)
    with connect(dsn) as conn:
        conn.execute(
            "UPDATE monster_heavy.proposals SET status='APPROVED' WHERE id=%s", (case[1].id,)
        )
    second = submit(dsn, case)
    assert store.execute(second.id, case[2].id).reason_code == "AlreadyExecuted"
    # Direct SQL still cannot bypass the original partial unique index.
    from monster_heavy.persistence.boundaries import _insert

    with pytest.raises(psycopg.errors.UniqueViolation), connect(dsn) as conn:
        _insert(
            conn,
            "execution_attempts",
            request_id=second.id,
            proposal_id=case[1].id,
            status="ACCEPTED",
            approval_id=accepted.approval_id,
            execution_evidence_id=accepted.execution_evidence_id,
            policy_version=accepted.policy_version,
            policy_digest=accepted.policy_digest,
            consequence_evidence_id=accepted.consequence_evidence_id,
            started_at=accepted.started_at,
            finished_at=datetime.now(UTC),
        )
    assert snapshot(dsn, case)[0]["revision"] == 1


def test_pending_replacement_has_no_inherited_approval(dsn, case):
    replacement = ProposalStore(dsn).create(case[4], supersedes_id=case[1].id)
    request = ExecutionStore(dsn).submit(str(uuid4()), replacement.id, ACTOR)
    result = ExecutionStore(dsn).execute(request.id, case[2].id)
    assert result.reason_code == "IneligibleProposal" and result.approval_id is None
    assert snapshot(dsn, case)[0] == case[0]


def test_actual_execution_notional_rechecked_with_current_policy(dsn, case):
    policy = PolicyPublicationStore(dsn).publish(
        policy_rules(maximum_order_notional=D(20)), None, PUBLISHER
    )
    evidence = ExecutionEvidenceStore(dsn).execution(replace(case[2].observation, price=D("10.5")))
    result = ExecutionStore(dsn).execute(submit(dsn, case).id, evidence.id)
    assert result.reason_code == "OrderNotionalExceeded"
    assert result.policy_version == policy.version
    assert snapshot(dsn, case)[0] == case[0]


def test_replay_ignores_new_evidence_and_policy(dsn, case):
    request = submit(dsn, case)
    store = ExecutionStore(dsn)
    accepted = store.execute(request.id, case[2].id)
    PolicyPublicationStore(dsn).publish(policy_rules(allowed_actions=("SELL",)), None, PUBLISHER)
    assert store.execute(request.id, uuid4()) == accepted
    assert store.execute(request.id) == accepted
    assert snapshot(dsn, case)[0]["revision"] == 1


@pytest.mark.concurrency
def test_policy_publication_waits_for_execution_decision(dsn, case, monkeypatch):
    from threading import Event

    import monster_heavy.persistence.execution as module

    ready, release = Event(), Event()
    original = module.ExecutionStore._finish

    def held(*args):
        result = original(*args)
        ready.set()
        assert release.wait(timeout=10)
        return result

    monkeypatch.setattr(module.ExecutionStore, "_finish", staticmethod(held))
    request = submit(dsn, case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        execution = pool.submit(ExecutionStore(dsn).execute, request.id, case[2].id)
        assert ready.wait(timeout=5)
        publication = pool.submit(
            PolicyPublicationStore(dsn).publish,
            policy_rules(allowed_actions=("SELL",)),
            None,
            PUBLISHER,
        )
        try:
            # Publication must not pass the executor's shared advisory lock.
            from concurrent.futures import TimeoutError

            with pytest.raises(TimeoutError):
                publication.result(timeout=0.1)
        finally:
            release.set()
        result = execution.result(timeout=5)
        policy = publication.result(timeout=5)
    assert result.status == "ACCEPTED" and result.policy_version == case[3].version
    assert policy.effective_at > result.started_at


def test_unknown_request_has_no_result(dsn):
    store = ExecutionStore(dsn)
    unknown = uuid4()
    assert store.result(unknown) is None
    with pytest.raises(BoundaryError, match="ExecutionRequestNotFound"):
        store.execute(unknown)


def test_high_precision_round_trip(dsn, case):
    from decimal import localcontext

    proposal = ProposalStore(dsn).create(
        replace(
            case[4], terms=replace(case[4].terms, quantity=D("0.12345678901234567890123456789"))
        )
    )
    ApprovalStore(dsn).decide(proposal.id, proposal.terms_hash, HUMAN, "review", "APPROVED")
    store = ExecutionStore(dsn)
    request = store.submit(str(uuid4()), proposal.id, ACTOR)
    with localcontext() as context:
        context.prec = 2
        result = store.execute(request.id, case[2].id)
    assert result.status == "ACCEPTED"
    state = snapshot(dsn, case)
    assert state[0]["cash"] == D("998.76543210987654321098765432110")
    assert state[1][0]["quantity"] == D("0.12345678901234567890123456789")


@pytest.mark.parametrize("target", ["evidence", "policy"])
def test_invalid_decimal_payload_is_a_durable_refusal(dsn, case, target):
    from psycopg.types.json import Jsonb

    from monster_heavy.persistence.boundaries import _insert

    evidence_id = case[2].id
    with connect(dsn) as conn:
        if target == "evidence":
            row = _insert(
                conn,
                "evidence",
                kind="EXECUTION",
                source="malformed-fixture",
                observed_at=datetime.now(UTC),
                payload=Jsonb(
                    {"schema_version": 1, "symbol": "TEST", "price": "invalid", "currency": "USD"}
                ),
            )
            evidence_id = row["id"]
        else:
            row = conn.execute(
                "SELECT * FROM monster_heavy.policy_versions WHERE version=%s", (case[3].version,)
            ).fetchone()
            values = row["rules"] | {"maximum_order_notional": "invalid"}
            _insert(
                conn,
                "policy_versions",
                version=case[3].version + 1,
                rules=Jsonb(values),
                effective_at=datetime.now(UTC),
                publisher_id="fixture",
                publisher_role="policy_publisher",
            )
    result = ExecutionStore(dsn).execute(submit(dsn, case).id, evidence_id)
    assert result.reason_code == (
        "InvalidExecutionEvidence" if target == "evidence" else "InvalidActivePolicy"
    )
    assert snapshot(dsn, case)[0] == case[0]
    assert audit(dsn, result)["payload"]["before"] == audit(dsn, result)["payload"]["after"]
