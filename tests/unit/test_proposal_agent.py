import ast
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import httpx2 as httpx
import pytest
from openai import OpenAI

from monster_heavy.application import ProposalService
from monster_heavy.domain import BoundaryError, ExecutionEvidence, GroundingEvidence, Observation
from monster_heavy.proposal_agent import ProposalAgent, ProposalContext
from monster_heavy.proposal_model import Candidate, OpenAIProposalModel


@pytest.fixture
def context():
    return ProposalContext(
        portfolio_id=uuid4(),
        grounding=GroundingEvidence(
            id=uuid4(),
            observation=Observation(
                symbol="TEST",
                price=Decimal("10.12345678901234567890123456789"),
                currency="USD",
                source="trusted",
                observed_at=datetime.now(UTC),
            ),
        ),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def envelope(content):
    return dict(
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
                    content=content,
                    refusal=None,
                ),
            )
        ],
    )


def model_for(payload, requests, status=200):
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status, json=payload)

    client = OpenAI(
        api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(handle))
    )
    return OpenAIProposalModel(client, model="test-family", model_version="test-snapshot")


def run(payload, context, status=200):
    requests = []
    store = Mock(spec=["create"])
    model = model_for(payload, requests, status)
    agent = ProposalAgent(model, ProposalService(store))
    return agent, store, requests


def test_valid_candidate_binds_trusted_fields_and_exact_decimals(context):
    quantity = "1.123456789012345678901234567890123456789"
    agent, store, requests = run(
        envelope(json.dumps(dict(symbol="TEST", side="BUY", quantity=quantity))), context
    )
    with localcontext() as ctx:
        ctx.prec = 3
        assert agent.propose(context) is store.create.return_value
    recommendation, supersedes = store.create.call_args.args
    assert supersedes is None
    assert recommendation.terms.quantity == Decimal(quantity)
    assert recommendation.terms.reference_price == context.grounding.observation.price
    assert recommendation.terms.portfolio_id == context.portfolio_id
    assert recommendation.terms.expires_at == context.expires_at
    assert recommendation.grounding_evidence_id == context.grounding.id
    assert recommendation.provenance.model == "test-family"
    assert recommendation.provenance.model_version == "test-snapshot"
    assert recommendation.provenance.prompt_version == "proposal-v1"
    assert len(requests) == 1
    request = requests[0]
    assert "tools" not in request and "functions" not in request
    assert request["response_format"]["json_schema"]["strict"] is True
    schema = request["response_format"]["json_schema"]["schema"]
    assert set(schema["properties"]) == {"symbol", "side", "quantity"}
    assert schema["additionalProperties"] is False
    sent = json.loads(request["messages"][1]["content"])
    assert set(sent) == {"symbol", "price", "currency", "source", "observed_at"}
    assert not any(callable(value) for value in sent.values())
    assert str(context.portfolio_id) not in json.dumps(request)
    assert str(context.grounding.id) not in json.dumps(request)


@pytest.mark.parametrize(
    "field",
    [
        "portfolio_id",
        "grounding_evidence_id",
        "reference_price",
        "expires_at",
        "provenance",
        "model",
        "model_version",
        "prompt_version",
        "status",
        "approval",
        "policy",
        "portfolio",
        "execution_evidence",
        "execution_request",
        "sql",
        "supersedes_id",
    ],
)
def test_model_cannot_supply_authority_or_capabilities(context, field):
    payload = dict(symbol="TEST", side="BUY", quantity="2")
    payload[field] = "attacker-controlled"
    agent, store, _ = run(envelope(json.dumps(payload)), context)
    with pytest.raises(BoundaryError):
        agent.propose(context)
    store.create.assert_not_called()


@pytest.mark.parametrize(
    "quantity",
    [
        1.1,
        1,
        True,
        None,
        "0",
        "-1",
        "NaN",
        "Infinity",
        "1e3",
        " 2",
        "2_0",
        "1" * 1001,
    ],
)
def test_invalid_quantity_never_reaches_domain_or_store(context, quantity):
    agent, store, _ = run(
        envelope(json.dumps(dict(symbol="TEST", side="BUY", quantity=quantity))), context
    )
    with pytest.raises(BoundaryError):
        agent.propose(context)
    store.create.assert_not_called()


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "null",
        "[]",
        "{}",
        '{"symbol":"TEST","side":"BUY"}',
        '{"symbol":"TEST","side":"EXECUTE","quantity":"2"}',
        '{"symbol":"OTHER","side":"BUY","quantity":"2"}',
        '{"symbol":"test","side":"BUY","quantity":"2"}',
        '{"symbol":"TEST","side":"BUY","quantity":"2","quantity":"3"}',
    ],
)
def test_malformed_or_out_of_contract_output_fails_closed(context, content):
    agent, store, _ = run(envelope(content), context)
    with pytest.raises(BoundaryError):
        agent.propose(context)
    store.create.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        "refusal",
        "length",
        "content_filter",
        "tool_calls",
        "function_call",
        "missing",
        "multiple",
        "wrong_model",
        "missing_model",
        "wrong_role",
        "no_content",
    ],
)
def test_unexpected_envelope_fails_closed(context, change):
    payload = envelope('{"symbol":"TEST","side":"BUY","quantity":"2"}')
    choice = payload["choices"][0]
    if change == "refusal":
        choice["message"]["refusal"] = "No"
    elif change in ("length", "content_filter"):
        choice["finish_reason"] = change
    elif change == "tool_calls":
        choice["message"]["tool_calls"] = [
            dict(id="call", type="function", function=dict(name="approve", arguments="{}"))
        ]
    elif change == "function_call":
        choice["message"]["function_call"] = dict(name="execute", arguments="{}")
    elif change == "missing":
        payload["choices"] = []
    elif change == "multiple":
        payload["choices"] *= 2
    elif change == "wrong_model":
        payload["model"] = "unexpected"
    elif change == "missing_model":
        del payload["model"]
    elif change == "wrong_role":
        choice["message"]["role"] = "user"
    else:
        choice["message"]["content"] = None
    agent, store, _ = run(payload, context)
    with pytest.raises(BoundaryError):
        agent.propose(context)
    store.create.assert_not_called()


@pytest.mark.parametrize("status", [429, 500])
def test_api_failure_has_no_retry_or_write(context, status):
    agent, store, requests = run({"error": {"message": "failed"}}, context, status)
    with pytest.raises(BoundaryError):
        agent.propose(context)
    assert len(requests) == 1
    store.create.assert_not_called()


def test_construct_bypass_is_revalidated(context):
    model = model_for({}, [])
    model.propose = Mock(
        return_value=Candidate.model_construct(symbol="TEST", side="BUY", quantity=1.2)
    )
    store = Mock(spec=["create"])
    with pytest.raises(BoundaryError):
        ProposalAgent(model, ProposalService(store)).propose(context)
    store.create.assert_not_called()


def test_expired_and_future_context_refused_before_model(context):
    model = model_for({}, [])
    model.propose = Mock()
    agent = ProposalAgent(model, ProposalService(Mock(spec=["create"])))
    with pytest.raises(BoundaryError, match="ProposalExpired"):
        agent.propose(replace(context, expires_at=datetime.now(UTC)))
    future = replace(context.grounding.observation, observed_at=context.expires_at)
    with pytest.raises(BoundaryError, match="FutureEvidence"):
        agent.propose(replace(context, grounding=replace(context.grounding, observation=future)))
    model.propose.assert_not_called()
    with pytest.raises(ValueError, match="GroundingEvidenceRequired"):
        replace(
            context,
            grounding=ExecutionEvidence(
                id=context.grounding.id, observation=context.grounding.observation
            ),
        )


def test_model_code_has_no_application_or_database_imports():
    import monster_heavy.proposal_model as module

    tree = ast.parse(Path(module.__file__).read_text())
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imports == {"typing", "openai", "pydantic", "monster_heavy.domain"}
    assert {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } == {"json"}
    assert set(vars(model_for({}, []))) == {"_client", "provenance"}
    assert {name for name in vars(OpenAIProposalModel) if not name.startswith("_")} == {"propose"}
    assert {name for name in vars(ProposalAgent) if not name.startswith("_")} == {"propose"}


def test_candidate_validated_before_recommendation_construction(context, monkeypatch):
    import monster_heavy.proposal_agent as module

    constructor = Mock(side_effect=AssertionError("Domain construction must not happen"))
    monkeypatch.setattr(module, "Recommendation", constructor)
    agent, store, _ = run(envelope('{"symbol":"TEST","side":"BUY","quantity":"0"}'), context)
    with pytest.raises(BoundaryError):
        agent.propose(context)
    constructor.assert_not_called()
    store.create.assert_not_called()


def test_expiry_rechecked_after_model_latency(context, monkeypatch):
    import monster_heavy.proposal_agent as module

    class Clock:
        @staticmethod
        def now(zone):
            return context.expires_at

    model = model_for({}, [])

    def delayed(observation):
        monkeypatch.setattr(module, "datetime", Clock)
        return Candidate(symbol="TEST", side="BUY", quantity="2")

    model.propose = delayed
    store = Mock(spec=["create"])
    with pytest.raises(BoundaryError, match="ProposalExpired"):
        ProposalAgent(model, ProposalService(store)).propose(context)
    store.create.assert_not_called()


def test_missing_parsed_output_fails_closed(context):
    from types import SimpleNamespace

    client = Mock()
    client.with_options.return_value = client
    client.chat.completions.parse.return_value = SimpleNamespace(
        model="test-snapshot",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    role="assistant",
                    refusal=None,
                    tool_calls=None,
                    function_call=None,
                    content='{"symbol":"TEST","side":"BUY","quantity":"2"}',
                    parsed=None,
                ),
            )
        ],
    )
    store = Mock(spec=["create"])
    model = OpenAIProposalModel(client, model="test-family", model_version="test-snapshot")
    with pytest.raises(BoundaryError):
        ProposalAgent(model, ProposalService(store)).propose(context)
    store.create.assert_not_called()
