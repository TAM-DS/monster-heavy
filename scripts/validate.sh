#!/bin/sh
set -eu
python -m compileall -q src tests
ruff check .
ruff format --check .
python -m monster_heavy.persistence.migrate
python -m monster_heavy.persistence.migrate --check
pytest
# Git is deliberately not required in the validation container.
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
    git diff --check
fi
