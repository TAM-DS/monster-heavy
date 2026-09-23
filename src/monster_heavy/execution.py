"""Pure deterministic paper-portfolio arithmetic; no model or I/O capabilities."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

from monster_heavy.domain import BoundaryError, verify_evidence


def exact(value: Fraction) -> Decimal:
    """Convert a terminating rational without consulting the Decimal context."""
    numerator, denominator = value.numerator, value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        twos += 1
        denominator //= 2
    while denominator % 5 == 0:
        fives += 1
        denominator //= 5
    if denominator != 1:
        raise ValueError("NonTerminatingDecimal")
    scale = max(twos, fives)
    numerator *= 2 ** (scale - twos) * 5 ** (scale - fives)
    return Decimal((int(numerator < 0), tuple(map(int, str(abs(numerator)))), -scale))


@dataclass(frozen=True)
class PortfolioChange:
    cash: Decimal
    quantity: Decimal
    notional: Decimal


def evaluate(terms, evidence, rules, created_at, now, currency, cash, quantity):
    if terms.expires_at <= now:
        raise BoundaryError("ProposalExpired")
    age = now - created_at
    if age < timedelta(0):
        raise BoundaryError("FutureProposal")
    if age >= timedelta(seconds=rules.maximum_proposal_age_seconds):
        raise BoundaryError("ProposalPolicyAgeExceeded")
    verify_evidence(terms, evidence, rules, now)
    if evidence.observation.currency != currency:
        raise BoundaryError("EvidenceCurrencyMismatch")
    if terms.side not in rules.allowed_actions:
        raise BoundaryError("DisallowedAction")
    if terms.symbol not in rules.allowed_symbols:
        raise BoundaryError("DisallowedSymbol")
    notional = Fraction(terms.quantity) * Fraction(evidence.observation.price)
    if notional > Fraction(rules.maximum_order_notional):
        raise BoundaryError("OrderNotionalExceeded")
    if terms.side == "BUY":
        if notional > Fraction(cash):
            raise BoundaryError("InsufficientCash")
        new_cash = Fraction(cash) - notional
        new_quantity = Fraction(quantity) + Fraction(terms.quantity)
    else:
        if terms.quantity > quantity:
            raise BoundaryError("InsufficientPosition")
        new_cash = Fraction(cash) + notional
        new_quantity = Fraction(quantity) - Fraction(terms.quantity)
    if new_quantity > Fraction(rules.maximum_resulting_position):
        raise BoundaryError("ResultingPositionExceeded")
    return PortfolioChange(exact(new_cash), exact(new_quantity), exact(notional))
