"""Adversarial SQL sessions must not redirect privileged audit operations."""

import psycopg
import pytest
from conftest import open_db, seed

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("operation", ["replay", "claim"])
def test_temporary_table_cannot_capture_privileged_audit_writes(dsn, operation):
    with open_db(dsn) as conn:
        data = seed(conn)
    request_id = data["request"]["id"]
    with open_db(dsn, app=True) as conn:
        conn.execute(
            "CREATE TEMP TABLE control_events (request_id uuid, kind text, worker_owner text)"
        )
        if operation == "replay":
            conn.execute(
                "SELECT monster_heavy.record_replay(%s, 'SUBMISSION_REPLAY')", (request_id,)
            )
            expected = "SUBMISSION_REPLAY"
        else:
            conn.execute(
                "UPDATE monster_heavy.execution_requests SET lease_owner='security-test', "
                "lease_expires_at=clock_timestamp() + interval '30 seconds' WHERE id=%s",
                (request_id,),
            )
            expected = "CLAIM"
        assert conn.execute("SELECT count(*) AS n FROM pg_temp.control_events").fetchone()["n"] == 0
        rows = conn.execute(
            "SELECT kind FROM monster_heavy.control_events WHERE request_id=%s", (request_id,)
        ).fetchall()
        assert rows == [{"kind": expected}]


def test_temporary_attempt_cannot_forge_execution_replay(dsn):
    with open_db(dsn) as conn:
        data = seed(conn)
    with open_db(dsn, app=True) as conn:
        conn.execute("CREATE TEMP TABLE execution_attempts (request_id uuid, status text)")
        conn.execute(
            "INSERT INTO pg_temp.execution_attempts VALUES (%s, 'ACCEPTED')",
            (data["request"]["id"],),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="Replay requires terminal result"):
            conn.execute(
                "SELECT monster_heavy.record_replay(%s, 'EXECUTION_REPLAY')",
                (data["request"]["id"],),
            )


def test_privileged_functions_have_safe_resolution_and_grants(dsn):
    with open_db(dsn) as conn:
        rows = conn.execute(
            "SELECT p.proname, p.proconfig FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='monster_heavy' AND p.prosecdef ORDER BY p.proname"
        ).fetchall()
        assert {r["proname"] for r in rows} == {"record_claim", "record_replay"}
        for row in rows:
            assert "search_path=pg_catalog, monster_heavy, pg_temp" in row["proconfig"]
        assert not conn.execute(
            "SELECT has_function_privilege('monster_heavy_audit', "
            "'monster_heavy.record_replay(uuid,text)', 'EXECUTE') AS allowed"
        ).fetchone()["allowed"]
