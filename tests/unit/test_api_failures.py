"""Audit transport failures must not be mistaken for absent evidence."""

from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from monster_heavy.api import create_app
from monster_heavy.persistence.audit import AuditStore

PATHS = ["/ready", "/metrics", f"/attempts/{uuid4()}"]


@pytest.mark.parametrize("path", PATHS)
def test_missing_configuration_is_unavailable(monkeypatch, path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"status": "alive"}
        response = client.get(path)
    assert response.status_code == 503
    assert response.json() == {"detail": "Database not ready"}


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("error", [psycopg.OperationalError, psycopg.errors.QueryCanceled])
def test_database_failures_are_sanitized(monkeypatch, path, error):
    def unavailable(*args):
        raise error("password=private-test-value host=internal-test-host")

    for name in ("ready", "metrics", "reconstruct"):
        monkeypatch.setattr(AuditStore, name, unavailable)
    with TestClient(create_app("unused")) as client:
        response = client.get(path)
    assert response.status_code == 503
    assert response.json() == {"detail": "Database not ready"}
    assert "private-test-value" not in response.text
    assert "internal-test-host" not in response.text


def test_programming_errors_are_not_reported_as_database_outages(monkeypatch):
    def broken(*args):
        raise TypeError("implementation defect")

    monkeypatch.setattr(AuditStore, "metrics", broken)
    with TestClient(create_app("unused")) as client:
        with pytest.raises(TypeError, match="implementation defect"):
            client.get("/metrics")
