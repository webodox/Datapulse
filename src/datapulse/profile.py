"""Profiling a DataFrame into JSON-serialisable aggregate statistics."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from pandas.api import types as pt

NUMERIC_KINDS = ("integer", "float")
QUANTILES = {"p01": 0.01, "p25": 0.25, "p50": 0.5, "p75": 0.75, "p99": 0.99}


def column_kind(s: pd.Series) -> str:
    """Logical type: boolean | datetime | integer | float | string.

    A float column whose non-null values are all whole numbers is reported as
    "integer", because pandas turns integer CSV columns into floats as soon as
    a null appears and that should not look like a type change.
    """
    if pt.is_bool_dtype(s):
        return "boolean"
    if pt.is_datetime64_any_dtype(s):
        return "datetime"
    if pt.is_integer_dtype(s):
        return "integer"
    if pt.is_float_dtype(s):
        values = s.dropna().to_numpy(dtype=float)
        if values.size and np.isfinite(values).all() and np.all(values == np.floor(values)):
            return "integer"
        return "float"
    return "string"


def bin_proportions(values, edges: List[float]) -> List[float]:
    values = np.asarray(values, dtype=float)
    if values.size == 0:
        return [0.0] * (len(edges) + 1)
    idx = np.searchsorted(np.asarray(edges, dtype=float), values, side="right")
    counts = np.bincount(idx, minlength=len(edges) + 1)
    return (counts / values.size).tolist()


def numeric_values(s: pd.Series) -> pd.Series:
    v = pd.to_numeric(s, errors="coerce").astype(float)
    return v[np.isfinite(v)]


def numeric_profile(s: pd.Series) -> Optional[Dict[str, Any]]:
    v = numeric_values(s)
    if v.empty:
        return None
    edges = sorted({float(x) for x in v.quantile(np.linspace(0.1, 0.9, 9)).tolist()})
    out: Dict[str, Any] = {
        "min": float(v.min()), "max": float(v.max()),
        "mean": float(v.mean()), "std": float(v.std(ddof=0)),
    }
    for name, q in QUANTILES.items():
        out[name] = float(v.quantile(q))
    out["bin_edges"] = edges
    out["bin_props"] = bin_proportions(v.to_numpy(), edges)
    return out


def profile_dataframe(df: pd.DataFrame, max_categories: int = 50) -> Dict[str, Any]:
    n = len(df)
    columns: Dict[str, Any] = {}
    for name in df.columns:
        s = df[name]
        kind = column_kind(s)
        nulls = int(s.isna().sum())
        info: Dict[str, Any] = {
            "dtype": str(s.dtype), "kind": kind,
            "null_count": nulls, "null_rate": (nulls / n) if n else 0.0,
            "unique_count": int(s.nunique(dropna=True)),
        }
        if kind in NUMERIC_KINDS:
            num = numeric_profile(s)
            if num:
                info["numeric"] = num
        elif kind == "string" and 0 < info["unique_count"] <= max_categories and info["unique_count"] <= 0.5 * n:
            counts = s.dropna().astype(str).value_counts()
            info["categories"] = {str(k): int(v) for k, v in counts.items()}
        columns[str(name)] = info
    return {"rows": n, "duplicate_rows": int(df.duplicated().sum()) if n else 0, "columns": columns}
