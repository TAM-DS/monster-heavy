from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
from uuid import uuid4

import pytest

from monster_heavy.domain import BoundaryError, ExecutionEvidence, Observation, PolicyRules, Terms
from monster_heavy.execution import evaluate, exact

D = Decimal
NOW = datetime.now(UTC)
RULES = PolicyRules(
    allowed_actions=("BUY", "SELL"),
    allowed_symbols=("TEST",),
    maximum_order_notional=D(100),
    maximum_resulting_position=D(10),
    maximum_execution_evidence_age_seconds=60,
    maximum_price_drift=D("0.05"),
    maximum_proposal_age_seconds=3600,
)
TERMS = Terms(
    portfolio_id=uuid4(),
    symbol="TEST",
    side="BUY",
    quantity=D(2),
    reference_price=D(10),
    expires_at=NOW + timedelta(hours=1),
)
EVIDENCE = ExecutionEvidence(
    id=uuid4(),
    observation=Observation(
        symbol="TEST", price=D(10), currency="USD", source="unit", observed_at=NOW
    ),
)


def run(
    terms=TERMS,
    evidence=EVIDENCE,
    rules=RULES,
    cash=Decimal(100),
    quantity=Decimal(3),
    created_at=NOW,
    now=NOW,
):
    return evaluate(terms, evidence, rules, created_at, now, "USD", cash, quantity)


@pytest.mark.parametrize("value", ["0", "-0.0001", "123456789.123456789", "100000", "0.125"])
def test_exact_conversion(value):
    with localcontext() as ctx:
        ctx.prec = 2
        assert exact(Fraction(D(value))) == D(value)


@pytest.mark.parametrize("side,cash,quantity", [("BUY", "80", "5"), ("SELL", "120", "1")])
def test_portfolio_arithmetic(side, cash, quantity):
    result = run(terms=replace(TERMS, side=side))
    assert (result.cash, result.quantity, result.notional) == (D(cash), D(quantity), D(20))


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"evidence": None}, "ExecutionEvidenceRequired"),
        ({"cash": D(19)}, "InsufficientCash"),
        ({"quantity": D(9)}, "ResultingPositionExceeded"),
        ({"terms": replace(TERMS, side="SELL"), "quantity": D(1)}, "InsufficientPosition"),
        ({"terms": replace(TERMS, expires_at=NOW)}, "ProposalExpired"),
        ({"created_at": NOW - timedelta(hours=1)}, "ProposalPolicyAgeExceeded"),
        ({"created_at": NOW + timedelta(seconds=1)}, "FutureProposal"),
        ({"rules": replace(RULES, allowed_actions=("SELL",))}, "DisallowedAction"),
        ({"rules": replace(RULES, allowed_symbols=("OTHER",))}, "DisallowedSymbol"),
        ({"rules": replace(RULES, maximum_order_notional=D(19))}, "OrderNotionalExceeded"),
    ],
)
def test_refusals(changes, reason):
    with pytest.raises(BoundaryError, match=reason):
        run(**changes)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"symbol": "OTHER"}, "EvidenceSymbolMismatch"),
        ({"currency": "EUR"}, "EvidenceCurrencyMismatch"),
        ({"price": D("10.50000001")}, "PriceDriftExceeded"),
        ({"price": D("9.49999999")}, "PriceDriftExceeded"),
        ({"observed_at": NOW - timedelta(seconds=60, microseconds=1)}, "StaleEvidence"),
        ({"observed_at": NOW + timedelta(microseconds=1)}, "FutureEvidence"),
    ],
)
def test_evidence_refusals(changes, reason):
    with pytest.raises(BoundaryError, match=reason):
        run(evidence=replace(EVIDENCE, observation=replace(EVIDENCE.observation, **changes)))


@pytest.mark.parametrize("price", ["9.5", "10.5"])
def test_inclusive_evidence_limits(price):
    assert run(
        evidence=replace(
            EVIDENCE,
            observation=replace(
                EVIDENCE.observation, price=D(price), observed_at=NOW - timedelta(seconds=60)
            ),
        )
    )


def test_actual_price_not_reference_notional():
    evidence = replace(EVIDENCE, observation=replace(EVIDENCE.observation, price=D("9.5")))
    assert run(evidence=evidence, rules=replace(RULES, maximum_order_notional=D(19)))
    with pytest.raises(BoundaryError, match="OrderNotionalExceeded"):
        run(
            evidence=replace(EVIDENCE, observation=replace(EVIDENCE.observation, price=D("10.5"))),
            rules=replace(RULES, maximum_order_notional=D(20)),
        )


def test_exact_cash_and_position_limits_and_sell_to_zero():
    assert run(cash=D(20), quantity=D(8)).cash == 0
    assert run(terms=replace(TERMS, side="SELL"), quantity=D(2)).quantity == 0
    # A sale reducing a previously oversized position must reach the current limit.
    assert run(terms=replace(TERMS, side="SELL"), quantity=D(12)).quantity == 10
    with pytest.raises(BoundaryError, match="ResultingPositionExceeded"):
        run(terms=replace(TERMS, side="SELL"), quantity=D(13))


def test_arithmetic_independent_of_decimal_context():
    terms = replace(TERMS, quantity=D("0.1234567890123456789"))
    with localcontext() as ctx:
        ctx.prec = 2
        result = run(terms=terms)
    assert result.cash == D("98.765432109876543211")
    assert result.quantity == D("3.1234567890123456789")
