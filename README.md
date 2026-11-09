# Monster Heavy

A reference implementation of a governed AI trust boundary — and proof that the boundary survives concurrency, retries, stale evidence, policy changes, worker failure, and compensation, not just the happy path.
Most "AI agent" demos stop at "the model calls a tool." That's not the hard part for a regulated or consequential system. The hard part is: what happens when the worker executing the AI's proposal dies mid-transaction? When two requests race for the same portfolio? When the policy changes between proposal and execution? When you need to reverse an action without erasing the record of what happened?
Monster Heavy answers those questions with running code, not a diagram on a slide.
> AI proposes → human authorizes → system verifies current evidence and current policy → execute or reject → preserve immutable evidence.
Monster Heavy contains one AI agent: the Proposal Agent. It reasons over trusted grounding and produces a constrained recommendation that can become a pending proposal. Human approval, policy checks, execution, worker scheduling, compensation, and audit are deterministic application code — not additional AI agents, and not left to the model's discretion.
Phase 5 has completed implementation review and release validation. Complete local validation and Docker-backed GitHub Actions Validate run #14 passed, satisfying AC-01 through AC-30. See the [release evidence](docs/PHASE5_RELEASE.md) and [AC-01–AC-30 test mapping](docs/ACCEPTANCE_CRITERIA.md).
## The business decision

How can a team introduce AI-generated recommendations into a consequential workflow while keeping authorization, execution, and recovery independently enforceable?

Monster Heavy uses paper trading as the test domain. The Proposal Agent can recommend terms; it cannot approve or execute them. The deterministic executor rechecks current policy, evidence, and portfolio capacity at the point of consequence. PostgreSQL commits the paper effect and its audit evidence together.

The intended business benefit is fewer duplicate consequences and a reconstructable explanation of both accepted and refused actions. This repository demonstrates those controls in specified tests; it does not measure customer savings, investment returns, or production loss reduction.

## Start here

| Audience | Review path |
| --- | --- |
| Business | [Ten-minute case walkthrough](docs/REVIEW_WALKTHROUGH.md): problem, decision, trade-offs, and limitations |
| Engineer | [Execution boundary](src/monster_heavy/persistence/execution.py), [worker recovery tests](tests/integration/test_recovery_release.py), and [acceptance mapping](docs/ACCEPTANCE_CRITERIA.md#phase-5-acceptance-evidence) |
| Operator | [Local environment](#local-environment), [audit failure behavior](docs/REVIEW_WALKTHROUGH.md#audit-availability), and [CI workflow](.github/workflows/validate.yml) |

**Recorded baseline:** 373 tests passed in the September 25 Phase 5 local validation; Docker-backed Validate run #14 passed for commit `74eb0544c444087545643eccf98a994a6883d8c9`. These are historical measurements, not a claim that every later commit has the same result. Consult [release evidence](docs/PHASE5_RELEASE.md#final-local-validation-2026-09-25) and the current [Actions runs](https://github.com/TAM-DS/monster-heavy/actions/workflows/validate.yml).

## Controls and evidence

| Failure or decision | Enforced behavior | Evidence |
| --- | --- | --- |
| Model output attempts to become action | Only a pending proposal is persisted | [Proposal boundary tests](tests/integration/test_proposal_agent_boundary.py) |
| Approval becomes stale | Current policy and fresh execution evidence can still reject it | [Execution tests](tests/integration/test_execution_core.py) |
| Workers race or retry | Locks, durable replay, and uniqueness prevent a second accepted paper consequence | [Execution tests](tests/integration/test_execution_core.py), [recovery tests](tests/integration/test_recovery_release.py) |
| Worker dies around commit | Before commit: no effect survives. After commit: retry returns the existing result | [Process-death tests](tests/integration/test_recovery_release.py) |
| An accepted action needs reversing | A new compensating proposal requires new approval and current controls | [Compensation tests](tests/integration/test_recovery_release.py) |
| Audit database is unavailable | Audit reads return a sanitized 503, not a successful empty history | [API failure tests](tests/unit/test_api_failures.py), [database-backed API tests](tests/integration/test_audit_api.py) |

## Companion projects

Monster Heavy is the second half of a two-repo proof. [Monster Light](https://github.com/TAM-DS/monster-light) establishes the trust boundary itself — the minimal proposal → approve → execute contract. Monster Heavy takes that same boundary and proves it holds up under concurrency, retries, stale evidence, policy drift, worker failure, and compensation.
[Monster Desk](https://github.com/TAM-DS/monster-desk) is a separate four-seat paper console illustrating the same authority pattern: Research proposes, Risk binds the ticket digest, Execution may submit only that digest, Surveillance records a halt. Desk is not wired to Heavy's PostgreSQL execution engine. Heavy remains the durable engine and has no frontend on purpose. A simulated fill in Desk is not a street fill.
## How a proposal moves through the system

```mermaid
flowchart TD
    A["Proposal Agent"] --> B[Pending Proposal — immutable terms]
    B --> C{Human Approval}
    C -- rejected --> R1[Durable Rejection — recorded, no consequence]
    C -- approved --> D["ExecutionStore.execute — locks request + proposal + portfolio"]
    D --> E{"Current policy — + fresh evidence?"}
    E -- stale / policy fail --> R2[Durable Rejection — recorded, no consequence]
    E -- valid --> F["Atomic Commit — paper consequence + evidence — exact decimal arithmetic"]
    F --> G[(PostgreSQL — durable state)]
    G --> H[Audit / Metrics — read-only role]
```
## What happens when things fail

```mermaid
sequenceDiagram
    participant W1 as Worker A
    participant W2 as Worker B (retry)
    participant DB as PostgreSQL (lease + execution boundary)

    W1->>DB: Acquire request lease
    DB-->>W1: Lease granted
    W1->>DB: Begin execute() — lock request/proposal/portfolio
    Note over W1: Process dies before commit
    DB-->>DB: Lease expires (terminal work excluded)
    W2->>DB: Reclaim expired lease
    W2->>DB: Re-enter same authoritative execution boundary
    DB-->>W2: Atomic commit or durable rejection
    Note over DB: Replay after a prior commit returns the same durable result — no double execution
```
Compensation is not a rollback. It's a new governed action, linked to the original accepted attempt: an operator requests the opposite side with the same portfolio, symbol, and quantity, using fresh grounding. It requires a new human approval and current execution validation, and it never erases or restores prior history — including the original cash balance, if prices have moved.
## Implemented behavior

Immutable proposal terms and human decisions preserve what was proposed and authorized.
`ExecutionStore.execute()` locks request, proposal, and portfolio state, checks current policy and fresh execution evidence, then atomically commits either a paper consequence with evidence or a durable rejection. Money uses exact decimal arithmetic.
PostgreSQL request leases coordinate workers. Expired leases can be reclaimed; terminal work is excluded. A stale worker still enters the same authoritative execution boundary. Process death before commit leaves no consequence; replay after commit returns the durable result.
Compensation is a new governed action, linked to an accepted attempt — never a rollback.
Metrics and decision reconstruction read PostgreSQL durable state. Logs are optional debugging output. Immutable claim/replay events retain retry and duplicate-delivery measurements.
FastAPI exposes only `GET /health`, `/ready`, `/metrics`, and `/attempts/{attempt_id}`. Audit transactions use a read-only PostgreSQL role. There are no HTTP mutation endpoints, fake authentication, or frontend. Production authentication remains outside v1 scope.
Worker processes are deterministic runners, not AI agents. Trusted composition supplies the paper observation provider to `Worker.run_once()`. Compose starts the audit environment; it does not generate trading intent, invent market observations, or start an autonomous trading loop.
Each behavior is exercised by the acceptance evidence. Worker-death tests kill real spawned processes around the PostgreSQL commit boundary; concurrency tests use independent connections. These tests establish the specified failure cases, not an availability, latency, or throughput SLA. Metric definitions, timestamp semantics, and provenance are documented in Phase 5.
## Local environment

Stack: Python 3.13, PostgreSQL 17, OpenAI SDK, FastAPI, Uvicorn, pytest, Docker Compose, and GitHub Actions. Requires Docker with Docker Compose.
```bash
cp .env.example .env
# Edit .env and choose POSTGRES_PASSWORD.
docker compose up --build -d --wait
```
This starts PostgreSQL, completes checksum-verified migrations, then starts the read-only API at `http://127.0.0.1:8000`. Ports bind to loopback. `GET /health` checks liveness; `GET /ready` checks database access and exact migration history. Inspect `docker compose ps -a` and `docker compose logs migrate api` for startup results. `docker compose down` preserves the database volume.
Compose configuration, Docker image build, PostgreSQL/migration startup, read-only API startup, and HTTP health/readiness/metrics passed in GitHub Actions Validate run #14. The release evidence also records direct PostgreSQL and live Uvicorn tests.
Run validation against a disposable development database:
```bash
docker compose --profile validation run --build --rm validation
```
Tests insert durable fixture history and create isolated migration databases. Do not point them at a production database. PostgreSQL tests never substitute SQLite or silently skip the database.
For a host Python environment with `uv` and PostgreSQL installed:
```bash
uv sync --frozen --extra dev
. .venv/bin/activate
export DATABASE_URL='postgresql://OWNER:PASSWORD@localhost:5432/monster_heavy'
export TEST_DATABASE_URL="$DATABASE_URL"
sh scripts/validate.sh
uv lock --check
git diff --check
```
Validation compiles sources, checks Ruff lint/format, applies/verifies migrations, and runs unit, PostgreSQL integration, concurrency, process-death, compensation, metrics, and API tests. CI uses the locked Docker image and verifies Compose configuration and API health. Dependency versions are recorded in `uv.lock`; the image verifies lockfile consistency before frozen installation. The release record records successful local and GitHub Actions validation.
## Scope and contracts

Paper trading only. No broker integration, broker credentials, or real-money path. No real market-data feed or live model call is needed for validation. The OpenAI adapter is tested with mocked HTTP responses. No MCP, LangGraph, Redis, Kafka, Temporal, Kubernetes, message broker, or additional AI agent is part of v1. Outbox records are durable; external event delivery and production identity provisioning remain outside scope.
- [Architecture contract](docs/ARCHITECTURE_CONTRACT.md)
- [Acceptance criteria and evidence](docs/ACCEPTANCE_CRITERIA.md)
- [Phase 1 persistence](docs/PERSISTENCE_CONTRACT.md)
- [Phase 2 application boundaries](docs/PHASE2_BOUNDARIES.md)
- [Phase 3 Proposal Agent](docs/PHASE3_BOUNDARIES.md)
- [Phase 4 authoritative execution](docs/PHASE4_BOUNDARIES.md)
- [Phase 5 recovery, compensation, and audit](docs/PHASE5_RELEASE.md)
Earlier phase documents retain their historical validation records and deferrals; Phase 5 supersedes their descriptions of missing workers, compensation, metrics, and read-only HTTP audit.
