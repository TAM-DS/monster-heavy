from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from monster_heavy.application import (
    ApprovalService,
    ExecutionEvidenceService,
    GroundingService,
    LifecycleService,
    PolicyService,
    ProposalService,
)
from monster_heavy.domain import (
    Actor,
    BoundaryError,
    Observation,
    PolicyRules,
    Provenance,
    Recommendation,
    Terms,
    verify_policy_terms,
)
from monster_heavy.persistence.boundaries import (
    ApprovalStore,
    ExecutionEvidenceStore,
    GroundingStore,
    LifecycleStore,
    PolicyPublicationStore,
    PolicyReadStore,
    ProposalStore,
)
from monster_heavy.persistence.database import connect

pytestmark = pytest.mark.integration
HUMAN = Actor(id="human-123", role="approver")
PUBLISHER = Actor(id="policy-admin", role="policy_publisher")


def policy_rules(**changes):
    values = dict(
        allowed_actions=("BUY", "SELL"),
        allowed_symbols=("TEST",),
        maximum_order_notional=Decimal("1000"),
        maximum_resulting_position=Decimal("100"),
        maximum_execution_evidence_age_seconds=60,
        maximum_price_drift=Decimal("0.05"),
        maximum_proposal_age_seconds=3600,
    )
    return PolicyRules(**(values | changes))


@pytest.fixture
def setup(dsn):
    with connect(dsn) as conn:
        portfolio = conn.execute(
            "INSERT INTO monster_heavy.portfolios(cash,currency) VALUES (1000,'USD') RETURNING *"
        ).fetchone()
        conn.execute("INSERT INTO monster_heavy.positions VALUES (%s,'TEST',3)", (portfolio["id"],))
    observation = Observation(
        symbol="TEST",
        price=Decimal("10"),
        currency="USD",
        source="trusted-test-provider",
        observed_at=datetime.now(UTC),
    )
    grounding = GroundingService(GroundingStore(dsn)).create(observation)
    recommendation = Recommendation(
        terms=Terms(
            portfolio_id=portfolio["id"],
            symbol="TEST",
            side="BUY",
            quantity=Decimal("2"),
            reference_price=Decimal("10"),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
        grounding_evidence_id=grounding.id,
        provenance=Provenance(model="structured-fixture", model_version="1", prompt_version="1"),
    )
    return portfolio, observation, recommendation


def proposal(dsn, setup):
    return ProposalService(ProposalStore(dsn)).create(setup[2])


def read(dsn, proposal_id):
    with connect(dsn) as conn:
        return conn.execute(
            "SELECT * FROM monster_heavy.proposals WHERE id=%s", (proposal_id,)
        ).fetchone()


def assert_no_consequence(dsn, portfolio):
    with connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT * FROM monster_heavy.portfolios WHERE id=%s", (portfolio["id"],)
            ).fetchone()
            == portfolio
        )
        assert conn.execute(
            "SELECT quantity FROM monster_heavy.positions WHERE portfolio_id=%s", (portfolio["id"],)
        ).fetchall() == [{"quantity": Decimal("3")}]
        for table in ("execution_requests", "execution_attempts"):
            assert (
                conn.execute(
                    f"SELECT count(*) AS n FROM monster_heavy.{table} r "
                    "JOIN monster_heavy.proposals p ON p.id=r.proposal_id WHERE p.portfolio_id=%s",
                    (portfolio["id"],),
                ).fetchone()["n"]
                == 0
            )


def test_creation_only_persists_pending_and_provenance(dsn, setup):
    value = proposal(dsn, setup)
    assert value.status == "PENDING"
    assert value.grounding_evidence_id == setup[2].grounding_evidence_id
    assert value.model_provenance == {
        "model": "structured-fixture",
        "model_version": "1",
        "prompt_version": "1",
    }
    with connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM monster_heavy.approvals WHERE proposal_id=%s",
                (value.id,),
            ).fetchone()["n"]
            == 0
        )
    assert_no_consequence(dsn, setup[0])


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_human_decision_atomic_and_non_consequential(dsn, setup, decision):
    value = proposal(dsn, setup)
    result = getattr(ApprovalService(ApprovalStore(dsn)), decision)(
        value.id, value.terms_hash, HUMAN, "reviewed exact terms"
    )
    assert result.terms_hash == value.terms_hash
    assert result.actor_id == HUMAN.id
    assert read(dsn, value.id)["status"] == result.decision
    with connect(dsn) as conn:
        evidence = conn.execute(
            "SELECT * FROM monster_heavy.evidence WHERE id=%s", (result.evidence_id,)
        ).fetchone()
        assert evidence["kind"] == "APPROVAL"
        assert evidence["payload"]["terms_hash"] == value.terms_hash
        assert evidence["payload"]["rationale"] == result.rationale
        assert evidence["observed_at"] == result.created_at
    with pytest.raises(BoundaryError, match="IneligibleProposal"):
        ApprovalService(ApprovalStore(dsn)).approve(value.id, value.terms_hash, HUMAN, "again")
    assert_no_consequence(dsn, setup[0])


def test_wrong_hash_leaves_no_decision(dsn, setup):
    value = proposal(dsn, setup)
    with pytest.raises(BoundaryError, match="TermsHashMismatch"):
        ApprovalService(ApprovalStore(dsn)).approve(value.id, "0" * 64, HUMAN, "wrong")
    assert read(dsn, value.id)["status"] == "PENDING"
    with connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM monster_heavy.approvals WHERE proposal_id=%s",
                (value.id,),
            ).fetchone()["n"]
            == 0
        )


def test_hash_determinism_and_scale_normalization(dsn, setup):
    first = proposal(dsn, setup)
    value = replace(
        setup[2],
        terms=replace(
            setup[2].terms, quantity=Decimal("2.000"), reference_price=Decimal("10.0000")
        ),
    )
    second = ProposalService(ProposalStore(dsn)).create(value)
    assert first.id != second.id
    assert first.terms_hash == second.terms_hash
    with connect(dsn) as conn:
        expected = conn.execute(
            "SELECT encode(sha256(convert_to(jsonb_build_array(portfolio_id,symbol,side,"
            "quantity,reference_price,expires_at AT TIME ZONE 'UTC')::text,'UTF8')),'hex') AS hash "
            "FROM monster_heavy.proposals WHERE id=%s",
            (first.id,),
        ).fetchone()["hash"]
    assert first.terms_hash == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", Decimal("3")),
        ("side", "SELL"),
        ("expires_at", datetime(2090, 1, 1, tzinfo=UTC)),
    ],
)
def test_execution_term_change_changes_hash(dsn, setup, field, value):
    first = proposal(dsn, setup)
    changed = replace(setup[2], terms=replace(setup[2].terms, **{field: value}))
    second = ProposalService(ProposalStore(dsn)).create(changed)
    assert first.terms_hash != second.terms_hash


@pytest.mark.parametrize("approved", [False, True])
def test_supersession_preserves_old_history_and_requires_new_approval(dsn, setup, approved):
    first = proposal(dsn, setup)
    service = ApprovalService(ApprovalStore(dsn))
    if approved:
        service.approve(first.id, first.terms_hash, HUMAN, "original")
    changed = replace(setup[2], terms=replace(setup[2].terms, quantity=Decimal("3")))
    second = ProposalService(ProposalStore(dsn)).create(changed, supersedes_id=first.id)
    assert read(dsn, first.id)["status"] == "SUPERSEDED"
    assert second.status == "PENDING"
    assert second.supersedes_id == first.id
    with pytest.raises(BoundaryError, match="TermsHashMismatch"):
        service.approve(second.id, first.terms_hash, HUMAN, "reuse")
    with pytest.raises(BoundaryError, match="IneligibleProposal"):
        service.approve(first.id, first.terms_hash, HUMAN, "old")
    service.approve(second.id, second.terms_hash, HUMAN, "new review")
    assert_no_consequence(dsn, setup[0])


def test_invalid_replacement_rolls_back_supersession(dsn, setup):
    first = proposal(dsn, setup)
    invalid = replace(setup[2], grounding_evidence_id=uuid4())
    with pytest.raises(BoundaryError, match="GroundingEvidenceRequired"):
        ProposalService(ProposalStore(dsn)).create(invalid, supersedes_id=first.id)
    assert read(dsn, first.id)["status"] == "PENDING"


@pytest.mark.parametrize("approved", [False, True])
def test_expired_proposal_cannot_be_authorized_or_replaced(dsn, setup, approved):
    # Create a valid historical fixture directly; immutable expiry is never edited.
    with connect(dsn) as conn:
        value = conn.execute(
            "INSERT INTO monster_heavy.proposals "
            "(portfolio_id,symbol,side,quantity,reference_price,"
            "grounding_evidence_id,model_provenance,created_at,expires_at) "
            "VALUES (%s,'TEST','BUY',2,10,%s,'{}',clock_timestamp()-interval '2 hours',"
            "clock_timestamp()-interval '1 hour') RETURNING *",
            (setup[0]["id"], setup[2].grounding_evidence_id),
        ).fetchone()
        if approved:
            # Phase 1 permits status fixtures; Phase 2 services cannot approve expired proposals.
            conn.execute(
                "UPDATE monster_heavy.proposals SET status='APPROVED' WHERE id=%s", (value["id"],)
            )
    with pytest.raises(BoundaryError):
        ApprovalService(ApprovalStore(dsn)).approve(value["id"], value["terms_hash"], HUMAN, "late")
    with pytest.raises(BoundaryError, match="ProposalExpired"):
        ProposalService(ProposalStore(dsn)).create(setup[2], supersedes_id=value["id"])
    lifecycle = LifecycleService(LifecycleStore(dsn))
    assert lifecycle.expire(value["id"]).status == "EXPIRED"
    assert lifecycle.expire(value["id"]).status == "EXPIRED"
    assert_no_consequence(dsn, setup[0])


def test_live_proposal_cannot_expire(dsn, setup):
    value = proposal(dsn, setup)
    with pytest.raises(BoundaryError, match="ProposalNotExpired"):
        LifecycleService(LifecycleStore(dsn)).expire(value.id)


def test_typed_evidence_separation_and_association(dsn, setup):
    execution = ExecutionEvidenceService(ExecutionEvidenceStore(dsn)).create(setup[1])
    assert execution.id != setup[2].grounding_evidence_id
    assert ExecutionEvidenceStore(dsn).get(execution.id) == execution
    with pytest.raises(BoundaryError, match="ExecutionEvidenceRequired"):
        ExecutionEvidenceStore(dsn).get(setup[2].grounding_evidence_id)
    with pytest.raises(BoundaryError, match="GroundingEvidenceRequired"):
        ProposalService(ProposalStore(dsn)).create(
            replace(setup[2], grounding_evidence_id=execution.id)
        )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("symbol", "OTHER", "GroundingTermsMismatch"),
        ("reference_price", Decimal("11"), "GroundingTermsMismatch"),
        ("expires_at", datetime(2000, 1, 1, tzinfo=UTC), "ProposalExpired"),
    ],
)
def test_creation_refuses_invalid_association(dsn, setup, field, value, reason):
    with pytest.raises(BoundaryError, match=reason):
        ProposalService(ProposalStore(dsn)).create(
            replace(setup[2], terms=replace(setup[2].terms, **{field: value}))
        )


def test_policy_versions_current_resolution_and_stricter_controls(dsn, setup):
    writer = PolicyService(PolicyPublicationStore(dsn))
    reader = PolicyReadStore(dsn)
    first = writer.publish(policy_rules(), None, PUBLISHER)
    value = proposal(dsn, setup)
    ApprovalService(ApprovalStore(dsn)).approve(value.id, value.terms_hash, HUMAN, "review")
    second = writer.publish(policy_rules(maximum_order_notional=Decimal("19")), None, PUBLISHER)
    future = writer.publish(policy_rules(), datetime.now(UTC) + timedelta(days=10), PUBLISHER)
    assert second.version == first.version + 1
    assert reader.active() == second
    assert reader.at(first.effective_at) == first
    assert reader.at(future.effective_at) == future
    assert reader.get(first.version) == first
    assert second.digest != first.digest
    with pytest.raises(BoundaryError, match="OrderNotionalExceeded"):
        verify_policy_terms(
            setup[2].terms, reader.active().rules, value.created_at, datetime.now(UTC)
        )
    assert read(dsn, value.id)["status"] == "APPROVED"
    assert_no_consequence(dsn, setup[0])


def test_no_policy_and_backdated_publication_fail_closed(dsn):
    with pytest.raises(BoundaryError, match="NoActivePolicy"):
        PolicyReadStore(dsn).at(datetime(1, 1, 1, tzinfo=UTC))
    with pytest.raises(BoundaryError, match="RetroactivePolicyPublication"):
        PolicyService(PolicyPublicationStore(dsn)).publish(
            policy_rules(), datetime(2000, 1, 1, tzinfo=UTC), PUBLISHER
        )


def test_equivalent_policy_rules_have_same_digest(dsn):
    writer = PolicyService(PolicyPublicationStore(dsn))
    first = writer.publish(policy_rules(), None, PUBLISHER)
    second = writer.publish(
        policy_rules(allowed_actions=("SELL", "BUY"), maximum_order_notional=Decimal("1000.000")),
        None,
        PUBLISHER,
    )
    assert first.digest == second.digest
    assert first.version != second.version


@pytest.mark.concurrency
@pytest.mark.parametrize("other", ["reject", "supersede"])
def test_independent_decisions_serialize(dsn, setup, other):
    first = proposal(dsn, setup)
    barrier = Barrier(2)

    def run(action):
        barrier.wait(timeout=10)
        try:
            if action == "supersede":
                return ProposalService(ProposalStore(dsn)).create(setup[2], supersedes_id=first.id)
            return getattr(ApprovalService(ApprovalStore(dsn)), action)(
                first.id, first.terms_hash, HUMAN, "race"
            )
        except BoundaryError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["approve", other]))
    with connect(dsn) as conn:
        count = conn.execute(
            "SELECT count(*) AS n FROM monster_heavy.approvals WHERE proposal_id=%s", (first.id,)
        ).fetchone()["n"]
    if other == "reject":
        assert count == 1
        assert sum(isinstance(result, str) for result in results) == 1
    else:
        assert read(dsn, first.id)["status"] == "SUPERSEDED"
        assert count in (0, 1)
    assert_no_consequence(dsn, setup[0])


@pytest.mark.concurrency
def test_policy_publications_allocate_distinct_versions(dsn):
    barrier = Barrier(2)

    def publish(_):
        barrier.wait(timeout=10)
        return PolicyService(PolicyPublicationStore(dsn)).publish(policy_rules(), None, PUBLISHER)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, range(2)))
    assert abs(results[0].version - results[1].version) == 1
    assert results[0].effective_at != results[1].effective_at


def test_approval_failure_rolls_back_evidence_and_status(dsn, setup, monkeypatch):
    import monster_heavy.persistence.boundaries as boundaries

    value = proposal(dsn, setup)
    original = boundaries._status

    def fail_after_status(conn, proposal_id, status):
        original(conn, proposal_id, status)
        conn.execute("SELECT 1/0")

    monkeypatch.setattr(boundaries, "_status", fail_after_status)
    with pytest.raises(psycopg.errors.DivisionByZero):
        ApprovalService(ApprovalStore(dsn)).approve(value.id, value.terms_hash, HUMAN, "rollback")
    assert read(dsn, value.id)["status"] == "PENDING"
    with connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM monster_heavy.approvals WHERE proposal_id=%s",
                (value.id,),
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM monster_heavy.evidence WHERE kind='APPROVAL' "
                "AND payload->>'proposal_id'=%s",
                (str(value.id),),
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.parametrize("kind", ["grounding", "execution"])
def test_future_observation_refused(dsn, setup, kind):
    store = GroundingStore(dsn) if kind == "grounding" else ExecutionEvidenceStore(dsn)
    with pytest.raises(BoundaryError, match="FutureEvidence"):
        getattr(store, kind)(replace(setup[1], observed_at=datetime.now(UTC) + timedelta(days=1)))


def test_currency_mismatch_refused(dsn, setup):
    grounding = GroundingStore(dsn).grounding(replace(setup[1], currency="EUR"))
    with pytest.raises(BoundaryError, match="EvidenceCurrencyMismatch"):
        ProposalService(ProposalStore(dsn)).create(
            replace(setup[2], grounding_evidence_id=grounding.id)
        )


@pytest.mark.parametrize("status", ["REJECTED", "SUPERSEDED", "EXECUTED"])
def test_terminal_states_cannot_reenter_lifecycle(dsn, setup, status):
    value = proposal(dsn, setup)
    with connect(dsn) as conn:
        conn.execute("UPDATE monster_heavy.proposals SET status=%s WHERE id=%s", (status, value.id))
    with pytest.raises(BoundaryError, match="IneligibleProposal"):
        LifecycleStore(dsn).expire(value.id)
    with pytest.raises(BoundaryError, match="IneligibleProposal"):
        ProposalStore(dsn).create(setup[2], supersedes_id=value.id)
    with pytest.raises(BoundaryError, match="IneligibleProposal"):
        ApprovalStore(dsn).decide(value.id, value.terms_hash, HUMAN, "terminal", "REJECTED")


@pytest.mark.concurrency
def test_two_replacements_cannot_branch_history(dsn, setup):
    first = proposal(dsn, setup)
    barrier = Barrier(2)

    def replace_once(_):
        barrier.wait(timeout=10)
        try:
            return ProposalStore(dsn).create(setup[2], supersedes_id=first.id)
        except BoundaryError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(replace_once, range(2)))
    assert sum(isinstance(result, str) for result in results) == 1
    assert read(dsn, first.id)["status"] == "SUPERSEDED"


def test_duplicate_effective_time_is_explicit_conflict(dsn):
    writer = PolicyPublicationStore(dsn)
    effective_at = datetime.now(UTC) + timedelta(days=20)
    writer.publish(policy_rules(), effective_at, PUBLISHER)
    with pytest.raises(BoundaryError, match="PolicyEffectiveTimeConflict"):
        writer.publish(policy_rules(), effective_at, PUBLISHER)


def test_adapters_work_as_phase1_runtime_role(dsn, setup, monkeypatch):
    import monster_heavy.persistence.boundaries as boundaries

    def runtime_connect(value):
        conn = connect(value)
        conn.execute("SET ROLE monster_heavy_app")
        return conn

    monkeypatch.setattr(boundaries, "connect", runtime_connect)
    value = proposal(dsn, setup)
    ApprovalService(ApprovalStore(dsn)).approve(value.id, value.terms_hash, HUMAN, "runtime role")
    result = PolicyPublicationStore(dsn).publish(policy_rules(), None, PUBLISHER)
    assert PolicyReadStore(dsn).active() == result
    assert_no_consequence(dsn, setup[0])


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "symbol": "TEST", "price": 10.0, "currency": "USD"},
        {"schema_version": True, "symbol": "TEST", "price": "10", "currency": "USD"},
        {"schema_version": 1, "symbol": "TEST", "price": "10"},
    ],
)
def test_unvalidated_stored_payload_cannot_enter_typed_boundary(dsn, payload):
    from psycopg.types.json import Jsonb

    with connect(dsn) as conn:
        row = conn.execute(
            "INSERT INTO monster_heavy.evidence(kind,source,payload,observed_at) "
            "VALUES ('EXECUTION','raw-fixture',%s,clock_timestamp()) RETURNING id",
            (Jsonb(payload),),
        ).fetchone()
    with pytest.raises(BoundaryError):
        ExecutionEvidenceStore(dsn).get(row["id"])


def test_supersession_failure_rolls_back_replacement(dsn, setup, monkeypatch):
    import monster_heavy.persistence.boundaries as boundaries

    value = proposal(dsn, setup)
    original = boundaries._status

    def fail_after_status(conn, proposal_id, status):
        original(conn, proposal_id, status)
        conn.execute("SELECT 1/0")

    monkeypatch.setattr(boundaries, "_status", fail_after_status)
    with pytest.raises(psycopg.errors.DivisionByZero):
        ProposalStore(dsn).create(setup[2], supersedes_id=value.id)
    assert read(dsn, value.id)["status"] == "PENDING"
    with connect(dsn) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM monster_heavy.proposals WHERE supersedes_id=%s",
                (value.id,),
            ).fetchone()["n"]
            == 0
        )


@pytest.mark.concurrency
def test_approval_rechecks_time_after_waiting_for_lock(dsn, setup):
    import time

    # The immutable fixture expires while an independent approval connection waits.
    recommendation = replace(
        setup[2], terms=replace(setup[2].terms, expires_at=datetime.now(UTC) + timedelta(seconds=1))
    )
    value = ProposalStore(dsn).create(recommendation)
    with connect(dsn) as locker, ThreadPoolExecutor(max_workers=1) as pool:
        locker.execute("SELECT id FROM monster_heavy.proposals WHERE id=%s FOR UPDATE", (value.id,))
        future = pool.submit(
            ApprovalStore(dsn).decide, value.id, value.terms_hash, HUMAN, "waited", "APPROVED"
        )
        try:
            deadline = time.monotonic() + 5
            while datetime.now(UTC) < value.expires_at:
                if time.monotonic() > deadline:
                    pytest.fail("Clock did not reach expiry")
                time.sleep(0.01)
        finally:
            locker.commit()
        with pytest.raises(BoundaryError, match="ProposalExpired"):
            future.result(timeout=10)
    assert read(dsn, value.id)["status"] == "PENDING"
    assert LifecycleStore(dsn).expire(value.id).status == "EXPIRED"
