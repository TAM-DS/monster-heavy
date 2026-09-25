# Monster Heavy

Governed AI proposals and deterministic paper execution with PostgreSQL recovery and audit.

> AI proposes → human authorizes → system verifies current evidence and current policy → execute or reject → preserve immutable evidence.

Monster Heavy contains **one AI agent: the Proposal Agent**. It reasons over trusted grounding
and produces a constrained recommendation that can become a pending proposal. Human approval,
policy checks, execution, worker scheduling, compensation and audit are deterministic application
code. They are not additional AI agents.

Phase 5 has completed implementation review and is in final release validation.
Complete local validation passes. Docker-backed startup (AC-27) and final GitHub Actions
evidence (AC-28) remain pending. See the [release evidence](docs/PHASE5_RELEASE.md) and
[AC-01–AC-30 test mapping](docs/ACCEPTANCE_CRITERIA.md#phase-5-acceptance-evidence).

## Implemented behavior

- Immutable proposal terms and human decisions preserve what was proposed and authorized.
- `ExecutionStore.execute()` locks request, proposal and portfolio state, checks current policy
  and fresh execution evidence, then atomically commits either a paper consequence with evidence
  or a durable rejection. Money uses exact decimal arithmetic.
- PostgreSQL request leases coordinate workers. Expired leases can be reclaimed; terminal work
  is excluded. A stale worker still enters the same authoritative execution boundary. Process
  death before commit leaves no consequence; replay after commit returns the durable result.
- Compensation is a **new governed action**, linked to an accepted attempt. An operator requests
  the opposite side with the same portfolio, symbol and quantity and fresh grounding. It needs
  a new human approval and current execution validation. It never rolls back or erases history,
  and changing prices mean it need not restore the original cash balance.
- Metrics and decision reconstruction read PostgreSQL durable state. Logs are optional debugging
  output. Immutable claim/replay events retain retry and duplicate-delivery measurements.
- FastAPI exposes only `GET /health`, `/ready`, `/metrics`, and `/attempts/{attempt_id}`.
  Audit transactions use a read-only PostgreSQL role. There are no HTTP mutation endpoints,
  fake authentication or frontend. Production authentication remains outside v1 scope.

Worker processes are deterministic runners, not AI agents. Trusted composition supplies the
paper observation provider to `Worker.run_once()`. Compose starts the audit environment; it does
not generate trading intent, invent market observations, or start an autonomous trading loop.

Each behavior is exercised by the [acceptance evidence](docs/ACCEPTANCE_CRITERIA.md).
Worker-death tests kill real spawned processes around the PostgreSQL commit boundary; concurrency
tests use independent connections. These tests establish the specified failure cases, not an
availability, latency, or throughput SLA. Metric definitions, timestamp semantics and provenance
are documented in [Phase 5](docs/PHASE5_RELEASE.md).

## Local environment

Stack: Python 3.13, PostgreSQL 17, OpenAI SDK, FastAPI, Uvicorn, pytest, Docker Compose and
GitHub Actions. Requires Docker with Docker Compose. Configure a local credential once:

```sh
cp .env.example .env
# Edit .env and choose POSTGRES_PASSWORD.
docker compose up --build -d --wait
```

The configuration starts PostgreSQL, completes checksum-verified migrations, then starts the
read-only API at `http://127.0.0.1:8000`. PostgreSQL and API host ports bind to loopback.
`GET /health` checks liveness; `GET /ready` checks database access and exact migration history.
Inspect `docker compose ps -a` and `docker compose logs migrate api` for startup results.
`docker compose down` preserves the database volume.

The Compose configuration has been validated; its actual container startup remains an explicit
release gate on a Docker-enabled host. Direct PostgreSQL and live Uvicorn tests are recorded
separately in the release evidence.

Run validation against a **disposable development database**:

```sh
docker compose --profile validation run --build --rm validation
```

Tests insert durable fixture history and create isolated migration databases. Do not point them
at a production database. PostgreSQL tests never substitute SQLite or silently skip the database.

For a host Python environment with uv and PostgreSQL installed:

```sh
uv sync --frozen --extra dev
. .venv/bin/activate
export DATABASE_URL='postgresql://OWNER:PASSWORD@localhost:5432/monster_heavy'
export TEST_DATABASE_URL="$DATABASE_URL"
sh scripts/validate.sh
uv lock --check
git diff --check
```

Validation compiles sources, checks Ruff lint/format, applies/verifies migrations and runs unit,
PostgreSQL integration, concurrency, process-death, compensation, metrics and API tests. CI uses
the locked Docker image and verifies Compose configuration and API health. Dependency versions
are recorded in `uv.lock`; the image verifies lockfile consistency before frozen installation.
The [release record](docs/PHASE5_RELEASE.md) distinguishes checks actually run from pending gates.

## Scope and contracts

**Paper trading only. No broker integration, broker credentials or real-money path.** No real
market-data feed or live model call is needed for validation. The OpenAI adapter is tested with
mocked HTTP responses. No MCP, LangGraph, Redis, Kafka, Temporal, Kubernetes, message broker or
additional AI agent is part of v1. Outbox records are durable; external event delivery and
production identity provisioning remain outside scope.

- [Architecture contract](docs/ARCHITECTURE_CONTRACT.md)
- [Acceptance criteria and evidence](docs/ACCEPTANCE_CRITERIA.md)
- [Phase 1 persistence](docs/PERSISTENCE_CONTRACT.md)
- [Phase 2 application boundaries](docs/PHASE2_BOUNDARIES.md)
- [Phase 3 Proposal Agent](docs/PHASE3_BOUNDARIES.md)
- [Phase 4 authoritative execution](docs/PHASE4_BOUNDARIES.md)
- [Phase 5 recovery, compensation and audit](docs/PHASE5_RELEASE.md)

Earlier phase documents retain their historical validation records and deferrals; Phase 5
supersedes their descriptions of missing workers, compensation, metrics and read-only HTTP audit.
