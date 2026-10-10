"""Propose validation rules from a profile. Suggestions are only ever printed or
written to a separate file; the user's datapulse.yml is never modified."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

import yaml


@dataclass
class Suggestion:
    section: str
    rationale: str
    fragment: Dict[str, Any]


def suggest_rules(profile: Dict[str, Any]) -> List[Suggestion]:
    cols, rows = profile["columns"], profile["rows"]
    out: List[Suggestion] = []

    out.append(Suggestion(
        "schema", f"The baseline has {len(cols)} columns; flag dropped columns and type changes as critical.",
        {"missing_columns": "critical", "added_columns": "warning", "type_changes": "critical"}))

    out.append(Suggestion(
        "row_count", f"The baseline has {rows:,} rows; flag swings of more than 20%.",
        {"severity": "warning", "max_change_pct": 20}))

    never_null = [c for c, i in cols.items() if i["null_count"] == 0]
    if never_null and rows:
        out.append(Suggestion(
            "null_rate", f"{len(never_null)} column(s) have no nulls in the baseline, so any null is a regression: "
                         f"{', '.join(never_null)}.",
            {"severity": "critical", "max_increase": 0.05, "columns": {c: 0.0 for c in never_null}}))
    elif rows:
        out.append(Suggestion("null_rate", "Every column has some nulls; allow a 5 point increase.",
                              {"severity": "critical", "max_increase": 0.05}))

    unique = [c for c, i in cols.items() if rows >= 2 and i["null_count"] == 0 and i["unique_count"] == rows]
    id_like = [c for c in unique if "id" in c.lower()]
    key = id_like[:1] or (unique[:1] if len(unique) == 1 else [])
    if key:
        out.append(Suggestion("duplicates", f"'{key[0]}' is unique and never null across all {rows:,} rows; "
                                            "treat it as a key.",
                              {"severity": "critical", "max_duplicate_rate": 0.0, "columns": key}))

    categorical = [c for c, i in cols.items() if "categories" in i]
    if categorical:
        detail = "; ".join(f"{c}: {len(cols[c]['categories'])} values" for c in categorical)
        out.append(Suggestion("categorical", f"Low-cardinality text columns where new values deserve a look "
                                             f"({detail}).",
                              {"severity": "warning", "max_unexpected_rate": 0.0, "columns": categorical}))

    numeric = [c for c, i in cols.items() if i.get("numeric") and i["numeric"]["std"] > 0]
    if numeric:
        out.append(Suggestion("distribution", f"Numeric columns with spread in the baseline: {', '.join(numeric)}.",
                              {"severity": "warning", "psi_threshold": 0.2, "max_mean_shift_std": 1.0,
                               "columns": numeric}))
    return out


def render_yaml(suggestions: List[Suggestion]) -> str:
    lines = [
        "# Rules suggested by `datapulse suggest`. Review each block, then copy what you want",
        "# into the `checks:` section of datapulse.yml. Nothing here has been applied.",
        "version: 1", "checks:",
    ]
    for s in suggestions:
        lines.append(f"  # {s.rationale}")
        block = yaml.safe_dump({s.section: s.fragment}, sort_keys=False, default_flow_style=None).rstrip()
        lines.extend("  " + ln for ln in block.splitlines())
    return "\n".join(lines) + "\n"
