"""Safe HTTP downloads: http/https only, SSRF blocks, size cap, timeouts."""
from __future__ import annotations

import ipaddress
import socket
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlparse

from datapulse.errors import DataPulseError

# Match the server upload limit so a pull cannot fill the disk larger than a user upload.
MAX_BYTES = 25 * 1024 * 1024
TIMEOUT = 30.0


def require_str(config: dict, key: str, *, label: str | None = None) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DataPulseError(f"{label or key} is required")
    return value.strip()

_PRIVATE = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _check_host(hostname: str) -> None:
    """Reject localhost and private/link-local addresses (SSRF)."""
    host = hostname.lower().rstrip(".")
    if host in ("localhost", "metadata.google.internal"):
        raise DataPulseError("URL host is not allowed (localhost / metadata)")
    try:
        ips = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror as exc:
        raise DataPulseError(f"Could not resolve host '{hostname}': {exc}") from exc
    if not ips:
        raise DataPulseError(f"Could not resolve host '{hostname}'")
    for raw in ips:
        ip = ipaddress.ip_address(raw)
        if any(ip in net for net in _PRIVATE):
            raise DataPulseError(f"URL host resolves to a private address ({ip}); blocked for safety")


def validate_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise DataPulseError("URL is required")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise DataPulseError("Only http and https URLs are allowed")
    if not parsed.hostname:
        raise DataPulseError("URL is missing a host")
    _check_host(parsed.hostname)
    return url


# Tests set this to an httpx.MockTransport so connectors never hit the network.
_TRANSPORT = None


def _client():
    try:
        import httpx
    except ImportError as exc:
        raise DataPulseError("httpx is required for remote sources. Install with: pip install 'datapulse[web]'") from exc
    return httpx


def fetch_bytes(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Mapping[str, str]] = None,
    follow_redirects: bool = True,
    max_bytes: int = MAX_BYTES,
    timeout: float = TIMEOUT,
) -> Tuple[bytes, Dict[str, str], str]:
    """GET/POST a URL and return (body, response_headers_lowercased, final_url).

    Raises DataPulseError on bad URL, private hosts, non-2xx, or oversize body.
    """
    httpx = _client()
    url = validate_url(url)
    method = (method or "GET").upper()
    if method not in ("GET", "POST", "HEAD"):
        raise DataPulseError(f"HTTP method '{method}' is not supported")

    try:
        with httpx.Client(follow_redirects=follow_redirects, timeout=timeout, transport=_TRANSPORT) as client:
            # Re-check the host after each redirect hop.
            req_headers = dict(headers or {})
            with client.stream(method, url, headers=req_headers) as response:
                final = str(response.url)
                if follow_redirects:
                    validate_url(final)
                if response.status_code >= 400:
                    raise DataPulseError(f"Request failed with HTTP {response.status_code} for {url}")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise DataPulseError(f"Download exceeds the {max_bytes // (1024 * 1024)} MB limit")
                    chunks.append(chunk)
                body = b"".join(chunks)
                hdrs = {k.lower(): v for k, v in response.headers.items()}
                return body, hdrs, final
    except DataPulseError:
        raise
    except Exception as exc:  # network errors, timeouts…
        raise DataPulseError(f"Could not fetch {url}: {exc}") from exc


def fetch_text(url: str, **kwargs: Any) -> Tuple[str, Dict[str, str], str]:
    body, headers, final = fetch_bytes(url, **kwargs)
    # Google often returns HTML login pages for private sheets; callers check content.
    charset = "utf-8"
    ctype = headers.get("content-type", "")
    if "charset=" in ctype.lower():
        charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    try:
        return body.decode(charset), headers, final
    except LookupError:
        return body.decode("utf-8", errors="replace"), headers, final
