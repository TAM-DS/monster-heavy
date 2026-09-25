# Phase 5 implementation and release evidence

The `phase5/recovery-release` branch is based directly on current Phase 4 main at
`81cca4d`. Thomas has completed implementation review. The narrow two-test
migration-history update was explicitly approved and applied. Phase 5 is in final
release validation; Docker-backed startup and GitHub Actions evidence remain pending.

## Scheduling and recovery

`WorkerStore.claim(owner, lease_seconds=30)` claims one available, incomplete request
using `FOR UPDATE SKIP LOCKED`. An optional request UUID narrows the scan for targeted
recovery. Availability, lease eligibility and expiry use PostgreSQL `clock_timestamp()`.
Owners must be nonempty and leases must be integer seconds in [1, 3600]. These limits
are input bounds, not reliability or throughput measurements.

Claims commit in a short transaction, before evidence acquisition or execution. A claim
update inserts an immutable `CLAIM` or `RECLAIM` control event in that same transaction.
Unexpired leases, future work, completed requests and requests with terminal attempts
(including `FAILED`) are excluded. Expiry permits another owner to claim work; no process
heartbeat or in-memory flag is needed. There is no lease renewal or separate acknowledgement
protocol in v1. Completion is already committed with the authoritative result.

`Worker.run_once()` is trusted deterministic composition, not an AI agent. Its evidence
provider must return a persisted `ExecutionEvidence`; the executor reloads that identifier.
A provider/internal failure leaves the claim recoverable after expiry. A scheduling caller
can call `run_once` repeatedly. No continuously running worker service or synthetic evidence
feed is automatically started by Compose: v1 does not invent observations or execution intent.

Leases do not authorize consequences and need no fencing-token authority. Both a stale worker
and a reclaiming worker enter the original locked `ExecutionStore.execute()` transaction.
Request locking/replay, proposal/portfolio locks, current policy/evidence checks and the
accepted-consequence unique index remain authoritative. The only executor changes add durable
observations on successful duplicate submission and committed-result replay.

`test_os_worker_death_and_recovery` uses Python's spawn mode, separate PostgreSQL sessions,
and SIGKILL. Before-commit death occurs after **all** executor writes but before commit; an
independent session verifies invisibility, and subsequent recovery verifies no surviving
consequence. After-commit death occurs before the worker returns/acknowledges to its caller;
replay returns the original attempt UUID. `test_expired_stale_worker_cannot_duplicate_consequence`
lets a real lease expire and races the old and new deliveries on independent connections.
These tests establish the described cases, not a generalized availability or throughput SLA.

## Compensation

`CompensationStore.request(original_attempt_id, operator, observation, expires_at)` creates
one new `PENDING` proposal. The original must be an `ACCEPTED` attempt. Terms are the same
portfolio, symbol and quantity, with the opposite side. A newly persisted `GROUNDING`
observation supplies the new reference price. It must be no more than 60 seconds old and
not in the future according to database time; its symbol/currency must match. This fixed
creation-time bound is separate from the active policy's execution-evidence freshness bound.

The proposal has `origin=COMPENSATION`, empty `model_provenance`, and an immutable
`compensation_requests` link containing requester identity/role and database request time.
No model is called or attributed. A composite foreign key enforces the original's
`ACCEPTED` status. Deferred constraint triggers require the link and deterministic terms. They also require
the grounding evidence currency to equal the compensation/original portfolio currency.
The `currency` case in `test_database_enforces_compensation_link_and_terms` supplies
wrong-currency grounding directly and verifies PostgreSQL rejects it.
An original attempt permits one compensation request; a concurrent/repeated request raises
`CompensationAlreadyRequested` and rolls back its proposal/evidence writes. A rejected
execution of that compensating proposal can be retried with a new execution key through the
existing rules. A rejected/expired compensating proposal is not silently replaced by this API.

The normal human approval boundary is required. The normal executor independently requires
fresh `EXECUTION` evidence and revalidates current policy and portfolio capacity. There is no
compensation execution bypass. Compensation can itself be rejected. A different reference or
execution price means the net cash need not return to its previous value. Original attempts,
ledger/evidence, and historical portfolio snapshots are never edited. Reconstruction in either
direction includes the original and the linked compensating attempts, including rejections.

## Durable metrics and reconstruction

`AuditStore.metrics()` uses a PostgreSQL repeatable-read, read-only snapshot. Counts cover:

| Field | Durable definition |
| --- | --- |
| `proposals`, `model_proposals` | All proposals; subset with MODEL origin |
| `approvals`, `human_rejections` | Human APPROVED / REJECTED decisions |
| `accepted_executions` | ACCEPTED attempts |
| `rejected_executions`, `rejected_by_reason` | REJECTED attempts, grouped by exact machine reason |
| `stale_evidence_rejections` | StaleEvidence rejections |
| `policy_related_rejections` | NoActivePolicy, InvalidActivePolicy, DisallowedAction, DisallowedSymbol, OrderNotionalExceeded, ResultingPositionExceeded, ProposalPolicyAgeExceeded, PriceDriftExceeded |
| `rejection_rate_denominator` | Total rejected attempts; stale/policy rejection counts can be divided by this when nonzero |
| `submission_replays` | Successful repeat submits with the same key and bound identity; conflicts are not counted |
| `execution_replays` | Calls to execute that return an already committed terminal result; passive result/audit reads do not count |
| `duplicate_proposal_suppressions` | AlreadyExecuted rejections under another request |
| `worker_claims`, `worker_retries_reclaims` | All scheduling claims; claims replacing an existing lease |
| `compensations_requested`, `compensations_accepted` | Linked operator requests; accepted executions of their proposals |
| `approval_to_execution_latency` | Samples/min/max/mean seconds from approval creation to accepted attempt finish/evaluation timestamp |

Latency measures approval-to-accepted-decision, not HTTP response time or PostgreSQL commit
acknowledgement latency. Rejected attempts and replay events do not add latency samples. Empty
latency sets yield zero samples and null aggregates. Monetary values and latency decimals are
serialized as decimal strings. Counts are cumulative durable history, not process-local counters.

`control_events` and `compensation_requests` reject update/delete/truncate, including owner DML.
The runtime role cannot insert control events directly: claim accounting is a database trigger;
replay accounting uses a narrowly granted function validating event kind/result existence.
As in earlier phases, trusted application code reports the operation and deployment owners
remain capable of DDL; this is not a production IAM boundary against a malicious superuser.

`AuditStore.reconstruct(attempt_id)` reads proposal, human approval and evidence, grounding,
execution evidence, active historical policy, request/key, immutable consequence snapshots,
attempt, ledger and direct compensation relationships. Missing approval/evidence/policy in a
rejected decision remains null. Audit reconstruction and metrics never read application logs.

## Read-only API and architecture decision

**ADR-05: v1 HTTP exposes audit reads only.** The earlier architecture contract described
future HTTP mutation operations. The explicit Phase 5 scope replaces that transport plan;
mutation capabilities remain trusted Python application boundaries. No acceptance criterion
is removed. Production authentication, login provisioning and public deployment are outside v1.

FastAPI routes are `GET /health` (liveness), `GET /ready` (database plus exact migration history),
`GET /metrics`, and `GET /attempts/{attempt_id}`. Unknown attempts return 404; malformed UUIDs
return 422; unready databases return a sanitized 503. No HTTP approval, execution, proposal,
compensation or policy mutation route exists. There is no fake authentication or frontend.

Each audit connection sets `monster_heavy_audit` and a read-only transaction. This group role
has SELECT permissions only. The local Compose environment uses its development owner login
then explicitly reduces the audit transaction role; production credential provisioning is
outside scope. Local API/PostgreSQL host ports bind to loopback. Uvicorn is the only newly added
production runtime; FastAPI and Uvicorn bring only their required transitive dependencies.

Compose starts PostgreSQL, completes migrations, then starts the API with a readiness health
check. CI validates Compose, builds the locked image, executes validation, starts the API and
checks health/readiness/metrics with HTTP. This host has no Docker/Compose runtime, so a local
Compose startup cannot yet be recorded as passing.

## Migration

`0002_recovery_release.sql` adds the origin column, two history tables, accepted-original FK,
compensation integrity triggers, transactional claim accounting, narrow replay function, and
read-only audit role. `0001_foundation.sql` is unchanged. The migration is forward-only,
transactional and checksum-verified through the existing runner. New tests exercise fresh
installation, populated foundation upgrade, unchanged original records and migration metadata,
idempotent migration, and rejection of missing/unknown/modified history.

## Release validation status

The complete acceptance mapping is in [ACCEPTANCE_CRITERIA.md](ACCEPTANCE_CRITERIA.md).
Thomas's implementation review is complete. The approved migration tests compare exact
migration names/checksums and restore tampered checksums by migration name. No execution
or governance assertion was weakened. Complete local validation passes.

Docker-backed image build/startup and final GitHub Actions evidence remain pending.
The live Uvicorn test does not substitute for those release gates.

Intentionally outside v1: production authentication/SSO, real broker/real-money integration,
real market-data acquisition, frontend, outbox delivery infrastructure, additional AI agents,
and all infrastructure excluded by the architecture contract. None of AC-01–30 is removed.

### Final local validation (2026-09-25)

Environment: Python 3.13.14, PostgreSQL 17.11, pytest 9.1.1, using a disposable local
PostgreSQL database. Thomas approved the test-formatting correction after the first
release-validation attempt stopped at Ruff E501 before executing tests.

| Validation | Actual result |
| --- | --- |
| `sh scripts/validate.sh` after the approved formatting correction | **373 passed in 13.63s**; compilation, Ruff lint/format, migration application/check and whitespace checks passed |
| `pytest --collect-only -q` | **373 cases: 175 unit and 198 integration**, collected in 0.24s |
| `uv lock --check` | PASS |
| `git diff --check` | PASS |
| Compose configuration, `--profile validation config --quiet`, using the Phase 5 standalone Compose command | PASS |
| Docker-backed image build and PostgreSQL/migration/API startup | PENDING evidence |
| Final GitHub Actions run | PENDING evidence |

The suite adds 76 Phase 5 cases (14 unit, 62 integration) to the original 297.
Run times are observations of these local runs, not performance promises.

### Files for review

Phase 5 runtime additions:

- `src/monster_heavy/persistence/migrations/0002_recovery_release.sql`
- `src/monster_heavy/persistence/worker.py`, `src/monster_heavy/worker.py`
- `src/monster_heavy/persistence/compensation.py`
- `src/monster_heavy/persistence/audit.py`, `src/monster_heavy/api.py`
- `src/monster_heavy/persistence/models.py` adds the proposal origin field.

New tests:

- `tests/integration/test_recovery_release.py`
- `tests/integration/test_release_controls.py`
- `tests/integration/test_phase5_migrations.py`
- `tests/integration/test_audit_api.py`
- `tests/unit/test_worker.py`
- `tests/unit/test_release_scope.py`

Delivery/documentation changes: `compose.yaml`, `.github/workflows/validate.yml`,
`pyproject.toml`, `uv.lock`, `README.md`, `docs/ARCHITECTURE_CONTRACT.md`,
`docs/ACCEPTANCE_CRITERIA.md`, this release record. The approved two-test migration-history update is in
`tests/integration/test_transactions.py`. The original migration and
`scripts/validate.sh` are unchanged.
