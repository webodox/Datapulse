"""The quality checks. Every issue carries measured facts and, separately, a
deterministic *suggested* explanation that is explicitly not verified."""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import pandas as pd

from .config import LEVEL, Config
from .models import CheckReport, Issue
from .profile import NUMERIC_KINDS, bin_proportions, column_kind, numeric_values, profile_dataframe


def _n(x) -> str:
    if x is None:
        return "n/a"
    x = float(x)
    if x.is_integer():
        return f"{int(x):,}"
    return f"{x:.4g}" if abs(x) < 0.1 else f"{x:,.2f}"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def psi(expected, actual, eps: float = 1e-4) -> float:
    e = np.clip(np.asarray(expected, dtype=float), eps, None)
    a = np.clip(np.asarray(actual, dtype=float), eps, None)
    return float(np.sum((a - e) * np.log(a / e)))


def _type_hint(b: str, c: str) -> str:
    if b in NUMERIC_KINDS and c == "string":
        return ("Some rows probably contain non-numeric text (currency symbols, units, 'N/A') "
                "so the whole column is now read as text.")
    if b == "integer" and c == "float":
        return "Fractional values appeared in a column that used to hold whole numbers."
    if b == "float" and c == "integer":
        return "The column now only contains whole numbers; a rounding or casting step may have been added upstream."
    if b == "string" and c in NUMERIC_KINDS:
        return "The column now parses as numbers; an upstream cast or a changed export format is likely."
    if "datetime" in (b, c):
        return "The date/time format of the export may have changed."
    return "An upstream schema or export change is a likely cause."


def check_schema(base: Dict, cur: Dict, cfg: Config) -> List[Issue]:
    bcols, ccols = base["columns"], cur["columns"]
    s = cfg.schema
    out: List[Issue] = []
    added = [c for c in ccols if c not in bcols]
    if s.missing_columns != "off":
        for c in (c for c in bcols if c not in ccols):
            kind = bcols[c]["kind"]
            cands = [a for a in added if ccols[a]["kind"] == kind]
            hint = "The column may have been dropped or renamed upstream."
            if cands:
                hint += f" Added column(s) with the same type that could be a rename: {', '.join(cands)}."
            out.append(Issue("schema.missing_column", s.missing_columns, f"Column '{c}' is missing",
                             f"'{c}' was in the baseline ({kind}) but is not in the current file.",
                             [c], {"baseline_type": kind, "present_in_current": False}, explanation=hint))
    if s.added_columns != "off":
        for c in added:
            kind = ccols[c]["kind"]
            out.append(Issue("schema.added_column", s.added_columns, f"New column '{c}'",
                             f"'{c}' ({kind}) is in the current file but not in the baseline.",
                             [c], {"current_type": kind},
                             explanation="A new field was likely added upstream. Consumers that select explicit "
                                         "columns are unaffected; consumers that read every column may be."))
    if s.type_changes != "off":
        for c in bcols:
            if c in ccols and bcols[c]["kind"] != ccols[c]["kind"]:
                b, k = bcols[c], ccols[c]
                if b["kind"] == "float" and k["kind"] == "integer":
                    continue  # whole-number floats are compatible with the baseline type
                out.append(Issue("schema.type_change", s.type_changes, f"Type of '{c}' changed",
                                 f"'{c}' changed from {b['kind']} to {k['kind']}.", [c],
                                 {"baseline_type": b["kind"], "current_type": k["kind"],
                                  "baseline_dtype": b["dtype"], "current_dtype": k["dtype"]},
                                 explanation=_type_hint(b["kind"], k["kind"])))
    return out


def check_row_count(base: Dict, cur: Dict, cfg: Config) -> List[Issue]:
    rc = cfg.row_count
    b, c = base["rows"], cur["rows"]
    out: List[Issue] = []
    if b > 0:
        change = (c - b) / b * 100
        if abs(change) > rc.max_change_pct:
            expl = ("Rows were added: a re-loaded or appended batch, or duplicated history, is possible."
                    if change > 0 else
                    "Rows were lost: an upstream filter, a partial export, or a truncated load is possible.")
            out.append(Issue("row_count", rc.severity, "Row count changed beyond threshold",
                             f"Rows went from {_n(b)} to {_n(c)} ({change:+.1f}%; allowed ±{rc.max_change_pct:g}%).",
                             [], {"baseline_rows": b, "current_rows": c, "change_pct": round(change, 2),
                                  "max_change_pct": rc.max_change_pct}, explanation=expl))
    if rc.min_rows is not None and c < rc.min_rows:
        out.append(Issue("row_count.min_rows", rc.severity, "Row count below minimum",
                         f"The file has {_n(c)} rows; the configured minimum is {_n(rc.min_rows)}.",
                         [], {"current_rows": c, "min_rows": rc.min_rows},
                         explanation="The export may be partial or empty."))
    return out


def check_null_rate(base: Dict, cur: Dict, cfg: Config) -> List[Issue]:
    nr = cfg.null_rate
    out: List[Issue] = []
    for col, b in base["columns"].items():
        c = cur["columns"].get(col)
        if c is None:
            continue
        limit = nr.columns.get(col, nr.max_increase)
        inc = c["null_rate"] - b["null_rate"]
        if inc > limit + 1e-12:
            out.append(Issue("null_rate", nr.severity, f"Null rate rose in '{col}'",
                             f"Null rate in '{col}' rose from {_pct(b['null_rate'])} to {_pct(c['null_rate'])} "
                             f"(+{inc * 100:.1f} points; allowed +{limit * 100:.1f}).",
                             [col], {"baseline_null_rate": round(b["null_rate"], 4),
                                     "current_null_rate": round(c["null_rate"], 4),
                                     "increase": round(inc, 4), "allowed_increase": limit,
                                     "current_null_count": c["null_count"]},
                             explanation="A failed join, a parsing problem, or an upstream field that stopped "
                                         "being populated can produce a sudden jump in missing values."))
    return out


def check_categorical(base: Dict, df: pd.DataFrame, cfg: Config) -> List[Issue]:
    cc = cfg.categorical
    out: List[Issue] = []
    for col, b in base["columns"].items():
        if "categories" not in b or col not in df.columns:
            continue
        if cc.columns is not None and col not in cc.columns:
            continue
        s = df[col].dropna()
        if s.empty or column_kind(df[col]) != "string":
            continue
        s = s.astype(str)
        known = set(b["categories"])
        unexpected = s[~s.isin(known)]
        rate = len(unexpected) / len(s)
        if len(unexpected) == 0 or rate <= cc.max_unexpected_rate + 1e-12:
            continue
        counts = unexpected.value_counts()
        folded = {k.strip().lower() for k in known}
        variants = sum(1 for v in counts.index if v.strip().lower() in folded)
        expl = "A new category was probably introduced upstream."
        if variants:
            expl = (f"{variants} unexpected value(s) match a known category apart from case or whitespace, "
                    "which suggests inconsistent formatting upstream. ") + expl
        out.append(Issue("categorical", cc.severity, f"Unexpected values in '{col}'",
                         f"{_n(len(counts))} value(s) not seen in the baseline appear in {_n(len(unexpected))} rows "
                         f"({_pct(rate)} of non-null rows; allowed {_pct(cc.max_unexpected_rate)}).",
                         [col], {"unexpected_distinct_values": len(counts), "unexpected_rows": int(len(unexpected)),
                                 "unexpected_rate": round(rate, 4), "baseline_category_count": len(known),
                                 "case_or_whitespace_variants": variants},
                         examples=[f"{v} ({c} rows)" for v, c in counts.head(cfg.report.max_examples).items()]
                         if cfg.report.show_values else [], explanation=expl))
    return out


def check_duplicates(base: Dict, df: pd.DataFrame, cfg: Config) -> List[Issue]:
    dp = cfg.duplicates
    n = len(df)
    if n == 0:
        return []
    keys = dp.columns
    if keys:
        missing = [k for k in keys if k not in df.columns]
        if missing:
            return [Issue("duplicates.config", dp.severity, "Duplicate-check key column missing",
                          f"Configured key column(s) {missing} are not in the current file, so uniqueness "
                          "could not be verified.", missing, {"missing_key_columns": len(missing)},
                          explanation="The key column may have been renamed or dropped.")]
        mask = df.duplicated(subset=keys, keep="first")
        base_rate = 0.0
    else:
        mask = df.duplicated(keep="first")
        base_rate = base["duplicate_rows"] / base["rows"] if base["rows"] else 0.0
    dups = int(mask.sum())
    rate = dups / n
    if rate - base_rate <= dp.max_duplicate_rate + 1e-12 or dups == 0:
        return []
    examples: List[str] = []
    if keys and cfg.report.show_values:
        vc = df.loc[mask, keys].astype(str).agg(" | ".join, axis=1).value_counts()
        examples = [f"{k} ({c + 1} rows)" for k, c in vc.head(cfg.report.max_examples).items()]
    what = f"key {keys}" if keys else "whole rows"
    return [Issue("duplicates", dp.severity, "Duplicate rows detected",
                  f"{_n(dups)} duplicate row(s) by {what} ({_pct(rate)} of rows; baseline {_pct(base_rate)}, "
                  f"allowed increase {_pct(dp.max_duplicate_rate)}).", list(keys),
                  {"duplicate_rows": dups, "duplicate_rate": round(rate, 4),
                   "baseline_duplicate_rate": round(base_rate, 4), "key_columns": ", ".join(keys) or "(all columns)"},
                  examples=examples,
                  explanation="A batch may have been loaded twice, or a join fanned out rows.")]


def _scale_hint(b_mean: float, c_mean: float) -> str:
    if not b_mean or not c_mean:
        return ""
    ratio = c_mean / b_mean
    for factor, label in ((100, "100x"), (1000, "1000x"), (0.01, "1/100"), (0.001, "1/1000")):
        if abs(ratio / factor - 1) < 0.1:
            return (f" The mean changed by roughly {label}, which is typical of a unit change "
                    "(for example dollars vs. cents).")
    return ""


def check_distribution(base: Dict, cur: Dict, df: pd.DataFrame, cfg: Config) -> List[Issue]:
    ds = cfg.distribution
    out: List[Issue] = []
    for col, b in base["columns"].items():
        bn = b.get("numeric")
        c = cur["columns"].get(col)
        if not bn or c is None or c["kind"] not in NUMERIC_KINDS:
            continue
        if ds.columns is not None and col not in ds.columns:
            continue
        if ds.columns is None and b["kind"] == "integer" and b["unique_count"] == base["rows"] > 1:
            continue  # identifier-like column (all values unique); name it in `columns` to check it anyway
        v = numeric_values(df[col])
        if v.empty:
            continue
        c_mean = float(v.mean())
        score = psi(bn["bin_props"], bin_proportions(v.to_numpy(), bn["bin_edges"]))
        if bn["std"] > 0:
            shift = abs(c_mean - bn["mean"]) / bn["std"]
            shifted = shift > ds.max_mean_shift_std
        else:
            shift = None
            shifted = abs(c_mean - bn["mean"]) > 1e-12
        if score <= ds.psi_threshold and not shifted:
            continue
        shift_txt = "n/a (baseline was constant)" if shift is None else f"{shift:.2f} std"
        out.append(Issue("distribution", ds.severity, f"Distribution shift in '{col}'",
                         f"Mean of '{col}' moved from {_n(bn['mean'])} to {_n(c_mean)} (shift {shift_txt}; "
                         f"allowed {ds.max_mean_shift_std:g}); PSI {score:.3f} (allowed {ds.psi_threshold:g}).",
                         [col], {"baseline_mean": round(bn["mean"], 4), "current_mean": round(c_mean, 4),
                                 "mean_shift_std": None if shift is None else round(shift, 3),
                                 "psi": round(score, 4), "baseline_median": round(bn["p50"], 4),
                                 "current_median": round(float(v.median()), 4),
                                 "current_min": round(float(v.min()), 4), "current_max": round(float(v.max()), 4)},
                         explanation="The population being exported may have changed (a new filter, segment or "
                                     "time window), or an upstream calculation changed." + _scale_hint(bn["mean"], c_mean)))
    return out


def _consumers_for(issue: Issue, cfg: Config) -> List[Dict[str, Any]]:
    cols = set(issue.columns)
    out = []
    for c in cfg.consumers:
        matched = sorted(cols & set(c.columns)) if cols else sorted(c.columns)
        if matched or not cols:
            out.append({"name": c.name, "type": c.type, "owner": c.owner, "columns": matched,
                        "dataset_wide": not cols})
    return out


def run_checks(baseline: Dict, df: pd.DataFrame, cfg: Config, data_file: str = "",
               baseline_label: str = "baseline", config_label: str = "defaults") -> CheckReport:
    base = baseline["profile"]
    cur = profile_dataframe(df, cfg.max_categories)
    checks = {  # name -> (enabled, run)
        "schema": (any(v != "off" for v in vars(cfg.schema).values()), lambda: check_schema(base, cur, cfg)),
        "row_count": (cfg.row_count.severity != "off", lambda: check_row_count(base, cur, cfg)),
        "null_rate": (cfg.null_rate.severity != "off", lambda: check_null_rate(base, cur, cfg)),
        "categorical": (cfg.categorical.severity != "off", lambda: check_categorical(base, df, cfg)),
        "duplicates": (cfg.duplicates.severity != "off", lambda: check_duplicates(base, df, cfg)),
        "distribution": (cfg.distribution.severity != "off", lambda: check_distribution(base, cur, df, cfg)),
    }
    ran = [name for name, (enabled, _) in checks.items() if enabled]
    issues = [issue for name in ran for issue in checks[name][1]()]
    for i in issues:
        i.consumers = _consumers_for(i, cfg)
    issues.sort(key=lambda i: (-LEVEL[i.severity], i.check, ",".join(i.columns)))
    return CheckReport(data_file=data_file, baseline_label=baseline_label, baseline_rows=base["rows"],
                       current_rows=cur["rows"], current_columns=len(cur["columns"]), issues=issues,
                       checks_run=ran, fail_on=cfg.fail_on, max_examples=cfg.report.max_examples,
                       config_label=config_label)
