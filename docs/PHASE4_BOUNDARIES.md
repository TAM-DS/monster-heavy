# Phase 4: authoritative deterministic execution core

**AI proposes → human authorizes → system verifies current evidence and current policy →
execute or reject → preserve immutable evidence.**

Phase 4 begins with persisted proposals, human decisions, and independently created execution
observations. It adds synchronous durable submission and deterministic execution. It adds no
model capability, schema migration, dependency, API, worker, or external integration. All 225
Phase 1–3 tests remain unchanged. Earlier phase documents describe their historical scope;
this document supersedes their execution deferrals only.

## Composition and durable intent

Trusted composition creates `persistence.execution.ExecutionStore(dsn)`:

```python
request = executor.submit(idempotency_key, approved_proposal_id, actor)
result = executor.execute(request.id, execution_evidence_id)
# Inspection or replay after an uncertain response:
result = executor.result(request.id)
result = executor.execute(request.id, execution_evidence_id)
```

`submit` commits an execution request independently of execution. The globally unique key is
permanently bound to proposal UUID and actor ID/role. Concurrent identical submissions return
the same request UUID. Conflicting reuse raises `IdempotencyConflict` without replacing intent.
Submission authorizes no consequence; executor eligibility checks still apply. Actor metadata
comes from trusted composition, not production authentication. Missing proposal IDs fail the
existing foreign key. Unknown request IDs produce `ExecutionRequestNotFound`.

Execution evidence is supplied to `execute`, not fixed at submission. This allows fresh evidence
to be loaded when the queued intent is actually attempted. The executor accepts only a UUID,
reloads the immutable observation, and never trusts an in-memory observation or earlier preflight.
The first committed terminal result wins for that request. Replay returns it even if another
caller supplies different evidence or the policy has changed. A deterministic rejection can be
attempted again only with a new key. A failed uncommitted transaction leaves the durable request
incomplete; a direct retry can supply fresh evidence. Acquiring that evidence is outside Phase 4.

`result` returns the durable terminal attempt or `None`. No volatile cache controls execution.
Neither Proposal Agent nor the approval boundary is changed or receives this capability.
`execution.py` is the pure evaluator; `persistence/execution.py` owns all transaction-dependent
reads, verification, mutation, and evidence writes. There is no model call or model import.

## Authoritative transaction and policy ordering

Each `execute` opens a new PostgreSQL connection and transaction:

1. Lock the request row. Return its existing terminal attempt on replay.
2. Lock the proposal, then portfolio, then existing affected position. Locking the portfolio
   also serializes operations when a position row does not yet exist and across different
   proposals spending the same cash.
3. Acquire shared advisory transaction lock `728431002`, paired with policy publication's
   existing exclusive lock. Sample `clock_timestamp()` **after** acquiring all state locks.
4. Resolve the greatest policy effective time at or before that instant. Reload approval for
   the exact proposal UUID/hash, execution evidence of kind `EXECUTION`, and any existing
   accepted consequence. Recheck status, expiry, policy age, evidence and portfolio controls.
5. Commit either an accepted consequence with its evidence, or a durable rejection.

The sampled instant is the decision's linearization point. An immediate policy publication
that commits before it is considered; a publication behind the shared lock waits until the
execution transaction ends. Scheduled versions effective by that instant are considered,
including those activating during a proposal-lock wait. A scheduled version becoming effective
after the sampled instant governs subsequent decisions. There is no cached policy, approval-time
policy exemption, or claim that policy time is sampled at physical commit. All decision
record timestamps use this evaluation instant. The policy version and database-computed digest
are retained through the existing composite foreign key, even on refusals when a policy exists.
Missing or malformed active policy fails closed.

The existing partial unique index on accepted attempts independently prevents a second accepted
consequence, regardless of keys or proposal-status tampering. Terminal attempts and ledger rows
remain immutable. Application serialization provides one terminal result per submitted request;
the database retains its broader historical attempt model. Administrative/raw SQL portfolio
writes are still a trusted boundary: the runtime group is not a sandbox against arbitrary SQL.
No new database security claim is made beyond the existing constraints and triggers.

## Exact paper-portfolio rules

- Only an unexpired `APPROVED` proposal with a matching immutable human approval is eligible.
  Expiry equality blocks execution. Superseded and other ineligible states cannot execute.
  Existing acceptance or `EXECUTED` takes precedence and yields `AlreadyExecuted`.
- Execution evidence must exist as its own `EXECUTION` row. Grounding never substitutes.
  Symbol and portfolio currency must match. Future observations fail. Evidence age may equal
  the active maximum; older observations fail. Proposal age must be strictly below the active
  maximum, as in Phase 2.
- Absolute fractional price drift is `abs(execution_price - reference_price) / reference_price`;
  equality with the limit passes. Both upward and downward drift are checked.
- Side and symbol must be allowed by current policy. Order notional is **quantity × execution
  price**, never reference price. Equality with the maximum passes.
- BUY requires cash at least equal to actual notional: subtract notional and add quantity.
- SELL requires position quantity at least equal to the sale: add notional and subtract quantity.
- The resulting affected position's share quantity must not exceed the current maximum, for
  BUY and SELL alike. A sale from an oversized position must reduce it to the allowed maximum
  or below. No shorting, borrowing, fees, cost basis, mark-to-market valuation, or fractional-share
  restriction is introduced. Other symbols' positions are untouched.
- Missing position means zero. An accepted trade upserts the position; zero-quantity rows are
  retained. Accepted execution increments portfolio revision exactly once.
- Finite Decimal values use exact rational arithmetic and terminating Decimal conversion.
  No monetary rounding or currency-specific scale is imposed; ambient Decimal precision cannot
  change a decision or mutation. PostgreSQL NUMERIC retains the result without a fixed scale.

Stable minimum refusal codes are `ExecutionEvidenceRequired`, `StaleEvidence`,
`PriceDriftExceeded`, `EvidenceSymbolMismatch`, `EvidenceCurrencyMismatch`, `ProposalExpired`,
`IneligibleProposal`, `DisallowedAction`, `DisallowedSymbol`, `OrderNotionalExceeded`,
`ResultingPositionExceeded`, `InsufficientCash`, `InsufficientPosition`, and `AlreadyExecuted`.
Additional fail-closed codes include `FutureEvidence`, `FutureProposal`,
`ProposalPolicyAgeExceeded`, `NoActivePolicy`, `InvalidActivePolicy`, and
`InvalidExecutionEvidence`. One deterministic reason is retained per attempt, in evaluator order.

## Accepted and rejected transactions

Accepted execution atomically updates cash/position/revision, sets proposal `EXECUTED`, inserts
immutable consequence evidence, inserts the `ACCEPTED` attempt, inserts its decision ledger and
outbox event, and completes the request. There is no response before transaction commit.

Rejected execution writes a terminal `REJECTED` attempt, consequence evidence proving unchanged
cash/affected quantity/revision, a ledger row, an outbox event, and request completion. It never
updates the portfolio, position, or proposal status. In particular insufficient cash leaves an
otherwise eligible proposal `APPROVED`; expiry does not perform a separate lifecycle transition.

Consequence snapshots retain currency, portfolio and symbol, before/after cash, affected quantity,
revision, outcome, evaluation time, supplied evidence identifier (including absent, wrong-kind,
or unknown identifiers), and accepted notional. Attempt links retain valid execution evidence,
current policy version/digest, and matching approval when available. Request and proposal links
reconstruct key/actor, terms, grounding and model provenance. Absent approval/evidence/policy is
represented honestly, not fabricated. Immutable database history is sufficient without logs.

Unexpected Python/SQL failures propagate and roll back the whole authoritative transaction.
They do not become deterministic rejections or terminal `FAILED` attempts in this phase.
`test_failure_after_all_writes_rolls_back_and_request_retries` injects SQL division by zero after
**all** writes, including request completion, and before commit. State/evidence roll back and
retry accepts once. No production failure-injection callback or executor capability is exposed;
tests replace the internal persistence helper.

## Acceptance evidence

| Criteria | Phase 4 evidence and scope |
| --- | --- |
| AC-01–03 | All existing authority/immutable-term tests unchanged; `test_pending_replacement_has_no_inherited_approval` proves executor cannot inherit another proposal's approval. |
| AC-04–07 | `test_durable_refusals`, `test_actual_execution_notional_rechecked_with_current_policy`, `test_rechecks_after_independent_lock_wait`: durable stale/drift/type/currency/policy refusals with no mutation, exact active policy reference. |
| AC-08 | Existing immutable-policy tests retained; `test_policy_publication_waits_for_execution_decision` proves publication/execution ordering. |
| AC-09–10 | `test_durable_refusals`, `test_existing_positions`, `test_acceptance_reconstruction_and_replay`, `test_high_precision_round_trip`: exact changes, durable rejection, unchanged state, one accepted ledger. |
| AC-11–13 | `test_independent_execution_connections_race` uses separate PostgreSQL backend connections for same/different keys; `test_idempotency_conflict`, replay tests, and `test_database_unique_slot_survives_application_status_tampering` cover binding and database enforcement. |
| AC-14 | Strengthened foundation: authoritative rollback and direct retry proven. Actual worker death, claim recovery, and scheduling remain deferred. |
| AC-15 | Core committed-result replay passes, including different evidence/policy on replay. Actual worker death/acknowledgment lifecycle remains deferred. |
| AC-16 | `test_failure_after_all_writes_rolls_back_and_request_retries`: injected database failure rolls back the real executor's complete write set. |
| AC-18–19 | Lock-wait expiry/supersession tests, durable refusal matrix, and pending replacement test. |
| AC-20–21, AC-26 | `audit` assertions across accepted/rejected tests reconstruct outcome, state snapshots, immutable references, ledger/outbox and completion entirely from PostgreSQL. |
| AC-22 | Existing direct mutation/truncate/upsert tests retained; runtime-role execution and terminal immutability exercised in `test_runtime_role_and_terminal_immutability`. |
| AC-28–30 | Full validation script, original authority tests, pure exact-arithmetic tests; no broker or live market/model integration added. |

`test_different_proposals_serialize_portfolio_capacity` also proves two different proposals cannot
overspend one portfolio. Pure unit tests cover inclusive boundaries, both drift directions,
sell-to-zero, oversized-position sales, actual-price notional, and low Decimal precision.

## Validation result

The complete `scripts/validate.sh` suite passes on Python 3.13.14 and a disposable local
PostgreSQL 17 cluster: **297 tests**, comprising **161 unit** and **136 integration** cases.
Phase 4 adds **72 tests** (28 unit and 44 integration); all **225 existing tests are unchanged**.
Compilation, Ruff lint/format checks, migration application/history verification, PostgreSQL
concurrency tests, and `git diff --check` pass. No live model, market, or broker calls are made.
Malformed stored decimal payloads are also durably refused by
`test_invalid_decimal_payload_is_a_durable_refusal`.

## Deferred behavior

No FastAPI, background workers, leasing, crash-reclaim scheduling, compensation, metrics, UI,
real market-data acquisition, real broker integration, additional agents, Redis, Kafka, Temporal,
Kubernetes, LangGraph, or MCP. Outbox rows are durable but are not delivered by this phase.
No worker-crash or complete release-gate claim is made. AC-17, AC-23–25 and the worker portions
of AC-14–15 remain deferred. Production authentication and database-login provisioning remain
outside scope. Changes await Thomas's review; no commit is part of this work.
