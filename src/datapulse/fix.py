"""AI repair agent: proposes fixes for a failed check, applies them to a copy, and proves they worked.

The agent can only use the safe repair operations defined here (no arbitrary code). Every operation it applies
is logged and replayed onto the original file to produce the fixed file, and the checks run again on the result,
so "9 issues -> 1" is measured, not claimed. Tool results contain counts and statistics, never data values.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from .agent import _tool
from .ai import create, get_client, json_format, safe_measured, slim_profile, text_of
from .checks import run_checks
from .config import Config
from .errors import DataPulseError
from .profile import profile_dataframe


class RepairError(ValueError):
    """An operation that can't be applied as asked; the message goes back to the agent."""


def _need(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        raise RepairError(f"There is no column named '{column}'.")
    return df[column]


def _numbers(s: pd.Series) -> pd.Series:
    """Read the leading number from text like "5 units", "$1,234.50" or " 12 kg"; anything else becomes NaN."""
    found = s.astype("string").str.extract(r"^\s*[$€£]?\s*([-+]?\d[\d,]*(?:\.\d+)?)", expand=False)
    return pd.to_numeric(found.str.replace(",", "", regex=False), errors="coerce").astype("float64")


def rename_column(df, baseline, column: str, new_name: str):
    _need(df, column)
    if new_name in df.columns:
        raise RepairError(f"A column named '{new_name}' already exists.")
    return df.rename(columns={column: new_name}), f"Renamed '{column}' to '{new_name}'."


def scale_column(df, baseline, column: str, factor: float):
    s = _need(df, column)
    if not np.isfinite(factor) or factor == 0 or abs(factor) > 1e6:
        raise RepairError("The factor must be a non-zero number no larger than 1,000,000.")
    values = pd.to_numeric(s, errors="coerce")
    if values.notna().sum() == 0:
        raise RepairError(f"'{column}' has no numbers to scale; convert it to numbers first.")
    out = df.copy()
    out[column] = values * factor
    return out, f"Multiplied {int(values.notna().sum()):,} values in '{column}' by {factor:g}."


def convert_to_number(df, baseline, column: str):
    s = _need(df, column)
    converted = _numbers(s)
    lost = int((s.notna() & converted.isna()).sum())
    out = df.copy()
    out[column] = converted
    return out, (f"Converted '{column}' to numbers ({int(converted.notna().sum()):,} values)."
                 + (f" {lost:,} values could not be read as numbers and are now empty." if lost else ""))


def remove_duplicates(df, baseline, columns: List[str]):
    for c in columns:
        _need(df, c)
    out = df.drop_duplicates(subset=columns or None, keep="first").reset_index(drop=True)
    what = f"key {', '.join(columns)}" if columns else "whole rows"
    return out, f"Removed {len(df) - len(out):,} duplicate rows (by {what}), keeping the first of each."


def fill_missing(df, baseline, column: str, strategy: str):
    s = _need(df, column)
    missing = int(s.isna().sum())
    if not missing:
        raise RepairError(f"'{column}' has no empty values.")
    if strategy == "median":
        values = pd.to_numeric(s, errors="coerce")
        if values.notna().sum() == 0:
            raise RepairError(f"'{column}' has no numbers to take a median of.")
        fill = values.median()
    elif strategy == "most_common":
        if s.dropna().empty:
            raise RepairError(f"'{column}' has no values to choose from.")
        fill = s.mode(dropna=True).iloc[0]
    else:
        raise RepairError("strategy must be 'median' or 'most_common'.")
    out = df.copy()
    out[column] = s.fillna(fill)
    return out, f"Filled {missing:,} empty values in '{column}' with the column's {strategy.replace('_', ' ')} value."


def drop_rows_missing(df, baseline, column: str):
    s = _need(df, column)
    out = df[s.notna()].reset_index(drop=True)
    return out, f"Removed {len(df) - len(out):,} rows where '{column}' was empty."


def drop_column(df, baseline, column: str):
    _need(df, column)
    return df.drop(columns=[column]), f"Removed the column '{column}'."


def standardize_categories(df, baseline, column: str):
    """Map values that equal a baseline category apart from case/whitespace onto that category."""
    s = _need(df, column)
    known = (baseline["columns"].get(column) or {}).get("categories")
    if not known:
        raise RepairError(f"The baseline has no list of known values for '{column}'.")
    canonical = {str(k).strip().lower(): k for k in known}
    text = s.astype("string")
    mapped = text.map(lambda v: v if pd.isna(v) or v in known else canonical.get(v.strip().lower(), v))
    changed = int((mapped != text).fillna(False).sum())
    out = df.copy()
    out[column] = mapped.where(s.notna(), s)
    return out, f"Standardized {changed:,} values in '{column}' to the baseline's spelling."


OPERATIONS: Dict[str, Callable] = {f.__name__: f for f in (
    rename_column, scale_column, convert_to_number, remove_duplicates, fill_missing, drop_rows_missing,
    drop_column, standardize_categories)}


def apply_operations(df: pd.DataFrame, baseline_profile: Dict[str, Any], operations: List[Dict[str, Any]]):
    """Replay logged operations onto a file. Returns the fixed frame and one effect line per operation."""
    effects = []
    for op in operations:
        df, effect = OPERATIONS[op["operation"]](df, baseline_profile, **op["args"])
        effects.append(effect)
    return df, effects


def _counts(issues) -> Dict[str, int]:
    out = {"total": len(issues)}
    for sev in ("critical", "warning", "info"):
        out[sev] = sum(1 for i in issues if i.severity == sev)
    return out


# ---------- the agent ----------

REASON = {"reason": {"type": "string", "description": "One sentence: which issue this fixes and why this is the right fix"}}
COLUMN = {"column": {"type": "string", "description": "Exact column name"}}

TOOLS = [
    _tool("list_issues", "Run every check on the current working copy and list the issues that remain."),
    _tool("get_column_stats", "Baseline vs. working-copy statistics for one column (type, empty rate, distinct "
                              "values, numeric summary).", COLUMN),
    _tool("rename_column", "Rename a column, e.g. when the baseline's column arrives under a new name.",
          {**COLUMN, "new_name": {"type": "string"}, **REASON}),
    _tool("scale_column", "Multiply a numeric column by a factor, e.g. 0.01 to turn cents back into dollars.",
          {**COLUMN, "factor": {"type": "number"}, **REASON}),
    _tool("convert_to_number", "Turn a text column back into numbers by reading each value's leading number "
                                 "(handles units like '5 units', commas and currency signs).",
          {**COLUMN, **REASON}),
    _tool("remove_duplicates", "Remove duplicate rows, keeping the first. Give key columns, or [] for whole rows.",
          {"columns": {"type": "array", "items": {"type": "string"}}, **REASON}),
    _tool("fill_missing", "Fill empty values with the column's median or most common value. Only for columns "
                          "where a typical value is a safe stand-in; never for identifiers.",
          {**COLUMN, "strategy": {"type": "string", "enum": ["median", "most_common"]}, **REASON}),
    _tool("drop_rows_missing", "Remove rows where a column is empty.", {**COLUMN, **REASON}),
    _tool("drop_column", "Remove a column, e.g. one that is new and unexpected.", {**COLUMN, **REASON}),
    _tool("standardize_categories", "Map values that match a known baseline value apart from upper/lower case or "
                                    "spaces onto the baseline's spelling.", {**COLUMN, **REASON}),
    _tool("undo_last", "Undo the most recent repair."),
]

SYSTEM = """You are a data engineer repairing a batch of data that failed its quality checks. You work on a copy; \
the original file is never changed, and a person reviews your repairs before using the result.

Start with list_issues, look at the columns involved, then apply repairs one at a time with the repair tools and \
check the effect with list_issues. Undo anything that makes things worse.

Only repair what the evidence supports:
- A column that is ~100x or ~1000x its baseline scale is a unit change; scale it back.
- Numbers that arrived as text should be converted back.
- Duplicate keys should be removed.
- Values that differ from known values only by case or spaces should be standardized.
- Do not invent data. Missing columns cannot be recreated, and filling empty identifiers would be fabrication; \
leave those for a person and say so.
- New columns are usually harmless; remove one only if it clearly does not belong.

When you are done, summarize in one or two short sentences, and give each not_fixed reason in one sentence. You \
only ever see counts and statistics, never the data itself."""

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary", "not_fixed", "warnings"],
    "properties": {
        "summary": {"type": "string"},
        "not_fixed": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["issue", "why"],
            "properties": {"issue": {"type": "string"}, "why": {"type": "string"}}}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
}


MAP_SYSTEM = """You are mapping a new data source onto a project's standard format. The standard is the \
project's baseline: its column names, types and value scales. The source holds the same kind of data in its own \
shape - different column names, other units (cents instead of dollars), numbers stored as text, different \
capitalisation. Build a recipe with the tools so the source matches the standard.

Start with list_issues, then compare columns with get_column_stats: look up each unmatched standard column and \
each unmatched source column, and match them by type, empty rate, distinct values and numeric range, not by name \
alone. Then:
- rename source columns to their standard names;
- scale a column whose values are ~100x or ~1000x the standard's (after renaming it);
- convert numbers stored as text;
- standardize capitalisation and spacing of known values;
- remove duplicate rows by the standard's key columns when the standard is unique on them;
- remove source-only columns that have no counterpart in the standard.
Never guess: if the statistics don't clearly support a match, leave it and list it under not_fixed. The steps you \
apply are replayed automatically on every future load once a person approves them. You only ever see counts and \
statistics, never the data itself."""

MAP_TOOLS = [t for t in TOOLS if t["name"] not in ("fill_missing", "drop_rows_missing")]


def _repair_intro(session, before) -> str:
    return (f"This batch has {len(session.original):,} rows and failed with {len(before.issues)} issue(s). "
            "Repair what can safely be repaired.")


def _map_intro(session, before) -> str:
    standard = list(session.baseline["profile"]["columns"])
    return (f"Standard columns: {json.dumps(standard)}\n"
            f"Source columns (after {len(session.operations)} automatic step(s)): {json.dumps(list(session.working.columns))}\n"
            f"The source has {len(session.original):,} rows and currently shows {len(session.check().issues)} issue(s) "
            "against the standard. Map it.")


MODES = {"repair": (SYSTEM, TOOLS, _repair_intro), "map": (MAP_SYSTEM, MAP_TOOLS, _map_intro)}


@dataclass
class RepairSession:
    original: pd.DataFrame
    baseline: Dict[str, Any]  # a baseline file's contents (build_baseline output)
    cfg: Config
    reference: Optional[pd.DataFrame] = None  # the baseline's rows, so the ML drift check runs too
    operations: List[Dict[str, Any]] = field(default_factory=list)

    allowed: Optional[set] = None  # tool names the agent may use; None means all

    def __post_init__(self):
        self.working, _ = apply_operations(self.original, self.baseline["profile"], self.operations)

    def check(self, df: Optional[pd.DataFrame] = None):
        return run_checks(self.baseline, self.working if df is None else df, self.cfg, reference=self.reference)

    def list_issues(self) -> Any:
        report = self.check()
        return {"rows": len(self.working), "issues": [
            {"check": i.check, "severity": i.severity, "title": i.title, "columns": i.columns,
             "measured": safe_measured(i.measured)} for i in report.issues]}

    def get_column_stats(self, column: str) -> Any:
        current = slim_profile(profile_dataframe(self.working, self.cfg.max_categories))
        base = slim_profile(self.baseline["profile"])
        return {"column": column, "baseline": base["columns"].get(column, "not present"),
                "working_copy": current["columns"].get(column, "not present")}

    def undo_last(self) -> Any:
        if not self.operations:
            raise RepairError("There is nothing to undo.")
        undone = self.operations.pop()
        self.working, _ = apply_operations(self.original, self.baseline["profile"], self.operations)
        return f"Undid {undone['operation']} on {undone['args']}."

    def repair(self, name: str, args: Dict[str, Any]) -> Any:
        reason = args.pop("reason", "")
        self.working, effect = OPERATIONS[name](self.working, self.baseline["profile"], **args)
        self.operations.append({"operation": name, "args": args, "reason": reason, "effect": effect})
        return {"effect": effect, "issues_remaining": _counts(self.check().issues)}

    def call(self, name: str, args: Dict[str, Any]) -> Any:
        if self.allowed is not None and name not in self.allowed:
            raise RepairError(f"{name} is not available for this task.")
        if name in OPERATIONS:
            return self.repair(name, dict(args))
        if name in ("list_issues", "get_column_stats", "undo_last"):
            return getattr(self, name)(**args)
        raise RepairError(f"Unknown tool {name}")


def run_repair(session: RepairSession, client=None, max_turns: int = 16,
               on_step: Optional[Callable[[str, Dict[str, Any]], None]] = None, mode: str = "repair") -> Dict[str, Any]:
    """Let the agent repair a copy, then replay its operations onto the original and re-run every check.

    mode "map" uses the same loop to translate a new source into the project's standard format (see unify.py).
    """
    cfg = session.cfg
    client = client or get_client(cfg)
    system, tools, intro = MODES[mode]
    session.allowed = {t["name"] for t in tools}
    before = session.check(session.original)
    messages: List[Dict[str, Any]] = [{"role": "user", "content": intro(session, before)}]
    for _ in range(max_turns):
        response = create(client, cfg, system=system, tools=tools, messages=messages,
                          output_config={**json_format(SCHEMA), "effort": "medium"})
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            try:
                result = json.loads(text_of(response))
            except ValueError as e:
                raise DataPulseError(f"The repair summary was not valid JSON: {e}")
            break
        results = []
        for c in calls:
            if on_step:
                on_step(c.name, c.input)
            try:
                content, error = json.dumps(session.call(c.name, c.input), default=str), False
            except (RepairError, TypeError) as e:
                content, error = str(e), True
            results.append({"type": "tool_result", "tool_use_id": c.id, "content": content, "is_error": error})
        messages.append({"role": "user", "content": results})
    else:
        raise DataPulseError(f"The AI agent did not finish within {max_turns} steps.")

    fixed, effects = apply_operations(session.original, session.baseline["profile"], session.operations)
    after = session.check(fixed)
    return {
        **result,
        "operations": [{**op, "effect": eff} for op, eff in zip(session.operations, effects)],
        "before": _counts(before.issues),
        "after": {**_counts(after.issues), "remaining": [i.title for i in after.issues]},
        "rows": {"before": len(session.original), "after": len(fixed)},
        "model": cfg.ai.model,
        "fixed_frame": fixed,  # removed by callers before serializing
    }
