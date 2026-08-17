"""Unit tests for `src/services/pdf_render.py` — real Pillow/pypdf, no browser.

`capture_spread` (Playwright) is NOT exercised here (mocked at the handler level)
— it requires a live Chromium + print route. These cover the deterministic raster
→ PDF primitives."""

from __future__ import annotations

import io
import os

from PIL import Image

from src.services.pdf_render import (
    crop_dps_halves,
    merge_pdfs,
    png_to_pdf,
)


def _png(w: int, h: int, color=(200, 30, 30, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


class TestCropDpsHalves:
    def test_even_width_split(self):
        left, right = crop_dps_halves(_png(100, 60))
        li = Image.open(io.BytesIO(left))
        ri = Image.open(io.BytesIO(right))
        assert li.size == (50, 60)
        assert ri.size == (50, 60)

    def test_odd_width_left_gets_extra(self):
        # W=101 → mid=50 → left 50, right 51, no gap/overlap
        left, right = crop_dps_halves(_png(101, 40))
        li = Image.open(io.BytesIO(left))
        ri = Image.open(io.BytesIO(right))
        assert li.size[0] + ri.size[0] == 101


class TestPngToPdf:
    def test_writes_single_page_pdf(self, tmp_path):
        out = str(tmp_path / "a.pdf")
        png_to_pdf(_png(80, 80), 300, out)
        assert os.path.getsize(out) > 0
        from pypdf import PdfReader
        assert len(PdfReader(out).pages) == 1

    def test_rgba_flattened_no_error(self, tmp_path):
        out = str(tmp_path / "b.pdf")
        # semi-transparent → must flatten onto white without raising
        png_to_pdf(_png(40, 40, (0, 0, 255, 128)), 300, out)
        assert os.path.getsize(out) > 0


class TestMergePdfs:
    def test_merge_preserves_page_count_and_order(self, tmp_path):
        p1 = str(tmp_path / "1.pdf")
        p2 = str(tmp_path / "2.pdf")
        png_to_pdf(_png(60, 60), 300, p1)
        png_to_pdf(_png(60, 60), 300, p2)
        merged = str(tmp_path / "m.pdf")
        count = merge_pdfs([p1, p2], merged)
        assert count == 2

    def test_single_pdf_merge(self, tmp_path):
        p1 = str(tmp_path / "1.pdf")
        png_to_pdf(_png(60, 60), 300, p1)
        merged = str(tmp_path / "m.pdf")
        assert merge_pdfs([p1], merged) == 1
