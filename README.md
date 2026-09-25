Monster Heavy
A reference implementation of a governed AI trust boundary, and proof that the boundary survives concurrency, retries, stale evidence, policy changes, worker failure, and compensation, not just the happy path.
Most "AI agent" demos stop at "the model calls a tool." That's not the hard part for a regulated or consequential system. The hard part is: what happens when the worker executing the AI's proposal dies mid-transaction? When two requests race for the same portfolio? When the policy changes between proposal and execution? When you need to reverse an action without erasing the record of what happened?
Monster Heavy answers those questions with running code, not a diagram on a slide.
> AI proposes → human authorizes → system verifies current evidence and current policy → execute or reject → preserve immutable evidence.
Monster Heavy contains one AI agent: the Proposal Agent. It reasons over trusted grounding and produces a constrained recommendation that can become a pending proposal. Human approval, policy checks, execution, worker scheduling, compensation, and audit are deterministic application code — not additional AI agents, and not left to the model's discretion.
Phase 5 has completed implementation review and release validation. Complete local validation and Docker-backed GitHub Actions Validate run #14 pass, satisfying AC-01 through AC-30. See the release evidence and AC-01–AC-30 test mapping.
Companion project
Monster Heavy is the second half of a two-repo proof. Monster Light establishes the trust boundary itself — the minimal proposal → approve → execute contract. Monster Heavy takes that same boundary and proves it holds up under concurrency, retries, stale evidence, policy drift, worker failure, and compensation. (Link the Monster Light repo here.)
How a proposal moves through the system
```mermaid
flowchart LR
    A[Proposal Agent<br/>reasons over trusted grounding] --> B[Pending Proposal<br/>immutable terms]
    B --> C{Human Approval}
    C -- rejected --> R1[Durable Rejection<br/>recorded, no consequence]
    C -- approved --> D["ExecutionStore.execute<br/>locks request + proposal + portfolio"]
    D --> E{"Current policy<br/>+ fresh evidence?"}
    E -- stale / policy fail --> R2[Durable Rejection<br/>recorded, no consequence]
    E -- valid --> F["Atomic Commit<br/>paper consequence + evidence<br/>exact decimal arithmetic"]
    F --> G[(PostgreSQL<br/>durable state)]
    G --> H[Audit / Metrics<br/>read-only role]
```
What happens when things fail
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
Implemented behavior
Immutable proposal terms and human decisions preserve what was proposed and authorized.
`ExecutionStore.execute()` locks request, proposal, and portfolio state, checks current policy and fresh execution evidence, then atomically commits either a paper consequence with evidence or a durable rejection. Money uses exact decimal arithmetic.
PostgreSQL request leases coordinate workers. Expired leases can be reclaimed; terminal work is excluded. A stale worker still enters the same authoritative execution boundary. Process death before commit leaves no consequence; replay after commit returns the durable result.
Compensation is a new governed action, linked to an accepted attempt — never a rollback.
Metrics and decision reconstruction read PostgreSQL durable state. Logs are optional debugging output. Immutable claim/replay events retain retry and duplicate-delivery measurements.
FastAPI exposes only `GET /health`, `/ready`, `/metrics`, and `/attempts/{attempt_id}`. Audit transactions use a read-only PostgreSQL role. There are no HTTP mutation endpoints, fake authentication, or frontend. Production authentication remains outside v1 scope.
Worker processes are deterministic runners, not AI agents. Trusted composition supplies the paper observation provider to `Worker.run_once()`. Compose starts the audit environment; it does not generate trading intent, invent market observations, or start an autonomous trading loop.
Each behavior is exercised by the acceptance evidence. Worker-death tests kill real spawned processes around the PostgreSQL commit boundary; concurrency tests use independent connections. These tests establish the specified failure cases, not an availability, latency, or throughput SLA. Metric definitions, timestamp semantics, and provenance are documented in Phase 5.
Local environment
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
Scope and contracts
Paper trading only. No broker integration, broker credentials, or real-money path. No real market-data feed or live model call is needed for validation. The OpenAI adapter is tested with mocked HTTP responses. No MCP, LangGraph, Redis, Kafka, Temporal, Kubernetes, message broker, or additional AI agent is part of v1. Outbox records are durable; external event delivery and production identity provisioning remain outside scope.
Architecture contract
Acceptance criteria and evidence
Phase 1 persistence
Phase 2 application boundaries
Phase 3 Proposal Agent
Phase 4 authoritative execution
Phase 5 recovery, compensation, and audit
Earlier phase documents retain their historical validation records and deferrals; Phase 5 supersedes their descriptions of missing workers, compensation, metrics, and read-only HTTP audit.
