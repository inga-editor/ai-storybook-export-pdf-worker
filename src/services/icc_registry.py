"""Bundled ICC profile registry for the export-PDF job (api/jobs/06).

CMYK is the DEFAULT export color mode; the default profile is ECI **FOGRA39**.
Profiles MUST be freely-redistributable originals (ECI / Idealliance / CIE) —
NEVER Adobe-shipped (restrictive licensing). License vetting is a hard pre-ship
gate.

VETTED 2026-06-01: `Coated_FOGRA39.icc` (= ECI `ISOcoated_v2_eci.icc`) is bundled
in `icc-profiles/` (see `icc-profiles/LICENSE-NOTES.md` vetting log) → default CMYK
is LIVE. If the binary is ever absent (e.g. a deploy that omits the mount), the
default CMYK path raises `IccProfileUnavailableError` (handler maps to a
`color`-stage error; ops would flip `color_mode='rgb'` default per ADR-033).

Resolution of the profile directory:
  1. `settings.icc_profile_dir` when set (deploy mounts `/opt/icc-profiles`).
  2. else the repo-local `ai-storybook-python-api/icc-profiles/` dir.

The registry is a pure mapping constant; binary bytes are loaded lazily on first
use and cached. No network, no Pillow dependency here — `color_convert` consumes
the bytes.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TypedDict

from src.config.settings import settings

logger = logging.getLogger(__name__)


class BundledIccProfile(TypedDict):
    """One bundled ICC profile entry (mirrors design §Bundled Profile Registry)."""

    id: str
    label: str
    condition: str
    file_path: str  # filename inside the resolved ICC dir
    rendering_intent: str  # 'RelativeColorimetric' | 'Perceptual'
    black_point_handling: bool  # K-only preservation for pure black
    output_intent_id: str  # PDF/X OutputConditionIdentifier


# Only license-vetted, freely-redistributable originals. fogra51 / gracol2006 are
# intentionally omitted until their binaries are vetted + bundled.
BUNDLED_PROFILES: dict[str, BundledIccProfile] = {
    "fogra39": {
        "id": "fogra39",
        "label": "ISO Coated v2 (ECI) — EU offset",
        "condition": "FOGRA39",
        "file_path": "Coated_FOGRA39.icc",
        "rendering_intent": "RelativeColorimetric",
        "black_point_handling": True,
        "output_intent_id": "FOGRA39",
    },
}


class UnknownIccProfileError(Exception):
    """`icc_profile_id` not in `BUNDLED_PROFILES`. Enqueue maps to 422
    UNKNOWN_ICC_PROFILE; handler treats as a safety-net `color` error."""

    def __init__(self, profile_id: str) -> None:
        super().__init__(f"unknown bundled ICC profile id={profile_id!r}")
        self.profile_id = profile_id


class IccProfileUnavailableError(Exception):
    """Profile is registered but its binary is missing on disk (not yet
    license-vetted/bundled). Handler maps to a `color`-stage failure."""

    def __init__(self, profile_id: str, path: str) -> None:
        super().__init__(
            f"ICC profile id={profile_id!r} binary not found at {path} "
            "(license-vet + bundle required before CMYK default can ship)"
        )
        self.profile_id = profile_id
        self.path = path


def _icc_dir() -> Path:
    """Resolve the directory holding bundled ICC binaries."""
    if settings.icc_profile_dir:
        return Path(settings.icc_profile_dir)
    # repo-local fallback: <package_root>/icc-profiles
    # icc_registry.py → services/ → src/ → <package_root>
    return Path(__file__).resolve().parents[2] / "icc-profiles"


def get_profile(profile_id: str) -> BundledIccProfile:
    """Return the registry entry or raise `UnknownIccProfileError`."""
    profile = BUNDLED_PROFILES.get(profile_id)
    if profile is None:
        raise UnknownIccProfileError(profile_id)
    return profile


def is_known_profile(profile_id: str) -> bool:
    """Cheap membership check for enqueue-time validation."""
    return profile_id in BUNDLED_PROFILES


# Cache loaded bytes by absolute path (profiles are small, immutable).
_BYTES_CACHE: dict[str, bytes] = {}


def load_bundled_icc(profile: BundledIccProfile) -> bytes:
    """Read the ICC binary for `profile`. Raises `IccProfileUnavailableError`
    when the file is absent (binary not yet bundled)."""
    path = _icc_dir() / profile["file_path"]
    key = str(path)
    cached = _BYTES_CACHE.get(key)
    if cached is not None:
        return cached
    if not os.path.isfile(path):
        logger.warning(
            "icc_profile_missing id=%s path=%s", profile["id"], path
        )
        raise IccProfileUnavailableError(profile["id"], key)
    data = path.read_bytes()
    _BYTES_CACHE[key] = data
    logger.debug("icc_profile_loaded id=%s bytes=%d", profile["id"], len(data))
    return data
