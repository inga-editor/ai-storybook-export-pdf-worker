"""Headless render + raster→PDF helpers for the export-PDF job (`api/jobs/06`).

PHASE 2 (capture) + crop + PHASE 3 (merge) primitives:
  - `capture_spread`    — Playwright/Chromium @ device_scale_factor=1 → full-page
                          PNG. The print PAGE produces 300 DPI via PRINT_RENDER_ZOOM
                          (editor 75-DPI baseline ×4); a 1:1 screenshot IS 300 DPI.
                          DSF MUST stay 1 — DSF=2 would stack to 600 DPI (4× memory,
                          OOM, dpi:300 metadata lie).
  - `crop_dps_halves`   — center-split a full-bleed DPS PNG into left/right halves
                          (page_unit='single'). Bleed falls correctly: outer edges
                          keep bleed, the spine (center) is no-bleed.
  - `png_to_pdf`        — Pillow RGB raster → single-page PDF with DPI metadata.
  - `merge_pdfs`        — pypdf concat per-spread PDFs in order → page count.

Blocking Pillow/pypdf calls are wrapped in `asyncio.to_thread` by the caller
(handler); the pure functions here stay sync. `capture_spread` is natively async
(Playwright async API).

Font dependency (Validation S1): the print route (Plan B) loads editor fonts via
`@font-face` + `<link rel=preload>` and gates `window.__PRINT_READY__` on
`document.fonts.ready`. This service does NOT bundle fonts — a missing manifest
surfaces as FONTS_NOT_READY (the ready flag never flips).
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageFile

logger = logging.getLogger(__name__)

# Decompression-bomb guard (image-api CLAUDE.md). Full-bleed DPS @300 DPI is
# large (a 13" trim ≈ 3900px/side); cap generously above legitimate raster size
# so a runaway zoom/malformed print page cannot OOM the host silently.
Image.MAX_IMAGE_PIXELS = 80_000_000  # ~ (8900px)^2 headroom over largest trim
ImageFile.LOAD_TRUNCATED_IMAGES = False

# Chromium launch flags — headless, no sandbox (container), bounded memory.
_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]
_NAV_TIMEOUT_MS = 60_000
_READY_TIMEOUT_MS = 30_000


class RenderError(Exception):
    """Per-spread render failure. `code` ∈ {RENDER_TIMEOUT, RENDER_CRASH,
    FONTS_NOT_READY}; isolated by the handler (failed_count++, continue)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def capture_spread(
    print_url: str,
    *,
    nav_timeout_ms: int = _NAV_TIMEOUT_MS,
    ready_timeout_ms: int = _READY_TIMEOUT_MS,
) -> bytes:
    """Render `print_url` headless and return a full-page PNG (300 DPI via zoom).

    Spawns a fresh Chromium per call (memory-bounded, failure-isolated). Raises
    `RenderError` on navigation timeout / ready-gate timeout / browser crash.
    """
    # Imported lazily so test envs without the Chromium binary still import the
    # module (pytest mocks this function).
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        except PlaywrightError as exc:  # launch/crash
            raise RenderError("RENDER_CRASH", f"chromium launch failed: {exc}") from exc
        try:
            context = await browser.new_context(device_scale_factor=1)
            page = await context.new_page()
            try:
                await page.goto(print_url, wait_until="networkidle", timeout=nav_timeout_ms)
            except PlaywrightTimeoutError as exc:
                raise RenderError("RENDER_TIMEOUT", "navigation timeout") from exc
            try:
                await page.wait_for_function(
                    "() => window.__PRINT_READY__ === true", timeout=ready_timeout_ms
                )
            except PlaywrightTimeoutError as exc:
                raise RenderError(
                    "FONTS_NOT_READY", "__PRINT_READY__ not set (fonts/images not loaded)"
                ) from exc
            png = await page.screenshot(full_page=True, type="png")
            return png
        except PlaywrightError as exc:  # crash mid-render
            raise RenderError("RENDER_CRASH", f"chromium error: {exc}") from exc
        finally:
            await browser.close()


def crop_dps_halves(png_bytes: bytes) -> tuple[bytes, bytes]:
    """Center-split a full-bleed DPS PNG into (left, right) halves.

    Odd widths give the left half the extra column (`W//2`), so left+right cover
    the full raster with no gap/overlap."""
    with Image.open(io.BytesIO(png_bytes)) as img:
        img.load()
        w, h = img.size
        mid = w // 2
        left = img.crop((0, 0, mid, h))
        right = img.crop((mid, 0, w, h))
        return _png_bytes(left), _png_bytes(right)


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def png_to_pdf(png_bytes: bytes, dpi: int, out_path: str) -> str:
    """Write a single-page PDF from a PNG raster, stamping `dpi` metadata.

    Flattens any alpha onto white (PDF has no straightforward alpha); RGB raster
    keeps file size bounded vs CMYK at this stage (color conversion is Phase 4)."""
    with Image.open(io.BytesIO(png_bytes)) as img:
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            rgb = background
        else:
            rgb = img.convert("RGB")
        rgb.save(out_path, format="PDF", resolution=float(dpi))
    return out_path


def merge_pdfs(pdf_paths: list[str], out_path: str) -> int:
    """Concat per-spread PDFs in order → merged PDF. Returns total page count."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    try:
        for path in pdf_paths:
            writer.append(path)
        with open(out_path, "wb") as fh:
            writer.write(fh)
        return len(writer.pages)
    finally:
        writer.close()
