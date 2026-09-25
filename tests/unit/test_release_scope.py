"""Deterministic release-scope checks: one AI agent and the locked paper-only stack."""

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_locked_dependencies_and_single_proposal_agent():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = {dependency.split(">=")[0] for dependency in project["dependencies"]}
    assert declared == {"fastapi", "uvicorn", "openai", "psycopg[binary]", "pydantic"}
    agents = []
    for path in (ROOT / "src/monster_heavy").rglob("*.py"):
        tree = ast.parse(path.read_text())
        agents.extend(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name.endswith("Agent")
        )
    assert agents == ["ProposalAgent"]


def test_audit_and_worker_do_not_acquire_model_or_broker_capabilities():
    for name in (
        "api.py",
        "worker.py",
        "execution.py",
        "persistence/audit.py",
        "persistence/worker.py",
        "persistence/compensation.py",
        "persistence/execution.py",
    ):
        tree = ast.parse((ROOT / "src/monster_heavy" / name).read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module.split(".")[0])
        assert set(imports) <= {
            "collections",
            "contextlib",
            "dataclasses",
            "datetime",
            "decimal",
            "fractions",
            "hashlib",
            "importlib",
            "os",
            "uuid",
            "fastapi",
            "psycopg",
            "monster_heavy",
        }
        assert all(
            "proposal_agent" not in node.module and "proposal_model" not in node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
