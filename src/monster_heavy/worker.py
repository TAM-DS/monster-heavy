"""Deterministic worker wiring. Trusted composition supplies fresh paper evidence, never a model."""

from collections.abc import Callable

from monster_heavy.domain import ExecutionEvidence
from monster_heavy.persistence.execution import ExecutionStore
from monster_heavy.persistence.models import ExecutionRequest
from monster_heavy.persistence.worker import WorkerStore


class Worker:
    def __init__(
        self,
        claims: WorkerStore,
        executor: ExecutionStore,
        evidence: Callable[[ExecutionRequest], ExecutionEvidence],
    ):
        self._claims = claims
        self._executor = executor
        self._evidence = evidence

    def run_once(self, owner: str, lease_seconds: int = 30):
        request = self._claims.claim(owner, lease_seconds)
        if request is None:
            return None
        evidence = self._evidence(request)
        if type(evidence) is not ExecutionEvidence:
            raise ValueError("ExecutionEvidenceRequired")
        # No lease-owner check authorizes execution. A stale worker still enters the
        # exact same locked, idempotent, current-evidence/current-policy transaction.
        return self._executor.execute(request.id, evidence.id)
