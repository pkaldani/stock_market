# Multi-target Dockerfile for the "Traders" app (see CLAUDE.md). Build context is the
# repo root, since pyproject.toml/uv.lock live there while the app code is under 6_mcp/.
#
# Three images come out of this one file — always build with --target:
#   docker build --target api      -t traders-api      .
#   docker build --target engine   -t traders-engine   .
#   docker build --target frontend -t traders-frontend .

# ---------------------------------------------------------------------------
# base: shared Python environment (uv + the project's dependencies)
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

# Dependency layer first so it's cached independently of app source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY 6_mcp ./6_mcp
RUN uv sync --frozen --no-dev

COPY 6_mcp/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /app/6_mcp
ENTRYPOINT ["entrypoint.sh"]

# ---------------------------------------------------------------------------
# api: read-only FastAPI backend (backend/api.py). No MCP subprocesses, no Node.
# ---------------------------------------------------------------------------
FROM base AS api

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------------------
# engine: the trading loop (backend/trading_floor.py). Its researcher sub-agent
# spawns MCP servers over npx/uvx, so this target needs Node.js on top of uv.
# ---------------------------------------------------------------------------
FROM base AS engine

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/*

CMD ["uv", "run", "-m", "backend.trading_floor"]

# ---------------------------------------------------------------------------
# frontend-build: compiles the TypeScript dashboard to static assets.
# ---------------------------------------------------------------------------
FROM node:22-slim AS frontend-build

WORKDIR /app
COPY 6_mcp/frontend/package.json 6_mcp/frontend/package-lock.json ./
RUN npm ci
COPY 6_mcp/frontend ./
RUN npm run build

# ---------------------------------------------------------------------------
# frontend: static assets behind nginx, proxying /api to the backend Service.
# ---------------------------------------------------------------------------
FROM nginx:1.27-alpine AS frontend

COPY --from=frontend-build /app/dist /usr/share/nginx/html
COPY 6_mcp/frontend/nginx.conf.template /etc/nginx/templates/default.conf.template
ENV BACKEND_UPSTREAM=trading-floor-api:8000

EXPOSE 80
