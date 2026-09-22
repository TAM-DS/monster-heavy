# Monster Heavy Acceptance Criteria

These criteria are the definition of done for the first complete Monster Heavy release.

A feature is not complete because a demo looks correct. It is complete when an automated test or reproducible failure scenario proves the required property.

## A. Authority boundaries

### AC-01 — Model output cannot execute

**Given** trusted grounding evidence  
**When** the model produces a valid structured recommendation  
**Then** only a `PENDING` proposal and its provenance are persisted  
**And** portfolio cash and positions are unchanged  
**And** no approval or accepted execution record exists.

### AC-02 — Human approval does not mutate the portfolio

**Given** a `PENDING` proposal  
**When** a human approves the immutable terms  
**Then** an approval record exists and the proposal becomes `APPROVED`  
**And** no portfolio mutation occurs  
**And** no accepted execution record exists.

### AC-03 — Approved terms are immutable

**Given** an approved proposal  
**When** symbol, side, quantity, approved price, or other execution terms are changed  
**Then** execution is refused  
**And** the original approval cannot be reused for the modified terms.

## B. Evidence and current reality

### AC-04 — Fresh execution evidence is required

**Given** an approved proposal  
**When** execution evidence exceeds the configured age limit  
**Then** the attempt is durably `REJECTED` with a stale-evidence reason  
**And** the portfolio remains unchanged  
**And** the ledger links the rejected attempt to the evidence that failed freshness validation.

### AC-05 — Price drift can invalidate approval

**Given** an approved proposal with an approved reference price  
**When** fresh execution evidence exceeds the active policy's permitted drift  
**Then** execution is durably `REJECTED`  
**And** no trade consequence occurs.

### AC-06 — Grounding evidence cannot stand in for execution evidence

**Given** a proposal grounded in valid market evidence  
**When** execution is attempted without separately valid execution evidence  
**Then** execution is refused.

## C. Current policy

### AC-07 — Policy is evaluated at execution time

**Given** a proposal approved under policy version A  
**And** policy version B becomes active before execution and is stricter  
**When** the proposal violates B  
**Then** execution is `REJECTED`  
**And** the attempt records policy B's immutable version/digest.

### AC-08 — Policy publication is versioned and immutable

**Given** a published policy version  
**When** policy rules change  
**Then** a new policy version is created  
**And** historical execution records continue to resolve the exact prior policy version.

## D. Deterministic portfolio controls

### AC-09 — Insufficient cash blocks an approved buy

**Given** a human-approved buy whose notional exceeds available cash  
**When** execution reaches authoritative portfolio validation  
**Then** the attempt is `REJECTED` with `InsufficientCash`  
**And** cash and positions remain bit-for-bit unchanged  
**And** the proposal remains `APPROVED` if still eligible for a future attempt.

### AC-10 — Valid execution changes state once

**Given** an approved proposal, valid policy, fresh evidence, and sufficient portfolio capacity  
**When** execution succeeds  
**Then** the portfolio changes exactly once  
**And** the proposal becomes `EXECUTED`  
**And** exactly one accepted execution/ledger record exists.

## E. Idempotency and concurrency

### AC-11 — Same idempotency key returns one durable result

**Given** an execution request with idempotency key K  
**When** K is submitted multiple times  
**Then** one durable execution request exists  
**And** all callers resolve the same result  
**And** there is at most one consequence.

### AC-12 — Concurrent workers cannot double-execute

**Given** one approved proposal  
**When** at least two independent workers race to execute it  
**Then** at most one worker commits an accepted consequence  
**And** there is exactly one portfolio mutation  
**And** exactly one accepted ledger record exists.

This test must use independent PostgreSQL connections/processes or equivalent isolated workers. A single-process mock does not satisfy the criterion.

### AC-13 — A second idempotency key still cannot create a second accepted consequence

**Given** proposal P has already executed successfully  
**When** a new execution request with a different idempotency key targets P  
**Then** the system refuses another accepted consequence.

## F. Failure recovery

### AC-14 — Worker death before commit has no consequence

**Given** a worker has claimed an execution request  
**When** the worker terminates before the authoritative transaction commits  
**Then** no portfolio mutation exists  
**And** another worker can later recover the request safely.

### AC-15 — Worker death after commit is safe to retry

**Given** an accepted consequence commits successfully  
**And** the worker terminates before returning/acknowledging success  
**When** the request is retried  
**Then** the existing committed result is returned or reconstructed  
**And** no second consequence occurs.

### AC-16 — Atomicity survives injected persistence failure

**Given** an otherwise valid execution  
**When** an injected database failure occurs before transaction commit  
**Then** portfolio state, proposal status, accepted execution, ledger evidence, and outbox state all roll back together.

No partial success is allowed.

### AC-17 — Recoverable work is reclaimable

**Given** a worker claims a queued request and its lease expires without a terminal result  
**When** another worker scans for work  
**Then** that worker can reclaim the request without violating idempotency or at-most-once consequence.

## G. Proposal lifecycle

### AC-18 — Expired proposal cannot execute

**Given** an approved proposal whose expiry time has passed  
**When** execution is attempted  
**Then** it is durably blocked  
**And** no portfolio mutation occurs.

### AC-19 — Superseded approval cannot authorize the replacement

**Given** proposal revision A is approved  
**And** revision B supersedes A  
**When** execution targets A or attempts to apply A's approval to B  
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

**Given** an existing decision-ledger record  
**When** ordinary application or repository code attempts to update, delete, or replace it  
**Then** PostgreSQL rejects the mutation.

The control must be tested directly.

## I. Compensation

### AC-23 — Compensation is a new governed action

**Given** an accepted consequence  
**When** an operator requests compensation  
**Then** the system creates a new compensating command linked to the original execution  
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
