# Phase 3: Proposal Agent

The Proposal Agent is Monster Heavy's only AI agent. It produces a non-authoritative
proposal; human authorization and all consequential execution remain separate boundaries.

## Architecture and composition

Trusted composition supplies a persisted `GroundingEvidence`, portfolio UUID, aware expiry,
an injected OpenAI Python SDK client, configured model family and exact model snapshot/version,
and `ProposalService(ProposalStore(dsn))` to the deterministic coordinator:

```text
ProposalContext (trusted immutable values)
  -> ProposalAgent.propose
  -> OpenAIProposalModel.propose(observation only)
  -> SDK chat.completions.parse(response_format=Candidate), no tools
  -> raw JSON and Pydantic validation + deterministic symbol/quantity/time checks
  -> domain Terms + Recommendation with trusted fields and provenance
  -> ProposalService.create -> ProposalStore.create -> PENDING
```

`proposal_model.py` receives no service, repository, database connection, DSN, portfolio
identity, grounding identity, or executable callback. It has no persistence imports or
function/tool dispatch. The SDK receives only a fixed versioned system prompt and JSON
observation data (symbol, decimal-string price, currency, source, observation time).
The separate deterministic `proposal_agent.py` coordinator holds only the model adapter
and ProposalService. It never calls approval, policy, lifecycle, evidence writers, or
execution-request operations. It does not request supersession.

Use a Structured Outputs capable model snapshot supported by the deployment's account.
No default model or implicit fallback is selected. `model_version` is the exact request
model; response metadata must match it. `model` is a trusted descriptive family label.
`prompt_version=proposal-v1` is a code constant tied to the fixed prompt. These values
are captured before generation and persisted using the existing Provenance structure.
Response content cannot supply or override provenance. An alias that resolves to a different
response model is refused; operators must configure the exact version.

## Output and authority

The frozen, strict, extra-forbidden Pydantic `Candidate` has exactly three required fields:
uppercase domain-format symbol, BUY/SELL side, and a plain decimal-string quantity (at most
1000 characters). JSON numbers, floats, booleans, exponent notation, non-finite values,
negative quantities and zero are refused. Quantity converts directly from string to Decimal
without rounding or arithmetic; trusted reference price is already a domain Decimal.
The size bound is a transport input limit, not a policy or trading limit.

The adapter independently reparses raw JSON, rejects duplicate keys, and compares it with
the SDK-parsed candidate. Missing/malformed/refused output, unexpected response model,
non-assistant role, multiple or missing choices, non-stop termination, tool/function calls,
and SDK failures fail closed before persistence. The coordinator revalidates even a
preconstructed Candidate, requires its symbol to equal the trusted observation, and checks
future observation/expiry both before and after generation. Only then does it construct
Recommendation. SDK retries are explicitly disabled; there is no recovery or fallback.

Portfolio UUID, grounding UUID, reference price, expiry and provenance come exclusively
from trusted context/configuration. ProposalService's existing database boundary independently
checks the persisted grounding's kind, symbol, price and portfolio currency, and expiry at
insertion. Proposal identity, PENDING status, creation time and terms hash remain database
controlled. The agent cannot create evidence or fabricate an approval.

This is capability separation for untrusted model output, not a Python sandbox against
malicious injected client code or dishonest composition. Trusted code must supply genuine
persisted grounding and appropriate adapters. Authentication/source acquisition and separate
database login provisioning remain outside scope. No prompt instruction grants authority.

## Acceptance evidence

| Criterion/invariant | Automated evidence |
| --- | --- |
| AC-01 / I1 | `test_model_output_only_creates_pending_without_consequence`: real SDK parsing through mocked HTTP and real PostgreSQL; valid output creates exactly one PENDING proposal with exact provenance, while portfolios, positions, approvals, policy, evidence, requests, attempts, ledger and outbox remain identical. Injected status yields no proposal. |
| AC-01 / I1 | Unit tests reject injected authoritative fields and capabilities, verify no tool registration, inspect model-module imports and object capabilities, and prove invalid candidates never construct Recommendation or call storage. |
| I11 | Exact high-precision Decimal test under low ambient precision; JSON numeric/float/non-finite/invalid quantities refused. PostgreSQL test verifies exact quantity and price round trips. |
| AC-06 foundation | Context rejects execution evidence in place of grounding; no model evidence-writing capability. Actual execution refusal remains deferred. |
| AC-18 foundation | Expiry checked before and after model latency; existing database expiry enforcement retained. Durable execution rejection remains deferred. |
| AC-28 / AC-30 | Full validation script: compilation, Ruff lint/format, migrations and history verification, all unit/PostgreSQL/concurrency tests, whitespace check. Claims map to tests here. |

Phase 3 adds 58 unit cases and 2 PostgreSQL cases: 225 total tests (133 unit, 92 integration).
All 165 preexisting Phase 1/2 cases are preserved unchanged. Validation used Python 3.13
and a disposable local PostgreSQL 17 cluster. No live OpenAI call was made: deterministic
fixtures exercise the installed SDK and parser without credentials, cost or network variability.
Live model availability, response quality and service connectivity are not proven.

## Deferred behavior

No FastAPI, workers, execution orchestration, portfolio mutation, retry/recovery, compensation,
UI, market-data acquisition, additional agents or infrastructure is added. Current-policy and
current-evidence execution enforcement remain deferred. There is no autonomous loop, model
quality benchmark, telemetry system or automatic abstention-to-proposal behavior: refusal
produces an error and no proposal. This phase does not claim the complete release gate.

The adapter follows the official OpenAI [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).
