"""Storage host allowlist + fetch-URL resolution (ADR-054 dual-read).

After the storage-service cutover the DB still holds Supabase Storage URLs (no
data migration yet) WHILE new writes land on the storage service. Every host
check must therefore accept BOTH shapes at once — this module is the ONE source
of the trusted-host set, shared by `ssrf_guard`, `apply_casting`, and
`combine_audio_chunks_core` (DRY, no drift).

Settings are read at CALL time (never cached module-level) so a monkeypatched
`settings` is honoured in tests.
"""

from __future__ import annotations

from urllib.parse import urlparse

from src.config.settings import settings

# `http` is accepted ONLY for these hosts (dev where ops points the public base at
# loopback). Prod points STORAGE_PUBLIC_BASE_URL at https://storage.{domain}, so the
# http branch is dead there.
LOOPBACK_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
)


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001 — malformed → no host
        return ""


def allowed_storage_hosts() -> set[str]:
    """Trusted storage hosts = host(SUPABASE_URL) ∪ host(STORAGE_PUBLIC_BASE_URL)
    ∪ host(STORAGE_INTERNAL_READ_BASE_URL) ∪ CSV `STORAGE_URL_ALLOWLIST`.

    Lowercased, empties dropped. The Supabase host keeps legacy URLs fetchable
    during dual-read; the storage hosts admit newly-written URLs."""
    hosts: set[str] = set()
    for source in (
        settings.supabase_url,
        settings.storage_public_base_url,
        settings.storage_internal_read_base_url,
    ):
        h = _host_of(source or "")
        if h:
            hosts.add(h)
    for extra in (settings.storage_url_allowlist or "").split(","):
        extra = extra.strip().lower()
        if extra:
            hosts.add(extra)
    return hosts


def is_acceptable_media_url(url: str) -> bool:
    """True when `url` is safe to WRITE into a snapshot (host ∈ allowlist AND
    https — or http only for a loopback host in dev). The server never FETCHES
    these URLs; this only blocks a `javascript:`/junk URL the player would render."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if host not in allowed_storage_hosts():
        return False
    if scheme == "https":
        return True
    return scheme == "http" and host in LOOPBACK_HOSTS


def to_fetch_url(url: str) -> str:
    """Rewrite a persisted public URL to the internal loopback-read base, when
    `STORAGE_INTERNAL_READ_BASE_URL` is configured AND `url` starts with the
    public base. No-op otherwise (the v1 default — internal base left empty)."""
    pub = (settings.storage_public_base_url or "").strip().rstrip("/")
    internal = (settings.storage_internal_read_base_url or "").strip().rstrip("/")
    if internal and pub and url.startswith(pub):
        return internal + url[len(pub):]
    return url
