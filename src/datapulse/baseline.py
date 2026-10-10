"""Versioned baseline profiles stored as JSON."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from . import __version__
from .errors import DataPulseError
from .io import file_sha256
from .profile import profile_dataframe

SCHEMA_VERSION = 1


def build_baseline(df: pd.DataFrame, data_file, version: int, max_categories: int) -> Dict[str, Any]:
    p = Path(data_file)
    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_version": version,
        "datapulse_version": __version__,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": {"file": p.as_posix(), "format": p.suffix.lower().lstrip("."), "sha256": file_sha256(p)},
        "profile": profile_dataframe(df, max_categories),
    }


def parse_baseline(text: str, origin: str) -> Dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise DataPulseError(f"Baseline {origin} is not valid JSON: {e}")
    if not isinstance(data, dict) or "schema_version" not in data or "profile" not in data:
        raise DataPulseError(f"Baseline {origin} is not a DataPulse baseline file.")
    if data["schema_version"] != SCHEMA_VERSION:
        raise DataPulseError(
            f"Baseline {origin} has schema_version {data['schema_version']}, expected {SCHEMA_VERSION}. "
            "Re-create it with `datapulse baseline`.")
    prof = data["profile"]
    if not isinstance(prof, dict) or "rows" not in prof or "columns" not in prof:
        raise DataPulseError(f"Baseline {origin} is missing its profile data.")
    data.setdefault("baseline_version", 1)
    return data


def load_baseline(path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise DataPulseError(f"Baseline not found: {p}. Create one with: datapulse baseline <data-file>")
    return parse_baseline(p.read_text(encoding="utf-8"), str(p))


def save_baseline(path, data: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def describe(data: Dict[str, Any], origin: str) -> str:
    src = data.get("source", {}).get("file", "?")
    return f"{origin} (v{data.get('baseline_version', 1)}, from {src}, created {data.get('created_at', '?')})"
