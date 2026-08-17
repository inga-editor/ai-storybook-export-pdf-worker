"""SSRF guard — block URLs resolving to non-public IPs."""

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

from src.services.storage_hosts import LOOPBACK_HOSTS, allowed_storage_hosts

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}
_BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
    "metadata",
}

# Allowlist for Supabase public-URL fetches: prod `*.supabase.co` only.
# Local-dev (`127.0.0.1` / `localhost`) is gated separately by port: only the
# canonical Supabase local port (54321) bypasses the private-IP guard, so
# random loopback services (debug ports, sidecars) are still blocked.
_SUPABASE_PROD_HOST_RE = re.compile(r"^[a-z0-9-]+\.supabase\.co$", re.IGNORECASE)
_SUPABASE_LOCAL_HOSTS = {"127.0.0.1", "localhost"}
_SUPABASE_LOCAL_PORT = 54321


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_url(url: str) -> str:
    """Validate URL is public. Raise 400 SSRF_BLOCKED on failure."""
    parsed = urlparse(url)

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        logger.warning("ssrf_blocked scheme=%s", parsed.scheme)
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": {"code": "SSRF_BLOCKED", "message": "Invalid URL scheme"}},
        )

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": {"code": "SSRF_BLOCKED", "message": "Missing hostname"}},
        )

    host_lower = hostname.lower()
    if host_lower in _BLOCKED_HOSTNAMES:
        logger.warning("ssrf_blocked host=%s", host_lower)
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": {"code": "SSRF_BLOCKED", "message": "Blocked hostname"}},
        )

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        logger.warning("ssrf_dns_fail host=%s err=%s", host_lower, exc)
        raise HTTPException(
            status_code=400,
            detail={"success": False, "error": {"code": "SSRF_BLOCKED", "message": "DNS resolution failed"}},
        ) from exc

    for info in infos:
        ip_str = info[4][0]
        if _is_blocked_ip(ip_str):
            logger.warning("ssrf_blocked_ip host=%s ip=%s", host_lower, ip_str)
            raise HTTPException(
                status_code=400,
                detail={"success": False, "error": {"code": "SSRF_BLOCKED", "message": "URL resolves to non-public address"}},
            )

    return url


def validate_supabase_url(url: str) -> str:
    """Validate URL points at an allowlisted Supabase Storage host.

    Used by `/api/text/combine-audio-chunks` (chunk audio fetch).

    Allowlist:
    - Prod: `*.supabase.co` (any scheme http/https) → runs through
      `validate_public_url` for DNS + private-IP guard.
    - Local dev: `127.0.0.1` or `localhost` ONLY on the canonical Supabase
      local port `54321`. Other loopback ports are rejected — keeps random
      debug/metrics sidecars on the same host out of reach.

    Raises 400 CHUNK_FETCH_FORBIDDEN on host/port mismatch; 400 SSRF_BLOCKED
    on private-IP resolution for non-loopback hosts.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": {"code": "CHUNK_FETCH_FORBIDDEN", "message": "Invalid URL scheme"},
            },
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": {"code": "CHUNK_FETCH_FORBIDDEN", "message": "Missing hostname"},
            },
        )

    if host in _SUPABASE_LOCAL_HOSTS:
        if parsed.port != _SUPABASE_LOCAL_PORT:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "error": {
                        "code": "CHUNK_FETCH_FORBIDDEN",
                        "message": (
                            f"Loopback host '{host}' only allowed on port "
                            f"{_SUPABASE_LOCAL_PORT} (Supabase local)"
                        ),
                    },
                },
            )
        return url

    # ADR-054 dual-read: accept the storage-service host(s) alongside *.supabase.co
    # (new narration chunks live on the storage service; old ones on Supabase). A
    # loopback storage host (dev — ops pointed the public/internal base at localhost)
    # bypasses the private-IP guard, mirroring the :54321 special-case above.
    if host in allowed_storage_hosts():
        if host in LOOPBACK_HOSTS:
            return url
        validate_public_url(url)
        return url

    if not _SUPABASE_PROD_HOST_RE.match(host):
        logger.warning("supabase_host_blocked host=%s", host)
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": {
                    "code": "CHUNK_FETCH_FORBIDDEN",
                    "message": f"Host '{host}' not in allowlist",
                },
            },
        )

    validate_public_url(url)
    return url


def validate_storage_host(url: str, supabase_url: str) -> str:
    """Validate URL host is an allowlisted storage host.

    Used by `/api/voice/clone-from-human` (recordUrl fetch). ⚡ADR-054 dual-read:
    the trusted set now spans the legacy Supabase host AND the storage-service
    host(s) via `allowed_storage_hosts()` (host of SUPABASE_URL ∪
    STORAGE_PUBLIC_BASE_URL ∪ STORAGE_INTERNAL_READ_BASE_URL ∪ allowlist CSV) —
    so a recordUrl written after the cutover still fetches. The `supabase_url`
    param is retained for signature compat and unioned defensively.

    A loopback storage host (dev) bypasses the private-IP guard; otherwise
    delegates to `validate_public_url` for DNS + private-IP guard.

    Raises HTTPException(400 INVALID_RECORD_URL).
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": {
                    "code": "INVALID_RECORD_URL",
                    "message": "Invalid URL scheme",
                },
            },
        )

    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": {
                    "code": "INVALID_RECORD_URL",
                    "message": "Missing hostname",
                },
            },
        )

    allowed = allowed_storage_hosts()
    param_host = (urlparse(supabase_url).hostname or "").lower()
    if param_host:
        allowed = allowed | {param_host}
    if not allowed:
        # Misconfig — no storage host configured at all.
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": {
                    "code": "INVALID_RECORD_URL",
                    "message": "Storage host not configured (ops: SUPABASE_URL / STORAGE_PUBLIC_BASE_URL)",
                },
            },
        )

    if host not in allowed:
        logger.warning(
            "storage_host_blocked host=%s allowed=%s", host, sorted(allowed),
        )
        raise HTTPException(
            status_code=400,
            detail={
                "success": False,
                "error": {
                    "code": "INVALID_RECORD_URL",
                    "message": f"Host '{host}' not in storage allowlist",
                },
            },
        )

    if host in LOOPBACK_HOSTS:
        return url
    validate_public_url(url)
    return url
