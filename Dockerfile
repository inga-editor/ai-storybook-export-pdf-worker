FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    UV_CACHE_DIR=/tmp/uv-cache \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN useradd -r -u 999 -m app

WORKDIR /app
RUN chown app: /app
USER app

# deps layer — cached until pyproject/uv.lock change; wheel cache lives in a build-cache mount, not the image
COPY --chown=app:app pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/tmp/uv-cache,uid=999 uv sync --frozen --no-install-project --no-dev

# Chromium headless-shell + system deps baked into the image at a fixed path —
# the browser pin dir tracks the playwright package in uv.lock, so a package
# bump invalidates the deps layer above and rebuilds this one with it: the
# binary can never go stale relative to the installed playwright version
# invoke the venv binary directly — `uv run` as root would seed /tmp/uv-cache
# root-owned into the layer and break runtime `uv run` for the app user
USER root
RUN /app/.venv/bin/playwright install --with-deps chromium-headless-shell \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/* /tmp/uv-cache
USER app

COPY --chown=app:app . .
RUN --mount=type=cache,target=/tmp/uv-cache,uid=999 uv sync --frozen --no-dev

EXPOSE 3203

# 127.0.0.1 + implicit single worker: in-memory queue/registry (ADR-055) — NEVER add --workers
CMD ["uv", "run", "--no-sync", "uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", "3203"]
