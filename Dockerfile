FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.4 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv lock --check && uv sync --frozen --extra dev
ENV PATH="/app/.venv/bin:$PATH"
COPY tests ./tests
COPY scripts ./scripts
CMD ["monster-heavy-migrate"]
