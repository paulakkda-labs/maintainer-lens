# Node is a build dependency only; application services keep a Python runtime.
FROM node:22-slim AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY web ./
RUN npm run build

# A shared Python 3.12 image for the API, dispatcher, worker, and migration job.
FROM python:3.12-slim

# Pin uv to avoid an unexpected tool upgrade during image builds.
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/

ENV UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# Separate dependency installation from source changes for build caching.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --locked --no-dev --no-editable

COPY --from=web-build /web/dist /app/web/dist

RUN groupadd --system maintainerlens && useradd --system --no-create-home --gid maintainerlens maintainerlens
USER maintainerlens

# The compose services set their own commands.
CMD ["uvicorn", "maintainer_lens.main:app", "--host", "0.0.0.0", "--port", "8000"]
