"""Pydantic models for the worker HTTP contract (`01-http-contract.md` §2).

`ExportPdfWorkerRequest` is a SELF-CONTAINED render plan — python-api resolved
everything from the DB (the worker reads no DB). The request carries no spread
CONTENT (that flows through the print page); only `spread_id` (token claim +
progress key) and `is_dps` (the one thing the worker can't self-derive without a
DB — drives the `single` raster split).

`extra="ignore"` on purpose: python-api owns this S2S contract and may add fields
ahead of a worker deploy — an unknown field must not 400 a valid submit.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SpreadEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    spread_id: str
    is_dps: bool  # python-api compute (export_pdf_scope.is_dps) — worker can't self-derive


class UploadTarget(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bucket: Literal["storybook-assets"]
    key_prefix: str  # "exports/{book_id}" — worker appends "/{timestamp_ms}.pdf"


class ExportPdfWorkerRequest(BaseModel):
    """Submit body. `spreads` order = page order in the final PDF."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    # Correlation — background_jobs.id. Log/trace only.
    source_job_id: str

    source: Literal["book", "remix"]
    book_id: str
    remix_id: str | None = None  # required when source='remix' (carried in token claims)
    spreads: list[SpreadEntry] = Field(min_length=1)

    language: str
    edition: Literal["classic", "dynamic", "interactive"] = "classic"
    page_unit: Literal["spread", "single"] = "spread"
    bleed_mm: float = 3.0
    dpi: int = Field(default=300, ge=100)

    color_mode: Literal["cmyk", "rgb"] = "cmyk"
    icc_profile_url: HttpUrl | None = None  # customer ICC (SSRF-guarded fetch in color phase)
    icc_profile_id: str | None = None       # bundled id (python-api pre-validated at enqueue)
    output_intent: Literal["pdfx1a", "pdfx4"] = "pdfx1a"

    upload: UploadTarget
