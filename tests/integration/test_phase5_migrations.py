"""Clean install and populated forward upgrade, each in an isolated PostgreSQL database."""

from hashlib import sha256
from importlib.resources import files
from uuid import uuid4

import pytest
from conftest import accepted, insert, seed
from psycopg import sql
from psycopg.conninfo import make_conninfo

from monster_heavy.persistence.audit import AuditStore
from monster_heavy.persistence.database import connect
from monster_heavy.persistence.migrate import migrate

pytestmark = pytest.mark.integration


@pytest.fixture
def isolated_database(dsn):
    name = "phase5_migration_" + uuid4().hex
    with connect(dsn) as admin:
        admin.autocommit = True
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = make_conninfo(dsn, dbname=name)
    try:
        yield target
    finally:
        with connect(dsn) as admin:
            admin.autocommit = True
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def foundation(dsn):
    path = files("monster_heavy.persistence").joinpath("migrations/0001_foundation.sql")
    source = path.read_text()
    with connect(dsn) as conn:
        conn.execute(
            "CREATE TABLE public.schema_migrations (name text PRIMARY KEY, "
            "checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(source)
        conn.execute(
            "INSERT INTO public.schema_migrations(name,checksum) VALUES (%s,%s)",
            (path.name, sha256(source.encode()).hexdigest()),
        )


def history(dsn):
    with connect(dsn) as conn:
        return conn.execute("SELECT * FROM public.schema_migrations ORDER BY name").fetchall()


@pytest.mark.parametrize("populated", [False, True])
def test_forward_migration_preserves_foundation_and_history(isolated_database, populated):
    dsn = isolated_database
    old = None
    if populated:
        foundation(dsn)
        with connect(dsn) as conn:
            conn.execute("SET search_path = monster_heavy, pg_catalog")
            data = seed(conn)
            data["proposal"] = conn.execute(
                "SELECT * FROM proposals WHERE id=%s", (data["proposal"]["id"],)
            ).fetchone()
            attempt = accepted(conn, data)
            ledger = insert(conn, "decision_ledger", attempt_id=attempt["id"], outcome="ACCEPTED")
        old = history(dsn)
        with pytest.raises(RuntimeError, match="Pending migration"):
            migrate(dsn, check=True)
    migrate(dsn)
    first = history(dsn)
    migrate(dsn)
    migrate(dsn, check=True)
    assert history(dsn) == first
    expected = {
        path.name: sha256(path.read_bytes()).hexdigest()
        for path in files("monster_heavy.persistence").joinpath("migrations").iterdir()
        if path.name.endswith(".sql")
    }
    assert {row["name"]: row["checksum"] for row in first} == expected
    assert len(first) == 2
    if populated:
        assert first[0] == old[0]
        with connect(dsn) as conn:
            for table, row in (
                ("execution_attempts", attempt),
                ("decision_ledger", ledger),
                ("proposals", data["proposal"]),
                ("portfolios", data["portfolio"]),
            ):
                stored = conn.execute(
                    sql.SQL("SELECT * FROM monster_heavy.{} WHERE id=%s").format(
                        sql.Identifier(table)
                    ),
                    (row["id"],),
                ).fetchone()
                if table == "proposals":
                    assert stored.pop("origin") == "MODEL"
                assert stored == row
    assert AuditStore(dsn).ready() == {"status": "ready"}


@pytest.mark.parametrize("corruption", ["checksum", "unknown", "missing"])
def test_phase5_migration_history_fails_closed(isolated_database, corruption):
    dsn = isolated_database
    migrate(dsn)
    with connect(dsn) as conn:
        if corruption == "checksum":
            conn.execute(
                "UPDATE public.schema_migrations SET checksum='tampered' "
                "WHERE name='0002_recovery_release.sql'"
            )
        elif corruption == "unknown":
            conn.execute(
                "INSERT INTO public.schema_migrations(name,checksum) "
                "VALUES ('unknown.sql','invalid')"
            )
        else:
            conn.execute(
                "DELETE FROM public.schema_migrations WHERE name='0002_recovery_release.sql'"
            )
    with pytest.raises(RuntimeError):
        migrate(dsn, check=True)
    with pytest.raises(RuntimeError):
        AuditStore(dsn).ready()
