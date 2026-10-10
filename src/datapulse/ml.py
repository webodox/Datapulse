"""Learned drift detection: train a classifier to tell baseline rows from current rows.

If a model can't tell the two files apart (ROC AUC near 0.5) they come from the same distribution. If it can,
the data changed, and the model's feature importances say which columns gave it away. Unlike the per-column
checks this also catches changes in how columns relate to each other. Runs locally; no data leaves the machine.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
from pandas.api import types as pt

from .profile import column_kind

MIN_ROWS = 50
MAX_CATEGORIES = 50


def _skip_reason(base: pd.Series, cur: pd.Series) -> Optional[str]:
    """Columns that differ between any two batches by design would make every batch look drifted."""
    both = pd.concat([base, cur], ignore_index=True).dropna()
    if both.empty:
        return "empty"
    kind = column_kind(base)
    if kind == "datetime":
        return "timestamp"
    if kind == "string":
        sample = both.astype(str).head(200)
        if pd.to_datetime(sample, errors="coerce", format="mixed").notna().mean() > 0.9:
            return "timestamp"
        if both.nunique() > 0.5 * len(both):
            return "identifier"
    elif kind == "integer" and base.nunique() == base.notna().sum() > 1:
        return "identifier"
    return None


def _encode(base: pd.Series, cur: pd.Series) -> Optional[np.ndarray]:
    """One numeric feature per column for both files stacked (baseline rows first); NaN kept as -1 / median."""
    both = pd.concat([base, cur], ignore_index=True)
    if column_kind(base) in ("integer", "float", "boolean") and column_kind(cur) in ("integer", "float", "boolean"):
        values = pd.to_numeric(both, errors="coerce").astype(float)
        if not np.isfinite(values).any():
            return None
        return np.column_stack([values.fillna(values.median()), values.isna()])
    text = both.astype("string")
    top = text.value_counts().index[:MAX_CATEGORIES]
    codes = pd.Categorical(text.where(text.isin(top), "__other__")).codes.astype(float)  # NaN -> -1
    return codes.reshape(-1, 1)


def drift_model(base: pd.DataFrame, cur: pd.DataFrame, exclude: Sequence[str] = (), max_rows: int = 5000,
                seed: int = 0) -> Dict[str, Any]:
    """Fit a random forest to separate baseline from current rows; report out-of-bag AUC and column drivers."""
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import roc_auc_score

    n = min(len(base), len(cur), max_rows)
    result: Dict[str, Any] = {"auc": None, "drivers": [], "skipped": {}, "rows_per_side": n}
    if n < MIN_ROWS:
        result["reason"] = f"needs at least {MIN_ROWS} rows in each file"
        return result
    base = base.sample(n, random_state=seed) if len(base) > n else base
    cur = cur.sample(n, random_state=seed) if len(cur) > n else cur

    blocks, owners = [], []
    for col in (c for c in base.columns if c in cur.columns):
        reason = "excluded in config" if col in exclude else _skip_reason(base[col], cur[col])
        encoded = None if reason else _encode(base[col], cur[col])
        if encoded is None:
            result["skipped"][col] = reason or "no usable values"
            continue
        blocks.append(encoded)
        owners += [col] * encoded.shape[1]
    if not blocks:
        result["reason"] = "no comparable columns"
        return result

    X = np.hstack(blocks)
    y = np.r_[np.zeros(n), np.ones(n)]
    model = RandomForestClassifier(n_estimators=150, min_samples_leaf=5, max_features="sqrt", oob_score=True,
                                   random_state=seed, n_jobs=1).fit(X, y)
    oob = np.nan_to_num(model.oob_decision_function_[:, 1], nan=0.5)
    # Below 0.5 is noise (exact duplicate rows in both files predict each other's label), not "less than no drift".
    result["auc"] = round(max(0.5, float(roc_auc_score(y, oob))), 4)
    weights: Dict[str, float] = {}
    for col, w in zip(owners, model.feature_importances_):
        weights[col] = weights.get(col, 0.0) + float(w)
    total = sum(weights.values()) or 1.0
    result["drivers"] = sorted(([c, round(w / total, 3)] for c, w in weights.items()), key=lambda d: -d[1])
    return result


def top_drivers(drivers: List[List[Any]], min_share: float = 0.1, limit: int = 3) -> List[List[Any]]:
    return [d for d in drivers if d[1] >= min_share][:limit] or drivers[:1]
