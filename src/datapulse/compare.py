"""Their columns next to ours: what a mapping recipe does to each column, and what the rules say about the result.

For every column of the standard this reports which source column feeds it, the steps applied on the way, the
column's statistics before mapping, after mapping and in the standard, and the issues the checks raise on it.
Sample values are only included when the project's report settings allow showing values.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import pandas as pd

from .checks import run_checks
from .config import Config
from .fix import OPERATIONS, RepairError, _counts
from .profile import NUMERIC_KINDS, column_kind, numeric_values

ROW_STEPS = ("remove_duplicates", "drop_rows_missing")
SAMPLES = 3


def compare_mapping(df: pd.DataFrame, baseline: Dict[str, Any], cfg: Config,
                    recipe: List[Dict[str, Any]]) -> Dict[str, Any]:
    profile = baseline["profile"]
    lineage: Dict[str, Dict[str, Any]] = {c: {"source": c, "steps": []} for c in df.columns}
    dropped: List[Dict[str, Any]] = []
    table_steps: List[str] = []
    problems: List[str] = []
    mapped = df
    for n, op in enumerate(recipe, 1):
        name, args = op["operation"], op["args"]
        try:
            mapped, effect = OPERATIONS[name](mapped, profile, **args)
        except (RepairError, KeyError, TypeError) as e:
            problems.append(f"Step {n} ({name.replace('_', ' ')}) no longer applies: {e}")
            continue
        step = {"operation": name, "effect": effect}
        if name == "rename_column":
            entry = lineage.pop(args["column"])
            entry["steps"].append(step)
            lineage[args["new_name"]] = entry
        elif name == "drop_column":
            dropped.append({"name": lineage.pop(args["column"])["source"], "effect": effect})
        elif name in ROW_STEPS:
            table_steps.append(effect)
        elif args.get("column") in lineage:
            lineage[args["column"]]["steps"].append(step)

    after = run_checks(baseline, mapped, cfg)
    before = run_checks(baseline, df, cfg)
    by_column: Dict[str, List[Dict[str, str]]] = {}
    for issue in after.issues:
        for col in issue.columns:
            by_column.setdefault(col, []).append({"severity": issue.severity, "title": issue.title,
                                                  "detail": issue.detail})
    show = cfg.report.show_values

    columns = []
    for name, standard in profile["columns"].items():
        entry = lineage.get(name)
        issues = by_column.get(name, [])
        if entry is None:
            columns.append({"name": name, "source": None, "status": "missing", "steps": [], "issues": issues,
                            "standard": _from_profile(standard), "before": None, "after": None})
            continue
        steps = entry["steps"]
        changed = [s for s in steps if s["operation"] != "rename_column"]
        status = ("flagged" if any(i["severity"] != "info" for i in issues)
                  else "converted" if changed else "renamed" if entry["source"] != name else "same")
        columns.append({
            "name": name, "source": entry["source"], "status": status, "steps": steps, "issues": issues,
            "standard": _from_profile(standard),
            "before": _stats(df[entry["source"]], show),
            "after": _stats(mapped[name], show),
        })
    extra = [{"name": entry["source"], "as": current, "after": _stats(mapped[current], show)}
             for current, entry in lineage.items() if current not in profile["columns"]]
    return {
        "columns": columns, "extra": extra, "dropped": dropped, "table_steps": table_steps, "problems": problems,
        "rows": {"before": len(df), "after": len(mapped), "standard": profile["rows"]},
        "before": _counts(before.issues), "after": _counts(after.issues),
        "table_issues": [{"severity": i.severity, "title": i.title, "detail": i.detail}
                         for i in after.issues if not i.columns],
        "show_values": show,
    }


def _number(x: Any) -> Optional[float]:
    if x is None:
        return None
    x = float(x)
    return round(x, 4) if math.isfinite(x) else None


def _from_profile(info: Dict[str, Any]) -> Dict[str, Any]:
    num = info.get("numeric") or {}
    top = sorted((info.get("categories") or {}).items(), key=lambda kv: -kv[1])[:SAMPLES]
    return {"kind": info["kind"], "null_rate": _number(info["null_rate"]), "mean": _number(num.get("mean")),
            "min": _number(num.get("min")), "max": _number(num.get("max")), "top": [k for k, _ in top]}


def _stats(s: pd.Series, show_values: bool) -> Dict[str, Any]:
    kind = column_kind(s)
    out: Dict[str, Any] = {"kind": kind, "null_rate": _number(s.isna().mean()) if len(s) else None,
                           "mean": None, "min": None, "max": None, "top": [], "samples": []}
    if kind in NUMERIC_KINDS:
        values = numeric_values(s)
        if not values.empty:
            out.update(mean=_number(values.mean()), min=_number(values.min()), max=_number(values.max()))
    elif kind == "string":
        out["top"] = [str(v) for v in s.dropna().astype(str).value_counts().head(SAMPLES).index]
    if show_values:
        out["samples"] = [str(v) for v in s.head(SAMPLES).tolist()]
    return out
