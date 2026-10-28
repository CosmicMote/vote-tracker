# syntax=docker/dockerfile:1
#
# Legislator Vote Tracker: the web app (UI + API + scheduler) in one image.
#   docker build -t vote-tracker .
# See "Running in Docker" in README.md for running it.

# --- Stage 1: build the React UI ------------------------------------------------
FROM node:22.11.0-bookworm-slim AS ui
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# vite.config.ts writes the build to ../src/tracker/web/static
RUN npm run build

# --- Stage 2: the Python app ------------------------------------------------------
FROM python:3.12.12-slim-bookworm

# Same uv version that writes uv.lock, so the lockfile format always matches.
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app
# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY --from=ui /build/src/tracker/web/static ./src/tracker/web/static
RUN uv sync --frozen --no-dev
COPY config.example.toml ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Run as an unprivileged user. All state lives in /data: config.toml, tracker.db, digests/, logs/.
RUN useradd --uid 1000 --create-home tracker \
    && mkdir -p /data && chown tracker:tracker /data \
    && chmod 755 /usr/local/bin/docker-entrypoint.sh
USER tracker
WORKDIR /data
VOLUME /data

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    TRACKER_CONFIG=/data/config.toml \
    API_PATH=""

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; p = os.environ.get('API_PATH', '').strip('/'); urllib.request.urlopen('http://127.0.0.1:8000' + ('/' + p if p else '') + '/api/weeks', timeout=4)"]

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["tracker", "serve", "--host", "0.0.0.0", "--port", "8000"]
