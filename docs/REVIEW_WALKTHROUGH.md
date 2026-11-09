# Monster Heavy: a ten-minute case review

## Decision and business context

A team wants AI assistance in a consequential workflow. A recommendation can be reasonable
when created and unsafe when executed: evidence ages, policy changes, portfolio capacity
changes, and workers retry after losing contact with the database.

The design decision is to keep the model outside the authority to change state. Paper trading
provides concrete cash, position, approval, and reversal constraints without a real-money path.

**Interview opening:** "I built a governed execution boundary for AI proposals. Human approval
is necessary, but execution still rechecks current evidence and policy. I tested the failure
cases around database commit so a retry cannot create a second accepted paper consequence."

This is a reference implementation. No customer ROI, live brokerage execution, production
identity integration, or enterprise deployment is demonstrated.

## Review sequence

| Time | Show | Explain |
| --- | --- | --- |
| 0–2 minutes | [Proposal boundary](../tests/integration/test_proposal_agent_boundary.py) | The model creates a pending proposal, with no approval or portfolio effect |
| 2–4 minutes | [Authoritative executor](../src/monster_heavy/persistence/execution.py) | Locks and fresh database time protect the decision after any lock wait |
| 4–6 minutes | [Recovery tests](../tests/integration/test_recovery_release.py) | Real spawned processes die before and after commit; the assertions distinguish rollback from replay |
| 6–8 minutes | Compensation test in the same file | A reversal needs a new proposal and approval; history survives and cash may differ |
| 8–10 minutes | [Audit API](../src/monster_heavy/api.py) and [release evidence](PHASE5_RELEASE.md) | Durable evidence supports review; availability, IAM, and operational limits remain explicit |

## Reproduce the focused failure cases

Use a disposable local environment. These tests write synthetic durable history; they are
not a read-only production diagnostic. From a checkout with Docker Compose:

```bash
cp .env.example .env
# Set POSTGRES_PASSWORD in .env.
docker compose up --build -d --wait
docker compose --profile validation run --build --rm validation \
  pytest -q tests/integration/test_recovery_release.py \
  tests/integration/test_audit_api.py tests/unit/test_api_failures.py
```

The focused run exercises process death, stale-worker races, compensation, reconstruction,
read-only access, and API failures. It is not the complete release gate. Run the full gate with:

```bash
docker compose --profile validation run --build --rm validation
```

The full gate compiles, lints, verifies migrations, and runs all unit and PostgreSQL tests.
A failed check stops validation; PostgreSQL tests do not silently substitute SQLite.
Stop containers with `docker compose down`; this preserves the named database volume.

## Architecture choices and trade-offs

| Choice | Reason | Cost or boundary |
| --- | --- | --- |
| One PostgreSQL transaction for consequence and evidence | A database failure cannot leave a committed paper effect without its corresponding record | Contention on shared portfolios; no throughput SLA has been established |
| Database leases for worker scheduling | Expired work can be reclaimed without a separate broker | No lease renewal or automatic always-on worker service |
| Locks and uniqueness remain authoritative | A stale worker cannot gain authority merely by holding an old lease | This is at-most-once accepted consequence inside this database, not end-to-end exactly-once delivery |
| A new compensating proposal | Reverse direction while retaining the original history and approval boundary | Compensation can be rejected and need not restore the original cash amount |
| Read-only HTTP surface | Reviewers can inspect evidence without a web mutation route | No public authenticated approval UI; production identity provisioning remains future work |
| One Proposal Agent | Keep probabilistic recommendations separate from deterministic authorization and execution | Trusted composition must supply grounding and execution observations |

The outbox is durable storage only. External delivery is not implemented. A future broker or
external tool would need its own delivery and reconciliation contract; this project's
database atomicity must not be presented as a guarantee for those external effects.

## Audit availability

`GET /health` is process liveness; it can return 200 when PostgreSQL is unavailable.
`GET /ready` checks database access and exact migration history.

Missing database configuration and PostgreSQL errors on readiness, metrics, or attempt reads
return HTTP 503 with `{"detail": "Database not ready"}`. Connection errors and query
cancellation do not become empty metrics or a false "attempt not found." Error details are
not included in the HTTP body. Programming errors are not broadly converted to outages.

A valid but absent attempt returns 404; malformed UUIDs return 422. These meanings must
remain distinct from unavailable evidence. Readiness is a separate endpoint, not a migration
check performed before every audit read.

The audit query timeout is five seconds per statement; it is not an end-to-end request
deadline or a connection-timeout guarantee. There is no automatic retry in the API.

## What the evidence establishes

- Historical Phase 5 local validation: 373 cases passed, with environment and timing recorded
  in [PHASE5_RELEASE.md](PHASE5_RELEASE.md#final-local-validation-2026-09-25).
- Process-death tests use real PostgreSQL sessions and killed spawned processes.
- API tests verify read-only routes, role restrictions, exact decimal serialization, and
  missing versus unavailable evidence.
- The current [CI workflow](../.github/workflows/validate.yml) builds the locked image,
  runs validation, and checks HTTP health/readiness/metrics.

Use the CI result for the commit being discussed. Do not treat historical counts as a fresh
run, or mocked model responses as a measured live-model evaluation.

## Before a production engagement

The next decision would depend on the customer's workflow, identity system, failure budget,
and external-effect semantics. Required work includes authenticated identities and roles,
least-privilege deployment credentials, secrets management, backup/restore verification,
load and timeout budgets, operational monitoring, and any external delivery/reconciliation
design. These are deployment requirements, not completed capabilities.

Database triggers protect history from ordinary DML, including the owner's tested DML paths.
A database owner or superuser capable of DDL is still trusted. This is not an independently
tamper-proof archive or a substitute for production IAM.
