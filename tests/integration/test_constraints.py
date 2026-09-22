from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
from conftest import accepted, insert, new_request, open_db, seed
from psycopg.types.json import Jsonb

from monster_heavy.persistence import models

pytestmark = pytest.mark.integration


def test_unique_idempotency_key(db, data):
    with pytest.raises(psycopg.errors.UniqueViolation):
        new_request(db, data["proposal"]["id"], data["request"]["idempotency_key"])


def test_second_key_cannot_accept_same_proposal(db, data):
    accepted(db, data)
    other = new_request(db, data["proposal"]["id"])
    with pytest.raises(psycopg.errors.UniqueViolation, match="one_accepted"):
        accepted(db, data, other)


@pytest.mark.parametrize("table", ["policy_versions", "decision_ledger", "evidence", "approvals"])
@pytest.mark.parametrize("operation", ["update", "delete", "truncate", "upsert"])
def test_history_is_immutable_even_for_owner(db, data, table, operation):
    attempt = accepted(db, data)
    ledger = insert(db, "decision_ledger", attempt_id=attempt["id"], outcome="ACCEPTED")
    if table == "policy_versions":
        key, value, field, replacement = (
            "version",
            data["policy"]["version"],
            "publisher_id",
            "other",
        )
    elif table == "decision_ledger":
        key, value, field, replacement = "id", ledger["id"], "outcome", "ACCEPTED"
    elif table == "evidence":
        key, value, field, replacement = (
            "id",
            data["evidence"]["GROUNDING"]["id"],
            "source",
            "other",
        )
    else:
        key, value, field, replacement = "id", data["approval"]["id"], "rationale", "other"
    if operation == "update":
        query, params = f"UPDATE {table} SET {field}=%s WHERE {key}=%s", (replacement, value)
    elif operation == "delete":
        query, params = f"DELETE FROM {table} WHERE {key}=%s", (value,)
    elif operation == "truncate":
        query, params = f"TRUNCATE {table} CASCADE", ()
    else:
        query = (
            f"INSERT INTO {table} SELECT * FROM {table} WHERE {key}=%s "
            f"ON CONFLICT ({key}) DO UPDATE SET {field}=%s"
        )
        params = (value, replacement)
    with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
        db.execute(query, params)


def test_policy_versions_preserve_exact_reference(db, data):
    attempt = accepted(db, data)
    old = data["policy"]
    insert(
        db,
        "policy_versions",
        version=old["version"] + 1,
        rules=Jsonb({"maximum_order_notional": "1.00"}),
        effective_at=datetime.now(UTC),
        publisher_id="operator",
        publisher_role="policy_publisher",
    )
    row = db.execute(
        """SELECT p.rules, p.digest FROM policy_versions p
        JOIN execution_attempts a ON (a.policy_version, a.policy_digest) = (p.version, p.digest)
        WHERE a.id=%s""",
        (attempt["id"],),
    ).fetchone()
    assert row == {"rules": old["rules"], "digest": old["digest"]}
    assert (
        old["digest"]
        == db.execute(
            "SELECT encode(sha256(convert_to(%s::jsonb::text, 'UTF8')), 'hex') AS digest",
            (Jsonb(old["rules"]),),
        ).fetchone()["digest"]
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("symbol", "OTHER"),
        ("side", "SELL"),
        ("quantity", "3"),
        ("reference_price", "9.99"),
        ("terms_hash", "0" * 64),
    ],
)
def test_proposal_terms_cannot_change(db, data, field, value):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(f"UPDATE proposals SET {field}=%s WHERE id=%s", (value, data["proposal"]["id"]))


def test_approval_cannot_bind_wrong_hash(db, data):
    replacement = dict(data["proposal"])
    replacement.pop("id")
    replacement["model_provenance"] = Jsonb(replacement["model_provenance"])
    replacement = insert(db, "proposals", **replacement)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        insert(
            db,
            "approvals",
            proposal_id=replacement["id"],
            terms_hash="0" * 64,
            decision="APPROVED",
            actor_id="human",
            actor_role="approver",
            rationale="test",
            evidence_id=data["evidence"]["APPROVAL"]["id"],
        )


def test_grounding_cannot_be_execution_evidence(db, data):
    data["evidence"]["EXECUTION"] = data["evidence"]["GROUNDING"]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        accepted(db, data)


@pytest.mark.parametrize("operation", ["update", "delete", "truncate"])
def test_accepted_slot_cannot_be_reopened(db, data, operation):
    attempt = accepted(db, data)
    statements = {
        "update": "UPDATE execution_attempts SET status='VERIFYING', finished_at=NULL WHERE id=%s",
        "delete": "DELETE FROM execution_attempts WHERE id=%s",
        "truncate": "TRUNCATE execution_attempts CASCADE",
    }
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(statements[operation], (attempt["id"],) if operation != "truncate" else ())


def test_single_matching_ledger_outcome(db, data):
    attempt = accepted(db, data)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction():
        insert(db, "decision_ledger", attempt_id=attempt["id"], outcome="REJECTED")
    insert(db, "decision_ledger", attempt_id=attempt["id"], outcome="ACCEPTED")
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert(db, "decision_ledger", attempt_id=attempt["id"], outcome="ACCEPTED")


def test_exact_decimal_and_utc_round_trip(db):
    cash = Decimal("123456789012345678901234567890.12345678901234567890123456789")
    row = insert(
        db, "portfolios", cash=cash, currency="USD", updated_at="2026-09-22T12:00:00-05:00"
    )
    assert row["cash"] == cash
    assert isinstance(row["cash"], Decimal)
    assert row["updated_at"] == datetime(2026, 9, 22, 17, tzinfo=UTC)
    assert row["updated_at"].utcoffset().total_seconds() == 0
    assert models.Portfolio(**row).cash == cash


@pytest.mark.parametrize("cash", ["-1", "NaN", "Infinity", "-Infinity"])
def test_invalid_database_money(db, cash):
    with pytest.raises(psycopg.errors.CheckViolation):
        insert(db, "portfolios", cash=cash, currency="USD")


def test_all_timestamp_and_amount_columns_have_correct_types(db):
    columns = db.execute("""SELECT column_name, data_type, domain_name
        FROM information_schema.columns WHERE table_schema='monster_heavy'""").fetchall()
    timestamps = [r for r in columns if r["column_name"].endswith("_at")]
    assert len(timestamps) >= 15
    assert all(r["data_type"] == "timestamp with time zone" for r in timestamps)
    amounts = [r for r in columns if r["column_name"] in ("cash", "quantity", "reference_price")]
    assert len(amounts) == 4
    assert all(r["data_type"] == "numeric" for r in amounts)


def test_mismatched_policy_digest_rejected(db, data):
    data["policy"]["digest"] = "0" * 64
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        accepted(db, data)


def test_other_proposal_approval_cannot_authorize_attempt(db, data):
    other = seed(db)
    data["approval"] = other["approval"]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        accepted(db, data)


def test_rejected_attempt_is_durable_and_reconstructable(dsn):
    with open_db(dsn) as db:
        data = seed(db)
        before = db.execute(
            "SELECT * FROM portfolios WHERE id=%s", (data["portfolio"]["id"],)
        ).fetchone()
        attempt = insert(
            db,
            "execution_attempts",
            request_id=data["request"]["id"],
            proposal_id=data["proposal"]["id"],
            status="REJECTED",
            approval_id=data["approval"]["id"],
            execution_evidence_id=data["evidence"]["EXECUTION"]["id"],
            policy_version=data["policy"]["version"],
            policy_digest=data["policy"]["digest"],
            reason_code="StaleEvidence",
            finished_at=datetime.now(UTC),
        )
        ledger = insert(db, "decision_ledger", attempt_id=attempt["id"], outcome="REJECTED")
    with open_db(dsn) as db:
        row = db.execute(
            """SELECT p.terms_hash, a.reason_code, r.idempotency_key,
                e.kind, v.digest, h.actor_id
            FROM decision_ledger l
            JOIN execution_attempts a ON a.id=l.attempt_id
            JOIN execution_requests r ON r.id=a.request_id
            JOIN proposals p ON p.id=a.proposal_id
            JOIN approvals h ON h.id=a.approval_id
            JOIN evidence e ON e.id=a.execution_evidence_id
            JOIN policy_versions v ON v.version=a.policy_version
            WHERE l.id=%s""",
            (ledger["id"],),
        ).fetchone()
        assert row == dict(
            terms_hash=data["proposal"]["terms_hash"],
            reason_code="StaleEvidence",
            idempotency_key=data["request"]["idempotency_key"],
            kind="EXECUTION",
            digest=data["policy"]["digest"],
            actor_id="human",
        )
        assert (
            db.execute(
                "SELECT * FROM portfolios WHERE id=%s", (data["portfolio"]["id"],)
            ).fetchone()
            == before
        )
        # A rejection consumes neither the proposal nor the accepted-consequence slot.
        accepted(db, data, new_request(db, data["proposal"]["id"]))
