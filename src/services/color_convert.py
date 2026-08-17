"""CMYK ICC color conversion + PDF/X assembly for export-PDF (`api/jobs/06`).

CMYK is the DEFAULT export mode. Pipeline (per page raster):
  1. `resolve_icc`        — ICC chain: customer url (SSRF-guarded, ≤1MB) →
                            bundled id → DEFAULT_ICC_PROFILE_ID (FOGRA39).
  2. `png_to_cmyk_pdf`    — Pillow ImageCms (littlecms, MIT) RGB→CMYK transform
                            (RelativeColorimetric + black-point compensation) →
                            single-page CMYK PDF.
  3. `assemble_pdfx`      — pikepdf embeds the ICC as an OutputIntent + PDF/X
                            conformance keys on the merged PDF.

RGB opt-out (`color_mode='rgb'`): `resolve_icc` returns `icc_bytes=None`; the
handler uses `pdf_render.png_to_pdf` (RGB) and skips `assemble_pdfx`.

Blocking Pillow/pikepdf calls are wrapped in `asyncio.to_thread` by the handler.
`resolve_icc` is async (customer ICC is a network fetch).
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from PIL import Image, ImageCms

from src.config.settings import settings
from src.services.icc_registry import (
    BundledIccProfile,
    get_profile,
    load_bundled_icc,
)
from src.services.ssrf_guard import validate_public_url

logger = logging.getLogger(__name__)

_ICC_FETCH_MAX_BYTES = 1_048_576  # 1 MB cap on customer ICC
_ICC_FETCH_TIMEOUT_S = 20.0

# ImageCms rendering-intent map (string from registry → PIL flag).
_INTENT_MAP = {
    "RelativeColorimetric": ImageCms.Intent.RELATIVE_COLORIMETRIC,
    "Perceptual": ImageCms.Intent.PERCEPTUAL,
    "Saturation": ImageCms.Intent.SATURATION,
    "AbsoluteColorimetric": ImageCms.Intent.ABSOLUTE_COLORIMETRIC,
}


class IccFetchError(Exception):
    """Customer `icc_profile_url` unfetchable/invalid → handler color-stage fail
    (code INVALID_ICC_PROFILE)."""


class ColorConversionError(Exception):
    """ImageCms transform / pikepdf assembly failure (code COLOR_CONVERSION_ERROR)."""


@dataclass
class IccResolution:
    """Resolved ICC inputs. `icc_bytes is None` ⇒ RGB (no conversion)."""

    icc_bytes: bytes | None
    source: str | None  # 'customer' | 'bundled' | 'default' | None
    profile_id: str | None
    profile: BundledIccProfile | None


async def resolve_icc(params: dict[str, Any]) -> IccResolution:
    """Resolve the ICC profile per the priority chain.

    Raises:
        IccFetchError — customer url SSRF-blocked / fetch fail / oversize.
        UnknownIccProfileError / IccProfileUnavailableError — bundled lookup
            (propagated; handler maps to color-stage error).
    """
    if params.get("color_mode") != "cmyk":
        return IccResolution(icc_bytes=None, source=None, profile_id=None, profile=None)

    icc_url = params.get("icc_profile_url")
    if icc_url:
        icc_bytes = await _fetch_customer_icc(str(icc_url))
        return IccResolution(icc_bytes=icc_bytes, source="customer", profile_id=None, profile=None)

    profile_id = params.get("icc_profile_id") or settings.default_icc_profile_id
    source = "bundled" if params.get("icc_profile_id") else "default"
    profile = get_profile(profile_id)  # raises UnknownIccProfileError
    icc_bytes = load_bundled_icc(profile)  # raises IccProfileUnavailableError
    return IccResolution(icc_bytes=icc_bytes, source=source, profile_id=profile_id, profile=profile)


async def _fetch_customer_icc(url: str) -> bytes:
    """SSRF-guarded fetch of a customer ICC profile (≤1MB).

    Redirects are DISABLED (`follow_redirects=False`) — `validate_public_url`
    only guards the initial host, so a public→private 3xx hop would bypass SSRF
    (CLAUDE.md: "check mọi redirect hop"). A redirect response is rejected; the
    caller must supply a direct URL. The size cap is enforced BOTH via the
    `Content-Length` header (pre-read) and the streamed body length (a lying/
    absent header cannot stream an unbounded body from an internal service)."""
    try:
        validate_public_url(url)
    except Exception as exc:  # HTTPException SSRF_BLOCKED
        raise IccFetchError(f"ICC url blocked: {exc}") from exc
    try:
        async with httpx.AsyncClient(
            timeout=_ICC_FETCH_TIMEOUT_S, follow_redirects=False
        ) as client:
            async with client.stream("GET", url) as resp:
                if 300 <= resp.status_code < 400:
                    raise IccFetchError("ICC url redirected (disallowed — give a direct URL)")
                resp.raise_for_status()
                declared = resp.headers.get("content-length")
                if declared is not None and int(declared) > _ICC_FETCH_MAX_BYTES:
                    raise IccFetchError(f"ICC profile too large: {declared} > {_ICC_FETCH_MAX_BYTES}")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _ICC_FETCH_MAX_BYTES:
                        raise IccFetchError(
                            f"ICC profile too large: > {_ICC_FETCH_MAX_BYTES}"
                        )
                    chunks.append(chunk)
            data = b"".join(chunks)
    except IccFetchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise IccFetchError(f"ICC fetch failed: {exc}") from exc
    if not data:
        raise IccFetchError("empty ICC profile")
    return data


def png_to_cmyk_pdf(
    png_bytes: bytes,
    icc_bytes: bytes,
    dpi: int,
    out_path: str,
    *,
    rendering_intent: str = "RelativeColorimetric",
    black_point: bool = True,
) -> str:
    """RGB PNG → CMYK PDF page via ImageCms ICC transform. Returns `out_path`.

    Black-point compensation keeps near-black neutral; full K-only mapping of
    pure RGB(0,0,0) is best-effort (warning 'gamut clipped' acceptable v1)."""
    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                rgb = bg
            else:
                rgb = img.convert("RGB")

        src_profile = ImageCms.createProfile("sRGB")
        dst_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_bytes))
        intent = _INTENT_MAP.get(rendering_intent, ImageCms.Intent.RELATIVE_COLORIMETRIC)
        flags = (
            ImageCms.Flags.BLACKPOINTCOMPENSATION if black_point else ImageCms.Flags.NONE
        )
        transform = ImageCms.buildTransform(
            src_profile, dst_profile, "RGB", "CMYK", renderingIntent=intent, flags=flags
        )
        cmyk = ImageCms.applyTransform(rgb, transform)
        cmyk.save(out_path, format="PDF", resolution=float(dpi))
        return out_path
    except ColorConversionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ColorConversionError(f"CMYK transform failed: {exc}") from exc


def assemble_pdfx(
    pdf_path: str,
    icc_bytes: bytes,
    *,
    output_intent: str,
    output_condition_id: str,
) -> None:
    """Embed the ICC as a PDF/X OutputIntent + conformance keys (in place).

    v1 sets OutputIntent + GTS_PDFXVersion; full PDF/X-1a structural conformance
    (no transparency, fonts embedded) is validated externally (veraPDF, future)."""
    try:
        import pikepdf

        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            icc_stream = pdf.make_stream(icc_bytes)
            icc_stream.N = 4  # CMYK component count (required for DestOutputProfile)

            output_intent_obj = pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name("/OutputIntent"),
                    S=pikepdf.Name("/GTS_PDFX"),
                    OutputConditionIdentifier=pikepdf.String(output_condition_id),
                    Info=pikepdf.String(output_condition_id),
                    DestOutputProfile=icc_stream,
                )
            )
            pdf.Root.OutputIntents = pikepdf.Array([output_intent_obj])

            version = "PDF/X-1:2001" if output_intent == "pdfx1a" else "PDF/X-4"
            with pdf.open_metadata() as meta:
                meta["pdfxid:GTS_PDFXVersion"] = version

            # PDF/X-1a:2001 (ISO 15930-1) identifies via the Document Info dict, NOT
            # only XMP — strict preflight (Acrobat/callas/veraPDF) reads these keys.
            # GTS_PDFXVersion + Trapped are mandatory; GTS_PDFXConformance names the
            # X-1a subset. (X-4 leans on XMP but Trapped is still required.)
            pdf.docinfo["/GTS_PDFXVersion"] = pikepdf.String(version)
            if output_intent == "pdfx1a":
                pdf.docinfo["/GTS_PDFXConformance"] = pikepdf.String("PDF/X-1a:2001")
            pdf.docinfo["/Trapped"] = pikepdf.Name("/False")
            pdf.save(pdf_path)
    except Exception as exc:  # noqa: BLE001
        raise ColorConversionError(f"PDF/X assembly failed: {exc}") from exc
