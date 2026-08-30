# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.11.6 AS uv

FROM python:3.12.13-slim-bookworm

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md .python-version ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-group build \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app \
    && chown -R app:app /app

USER app

EXPOSE 3070

CMD ["uv", "run", "--frozen", "--no-sync", "uvicorn", "oald10_easydict_service.service:app", "--host", "0.0.0.0", "--port", "3070"]
