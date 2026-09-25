from types import SimpleNamespace
from uuid import uuid4

import pytest

from monster_heavy.domain import ExecutionEvidence, GroundingEvidence
from monster_heavy.persistence.worker import WorkerStore
from monster_heavy.worker import Worker


@pytest.mark.parametrize("duration", [0, -1, True, 1.5, 3601, None])
def test_invalid_lease_duration_never_opens_connection(duration):
    with pytest.raises(ValueError, match="InvalidLeaseSeconds"):
        WorkerStore("unused").claim("owner", duration)


@pytest.mark.parametrize("owner", ["", " ", None, 1])
def test_invalid_lease_owner_never_opens_connection(owner):
    with pytest.raises(ValueError):
        WorkerStore("unused").claim(owner)


def test_worker_returns_without_evidence_when_no_claim():
    def unexpected(*args):
        pytest.fail("Unclaimed work must not reach evidence or execution")

    worker = Worker(
        SimpleNamespace(claim=lambda *args: None), SimpleNamespace(execute=unexpected), unexpected
    )
    assert worker.run_once("owner") is None


def test_worker_routes_fresh_evidence_to_authoritative_executor():
    from datetime import UTC, datetime
    from decimal import Decimal

    from monster_heavy.domain import Observation

    request = SimpleNamespace(id=uuid4())
    evidence = ExecutionEvidence(
        id=uuid4(),
        observation=Observation(
            symbol="TEST",
            price=Decimal(10),
            currency="USD",
            source="paper",
            observed_at=datetime.now(UTC),
        ),
    )
    calls = []

    def execute(request_id, evidence_id):
        calls.append((request_id, evidence_id))
        return "authoritative-result"

    claims = SimpleNamespace(claim=lambda *args: request)
    worker = Worker(claims, SimpleNamespace(execute=execute), lambda r: evidence)
    assert worker.run_once("owner") == "authoritative-result"
    assert calls == [(request.id, evidence.id)]
    invalid = Worker(
        claims,
        SimpleNamespace(execute=execute),
        lambda r: GroundingEvidence(id=evidence.id, observation=evidence.observation),
    )
    with pytest.raises(ValueError, match="ExecutionEvidenceRequired"):
        invalid.run_once("owner")
    assert len(calls) == 1
