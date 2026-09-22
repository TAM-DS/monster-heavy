# Monster Heavy

### Governed AI execution under enterprise failure conditions

> **AI proposes → human authorizes → system verifies current evidence and current policy → execute or reject → preserve immutable evidence.**

Monster Heavy is the production-oriented successor to Monster Light.

Monster Light proved that a consequential AI workflow can keep recommendation, human authorization, deterministic validation, execution, and audit as separate responsibilities in a small inspectable system.

Monster Heavy asks the harder question:

> **Does that trust model still hold when execution becomes concurrent, retryable, policy-governed, distributed across workers, and exposed to partial failure?**

## Status

**Architecture locked. Implementation has not started.**

The first artifacts in this repository are the architecture contract and acceptance criteria. They exist specifically to prevent the implementation from drifting into a larger but less meaningful system.

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

A proposal is not authority.  
Approval is not execution.  
A retry is not a new consequence.  
A successful response is not the source of truth.  
The durable decision record is.

## Scope

Paper trading only. No broker integration. No real money.

The portfolio project is about enterprise AI governance, reliable execution, failure recovery, and evidence—not market prediction.
