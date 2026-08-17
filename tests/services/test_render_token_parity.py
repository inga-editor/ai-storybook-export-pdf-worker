"""Render-token parity: the worker SIGNS a per-spread token; python-api's
`get-render-preview` VERIFIES it with the SAME `PRINT_RENDER_TOKEN_SECRET`. The
`render_token.py` module is a byte-for-byte copy of python-api's, so signing here
and verifying with the copied verifier proves the claims contract + secret align
(the FE print page + verifier stay 0-line-changed)."""

from __future__ import annotations

import time

import jwt
import pytest

from src.services.render_token import (
    RenderTokenClaims,
    RenderTokenExpiredError,
    RenderTokenInvalidError,
    sign_render_token,
    verify_render_token,
)

_TTL = 600


def _claims(**over) -> RenderTokenClaims:
    base = dict(source="book", book_id="book-1", spread_id="sp0",
                edition="classic", language="en_US", bleed_mm=3.0,
                exp=int(time.time()) + _TTL)
    base.update(over)
    return RenderTokenClaims(**base)


class TestSignVerifyParity:
    def test_book_token_roundtrips_full_claims(self):
        token = sign_render_token(_claims())
        decoded = verify_render_token(token)
        assert decoded.source == "book"
        assert decoded.book_id == "book-1"
        assert decoded.spread_id == "sp0"
        assert decoded.edition == "classic"
        assert decoded.language == "en_US"
        assert decoded.bleed_mm == 3.0
        assert decoded.remix_id is None

    def test_book_token_omits_remix_id(self):
        # exclude_none on sign → the encoded payload has no remix_id key.
        token = sign_render_token(_claims())
        raw = jwt.decode(token, options={"verify_signature": False})
        assert "remix_id" not in raw

    def test_remix_token_carries_remix_id(self):
        token = sign_render_token(_claims(source="remix", remix_id="remix-9"))
        decoded = verify_render_token(token)
        assert decoded.source == "remix"
        assert decoded.remix_id == "remix-9"

    def test_ttl_is_600s(self):
        before = int(time.time())
        claims = _claims()
        assert _TTL - 2 <= claims.exp - before <= _TTL + 2

    def test_expired_token_rejected(self):
        token = sign_render_token(_claims(exp=int(time.time()) - 3600))
        with pytest.raises(RenderTokenExpiredError):
            verify_render_token(token)

    def test_tampered_token_rejected(self):
        token = sign_render_token(_claims())
        with pytest.raises(RenderTokenInvalidError):
            verify_render_token(token + "tamper")
