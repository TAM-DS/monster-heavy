# Phase 1 persistence contract

This implements the foundation of the architecture contract, not the complete release.
No API, model adapter, worker, execution service, market integration, or compensation
workflow exists. PostgreSQL 17 and Python 3.13 are the tested versions. Psycopg is the
PostgreSQL driver; plain SQL migrations avoid an ORM or additional migration framework.

## Layout and records

- `src/monster_heavy/persistence/database.py`: UTC PostgreSQL connections.
- `models.py`: typed row records compatible with psycopg `class_row`.
- `migrate.py`: forward-only SQL migration runner, transaction/advisory lock, SHA-256
  migration checksums, and `--check` for missing/unknown/modified migration history.
- `migrations/0001_foundation.sql`: authoritative schema, constraints, triggers, grants.
- `tests/unit` and `tests/integration`: record validation and real PostgreSQL tests.

| Table | Meaning and durable links |
| --- | --- |
| `proposals` | One immutable revision per UUID; terms, database-computed terms hash, grounding evidence, model provenance, portfolio, expiry, optional superseded proposal; only status is mutable. |
| `approvals` | Immutable human decision, actor/role, rationale, approval evidence, exact proposal UUID and terms hash. |
| `evidence` | Immutable typed grounding, approval, execution, or consequence payload with source and observation time. |
| `policy_versions` | Immutable rules, unique positive version, database-computed digest, publisher identity, publication and effective times; this row is policy evidence. |
| `portfolios` / `positions` | Nonnegative cash and long-only quantities; currency and revision available for later authoritative execution. |
| `execution_requests` | Globally unique idempotency key bound permanently to one proposal and actor; availability, lease, and completion fields. |
| `execution_attempts` | Request/proposal identity, matching human approval, distinct execution evidence, exact policy version/digest, outcome/reason, consequence evidence; terminal rows immutable. |
| `decision_ledger` | One immutable terminal outcome per attempt; foreign key requires the outcome to match the attempt. All provenance is reconstructable through immutable linked rows. |
| `outbox` | Durable event linked to a ledger record; event identity/payload immutable, delivery and lease metadata mutable. |

All tables live in `monster_heavy`; migration history lives in `public.schema_migrations`.
Use qualified names or `SET LOCAL search_path = monster_heavy, pg_catalog` in transactions.
IDs are UUIDs. No sequence allocation is needed by the runtime role.

## Database guarantees

- Execution-request keys are globally unique, including concurrent inserts.
- A partial unique index permits at most one `ACCEPTED` attempt per proposal, across
  different requests/keys. Immutable terminal attempts prevent freeing that slot.
- Policy, evidence, approval, and ledger rows reject update/delete, including upserts.
  Statement triggers reject truncation, including cascading truncation. Identity and
  immutable terms cannot change on proposals, requests, attempts, and outbox records.
- Composite foreign keys bind approvals to exact terms, attempts to the request's
  proposal and that proposal's approval, and policy references to the exact digest.
  Typed evidence foreign keys prevent grounding from substituting for execution evidence.
- Monetary columns and quantities use finite PostgreSQL `NUMERIC`, returned as Python
  `Decimal`. No fixed scale is assumed: storage does not silently round values. Currency
  precision and order rounding remain future domain rules. JSON monetary snapshots and
  policy limits must use decimal strings; the record models reject binary floats.
- Every timestamp column is finite `TIMESTAMPTZ`. PostgreSQL stores instants; Compose and
  application connections select UTC for display/readback. Python models reject naive
  datetimes and normalize aware datetimes to UTC. SQL cannot recover whether a caller
  originally supplied a naive timestamp or float after PostgreSQL has coerced its input;
  future input boundaries must use these types rather than permissive raw SQL coercions.

Database owner/superuser access is an administrative trust boundary: owners can alter
DDL. The migration creates the non-login `monster_heavy_app` group role, which has no
DDL, truncate, or history-update/delete privileges. Deployment should create a login
and grant it this group, without schema ownership, superuser privileges, or owner-role
membership. Migrations require a separate owner with `CREATEROLE` (or a preprovisioned
safe group role). Compose uses an owner credential for local migration/testing only;
there is no running application service or production credential in this phase.

## Small design decisions and limits

Proposal revision identity is a new UUID linked via `supersedes_id`; there is no mutable
revision number. The terms hash covers portfolio, symbol, side, quantity, reference
price, and expiry using PostgreSQL's JSON representation and SHA-256. Approval binds
both the UUID and hash. Future execution terms must extend this hash contract.

Policy rows are published immutable versions with distinct `effective_at` times. The
intended current policy is the latest effective row at the transaction's evaluation
time. Publishing/activation authorization, retroactive-publication restrictions,
policy-rule validation, and evaluation belong to the later policy boundary. There is
no mutable active-policy flag and no policy evaluator in Phase 1.

Attempts may omit evidence/policy before verification fails; accepted attempts require
approval, execution evidence, policy, and consequence evidence. Later application code
must populate rejection evidence whenever available and choose machine-readable reasons.
Opaque evidence JSON accommodates source snapshots without implementing market data or
policy rules. Its semantic validation is not claimed here.

The schema supports row locks and one transaction across cash/positions, proposal state,
attempt, ledger, and outbox. The rollback test writes all of these directly and injects a
SQL failure. It proves atomic storage when used in one transaction; it does not enforce
that arbitrary portfolio writes are accompanied by an accepted decision. That authority
boundary and transaction service are explicitly deferred. Proposal lifecycle eligibility,
request replay/result reconstruction, lease claiming/recovery, current-policy checks,
and worker crash semantics are also deferred. No full AC-10 through AC-17 claim is made.

## Evidence map

| Contract / criterion | Phase 1 evidence |
| --- | --- |
| I2 / AC-03 | `test_proposal_terms_cannot_change`, `test_approval_cannot_bind_wrong_hash`, `test_other_proposal_approval_cannot_authorize_attempt`; application refusal/eligibility deferred. |
| I3 / AC-06 | `test_grounding_cannot_be_execution_evidence`; freshness validation deferred. |
| I4 / AC-08 | `test_policy_versions_preserve_exact_reference`, `test_mismatched_policy_digest_rejected`, policy mutation cases in `test_history_is_immutable_even_for_owner`; current-policy evaluation deferred. |
| I5–I6 / AC-11–13 | `test_unique_idempotency_key`, `test_second_key_cannot_accept_same_proposal`, `test_independent_connections_race`, `test_accepted_slot_cannot_be_reopened`; races use two PostgreSQL backend connections. No worker or portfolio concurrency claim. |
| I8 / AC-16 foundation | `test_transaction_rolls_back_all_consequence_records`; direct SQL transaction, no execution service. |
| I9 / AC-20 foundation | `test_rejected_attempt_is_durable_and_reconstructable`; schema links and unchanged portfolio, no rejection evaluator. |
| I11 | Decimal/UTC unit tests, `test_exact_decimal_and_utc_round_trip`, invalid-money cases, schema column checks. |
| I12 / AC-22 | Update/delete/truncate/upsert cases in `test_history_is_immutable_even_for_owner`; `test_runtime_role_cannot_mutate_or_disable_history`. |
| AC-27–28 foundation | Compose PostgreSQL + migration workflow; validation script and GitHub Actions; migration history/checksum and model/schema tests. |

All other release criteria remain unimplemented. No architecture decision blocks Phase 1.
The later phase must select currency precision/rounding and the concrete policy/evidence
payload schemas before implementing deterministic execution. These are intentionally
not invented by the storage foundation.
