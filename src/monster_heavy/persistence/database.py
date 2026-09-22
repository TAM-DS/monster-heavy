"""Connections preserve Decimal values and return timestamps in UTC."""

import os

import psycopg
from psycopg.rows import dict_row


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(
        dsn or os.environ["DATABASE_URL"], options="-c timezone=UTC", row_factory=dict_row
    )
