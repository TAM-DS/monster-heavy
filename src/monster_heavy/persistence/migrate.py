"""Transactional, checksum-verified SQL migrations serialized by a PostgreSQL lock."""

import argparse
from hashlib import sha256
from importlib.resources import files

from monster_heavy.persistence.database import connect


def migrate(dsn: str | None = None, *, check: bool = False) -> None:
    migrations = sorted(
        (
            p
            for p in files("monster_heavy.persistence").joinpath("migrations").iterdir()
            if p.name.endswith(".sql")
        ),
        key=lambda p: p.name,
    )
    with connect(dsn) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(72640101)")
        exists = conn.execute("SELECT to_regclass('public.schema_migrations') AS name").fetchone()
        if not exists["name"]:
            if check:
                raise RuntimeError("Database has not been migrated")
            conn.execute("""
                CREATE TABLE public.schema_migrations (
                    name text PRIMARY KEY,
                    checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
        applied = {
            r["name"]: r["checksum"]
            for r in conn.execute("SELECT name, checksum FROM public.schema_migrations")
        }
        known = {p.name for p in migrations}
        if applied.keys() - known:
            raise RuntimeError("Database contains unknown migrations")
        for path in migrations:
            source = path.read_text(encoding="utf-8")
            checksum = sha256(source.encode()).hexdigest()
            if path.name in applied:
                if applied[path.name] != checksum:
                    raise RuntimeError(f"Migration checksum mismatch: {path.name}")
                continue
            if check:
                raise RuntimeError(f"Pending migration: {path.name}")
            conn.execute(source)
            conn.execute(
                "INSERT INTO public.schema_migrations (name, checksum) VALUES (%s, %s)",
                (path.name, checksum),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify migration history without DDL")
    args = parser.parse_args()
    migrate(check=args.check)


if __name__ == "__main__":
    main()
