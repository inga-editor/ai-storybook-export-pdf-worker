"""Pytest fixtures + env bootstrap for the Export-PDF Worker.

CRITICAL: env is set at the TOP, BEFORE any `src` import — `render_token` loads
`PRINT_RENDER_TOKEN_SECRET` fail-fast AT IMPORT, and `settings` is a module-global
constructed at import (mirrors the python-api / storage-service conftests).
"""

import os

# --- env BEFORE src imports -------------------------------------------------
os.environ.setdefault("PRINT_RENDER_TOKEN_SECRET", "test-render-token-secret-0123456789abcdef")
os.environ.setdefault("EXPORT_PDF_WORKER_TOKEN", "test-worker-token")
os.environ.setdefault("STORAGE_SERVICE_URL", "http://127.0.0.1:8200")
os.environ.setdefault("STORAGE_SERVICE_API_KEY", "test-storage-key")
os.environ.setdefault("STORAGE_PUBLIC_BASE_URL", "http://127.0.0.1:8200")
os.environ.setdefault("QUEUE_MAX", "4")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.jobs import registry  # noqa: E402
from src.main import app  # noqa: E402

WORKER_TOKEN = "test-worker-token"
AUTH = {"X-Worker-Token": WORKER_TOKEN}


@pytest.fixture
def client():
    """`TestClient(app)` WITHOUT the context manager → lifespan does NOT run, so the
    queue consumer never spawns. Submitted jobs stay `queued` (no Chromium needed);
    tests that need the pipeline drive `run_job` directly."""
    return TestClient(app)


@pytest.fixture
def auth() -> dict:
    return dict(AUTH)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Clear the module-global job registry + drain the FIFO queue between tests so
    in-memory state never leaks across tests."""
    yield
    registry._JOBS.clear()
    while not registry._QUEUE.empty():
        try:
            registry._QUEUE.get_nowait()
        except Exception:  # noqa: BLE001
            break
