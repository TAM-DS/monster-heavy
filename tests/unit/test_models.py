from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from monster_heavy.persistence.models import Evidence, Portfolio


def portfolio(**changes):
    values = dict(
        id=uuid4(), cash=Decimal("0.1"), currency="USD", revision=0, updated_at=datetime.now(UTC)
    )
    return Portfolio(**(values | changes))


def test_decimal_and_utc():
    when = datetime(2026, 1, 1, 12, tzinfo=timezone(timedelta(hours=-6)))
    row = portfolio(updated_at=when)
    assert row.cash == Decimal("0.1")
    assert row.updated_at == datetime(2026, 1, 1, 18, tzinfo=UTC)
    assert row.updated_at.tzinfo is UTC


@pytest.mark.parametrize("cash", [0.1, 1, "0.1", Decimal("NaN"), Decimal("Infinity")])
def test_invalid_money(cash):
    with pytest.raises((TypeError, ValueError)):
        portfolio(cash=cash)


def test_naive_timestamp_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        portfolio(updated_at=datetime(2026, 1, 1))


def test_json_float_rejected():
    with pytest.raises(TypeError, match="floating point"):
        Evidence(
            id=uuid4(),
            kind="CONSEQUENCE",
            source="test",
            payload={"cash": [0.1]},
            observed_at=datetime.now(UTC),
            recorded_at=datetime.now(UTC),
        )
