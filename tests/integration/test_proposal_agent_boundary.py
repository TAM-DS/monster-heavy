import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx2
import pytest
from openai import OpenAI
from psycopg import sql

from monster_heavy.application import GroundingService, ProposalService
from monster_heavy.domain import BoundaryError, Observation
from monster_heavy.persistence.boundaries import GroundingStore, ProposalStore
from monster_heavy.persistence.database import connect
from monster_heavy.proposal_agent import ProposalAgent, ProposalContext
from monster_heavy.proposal_model import OpenAIProposalModel

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("extra", [False, True])
def test_model_output_only_creates_pending_without_consequence(dsn, extra):
    with connect(dsn) as conn:
        portfolio = conn.execute(
            "INSERT INTO monster_heavy.portfolios(cash,currency) VALUES (1000,'USD') RETURNING *"
        ).fetchone()
        conn.execute("INSERT INTO monster_heavy.positions VALUES (%s,'TEST',3)", (portfolio["id"],))
    grounding = GroundingService(GroundingStore(dsn)).create(
        Observation(
            symbol="TEST",
            price=Decimal("10.12345678901234567890123456789"),
            currency="USD",
            source="trusted-fixture",
            observed_at=datetime.now(UTC),
        )
    )
    context = ProposalContext(
        portfolio_id=portfolio["id"],
        grounding=grounding,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    tables = (
        "portfolios",
        "positions",
        "approvals",
        "policy_versions",
        "evidence",
        "execution_requests",
        "execution_attempts",
        "decision_ledger",
        "outbox",
    )

    def snapshot():
        with connect(dsn) as conn:
            return {
                table: conn.execute(
                    sql.SQL("SELECT * FROM monster_heavy.{} ORDER BY 1").format(
                        sql.Identifier(table)
                    )
                ).fetchall()
                for table in tables
            }

    before = snapshot()
    candidate = dict(symbol="TEST", side="SELL", quantity="0.12345678901234567890123456789")
    if extra:
        candidate["status"] = "APPROVED"

    def respond(request):
        assert "tools" not in json.loads(request.content)
        return httpx2.Response(
            200,
            json=dict(
                id="chat-test",
                object="chat.completion",
                created=1,
                model="test-snapshot",
                choices=[
                    dict(
                        index=0,
                        finish_reason="stop",
                        message=dict(
                            role="assistant",
                            content=json.dumps(candidate),
                            refusal=None,
                        ),
                    )
                ],
            ),
        )

    client = OpenAI(
        api_key="test-only", http_client=httpx2.Client(transport=httpx2.MockTransport(respond))
    )
    agent = ProposalAgent(
        OpenAIProposalModel(client, model="test-family", model_version="test-snapshot"),
        ProposalService(ProposalStore(dsn)),
    )
    if extra:
        with pytest.raises(BoundaryError):
            agent.propose(context)
    else:
        proposal = agent.propose(context)
        assert proposal.status == "PENDING"
        assert proposal.quantity == Decimal(candidate["quantity"])
        assert proposal.reference_price == grounding.observation.price
        assert proposal.grounding_evidence_id == grounding.id
        assert proposal.model_provenance == dict(
            model="test-family", model_version="test-snapshot", prompt_version="proposal-v1"
        )
    assert snapshot() == before
    with connect(dsn) as conn:
        rows = conn.execute(
            "SELECT * FROM monster_heavy.proposals WHERE portfolio_id=%s", (portfolio["id"],)
        ).fetchall()
    assert len(rows) == (0 if extra else 1)
    if rows:
        assert rows[0]["status"] == "PENDING"
