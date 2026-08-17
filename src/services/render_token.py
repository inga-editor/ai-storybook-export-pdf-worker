"""HS256 render-token service for `/api/share/get-render-preview`.

SOURCE-OF-TRUTH for the `RenderTokenClaims` contract shared between the verifier
(this endpoint — Plan A) and the export-PDF job signer (Plan C, `api/jobs/06`).
Both `sign_render_token` and `verify_render_token` live here so the claims shape
can only drift in one place.

Secret loading is FAIL-FAST AT IMPORT (plan Validation Session 1): an empty
`PRINT_RENDER_TOKEN_SECRET` raises at module load so misconfiguration surfaces
at boot/deploy, NOT on the first request. The secret is read through pydantic
`settings` (which loads `.env` + os.environ), so the project's standard env
convention applies. Tests set the env var BEFORE the first settings import (see
`tests/conftest.py` top-level `os.environ.setdefault`).

Never log token plaintext or the secret.
"""

from __future__ import annotations

from typing import Literal

import jwt
from pydantic import BaseModel

from src.config.settings import settings

_ALGORITHM = "HS256"
# Signer (job 06) and verifier (this service) may run on different hosts; allow
# a small clock-skew window so a freshly-signed token isn't read as expired at
# the start of a per-spread render. Small relative to the 600s exp window.
_LEEWAY_SEC = 30


def _load_secret() -> str:
    secret = settings.print_render_token_secret
    if not secret:
        raise RuntimeError(
            "PRINT_RENDER_TOKEN_SECRET is not set — required to sign/verify "
            "render tokens. Set it in the environment / .env (shared secret "
            "with the export-PDF job signer, api/jobs/06)."
        )
    return secret


# Fail-fast at import — see module docstring (plan Validation Session 1).
_SECRET = _load_secret()


class RenderTokenClaims(BaseModel):
    """Render-token claims. `remix_id` required only when `source='remix'`
    (handler resolves the constraint; model keeps it optional so book-mode
    tokens omit the field entirely via `exclude_none` on sign)."""

    source: Literal["book", "remix"]
    book_id: str
    remix_id: str | None = None
    spread_id: str
    edition: Literal["classic", "dynamic", "interactive"]
    language: str
    bleed_mm: float
    exp: int  # unix epoch


class RenderTokenInvalidError(Exception):
    """Bad signature / malformed / missing-claim token → 401 invalid_token."""


class RenderTokenExpiredError(Exception):
    """Token `exp` is in the past → 401 token_expired."""


def sign_render_token(claims: RenderTokenClaims) -> str:
    """Encode claims as an HS256 render token.

    CONTRACT (Plan C, `api/jobs/06`): sign ONE token PER SPREAD, immediately
    before that spread is rendered, with `exp = now + 600s`. Do NOT sign a
    single token for an entire book — a long sequential headless render would
    outlive a book-wide `exp`. Re-signing per spread keeps the replay window
    short (600s).
    """
    payload = claims.model_dump(exclude_none=True)  # drop remix_id when None
    return jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)


def verify_render_token(token: str) -> RenderTokenClaims:
    """Decode + verify an HS256 render token.

    Raises:
        RenderTokenExpiredError — `exp` < now (PyJWT auto-checks `exp`).
        RenderTokenInvalidError — empty / malformed / bad-signature / missing
            required claims.
    """
    if not token or not isinstance(token, str):
        raise RenderTokenInvalidError("empty token")
    try:
        payload = jwt.decode(
            token, _SECRET, algorithms=[_ALGORITHM], leeway=_LEEWAY_SEC
        )
    except jwt.ExpiredSignatureError as exc:
        raise RenderTokenExpiredError(str(exc)) from exc
    except jwt.PyJWTError as exc:
        raise RenderTokenInvalidError(str(exc)) from exc
    try:
        return RenderTokenClaims(**payload)
    except Exception as exc:  # pydantic ValidationError → missing/bad-shape claims
        raise RenderTokenInvalidError(str(exc)) from exc
