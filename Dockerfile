FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY config.toml ./
COPY src ./src

RUN useradd --create-home --uid 10001 autodoist \
    && mkdir -p /app/data \
    && chown -R autodoist:autodoist /app/data

USER autodoist
EXPOSE 8000

CMD ["uvicorn", "src.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
