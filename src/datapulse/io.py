"""Reading CSV / Parquet / JSON files with clear errors."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from .errors import DataPulseError

SUPPORTED = (".csv", ".parquet", ".pq", ".json", ".jsonl", ".ndjson", ".xlsx", ".xls")
JSON_LINES = (".jsonl", ".ndjson")
EXCEL = (".xlsx", ".xls")
# Unstructured inputs: the document unifier extracts records from these (see datapulse.unifier.documents).
DOCUMENTS = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".docx", ".md", ".markdown", ".txt",
             ".eml", ".html", ".htm")
ARCHIVES = (".zip",)
INPUTS = SUPPORTED + DOCUMENTS + ARCHIVES


def load_dataframe(path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise DataPulseError(f"Data file not found: {p}")
    if not p.is_file():
        raise DataPulseError(f"Not a file: {p}")
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        raise DataPulseError(
            f"Unsupported file type '{p.suffix}' for {p}. "
            "Supported: .csv, .parquet, .json, .jsonl, .xlsx"
        )
    try:
        if ext == ".csv":
            df = pd.read_csv(p)
        elif ext == ".json" or ext in JSON_LINES:  # an array of row objects, or one object per line
            df = pd.read_json(p, orient="records", lines=ext in JSON_LINES, convert_dates=False)
        elif ext in EXCEL:
            df = pd.read_excel(p)  # first sheet; needs openpyxl for .xlsx
        else:
            df = pd.read_parquet(p)
    except pd.errors.EmptyDataError:
        raise DataPulseError(f"{p} is empty (no header row).")
    except ImportError as e:
        hint = "openpyxl" if ext in EXCEL else "pyarrow"
        raise DataPulseError(f"Missing dependency to read {p}: {e}. Try: pip install {hint}")
    except Exception as e:  # corrupt files, bad encodings, ragged CSV rows...
        raise DataPulseError(f"Could not read {p} as {ext[1:]}: {e}")
    return normalize_frame(df, str(p))


def normalize_frame(df: pd.DataFrame, label: str) -> pd.DataFrame:
    """Shared clean-up for every way data arrives (files, databases, links): text column names, no duplicates,
    nested values (lists/dicts) as text so they can be compared."""
    if df.shape[1] == 0:
        raise DataPulseError(f"{label} has no columns.")
    df.columns = [str(c) for c in df.columns]
    if df.columns.duplicated().any():
        dupes = sorted(set(df.columns[df.columns.duplicated()]))
        raise DataPulseError(f"{label} has duplicate column names: {dupes}")
    for col in df.columns:
        s = df[col]
        if s.dtype == object and s.map(lambda v: isinstance(v, (list, dict, set, tuple, np.ndarray))).any():
            df[col] = s.map(lambda v: v if v is None or (isinstance(v, float) and np.isnan(v)) else str(v))
    return df


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
