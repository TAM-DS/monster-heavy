# Monster Heavy

### Governed AI execution under enterprise failure conditions

> **AI proposes → human authorizes → system verifies current evidence and current policy → execute or reject → preserve immutable evidence.**

Monster Heavy is the production-oriented successor to Monster Light.

Monster Light proved that a consequential AI workflow can keep recommendation, human authorization, deterministic validation, execution, and audit as separate responsibilities in a small inspectable system.

Monster Heavy asks the harder question:

> **Does that trust model still hold when execution becomes concurrent, retryable, policy-governed, distributed across workers, and exposed to partial failure?**

## Status

**Architecture locked. Phase 4 authoritative deterministic paper execution implemented.**

The architecture contract and acceptance criteria remain the source of truth. Phase 2 adds validated, immutable proposals, human decisions, typed evidence, lifecycle checks, and versioned policy publication/resolution on the Phase 1 PostgreSQL foundation. Phase 3 adds the sole AI agent: constrained model output becomes only a pending proposal through deterministic validation and ProposalService. Phase 4 adds durable execution requests, current-policy/current-evidence revalidation, atomic portfolio consequences, durable refusals, and committed-result replay. This is not the complete release.

See [Phase 2 boundaries and acceptance evidence](docs/PHASE2_BOUNDARIES.md) for the service contracts, design decisions, tests, and deferred execution behavior.

See [Phase 3 authority boundary and acceptance evidence](docs/PHASE3_BOUNDARIES.md) for model constraints, trusted field binding, provenance, and validation results.

See [Phase 4 execution boundaries and acceptance evidence](docs/PHASE4_BOUNDARIES.md) for transaction ordering, idempotency, exact portfolio rules, concurrency, and rollback tests.

- [Architecture Contract](docs/ARCHITECTURE_CONTRACT.md)
- [Acceptance Criteria](docs/ACCEPTANCE_CRITERIA.md)

## Product thesis

Monster Heavy is not a more complicated trading bot.

It is a governed AI decision-and-execution platform demonstrated through a paper-trading domain because the domain makes authority, evidence, portfolio state, price drift, retries, and consequences easy to inspect.

The system must remain trustworthy when:

- multiple workers race the same approved proposal;
- requests are retried after timeouts;
- market evidence becomes stale or changes;
- policy changes after human approval;
- portfolio state changes between approval and execution;
- a worker dies before or after a commit;
- the same command is delivered more than once;
- a previously accepted consequence requires an explicit compensating action.

## Planned stack

- Python 3.13
- FastAPI
- PostgreSQL
- OpenAI SDK
- pytest
- Docker Compose
- GitHub Actions

The system will deliberately avoid infrastructure that does not earn its place. No Kafka, Kubernetes, Redis, Temporal, LangGraph, MCP, vector database, or additional cloud platform is part of the locked scope.

## Non-negotiable invariant

**Neither the model nor the human approver can bypass current reality.**

A proposal is not authority.<br>
Approval is not execution.<br>
A retry is not a new consequence.<br>
A successful response is not the source of truth.<br>
The durable decision record is.

## Scope

Paper trading only. No broker integration. No real money.

The portfolio project is about enterprise AI governance, reliable execution, failure recovery, and evidence—not market prediction.

## Local validation environment

Requires Docker with Docker Compose. Set up the local credential once:

```sh
cp .env.example .env
# Edit .env and choose POSTGRES_PASSWORD.
docker compose up --build -d
```

This starts PostgreSQL 17 with persistent storage and runs the migration container to
completion. There is no API or worker service. Check migration completion
with `docker compose logs migrate` and service health with `docker compose ps -a`.
`docker compose down` preserves the database volume.

Run the full validation suite against this **disposable development database**:

```sh
docker compose --profile validation run --build --rm validation
```

Tests insert fixture history, including committed concurrency cases. Do not point them
at a production database. They never silently substitute SQLite or skip PostgreSQL tests.

For a host Python 3.13 environment with uv and PostgreSQL already installed:

```sh
uv sync --frozen --extra dev
. .venv/bin/activate
export DATABASE_URL='postgresql://OWNER:PASSWORD@localhost:5432/monster_heavy'
export TEST_DATABASE_URL="$DATABASE_URL"
sh scripts/validate.sh
```

`uv.lock` records the resolved Python dependencies. The `dev` extra includes pytest
and Ruff. Both host setup and the Docker image install with `uv sync --frozen --extra dev`.
The image uses uv 0.12.4 and runs `uv lock --check` before installation to reject a
lockfile that is out of date with `pyproject.toml`. Run that check locally with
`uv lock --check`; use `uv lock` only when intentionally updating the lockfile.

Validation compiles sources, checks lint/formatting, applies and verifies migrations,
runs unit and PostgreSQL integration tests (including independent-connection races),
and runs `git diff --check` when Git metadata is available. GitHub Actions builds the
locked images, starts the declared Compose PostgreSQL service, waits for its health
check, and runs the same suite through the Compose validation service. Its migration
dependency must complete successfully first. CI checks committed whitespace separately,
prints Compose logs, and always runs cleanup of containers, the network, and the database
volume, including after failed validation. Unit tests alone can run with `pytest tests/unit`.

See [Phase 1 persistence contract](docs/PERSISTENCE_CONTRACT.md) for the schema, runtime
role, exact database guarantees, test-to-criterion mapping, and deferred behavior.
The architecture contract remains unchanged. Phase-specific acceptance evidence is linked above. The OpenAI adapter is injectable; validation uses mocked HTTP responses and makes no live
OpenAI calls. No business endpoints, workers, market data, compensation, or real-money
execution path is present.
