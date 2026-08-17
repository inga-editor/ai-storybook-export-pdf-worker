"""Unit tests for `src/services/icc_registry.py` — fills the coverage gap that
existed in python-api (no dedicated icc_registry test). Exercises the real bundled
FOGRA39 binary load, the unknown-id error, and the dir-override + missing-binary
paths."""

from __future__ import annotations

import pytest

from src.config.settings import settings
from src.services import icc_registry
from src.services.icc_registry import (
    IccProfileUnavailableError,
    UnknownIccProfileError,
    get_profile,
    is_known_profile,
    load_bundled_icc,
)


class TestGetProfile:
    def test_known_fogra39(self):
        p = get_profile("fogra39")
        assert p["condition"] == "FOGRA39"
        assert p["file_path"] == "Coated_FOGRA39.icc"

    def test_unknown_raises(self):
        with pytest.raises(UnknownIccProfileError):
            get_profile("nope")

    def test_is_known_profile(self):
        assert is_known_profile("fogra39") is True
        assert is_known_profile("nope") is False


class TestLoadBundledIcc:
    def test_loads_real_bundled_binary(self):
        data = load_bundled_icc(get_profile("fogra39"))
        assert isinstance(data, bytes)
        assert len(data) > 1000  # a real ICC profile is > 1KB

    def test_dir_override(self, tmp_path, monkeypatch):
        # Point the registry at a custom dir holding a stand-in binary.
        (tmp_path / "Coated_FOGRA39.icc").write_bytes(b"FAKE-ICC-BYTES")
        monkeypatch.setattr(settings, "icc_profile_dir", str(tmp_path))
        icc_registry._BYTES_CACHE.clear()  # avoid a repo-dir cache hit from another test
        data = load_bundled_icc(get_profile("fogra39"))
        assert data == b"FAKE-ICC-BYTES"

    def test_missing_binary_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "icc_profile_dir", str(tmp_path))  # empty dir
        icc_registry._BYTES_CACHE.clear()
        with pytest.raises(IccProfileUnavailableError):
            load_bundled_icc(get_profile("fogra39"))
