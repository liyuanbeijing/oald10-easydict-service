# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

FROM python:3.12.13-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

COPY --from=uv /uv /uvx /bin/

# Install dependencies first for optimal Docker layer caching
COPY pyproject.toml uv.lock README.md .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-group build --no-install-project

# Copy application source code and install the package
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-group build --no-editable


FROM python:3.12.13-slim-bookworm AS runner

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Create non-root system user
RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app app

# Copy prepared virtualenv with all dependencies and installed package
COPY --from=builder --chown=app:app /app/.venv /app/.venv

USER app

EXPOSE 33070

CMD ["uvicorn", "oald10_easydict_service.service:app", "--host", "0.0.0.0", "--port", "33070"]
