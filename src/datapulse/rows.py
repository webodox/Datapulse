"""Bad rows: which rows break the project's rules or don't match the standard, cell by cell, and how to fix them.

A cell is flagged when its value is the reason a check could fail: empty where the standard is never empty, a
value the standard doesn't know, text in a number column, a number outside the standard's range, a duplicate,
or a broken explicit rule. A flag is *failing* when its check raised an issue on this data; otherwise the value
only differs from the standard and the rules allowed it.

Fixes are the same safe operations the repair agent uses. They are replayed on a copy that keeps each row's
original row number (and a unified table's source column), so a preview can say which rows a fix touched.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from .checks import _blank, rule_violations, run_checks
from .config import LEVEL, Config
from .fix import OPERATIONS, RepairError, _counts
from .profile import NUMERIC_KINDS, column_kind

ROW = "_row"
MAX_ROWS = 200
RULE_LABEL = {
    "null_rate": "empty values", "categorical": "allowed values", "schema": "columns & types",
    "distribution": "value ranges", "duplicates": "duplicates", "rules": "your rules",
}
# How strongly a flag is shown: a failing check's severity, else "standard" (differs; the rules allowed it).
RANK = {"critical": 3, "warning": 2, "info": 1, "standard": 0}


def _issue_keys(issues: Iterable[Any]) -> Dict[Tuple[str, str], str]:
    """(check family, column) -> severity of the issues a run raised; column "" for table-wide issues."""
    out: Dict[Tuple[str, str], str] = {}
    for i in issues:
        check, columns, sev = (i["check"], i["columns"], i["severity"]) if isinstance(i, dict) else (i.check, i.columns, i.severity)
        family = check.split(".")[0]
        for col in columns or [""]:
            if LEVEL[sev] >= LEVEL.get(out.get((family, col), "info"), 0):
                out[(family, col)] = sev
    return out


def _identifier(info: Dict[str, Any], rows: int) -> bool:
    return info["kind"] == "integer" and info.get("unique_count") == rows > 1


_ID_NAME = re.compile(r"(^|[_\s])id$|[a-z]Id$|ID$", re.IGNORECASE)


def _id_like(column: str, info: Dict[str, Any], rows: int) -> bool:
    """Identifiers can't be filled in with a typical value."""
    return bool(_ID_NAME.search(column)) or _identifier(info, rows) or \
        (info["kind"] == "string" and info.get("unique_count", 0) > 0.5 * rows)


def _fmt(x: Any) -> str:
    x = float(x)
    return f"{int(x):,}" if x.is_integer() else f"{x:,.2f}"


def find_flags(df: pd.DataFrame, baseline: Dict[str, Any], cfg: Config, issues: Iterable[Any]) -> List[Dict[str, Any]]:
    """[{rule, kind, column (None for the whole row), mask, reason(value) -> str, severity}]"""
    profile = baseline["profile"]
    raised = _issue_keys(issues)
    show = cfg.report.show_values
    flags: List[Dict[str, Any]] = []

    def add(rule: str, kind: str, column: Optional[str], mask: pd.Series, reason, issue_column: Optional[str] = None):
        mask = mask.fillna(False).astype(bool)
        if mask.any():
            sev = raised.get((rule, issue_column if issue_column is not None else (column or "")), "standard")
            flags.append({"rule": rule, "kind": kind, "column": column, "mask": mask, "reason": reason, "severity": sev})

    for col, info in profile["columns"].items():
        if col not in df.columns:
            continue
        s = df[col]
        empty = _blank(s)
        if cfg.null_rate.severity != "off" and (info["null_rate"] == 0 or ("null_rate", col) in raised):
            rate = f"{info['null_rate'] * 100:.1f}%"
            add("null_rate", "empty", col, empty, lambda v, rate=rate: f"Empty; {rate} of the standard's values are empty")
        known = info.get("categories")
        checked = cfg.categorical.columns is None or col in cfg.categorical.columns
        if known and checked and cfg.categorical.severity != "off" and column_kind(s) == "string":
            folded = {str(k).strip().lower(): str(k) for k in known}

            def unknown(v, folded=folded):
                close = folded.get(str(v).strip().lower())
                if not show:
                    return "Not a value the standard has" + (" (differs only in case or spaces)" if close else "")
                return f"'{v}' isn't a value the standard has" + (f"; did you mean '{close}'?" if close else "")
            add("categorical", "unknown", col, ~empty & ~s.astype("string").isin([str(k) for k in known]), unknown)
        if info["kind"] in NUMERIC_KINDS:
            numbers = pd.to_numeric(s, errors="coerce")
            if cfg.schema.type_changes != "off":
                add("schema", "not_number", col, ~empty & numbers.isna(),
                    lambda v: (f"'{v}' is not a number" if show else "Not a number") + "; the standard's column is numbers")
            num = info.get("numeric") or {}
            in_scope = (cfg.distribution.columns is None and not _identifier(info, profile["rows"])) or \
                (cfg.distribution.columns is not None and col in cfg.distribution.columns)
            if cfg.distribution.severity != "off" and in_scope and num.get("min") is not None:
                lo, hi = num["min"], num["max"]
                add("distribution", "range", col, (numbers < lo) | (numbers > hi),
                    lambda v, lo=lo, hi=hi: (f"{v} is" if show else "Is") +
                    f" outside the standard's range {_fmt(lo)} to {_fmt(hi)}")

    dp = cfg.duplicates
    if dp.severity != "off" and all(k in df.columns for k in dp.columns):
        data = df[dp.columns] if dp.columns else df
        what = f"by {', '.join(dp.columns)}" if dp.columns else "of every value"
        add("duplicates", "duplicate", dp.columns[0] if dp.columns else None, data.duplicated(keep="first"),
            lambda v: f"Repeats an earlier row ({what})", issue_column=dp.columns[0] if dp.columns else "")

    if cfg.rules.severity != "off":
        for v in rule_violations(df, cfg):
            if v["mask"] is not None:
                title = v["title"]
                add("rules", v["kind"], v["column"], v["mask"], lambda _v, title=title: f"Breaks your rule: {title}")
    return flags


def _cell(value: Any) -> Optional[str]:
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    return str(value)


def bad_rows(df: pd.DataFrame, baseline: Dict[str, Any], cfg: Config, issues: Iterable[Any],
             source_column: Optional[str] = None, limit: int = MAX_ROWS) -> Dict[str, Any]:
    """The rows that break a rule or differ from the standard, worst first. `df` may carry ROW (original row
    numbers) and `source_column` (which source each row came from); neither is checked."""
    data = df.drop(columns=[c for c in (ROW, source_column) if c and c in df.columns])
    flags = find_flags(data, baseline, cfg, issues)
    numbers = df[ROW] if ROW in df.columns else pd.Series(range(1, len(df) + 1), index=df.index)
    rank = pd.Series(-1, index=df.index)
    for f in flags:
        rank = rank.where(~f["mask"] | (rank >= RANK[f["severity"]]), RANK[f["severity"]])
    flagged = rank[rank >= 0]
    order = flagged.sort_values(ascending=False, kind="stable").index[:limit]

    summary: Dict[Tuple[str, str, Optional[str]], Dict[str, Any]] = {}
    for f in flags:
        key = (f["rule"], f["kind"], f["column"])
        summary[key] = {"rule": f["rule"], "label": RULE_LABEL[f["rule"]], "kind": f["kind"], "column": f["column"],
                        "rows": int(f["mask"].sum()), "severity": f["severity"]}

    show = cfg.report.show_values
    columns = list(data.columns)
    rows = []
    for idx in order:
        cells: Dict[str, List[Dict[str, str]]] = {}
        whole: List[Dict[str, str]] = []
        for f in flags:
            if f["mask"].at[idx]:
                value = data.at[idx, f["column"]] if f["column"] else None
                entry = {"rule": f["rule"], "reason": f["reason"](value), "severity": f["severity"]}
                (cells.setdefault(f["column"], []) if f["column"] else whole).append(entry)
        rows.append({"row": int(numbers.at[idx]),
                     "source": _cell(df.at[idx, source_column]) if source_column and source_column in df.columns else None,
                     "values": {c: _cell(data.at[idx, c]) for c in columns} if show else None,
                     "cells": cells, "row_flags": whole})
    failing = int((rank >= RANK["info"]).sum())
    return {
        "total_rows": len(df), "bad_rows": len(flagged), "failing_rows": failing, "columns": columns,
        "summary": sorted(summary.values(), key=lambda s: (-RANK[s["severity"]], -s["rows"])),
        "rows": rows, "shown": len(rows), "show_values": show,
    }


def suggest_fixes(df: pd.DataFrame, baseline: Dict[str, Any], cfg: Config, issues: Iterable[Any],
                  source_column: Optional[str] = None) -> List[Dict[str, Any]]:
    """Safe steps for the flagged rows, most useful first. `default` steps correct values; the others remove or
    invent data, so a person has to tick them."""
    data = df.drop(columns=[c for c in (ROW, source_column) if c and c in df.columns])
    profile = baseline["profile"]["columns"]
    out: List[Dict[str, Any]] = []
    seen = set()

    def suggest(operation: str, args: Dict[str, Any], reason: str, rows: int, default: bool):
        key = (operation, repr(sorted(args.items())))
        if key not in seen and rows:
            seen.add(key)
            out.append({"operation": operation, "args": args, "reason": reason, "rows": rows, "default": default})

    for f in find_flags(data, baseline, cfg, issues):
        col, rows, kind = f["column"], int(f["mask"].sum()), f["kind"]
        info = profile.get(col) if col else None
        if kind == "unknown":
            known = {str(k).strip().lower() for k in info["categories"]}
            close = int((f["mask"] & data[col].astype("string").str.strip().str.lower().isin(known)).sum())
            suggest("standardize_categories", {"column": col},
                    f"{close:,} values in '{col}' match the standard apart from case or spaces.", close, True)
            if rows > close and not (col in cfg.rules.columns and cfg.rules.columns[col].allowed):
                suggest("drop_rows_not_in", {"column": col, "values": None},
                        f"Remove the rows whose '{col}' is still not a value the standard has.", rows - close, False)
        elif kind == "not_number":
            suggest("convert_to_number", {"column": col},
                    f"'{col}' holds numbers stored as text (like '5 units'); read the number back.", rows, True)
        elif kind == "range":
            values = pd.to_numeric(data[col], errors="coerce")
            mean, base_mean = values.mean(), (info.get("numeric") or {}).get("mean")
            ratio = mean / base_mean if base_mean else None
            factor = next((1 / r for r in (100, 1000, 0.01, 0.001) if ratio and abs(ratio / r - 1) < 0.1), None)
            if factor:
                suggest("scale_column", {"column": col, "factor": factor},
                        f"'{col}' is about {1 / factor:g}x the standard (a unit change, like cents vs. dollars).",
                        rows, True)
        elif kind == "duplicate":
            keys = cfg.duplicates.columns
            suggest("remove_duplicates", {"columns": list(keys)},
                    f"Remove {rows:,} repeated rows, keeping the first of each.", rows, True)
        elif kind == "unique":
            suggest("remove_duplicates", {"columns": [col]},
                    f"'{col}' must be unique; keep the first row of each value.", rows, True)
        elif kind in ("empty", "required"):
            if info is None or kind == "required" or _id_like(col, info, baseline["profile"]["rows"]):
                suggest("drop_rows_missing", {"column": col},
                        f"Remove the rows with no '{col}'; filling it in would be made-up data.", rows, False)
            else:
                strategy = "median" if info["kind"] in NUMERIC_KINDS else "most_common"
                suggest("fill_missing", {"column": col, "strategy": strategy},
                        f"Fill empty '{col}' with its {strategy.replace('_', ' ')} value.", rows, False)
        elif kind in ("min", "max"):
            rule = cfg.rules.columns[col]
            suggest("drop_rows_outside_range", {"column": col, "min": rule.min, "max": rule.max},
                    f"Remove the rows where '{col}' breaks your range rule.", rows, False)
        elif kind == "allowed":
            suggest("drop_rows_not_in", {"column": col, "values": list(cfg.rules.columns[col].allowed or [])},
                    f"Remove the rows whose '{col}' is not an allowed value.", rows, False)
    order = {"scale_column": 0, "convert_to_number": 1, "standardize_categories": 2, "remove_duplicates": 3}
    return sorted(out, key=lambda s: (not s["default"], order.get(s["operation"], 9)))


def apply_fixes(df: pd.DataFrame, profile: Dict[str, Any], operations: List[Dict[str, Any]],
                source_column: Optional[str] = None) -> Tuple[pd.DataFrame, List[str], List[str]]:
    """Replay steps on a copy that keeps ROW (original row numbers) and the source column. Steps that no longer
    apply are skipped and reported. Whole-row duplicates ignore the two bookkeeping columns."""
    out = df.copy()
    if ROW not in out.columns:
        out.insert(0, ROW, range(1, len(out) + 1))
    effects, problems = [], []
    for n, op in enumerate(operations, 1):
        args = dict(op["args"])
        if op["operation"] == "remove_duplicates" and not args.get("columns"):
            args["columns"] = [c for c in out.columns if c not in (ROW, source_column)]
        try:
            out, effect = OPERATIONS[op["operation"]](out, profile, **args)
        except (RepairError, KeyError, TypeError) as e:
            problems.append(f"Step {n} ({op['operation'].replace('_', ' ')}) was skipped: {e}")
            continue
        effects.append(effect)
    return out, effects, problems


def preview_fixes(df: pd.DataFrame, baseline: Dict[str, Any], cfg: Config, operations: List[Dict[str, Any]],
                  before_issues: Iterable[Any], source_column: Optional[str] = None,
                  reference: Optional[pd.DataFrame] = None, combined: bool = False) -> Dict[str, Any]:
    """What the steps would do: bad rows and issue counts after, plus which original rows were removed."""
    fixed, effects, problems = apply_fixes(df, baseline["profile"], operations, source_column)
    checked = fixed.drop(columns=[c for c in (ROW, source_column) if c and c in fixed.columns])
    if combined:
        from dataclasses import replace
        cfg = replace(cfg, row_count=replace(cfg.row_count, severity="off"))
    after = run_checks(baseline, checked, cfg, reference=reference)
    kept = set(fixed[ROW])
    return {
        "effects": effects, "problems": problems,
        "before": _counts([_Sev(i) for i in before_issues]),
        "after": _counts(after.issues), "remaining": [i.title for i in after.issues],
        "removed_rows": len(df) - len(fixed), "removed": sorted(set(range(1, len(df) + 1)) - kept)[:MAX_ROWS],
        "rows": bad_rows(fixed, baseline, cfg, after.issues, source_column),
    }


class _Sev:
    def __init__(self, issue: Any):
        self.severity = issue["severity"] if isinstance(issue, dict) else issue.severity
