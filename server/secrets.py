"""Encrypt source credentials at rest with Fernet (DATAPULSE_SECRET_KEY)."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any, Dict, Optional

from datapulse.errors import DataPulseError

_ENV = "DATAPULSE_SECRET_KEY"


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise DataPulseError(
            "cryptography is required to store source secrets. Install with: pip install 'datapulse[web]'"
        ) from exc
    raw = os.getenv(_ENV, "").strip()
    if not raw:
        # Dev fallback: derive a stable key from DATAPULSE_HOME so local installs work
        # without extra setup. Production should set DATAPULSE_SECRET_KEY explicitly.
        home = os.getenv("DATAPULSE_HOME", ".datapulse")
        digest = hashlib.sha256(f"datapulse-dev:{home}".encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    elif len(raw) == 44 and raw.endswith("="):
        key = raw.encode()  # already a Fernet key
    else:
        key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
    return Fernet(key)


def encrypt_secrets(secrets: Optional[Dict[str, Any]]) -> Optional[str]:
    if not secrets:
        return None
    token = _fernet().encrypt(json.dumps(secrets).encode("utf-8"))
    return token.decode("ascii")


def decrypt_secrets(blob: Optional[str]) -> Dict[str, Any]:
    if not blob:
        return {}
    try:
        from cryptography.fernet import InvalidToken
    except ImportError as exc:
        raise DataPulseError(
            "cryptography is required to read source secrets. Install with: pip install 'datapulse[web]'"
        ) from exc
    try:
        raw = _fernet().decrypt(blob.encode("ascii"))
    except InvalidToken as exc:
        raise DataPulseError(
            "Could not decrypt source secrets. Was DATAPULSE_SECRET_KEY changed?"
        ) from exc
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise DataPulseError("Stored secrets are corrupt")
    return data


def mask_secrets(secrets: Dict[str, Any]) -> Dict[str, Any]:
    """Shape returned to the UI: keys present, values replaced with a mask."""
    out: Dict[str, Any] = {}
    for key, value in secrets.items():
        if isinstance(value, dict):
            out[key] = {k: "••••••••" for k in value}
        elif value:
            out[key] = "••••••••"
        else:
            out[key] = value
    return out
