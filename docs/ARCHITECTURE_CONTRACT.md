# Monster Heavy Architecture Contract

## 1. Purpose

This document is the implementation contract for Monster Heavy.

Its purpose is to preserve the system's trust model while the implementation adds concurrency, retries, durable execution, versioned policy, worker failure, and explicit compensation.

If an implementation shortcut violates an invariant in this document, the shortcut is wrong even if the happy-path demo works.

## 2. Core trust model

```text
AI       -> PROPOSE
Human    -> AUTHORIZE
System   -> VERIFY CURRENT REALITY + CURRENT POLICY
Executor -> APPLY CONSEQUENCE AT MOST ONCE
Ledger   -> PRESERVE EVIDENCE
```

The model may interpret evidence and propose an action.

The human may approve or reject the immutable proposed terms.

Neither party has direct authority to mutate the authoritative portfolio.

Only the deterministic execution boundary may apply a consequence, and only after it re-verifies the conditions that are true at execution time.

## 3. Architectural invariants

### I1 — The model has zero mutation authority

Model output may create only a non-authoritative proposal.

Model code must not receive a repository or service capable of mutating portfolio state, recording human approval, changing policy, or marking its own proposal executed.

### I2 — Human approval is scoped and immutable

Approval refers to one immutable proposal revision and its terms hash.

Changing symbol, side, quantity, approved reference price, or any other execution term requires a new proposal or superseding revision.

Approval permits an execution attempt. It does not guarantee execution.

### I3 — Execution uses current reality

Every consequential attempt must verify fresh execution evidence independently of the evidence used to generate the proposal.

Grounding evidence answers:

> What informed the recommendation?

Execution evidence answers:

> What was true when the consequence was attempted?

Those records must never be silently substituted for one another.

### I4 — Execution uses current policy

A human approves the proposal, not an exemption from future controls.

The executor must evaluate the policy version active at execution time. A policy that becomes stricter after approval may block execution.

The attempt record must retain the exact policy version and digest used in the decision.

### I5 — At most one accepted consequence per proposal

Concurrent workers, duplicate delivery, API retries, and client timeouts must never cause the same proposal to mutate the portfolio twice.

At-most-once consequence must be enforced by the database as well as application code.

### I6 — Retry may repeat intent, never consequence

Every execution request carries an idempotency key.

Repeating the same request after a timeout must return or reconstruct the existing durable result rather than create a second consequence.

A new attempt after a deterministic rejection is allowed only with a new idempotency key while the proposal remains eligible.

### I7 — Durable state outranks process memory

The system must recover correctly after process death.

No correctness property may depend on an in-memory flag, task object, local cache, or HTTP response having been delivered.

### I8 — Consequence and evidence commit atomically

For a successful paper trade, the following must share one PostgreSQL transaction:

- authoritative portfolio mutation;
- accepted execution record;
- proposal transition to `EXECUTED`;
- linked decision-ledger record;
- durable outbox/event record, if an event is emitted.

There must be no state in which the portfolio changed but the durable decision evidence did not.

### I9 — Rejections are evidence too

A rejected consequential attempt must be durably recorded even when it is blocked before portfolio mutation.

The execution ledger is broader than the trade audit from Monster Light. It records attempts blocked by stale evidence, price drift, policy, portfolio state, expiry, duplicate suppression, or other deterministic controls.

### I10 — Compensation is explicit

An accepted consequence is never silently “rolled back” by application code.

If a compensating action is required, it is represented as a new governed command linked to the original accepted execution. The ledger preserves both.

### I11 — Money and time are deterministic

Monetary values use `Decimal`, never binary floating point.

All persisted timestamps are timezone-aware UTC.

### I12 — The ledger is append-only

Decision-ledger rows are immutable after insertion.

Database controls must prevent ordinary update, delete, or replacement of ledger history.

## 4. Bounded components

### Proposal boundary

Responsibilities:

- accept trusted grounding evidence;
- call the model through a narrow adapter;
- validate structured output;
- persist an immutable `PENDING` proposal;
- record model and grounding provenance.

Not allowed:

- approval;
- policy mutation;
- execution;
- portfolio mutation.

### Approval boundary

Responsibilities:

- approve or reject a specific proposal revision;
- preserve approver metadata, timestamp, rationale, and proposal terms hash;
- ensure only eligible `PENDING` proposals can be approved.

Not allowed:

- portfolio mutation;
- execution-evidence fabrication;
- control bypass.

### Policy boundary

Policies are deterministic and versioned.

The first implementation is expected to support controls such as:

- allowed action types;
- allowed symbols;
- maximum order notional;
- maximum resulting position;
- maximum execution-evidence age;
- maximum permitted price drift from approved terms;
- proposal expiry.

Every published policy version is immutable.

### Evidence boundary

Evidence is typed.

Planned evidence classes:

- **grounding evidence** — used by the model;
- **approval evidence** — what the human approved;
- **execution evidence** — fresh market observation used by the executor;
- **policy evidence** — policy version and digest used for the attempt;
- **consequence evidence** — authoritative state before and after an accepted action.

Evidence records are referenced by identifier from proposals and execution attempts.

### Execution orchestration boundary

The trusted Python execution application boundary accepts execution intent and creates a durable
execution request. The v1 HTTP API is read-only (ADR-05 in PHASE5_RELEASE.md).

Workers claim work from PostgreSQL rather than relying on an additional message broker.

The initial design uses PostgreSQL row-level locking and a durable queue/outbox pattern so multiple workers can safely operate without Redis, Kafka, or Temporal.

Expected worker behavior:

1. claim one eligible request using a database lock/lease;
2. load the immutable proposal and approval;
3. obtain or load fresh execution evidence;
4. load the current active policy;
5. open the authoritative transaction;
6. lock the proposal and affected portfolio state;
7. re-check proposal eligibility, evidence, policy, and portfolio invariants;
8. either record a durable rejection or apply one atomic accepted consequence;
9. commit;
10. acknowledge completion only after commit.

A crash before commit must leave no consequence.

A crash after commit but before acknowledgment must be safe to retry.

### Portfolio domain

The initial consequential domain remains intentionally small:

- cash;
- long-only positions;
- buy/sell paper trades;
- deterministic position and cash rules.

Monster Heavy is not a brokerage simulator. Portfolio behavior exists to make consequence and failure observable.

### Decision ledger

Every execution request receives a durable outcome record.

The ledger must support reconstruction of:

- proposal;
- approval;
- grounding evidence;
- execution evidence;
- policy version;
- attempt identity and idempotency key;
- result;
- rejection/failure reason;
- portfolio state transition when accepted;
- linked compensation when applicable.

## 5. State model

### Proposal

```text
PENDING
  -> APPROVED
  -> REJECTED
  -> EXPIRED
  -> SUPERSEDED

APPROVED
  -> EXECUTED
  -> EXPIRED
  -> SUPERSEDED
```

A deterministic execution rejection does **not** automatically change an `APPROVED` proposal to `REJECTED`. The proposal and the execution attempt are different facts.

### Execution attempt

```text
RECEIVED
  -> VERIFYING
  -> ACCEPTED
  -> REJECTED
  -> FAILED
```

`ACCEPTED` is terminal for the original consequence.

A later explicit compensating command may add a linked `COMPENSATED` ledger event; it does not erase the accepted record.

### Execution-request delivery

A request may be delivered or claimed more than once.

Its consequence may not.

## 6. At-most-once design

Application checks alone are insufficient.

The initial database design must include all of the following:

- a unique idempotency constraint for execution-request keys;
- row-level locking on the proposal and affected portfolio during authoritative execution;
- a database constraint that prevents more than one accepted consequence for the same proposal;
- a single transaction for accepted portfolio mutation and decision evidence;
- retry behavior that reads the already-committed result.

The exact schema may evolve, but these properties may not.

## 7. Failure semantics

### Deterministic rejection

Examples:

- stale evidence;
- excessive price drift;
- insufficient cash;
- disallowed symbol;
- order exceeds policy limit;
- proposal expired;
- policy tightened after approval.

Result:

- no portfolio mutation;
- durable `REJECTED` attempt;
- specific machine-readable reason;
- linked evidence and policy version;
- proposal remains `APPROVED` if retry is still semantically valid.

### Internal failure before commit

Result:

- no portfolio mutation;
- request remains recoverable or becomes a durable `FAILED` attempt according to the failure class;
- retry must be safe.

### Failure after commit but before response/acknowledgment

Result:

- committed accepted consequence remains authoritative;
- retry discovers the existing result;
- no second portfolio mutation;
- no second accepted ledger record.

## 8. Observability contract

Operational telemetry must describe decisions, not just processes.

Minimum metrics:

- proposals generated;
- approval and rejection counts;
- approval-to-execution latency;
- execution attempts by outcome;
- rejection counts by control/reason;
- stale-evidence rate;
- policy-block rate;
- duplicate/idempotent replay count;
- worker retry count;
- accepted consequence count;
- compensation count.

Logs and traces may aid debugging, but they are not the authoritative audit record.

## 9. API boundary

FastAPI is the read-only audit boundary in v1, following the explicit Phase 5 scope
(ADR-05 in [Phase 5 release evidence](PHASE5_RELEASE.md)). It exposes health/readiness,
durable control metrics, and reconstruction of attempts and their linked compensation history.

Proposal creation, human approval, execution submission, compensation requests and policy
publication remain trusted Python application boundaries. No HTTP mutation endpoint or fake
authentication is provided. This transport decision does not change any governance invariant
or remove an acceptance criterion. Production authentication remains outside v1 scope.

## 10. Security and identity boundary

This portfolio implementation will preserve actor identifiers and roles in records, but production-grade IAM, SSO, secrets management, and broker authentication are outside the initial scope.

No API key or secret may be committed to the repository.

## 11. Locked technology choices

Initial implementation:

- Python 3.13
- FastAPI
- PostgreSQL
- OpenAI SDK
- pytest
- Docker Compose
- GitHub Actions

## 12. Explicit non-goals

The first version will not add:

- real broker connectivity;
- real money;
- autonomous model execution authority;
- Kafka;
- RabbitMQ;
- Redis;
- Temporal;
- Kubernetes;
- LangGraph;
- CrewAI;
- MCP;
- vector search;
- multi-cloud deployment;
- market-prediction benchmarking;
- high-fidelity brokerage order types;
- a large frontend.

Any proposal to add one of these requires evidence that an acceptance criterion cannot be met cleanly without it.

## 13. Architecture success condition

Monster Heavy succeeds when a reviewer can deliberately attack the workflow with concurrency, retries, stale evidence, policy changes, and process failure—and the durable record still proves exactly one of two things:

> **The consequence was valid and occurred once.**

or

> **The consequence was blocked and authoritative state did not change.**
