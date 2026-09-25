import psycopg
import pytest
from conftest import accepted, insert, open_db, seed
from psycopg.types.json import Jsonb

from monster_heavy.persistence import models
from monster_heavy.persistence.migrate import migrate

pytestmark = pytest.mark.integration


def test_transaction_rolls_back_all_consequence_records(dsn):
    with open_db(dsn) as conn:
        data = seed(conn)
    with pytest.raises(psycopg.errors.DivisionByZero), open_db(dsn, app=True) as conn:
        conn.execute("SELECT id FROM proposals WHERE id=%s FOR UPDATE", (data["proposal"]["id"],))
        conn.execute("SELECT id FROM portfolios WHERE id=%s FOR UPDATE", (data["portfolio"]["id"],))
        conn.execute(
            "UPDATE portfolios SET cash=cash-20.50 WHERE id=%s", (data["portfolio"]["id"],)
        )
        conn.execute(
            "UPDATE positions SET quantity=2 WHERE portfolio_id=%s", (data["portfolio"]["id"],)
        )
        conn.execute(
            "UPDATE proposals SET status='EXECUTED' WHERE id=%s", (data["proposal"]["id"],)
        )
        attempt = accepted(conn, data)
        ledger = insert(conn, "decision_ledger", attempt_id=attempt["id"], outcome="ACCEPTED")
        event = insert(
            conn,
            "outbox",
            ledger_id=ledger["id"],
            event_type="ExecutionAccepted",
            payload=Jsonb({"attempt_id": str(attempt["id"])}),
        )
        conn.execute("SELECT 1 / 0")
    with open_db(dsn) as conn:
        assert (
            conn.execute(
                "SELECT * FROM portfolios WHERE id=%s", (data["portfolio"]["id"],)
            ).fetchone()
            == data["portfolio"]
        )
        assert (
            conn.execute(
                "SELECT quantity FROM positions WHERE portfolio_id=%s", (data["portfolio"]["id"],)
            ).fetchone()["quantity"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT status FROM proposals WHERE id=%s", (data["proposal"]["id"],)
            ).fetchone()["status"]
            == "APPROVED"
        )
        for table, row in [
            ("execution_attempts", attempt),
            ("decision_ledger", ledger),
            ("outbox", event),
        ]:
            assert (
                conn.execute(f"SELECT * FROM {table} WHERE id=%s", (row["id"],)).fetchone() is None
            )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE decision_ledger SET outcome=outcome",
        "DELETE FROM decision_ledger",
        "TRUNCATE decision_ledger CASCADE",
        "ALTER TABLE decision_ledger DISABLE TRIGGER ALL",
        "DROP TABLE decision_ledger CASCADE",
        "UPDATE policy_versions SET rules=rules",
    ],
)
def test_runtime_role_cannot_mutate_or_disable_history(db, data, statement):
    attempt = accepted(db, data)
    insert(db, "decision_ledger", attempt_id=attempt["id"], outcome="ACCEPTED")
    db.execute("SET LOCAL ROLE monster_heavy_app")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db.execute(statement)


def test_all_row_models_match_migrated_schema(db, data):
    attempt = accepted(db, data)
    ledger = insert(db, "decision_ledger", attempt_id=attempt["id"], outcome="ACCEPTED")
    insert(db, "outbox", ledger_id=ledger["id"], event_type="ExecutionAccepted", payload=Jsonb({}))
    for table, model in [
        ("evidence", models.Evidence),
        ("policy_versions", models.PolicyVersion),
        ("portfolios", models.Portfolio),
        ("positions", models.Position),
        ("proposals", models.Proposal),
        ("approvals", models.Approval),
        ("execution_requests", models.ExecutionRequest),
        ("execution_attempts", models.ExecutionAttempt),
        ("decision_ledger", models.DecisionLedger),
        ("outbox", models.OutboxRecord),
    ]:
        row = db.execute(f"SELECT * FROM {table} LIMIT 1").fetchone()
        assert isinstance(model(**row), model)


def test_migration_is_repeatable_and_verified(dsn):
    migrate(dsn)
    migrate(dsn, check=True)
    from hashlib import sha256
    from importlib.resources import files

    expected = {
        path.name: sha256(path.read_bytes()).hexdigest()
        for path in files("monster_heavy.persistence").joinpath("migrations").iterdir()
        if path.name.endswith(".sql")
    }
    with open_db(dsn) as conn:
        actual = {
            row["name"]: row["checksum"]
            for row in conn.execute("SELECT name, checksum FROM public.schema_migrations")
        }
        assert actual == expected


def test_migration_checksum_mismatch_is_detected(dsn):
    # Restore even on failure: no modified migration history is left by this test.
    with open_db(dsn) as conn:
        original = conn.execute("SELECT name, checksum FROM public.schema_migrations").fetchall()
        conn.execute("UPDATE public.schema_migrations SET checksum='tampered'")
    try:
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            migrate(dsn, check=True)
    finally:
        with open_db(dsn) as conn:
            for row in original:
                conn.execute(
                    "UPDATE public.schema_migrations SET checksum=%s WHERE name=%s",
                    (row["checksum"], row["name"]),
                )
