from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest

from monster_heavy.application import ApprovalService, PolicyService, ProposalService
from monster_heavy.domain import (
    Actor,
    BoundaryError,
    ExecutionEvidence,
    GroundingEvidence,
    Observation,
    PolicyRules,
    Provenance,
    Recommendation,
    Terms,
    eligible,
    verify_evidence,
    verify_policy_terms,
)
from monster_heavy.persistence.boundaries import decimal_string

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def terms(**changes):
    values = dict(
        portfolio_id=uuid4(),
        symbol="TEST",
        side="BUY",
        quantity=Decimal("2"),
        reference_price=Decimal("10"),
        expires_at=NOW + timedelta(hours=1),
    )
    return Terms(**(values | changes))


def rules(**changes):
    values = dict(
        allowed_actions=("SELL", "BUY"),
        allowed_symbols=("TEST",),
        maximum_order_notional=Decimal("1000"),
        maximum_resulting_position=Decimal("100"),
        maximum_execution_evidence_age_seconds=60,
        maximum_price_drift=Decimal("0.05"),
        maximum_proposal_age_seconds=3600,
    )
    return PolicyRules(**(values | changes))


def observation(**changes):
    return Observation(
        **(
            dict(
                symbol="TEST",
                price=Decimal("10"),
                currency="USD",
                source="trusted_fixture",
                observed_at=NOW,
            )
            | changes
        )
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("portfolio_id", "not-a-uuid"),
        ("symbol", "test"),
        ("symbol", ""),
        ("side", "HOLD"),
        ("quantity", 1.0),
        ("quantity", 1),
        ("quantity", "1"),
        ("quantity", Decimal("NaN")),
        ("quantity", Decimal("Infinity")),
        ("quantity", Decimal("0")),
        ("reference_price", Decimal("-1")),
        ("expires_at", datetime(2026, 1, 1)),
    ],
)
def test_invalid_terms(field, value):
    with pytest.raises(ValueError):
        terms(**{field: value})


def test_terms_immutable_and_utc():
    value = terms(expires_at=NOW.astimezone(timezone(timedelta(hours=5))))
    assert value.expires_at.tzinfo is UTC
    with pytest.raises(FrozenInstanceError):
        value.quantity = Decimal("3")


@pytest.mark.parametrize(
    "field,value",
    [
        ("allowed_actions", ["BUY"]),
        ("allowed_actions", ("BUY", "BUY")),
        ("allowed_actions", ("HOLD",)),
        ("allowed_symbols", ()),
        ("allowed_symbols", ("test",)),
        ("maximum_order_notional", 1.0),
        ("maximum_resulting_position", Decimal("0")),
        ("maximum_execution_evidence_age_seconds", True),
        ("maximum_execution_evidence_age_seconds", 0),
        ("maximum_proposal_age_seconds", -1),
        ("maximum_price_drift", Decimal("NaN")),
        ("maximum_price_drift", Decimal("1.01")),
        ("maximum_price_drift", 0.05),
    ],
)
def test_invalid_policy(field, value):
    with pytest.raises(ValueError):
        rules(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", 1.0),
        ("currency", "usd"),
        ("source", " "),
        ("observed_at", NOW.replace(tzinfo=None)),
    ],
)
def test_invalid_observation(field, value):
    with pytest.raises(ValueError):
        observation(**{field: value})


@pytest.mark.parametrize(
    "status", ["PENDING", "APPROVED", "REJECTED", "EXPIRED", "SUPERSEDED", "EXECUTED"]
)
@pytest.mark.parametrize("pending_only", [True, False])
def test_lifecycle_eligibility(status, pending_only):
    allowed = ("PENDING",) if pending_only else ("PENDING", "APPROVED")
    if status in allowed:
        eligible(status, NOW + timedelta(seconds=1), NOW, pending_only=pending_only)
    else:
        with pytest.raises(BoundaryError, match="IneligibleProposal"):
            eligible(status, NOW + timedelta(seconds=1), NOW, pending_only=pending_only)


@pytest.mark.parametrize("seconds", [-1, 0])
def test_expiry_is_inclusive(seconds):
    with pytest.raises(BoundaryError, match="ProposalExpired"):
        eligible("APPROVED", NOW + timedelta(seconds=seconds), NOW)


@pytest.mark.parametrize(
    "age,price,reason",
    [
        (60, "10.5", None),
        (61, "10", "StaleEvidence"),
        (-1, "10", "FutureEvidence"),
        (0, "10.5000000000000000000000000001", "PriceDriftExceeded"),
        (0, "9.4999999999999999999999999999", "PriceDriftExceeded"),
        (0, "9.5", None),
    ],
)
def test_freshness_and_exact_drift(age, price, reason):
    evidence = ExecutionEvidence(
        id=uuid4(),
        observation=observation(price=Decimal(price), observed_at=NOW - timedelta(seconds=age)),
    )
    with localcontext() as context:
        context.prec = 2
        if reason:
            with pytest.raises(BoundaryError, match=reason):
                verify_evidence(terms(), evidence, rules(), NOW)
        else:
            verify_evidence(terms(), evidence, rules(), NOW)


def test_grounding_is_never_execution_evidence():
    with pytest.raises(BoundaryError, match="ExecutionEvidenceRequired"):
        verify_evidence(
            terms(), GroundingEvidence(id=uuid4(), observation=observation()), rules(), NOW
        )


def test_evidence_symbol_must_match():
    with pytest.raises(BoundaryError, match="EvidenceSymbolMismatch"):
        verify_evidence(
            terms(),
            ExecutionEvidence(id=uuid4(), observation=observation(symbol="OTHER")),
            rules(),
            NOW,
        )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"allowed_actions": ("SELL",)}, "DisallowedAction"),
        ({"allowed_symbols": ("OTHER",)}, "DisallowedSymbol"),
        (
            {"maximum_order_notional": Decimal("19.999999999999999999999999")},
            "OrderNotionalExceeded",
        ),
        ({"maximum_proposal_age_seconds": 1}, "ProposalPolicyAgeExceeded"),
    ],
)
def test_stricter_policy_blocks_terms(changes, reason):
    with pytest.raises(BoundaryError, match=reason), localcontext() as context:
        context.prec = 2
        verify_policy_terms(terms(), rules(**changes), NOW - timedelta(seconds=1), NOW)


def test_policy_term_limits_inclusive():
    verify_policy_terms(terms(), rules(maximum_order_notional=Decimal("20")), NOW, NOW)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("1.000", "1"),
        ("1E+4", "10000"),
        ("0.00000100", "0.000001"),
        ("123456789.12345678900", "123456789.123456789"),
    ],
)
def test_decimal_canonicalization_does_not_round(value, expected):
    with localcontext() as context:
        context.prec = 2
        assert decimal_string(Decimal(value)) == expected


def test_validated_structured_input_only():
    with pytest.raises(ValueError, match="InvalidStructuredRecommendation"):
        ProposalService(object()).create({"side": "BUY"})
    with pytest.raises(ValueError):
        Recommendation(
            terms=terms(),
            grounding_evidence_id="bad",
            provenance=Provenance(model="fixture", model_version="1", prompt_version="1"),
        )


def test_unauthorized_roles_never_reach_store():
    actor = Actor(id="model", role="proposer")
    with pytest.raises(BoundaryError, match="UnauthorizedRole"):
        ApprovalService(object()).approve(uuid4(), "a" * 64, actor, "reason")
    with pytest.raises(BoundaryError, match="UnauthorizedRole"):
        PolicyService(object()).publish(rules(), None, actor)


def test_proposal_service_requires_only_create_capability():
    class OnlyCreate:
        def create(self, recommendation, supersedes_id):
            return recommendation, supersedes_id

    value = Recommendation(
        terms=terms(),
        grounding_evidence_id=uuid4(),
        provenance=Provenance(model="fixture", model_version="1", prompt_version="1"),
    )
    assert ProposalService(OnlyCreate()).create(value) == (value, None)
    assert replace(rules(), allowed_actions=("BUY", "SELL")) == rules()


def test_evidence_reference_is_validated_and_immutable():
    with pytest.raises(ValueError, match="InvalidEvidenceReference"):
        ExecutionEvidence(id="not-a-uuid", observation=observation())
    value = ExecutionEvidence(id=uuid4(), observation=observation())
    with pytest.raises(FrozenInstanceError):
        value.observation.price = Decimal("100")


def test_public_capabilities_are_separate():
    from monster_heavy.persistence.boundaries import (
        ApprovalStore,
        ExecutionEvidenceStore,
        GroundingStore,
        LifecycleStore,
        PolicyPublicationStore,
        PolicyReadStore,
        ProposalStore,
    )

    expected = {
        ProposalStore: {"create"},
        ApprovalStore: {"decide"},
        LifecycleStore: {"expire"},
        GroundingStore: {"grounding"},
        ExecutionEvidenceStore: {"execution", "get"},
        PolicyPublicationStore: {"publish"},
        PolicyReadStore: {"active", "at", "get"},
    }
    for adapter, operations in expected.items():
        assert {name for name in dir(adapter) if not name.startswith("_")} == operations


def test_domain_and_application_do_not_import_mutation_infrastructure():
    import ast
    from pathlib import Path

    root = Path(__file__).parents[2] / "src" / "monster_heavy"
    for path in (root / "domain.py", root / "application" / "__init__.py"):
        tree = ast.parse(path.read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        assert not any(
            name.startswith(
                (
                    "psycopg",
                    "monster_heavy.persistence.database",
                    "monster_heavy.persistence.boundaries",
                )
            )
            for name in imports
        )
