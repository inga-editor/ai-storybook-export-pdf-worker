"""Unit tests for `src/services/color_convert.py` — ICC chain + PDF/X assembly.

`png_to_cmyk_pdf` real ImageCms transform with a real FOGRA39 binary is validated
at the Final Step (binary is a license-vet ship blocker); here we cover the
resolution chain (mocked binary), RGB opt-out, error paths, and the real pikepdf
OutputIntent assembly."""

from __future__ import annotations

import io
import os
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from src.services import color_convert
from src.services.color_convert import (
    ColorConversionError,
    IccFetchError,
    assemble_pdfx,
    png_to_cmyk_pdf,
    resolve_icc,
)
from src.services.icc_registry import UnknownIccProfileError
from src.services.pdf_render import png_to_pdf


class TestResolveIccChain:
    async def test_rgb_mode_returns_no_icc(self):
        res = await resolve_icc({"color_mode": "rgb"})
        assert res.icc_bytes is None
        assert res.source is None

    async def test_cmyk_default_uses_fogra39(self, monkeypatch):
        monkeypatch.setattr(color_convert, "load_bundled_icc", lambda profile: b"ICCDATA")
        res = await resolve_icc({"color_mode": "cmyk"})
        assert res.icc_bytes == b"ICCDATA"
        assert res.source == "default"
        assert res.profile_id == "fogra39"
        assert res.profile["condition"] == "FOGRA39"

    async def test_cmyk_bundled_id(self, monkeypatch):
        monkeypatch.setattr(color_convert, "load_bundled_icc", lambda profile: b"X")
        res = await resolve_icc({"color_mode": "cmyk", "icc_profile_id": "fogra39"})
        assert res.source == "bundled"
        assert res.profile_id == "fogra39"

    async def test_cmyk_unknown_id_raises(self):
        with pytest.raises(UnknownIccProfileError):
            await resolve_icc({"color_mode": "cmyk", "icc_profile_id": "nope"})

    async def test_customer_url_precedence(self, monkeypatch):
        fetch = AsyncMock(return_value=b"CUSTOMER")
        monkeypatch.setattr(color_convert, "_fetch_customer_icc", fetch)
        # load_bundled_icc must NOT be reached when url present
        monkeypatch.setattr(color_convert, "load_bundled_icc",
                            lambda p: (_ for _ in ()).throw(AssertionError("bundled used")))
        res = await resolve_icc({
            "color_mode": "cmyk",
            "icc_profile_url": "https://cdn.example.com/p.icc",
            "icc_profile_id": "fogra39",
        })
        assert res.icc_bytes == b"CUSTOMER"
        assert res.source == "customer"
        fetch.assert_awaited_once()


class _FakeStreamResp:
    def __init__(self, *, status_code=200, headers=None, chunks=(b"ICC",)):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks

    async def __aenter__(self): return self
    async def __aexit__(self, *a): ...
    def raise_for_status(self): ...
    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _FakeClient:
    def __init__(self, resp): self._resp = resp
    async def __aenter__(self): return self
    async def __aexit__(self, *a): ...
    def stream(self, method, url): return self._resp


def _patch_client(monkeypatch, resp):
    monkeypatch.setattr(color_convert, "validate_public_url", lambda u: u)
    monkeypatch.setattr(color_convert.httpx, "AsyncClient", lambda **k: _FakeClient(resp))


class TestFetchCustomerIcc:
    async def test_ssrf_block_raises(self, monkeypatch):
        def _block(url):
            raise RuntimeError("SSRF_BLOCKED")
        monkeypatch.setattr(color_convert, "validate_public_url", _block)
        with pytest.raises(IccFetchError):
            await color_convert._fetch_customer_icc("http://169.254.169.254/x")

    async def test_redirect_rejected(self, monkeypatch):
        # C1 regression: a 3xx hop must be rejected (no follow_redirects bypass).
        _patch_client(monkeypatch, _FakeStreamResp(status_code=302))
        with pytest.raises(IccFetchError):
            await color_convert._fetch_customer_icc("https://ok.example.com/p.icc")

    async def test_oversize_header_raises(self, monkeypatch):
        _patch_client(monkeypatch, _FakeStreamResp(
            headers={"content-length": str(color_convert._ICC_FETCH_MAX_BYTES + 1)}))
        with pytest.raises(IccFetchError):
            await color_convert._fetch_customer_icc("https://ok.example.com/p.icc")

    async def test_oversize_streamed_body_raises(self, monkeypatch):
        # lying/absent content-length → streamed length still bounded
        big = b"x" * (color_convert._ICC_FETCH_MAX_BYTES + 10)
        _patch_client(monkeypatch, _FakeStreamResp(chunks=(big,)))
        with pytest.raises(IccFetchError):
            await color_convert._fetch_customer_icc("https://ok.example.com/p.icc")

    async def test_happy_fetch(self, monkeypatch):
        _patch_client(monkeypatch, _FakeStreamResp(chunks=(b"IC", b"CD")))
        data = await color_convert._fetch_customer_icc("https://ok.example.com/p.icc")
        assert data == b"ICCD"


class TestPngToCmykPdf:
    def test_invalid_icc_raises_color_error(self, tmp_path):
        buf = io.BytesIO()
        Image.new("RGB", (40, 40), (10, 20, 30)).save(buf, "PNG")
        with pytest.raises(ColorConversionError):
            png_to_cmyk_pdf(buf.getvalue(), b"not-a-real-icc", 300,
                            str(tmp_path / "x.pdf"))


class TestAssemblePdfx:
    def test_embeds_output_intent(self, tmp_path):
        buf = io.BytesIO()
        Image.new("RGB", (40, 40), (10, 20, 30)).save(buf, "PNG")
        pdf_path = str(tmp_path / "a.pdf")
        png_to_pdf(buf.getvalue(), 300, pdf_path)
        assemble_pdfx(pdf_path, b"\x00" * 200, output_intent="pdfx1a",
                      output_condition_id="FOGRA39")
        import pikepdf
        with pikepdf.open(pdf_path) as pdf:
            oi = pdf.Root.OutputIntents[0]
            assert str(oi.S) == "/GTS_PDFX"
            assert str(oi.OutputConditionIdentifier) == "FOGRA39"
            assert int(oi.DestOutputProfile.N) == 4
            # PDF/X-1a:2001 identification lives in the Info dict, not only XMP.
            assert str(pdf.docinfo["/GTS_PDFXVersion"]) == "PDF/X-1:2001"
            assert str(pdf.docinfo["/GTS_PDFXConformance"]) == "PDF/X-1a:2001"
            assert str(pdf.docinfo["/Trapped"]) == "/False"
        assert os.path.getsize(pdf_path) > 0
