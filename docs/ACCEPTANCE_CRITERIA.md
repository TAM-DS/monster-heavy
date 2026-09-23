# Monster Heavy Acceptance Criteria

These criteria are the definition of done for the first complete Monster Heavy release.

A feature is not complete because a demo looks correct. It is complete when an automated test or reproducible failure scenario proves the required property.

## Current implementation evidence

[Phase 4 evidence](PHASE4_BOUNDARIES.md#acceptance-evidence) maps the deterministic execution
core to these criteria. It strengthens or completes core execution checks without claiming
worker leasing/death recovery, compensation, metrics, or the full release gate. Prior phase
evidence remains in the Phase 2/3 and persistence documents.

## A. Authority boundaries

### AC-01 — Model output cannot execute

**Given** trusted grounding evidence<br>
**When** the model produces a valid structured recommendation<br>
**Then** only a `PENDING` proposal and its provenance are persisted<br>
**And** portfolio cash and positions are unchanged<br>
**And** no approval or accepted execution record exists.

### AC-02 — Human approval does not mutate the portfolio

**Given** a `PENDING` proposal<br>
**When** a human approves the immutable terms<br>
**Then** an approval record exists and the proposal becomes `APPROVED`<br>
**And** no portfolio mutation occurs<br>
**And** no accepted execution record exists.

### AC-03 — Approved terms are immutable

**Given** an approved proposal<br>
**When** symbol, side, quantity, approved price, or other execution terms are changed<br>
**Then** execution is refused<br>
**And** the original approval cannot be reused for the modified terms.

## B. Evidence and current reality

### AC-04 — Fresh execution evidence is required

**Given** an approved proposal<br>
**When** execution evidence exceeds the configured age limit<br>
**Then** the attempt is durably `REJECTED` with a stale-evidence reason<br>
**And** the portfolio remains unchanged<br>
**And** the ledger links the rejected attempt to the evidence that failed freshness validation.

### AC-05 — Price drift can invalidate approval

**Given** an approved proposal with an approved reference price<br>
**When** fresh execution evidence exceeds the active policy's permitted drift<br>
**Then** execution is durably `REJECTED`<br>
**And** no trade consequence occurs.

### AC-06 — Grounding evidence cannot stand in for execution evidence

**Given** a proposal grounded in valid market evidence<br>
**When** execution is attempted without separately valid execution evidence<br>
**Then** execution is refused.

## C. Current policy

### AC-07 — Policy is evaluated at execution time

**Given** a proposal approved under policy version A<br>
**And** policy version B becomes active before execution and is stricter<br>
**When** the proposal violates B<br>
**Then** execution is `REJECTED`<br>
**And** the attempt records policy B's immutable version/digest.

### AC-08 — Policy publication is versioned and immutable

**Given** a published policy version<br>
**When** policy rules change<br>
**Then** a new policy version is created<br>
**And** historical execution records continue to resolve the exact prior policy version.

## D. Deterministic portfolio controls

### AC-09 — Insufficient cash blocks an approved buy

**Given** a human-approved buy whose notional exceeds available cash<br>
**When** execution reaches authoritative portfolio validation<br>
**Then** the attempt is `REJECTED` with `InsufficientCash`<br>
**And** cash and positions remain bit-for-bit unchanged<br>
**And** the proposal remains `APPROVED` if still eligible for a future attempt.

### AC-10 — Valid execution changes state once

**Given** an approved proposal, valid policy, fresh evidence, and sufficient portfolio capacity<br>
**When** execution succeeds<br>
**Then** the portfolio changes exactly once<br>
**And** the proposal becomes `EXECUTED`<br>
**And** exactly one accepted execution/ledger record exists.

## E. Idempotency and concurrency

### AC-11 — Same idempotency key returns one durable result

**Given** an execution request with idempotency key K<br>
**When** K is submitted multiple times<br>
**Then** one durable execution request exists<br>
**And** all callers resolve the same result<br>
**And** there is at most one consequence.

### AC-12 — Concurrent workers cannot double-execute

**Given** one approved proposal<br>
**When** at least two independent workers race to execute it<br>
**Then** at most one worker commits an accepted consequence<br>
**And** there is exactly one portfolio mutation<br>
**And** exactly one accepted ledger record exists.

This test must use independent PostgreSQL connections/processes or equivalent isolated workers. A single-process mock does not satisfy the criterion.

### AC-13 — A second idempotency key still cannot create a second accepted consequence

**Given** proposal P has already executed successfully<br>
**When** a new execution request with a different idempotency key targets P<br>
**Then** the system refuses another accepted consequence.

## F. Failure recovery

### AC-14 — Worker death before commit has no consequence

**Given** a worker has claimed an execution request<br>
**When** the worker terminates before the authoritative transaction commits<br>
**Then** no portfolio mutation exists<br>
**And** another worker can later recover the request safely.

### AC-15 — Worker death after commit is safe to retry

**Given** an accepted consequence commits successfully<br>
**And** the worker terminates before returning/acknowledging success<br>
**When** the request is retried<br>
**Then** the existing committed result is returned or reconstructed<br>
**And** no second consequence occurs.

### AC-16 — Atomicity survives injected persistence failure

**Given** an otherwise valid execution<br>
**When** an injected database failure occurs before transaction commit<br>
**Then** portfolio state, proposal status, accepted execution, ledger evidence, and outbox state all roll back together.

No partial success is allowed.

### AC-17 — Recoverable work is reclaimable

**Given** a worker claims a queued request and its lease expires without a terminal result<br>
**When** another worker scans for work<br>
**Then** that worker can reclaim the request without violating idempotency or at-most-once consequence.

## G. Proposal lifecycle

### AC-18 — Expired proposal cannot execute

**Given** an approved proposal whose expiry time has passed<br>
**When** execution is attempted<br>
**Then** it is durably blocked<br>
**And** no portfolio mutation occurs.

### AC-19 — Superseded approval cannot authorize the replacement

**Given** proposal revision A is approved<br>
**And** revision B supersedes A<br>
**When** execution targets A or attempts to apply A's approval to B<br>
**Then** execution is refused.

## H. Ledger and evidence reconstruction

### AC-20 — Rejected attempts are reconstructable

For any rejected attempt, a reviewer can retrieve:

- proposal and immutable terms;
- human approval;
- execution evidence;
- active policy version;
- idempotency/request identity;
- machine-readable rejection reason;
- proof that portfolio state did not change.

### AC-21 — Accepted attempts are reconstructable

For any accepted attempt, a reviewer can retrieve:

- proposal and immutable terms;
- human approval;
- grounding evidence;
- execution evidence;
- policy version;
- portfolio state before and after;
- accepted execution record;
- proposal transition to `EXECUTED`.

### AC-22 — Ledger mutation is prohibited

**Given** an existing decision-ledger record<br>
**When** ordinary application or repository code attempts to update, delete, or replace it<br>
**Then** PostgreSQL rejects the mutation.

The control must be tested directly.

## I. Compensation

### AC-23 — Compensation is a new governed action

**Given** an accepted consequence<br>
**When** an operator requests compensation<br>
**Then** the system creates a new compensating command linked to the original execution<br>
**And** the original accepted ledger record remains unchanged.

### AC-24 — Compensation cannot silently erase history

After successful compensation, the decision history must show both:

1. the original accepted consequence; and
2. the explicit compensating consequence.

Net portfolio state may return to an earlier value, but historical evidence may not.

## J. Observability

### AC-25 — Decision metrics describe control behavior

A reproducible test/demo run must expose counts or measurements for:

- proposals;
- approvals;
- accepted executions;
- rejected executions by reason;
- stale-evidence rejections;
- policy rejections;
- idempotent replays/duplicate suppression;
- worker retries;
- compensations;
- approval-to-execution latency.

### AC-26 — Logs are not the audit source of truth

Deleting local/process logs must not prevent reconstruction of a durable accepted or rejected decision from PostgreSQL.

## K. Reproducibility and delivery

### AC-27 — One-command local environment

From a clean checkout, the documented Docker Compose workflow must start all required runtime services without requiring Kafka, Redis, Temporal, or Kubernetes.

### AC-28 — Automated validation

GitHub Actions must run at minimum:

- source compilation/linting;
- unit tests;
- PostgreSQL-backed integration tests;
- concurrency/idempotency tests;
- migration/schema verification;
- whitespace check.

### AC-29 — No real-money path exists

The release contains no broker credentials, broker SDK integration, or endpoint capable of submitting a real financial order.

### AC-30 — README claims map to evidence

Every quantitative or reliability claim in the final README must map to a test, deterministic demo, or recorded measurement produced by the repository.

## Release gate

Monster Heavy v1 is complete only when all acceptance criteria are either:

- **PASS**, with a linked automated test or deterministic demonstration; or
- explicitly removed from scope through a documented architecture change before release.

“No test because the happy path worked” is not an acceptable status.
