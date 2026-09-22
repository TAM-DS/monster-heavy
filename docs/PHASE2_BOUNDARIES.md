# Phase 2 application and domain boundaries

Phase 2 accepts validated structured recommendations without a model adapter. It adds
proposal, human decision, lifecycle, typed observation, and policy publication/resolution
services. It adds no execution path, framework, dependency, or migration.

## Composition and authority

`domain.py` contains frozen validated values and pure refusal rules. `application/__init__.py`
contains services and narrow capability protocols. `persistence/boundaries.py` implements
those protocols as atomic PostgreSQL operations. Transaction-dependent business checks run
inside these adapters, using the domain eligibility rules, so a read/check/write cannot be
split across transactions by a caller. Each operation opens and commits its own connection;
failures roll back the whole operation. No connection is returned to service callers.

Trusted composition code wires only the appropriate adapter:

| Service | Adapter capability |
| --- | --- |
| `ProposalService` | `ProposalStore.create` only |
| `ApprovalService` | `ApprovalStore.decide` only |
| `LifecycleService` | `LifecycleStore.expire` only |
| `GroundingService` | `GroundingStore.grounding` only |
| `ExecutionEvidenceService` | `ExecutionEvidenceStore.execution`; its separate typed reader refuses grounding IDs |
| `PolicyService` | `PolicyPublicationStore.publish` only |
| `PolicyResolver` | `PolicyReadStore.active`; adapter also permits historical inspection by time/version |

None exposes portfolio mutation, execution requests, attempts, or ledger mutation. Proposal
creation reads portfolio currency to validate its grounding association. There is no aggregate
repository or generic SQL operation on the public adapters. The only evidence writer available
to approval is internal creation of approval evidence; it cannot fabricate execution evidence.

Actors are supplied by trusted composition code. Exact roles `approver` and `policy_publisher`
are required for their respective operations. This preserves actor IDs/roles and separates
application capabilities; it does not authenticate a supplied actor or sandbox malicious Python.
Production IAM and separate database login provisioning remain out of scope. The Phase 1
`monster_heavy_app` role remains unchanged, and integration tests exercise the adapters as it.

## Terms, evidence, and human decisions

`Recommendation` requires `Terms`, a grounding UUID, and `Provenance` (model, model version,
prompt version). The schema is deliberately small: no unvalidated extra execution terms.
Decimal inputs must be finite positive `Decimal`, never floats, integers, or strings. Symbols
use uppercase ASCII letters/digits plus dot/dash (maximum 32 characters); timestamps must be
aware and normalize to UTC. There is no monetary rounding. Grounding must match symbol,
reference price, and portfolio currency. Expired input is refused before insertion.

The existing PostgreSQL trigger remains the authoritative SHA-256 implementation. It hashes
portfolio UUID, symbol, side, quantity, reference price, and UTC expiry using PostgreSQL JSON
text. The adapter canonicalizes decimal scales without rounding before insertion, so equivalent
Phase 2 inputs such as `2` and `2.000` hash identically. It does not recompute or alter historical
Phase 1 hashes. Hash comparison always uses the persisted value. UUID and hash jointly identify
approved terms; identical hashes on separate proposals do not transfer approval authority.
Changing any execution term requires another proposal UUID.

Observation payload schema version 1 contains symbol, decimal-string price, and currency;
source and observation time use the existing evidence columns. Grounding and execution records
use distinct immutable typed references and distinct rows, even for identical observations.
Future observations are refused. Stored records are validated when entering the typed boundary;
opaque Phase 1 fixtures are not silently interpreted as schema version 1. Trusted callers supply
observations; no market-data acquisition or source authentication is implemented.

Approval/rejection requires a caller-supplied exact terms hash, actor, and rationale (empty
rationale is permitted). Under a proposal row lock, the adapter checks `PENDING` and expiry,
compares the hash, then inserts approval evidence and the immutable decision and changes status
in one transaction. The evidence retains UUID/hash, decision, actor/role, rationale, and time.
Repeated or conflicting decisions are explicit refusals, not idempotent replays.

## Lifecycle and concurrency

Approval/rejection accepts only unexpired `PENDING`. Supersession accepts unexpired `PENDING`
or `APPROVED`, creates a new `PENDING` proposal in the same portfolio, and transitions the old
proposal to `SUPERSEDED` atomically. The old approval remains immutable and attached only to the
old UUID/hash. A replacement requires independent human review. The unique `supersedes_id` and
row lock prevent branching replacement history. Approval racing supersession can succeed first,
but then authorizes only the superseded old proposal; it cannot authorize the replacement.

`expire` moves elapsed `PENDING`/`APPROVED` to `EXPIRED`; repeated expiry is harmless. It refuses
live proposals and other terminal states. There is no background expiry job: eligibility checks
always compare expiry against database time, even before the explicit status transition. Refused
approval or supersession does not itself write `EXPIRED`. Equality with expiry is already expired.
Time is sampled with `clock_timestamp()` after acquiring the proposal lock, avoiding stale
transaction-start timestamps during waits. `REJECTED`, `SUPERSEDED`, `EXPIRED`, and `EXECUTED`
cannot reenter these services' eligible states. This phase never produces `EXECUTED`.

These are application lifecycle guarantees. The Phase 1 administrative/raw SQL ability to set
status is unchanged; later execution must use the same eligibility checks in its authoritative
transaction. No schema change is needed: immutable terms, unique decisions, typed foreign keys,
row locking, and atomic transactions already support every Phase 2 invariant.

## Policy publication and resolution

`PolicyRules` schema version 1 explicitly validates:

- unique nonempty allowed action/symbol tuples, canonicalized by sorting;
- positive maximum order notional and resulting-position **share quantity**;
- positive integer maximum evidence-age and proposal-age seconds;
- a maximum fractional price-drift ratio between zero and one (0.05 means 5%).

Decimal policy values are stored as canonical strings; PostgreSQL computes the immutable digest.
An advisory transaction lock serializes version allocation (`max(version) + 1`) and publication.
`effective_at=None` means immediate activation at database publication time. An aware future
instant schedules activation; backdating is refused. Duplicate effective times produce an explicit
conflict. Version order is publication order; activation order is effective time. A future-scheduled
version can therefore activate after a later-published immediate version. There is no mutable
active flag or implicit cancellation of scheduled policies.

`PolicyResolver.active()` reads the greatest effective time at or before the database statement
time on a new connection each time, without caching. Unique effective times make this unambiguous.
Historical `at(time)` and `get(version)` reads are for inspection. Missing/unsupported policies
fail closed. Publication cannot change a previously published version or digest. Policies are
not bound into approval: approval covers terms, not an exemption from future controls.

Pure `verify_evidence` checks execution type, symbol, future time, age, and symmetric absolute
price drift against the approved reference price. Freshness and drift limits are inclusive.
Pure `verify_policy_terms` checks action/symbol, reference-price notional, expiry, and current
policy's maximum elapsed proposal age. Age equality expires eligibility. Integer-ratio arithmetic
makes monetary comparisons independent of the ambient Decimal precision. These functions are
preflight checks only; they do not record execution decisions or grant execution authority.

The future executor must resolve current policy again inside its authoritative transaction,
recheck eligibility and independently valid execution evidence, validate currency, actual-price
notional, resulting position and cash, and preserve exact evidence/policy references in the ledger.
Its policy-publication concurrency/linearization protocol is deferred with execution orchestration.
The maximum-position field is validated/published here; position/cash evaluation is deferred.

## Acceptance evidence

| Criterion | Phase 2 evidence and limit |
| --- | --- |
| AC-01 | `test_creation_only_persists_pending_and_provenance`, capability/import tests: structured input creates only a pending proposal with provenance; cash/positions unchanged and no approval/request/attempt. The model adapter itself is deferred. |
| AC-02 | `test_human_decision_atomic_and_non_consequential`: exact approval evidence/status and unchanged portfolio, no execution records. |
| AC-03 | Hash determinism/change tests, wrong-hash refusal, supersession tests, plus all original immutable-term/composite-FK tests. Executor refusal remains deferred. |
| AC-04–05 | `test_freshness_and_exact_drift`: boundary arithmetic and refusal reasons including exact thresholds. Durable rejected attempts/ledger outcomes require the future executor. |
| AC-06 | Typed evidence association/retrieval tests plus `test_grounding_is_never_execution_evidence` and original PostgreSQL typed-FK tests. |
| AC-07 | `test_policy_versions_current_resolution_and_stricter_controls`: policy B resolves after approval and its stricter limit blocks preflight without portfolio mutation. Durable attempt referencing B remains deferred. |
| AC-08 | Version/current/historical/digest tests, concurrent publication, effective-time conflict, and original direct immutable-policy tests. |
| AC-18 | Expired proposal refusal/explicit expiry tests, including clock recheck after lock wait. Durable execution rejection remains deferred. |
| AC-19 | Supersession, atomic rollback, competing replacements, and approval/supersession race tests; old approvals cannot authorize new proposals. Execution of the old proposal remains deferred. |

All Phase 1 tests remain unchanged and run in the same suite. Additional tests cover invalid
structured values, UTC normalization, float rejection, future/mismatched evidence, unauthorized
roles, terminal lifecycle states, runtime-role operation, and injected SQL failure rollback.
The complete release acceptance criteria are not claimed passed by this phase.

## Deferred decisions

No blocking question remains for Phase 2. Currency precision/rounding, trusted observation
acquisition, real identity authentication, execution transaction policy/evidence revalidation,
position/cash controls, durable rejected attempts, workers, retry/recovery, compensation,
observability, API, model integration, and UI remain deferred. No new infrastructure is introduced.
