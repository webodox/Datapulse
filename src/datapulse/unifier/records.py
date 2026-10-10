"""Read tabular and nested files into raw records. No type guessing: the unify spec decides types."""
from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from datapulse.errors import DataPulseError
from datapulse.io import EXCEL, JSON_LINES, SUPPORTED


def _plain(value: Any) -> Any:
    """numpy / pandas scalars to plain JSON values."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat(sep=" ")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, np.ndarray):
        return [_plain(v) for v in value.tolist()]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _frame_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    df.columns = [str(c) for c in df.columns]
    return [{k: _plain(v) for k, v in row.items()} for row in df.to_dict(orient="records")]


def read_raw_records(path: Path, label: str = "") -> List[Dict[str, Any]]:
    """CSV/Excel as strings, Parquet with its own types, JSON/JSONL with nesting kept."""
    p = Path(path)
    name = label or p.name
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        raise DataPulseError(f"Unsupported data file type '{ext}' for {name}")
    try:
        if ext == ".csv":
            records = _frame_records(pd.read_csv(p, dtype=str))
        elif ext in EXCEL:
            records = _frame_records(pd.read_excel(p, dtype=str))
        elif ext in (".parquet", ".pq"):
            records = _frame_records(pd.read_parquet(p))
        elif ext in JSON_LINES:
            records = []
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip():
                    records.append(json.loads(line))
        else:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                from datapulse.sources.rest import extract_records  # an API response saved to a file
                data = extract_records(data, None)
            records = data
    except pd.errors.EmptyDataError:
        raise DataPulseError(f"{name} is empty (no header row).")
    except (ValueError, UnicodeDecodeError) as exc:
        raise DataPulseError(f"Could not read {name} as {ext[1:]}: {exc}")
    except Exception as exc:  # corrupt spreadsheets, broken parquet...
        raise DataPulseError(f"Could not read {name} as {ext[1:]}: {exc}")
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise DataPulseError(f"{name} must contain row objects (an array of objects or one object per line)")
    if not records:
        raise DataPulseError(f"{name} has no rows.")
    return records
