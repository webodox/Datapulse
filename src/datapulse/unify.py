"""Unify: bring every source into the project's standard format, then combine them into one table.

The standard format is the project's baseline (its columns, types and value scales). Mapping a source produces a
recipe built from the repair agent's safe operations (rename, scale, convert, standardize, de-duplicate, drop):
1. columns whose names match apart from case and punctuation are mapped without AI;
2. the AI proposes the rest from statistics only (the same agent loop as repairs, in "map" mode);
3. a person approves the recipe once;
4. every later pull replays it with no AI, so loads are fast, free and predictable. A step that no longer applies
   (the source changed shape) is reported, never guessed around, and the checks flag what's left.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .ai import get_api_key
from .config import Config
from .fix import OPERATIONS, RepairError, RepairSession, _counts, apply_operations, run_repair

SOURCE_COLUMN = "_source"


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def name_matches(columns: List[str], standard: List[str]) -> List[Dict[str, Any]]:
    """Renames for columns that equal a standard column apart from case, spaces and punctuation."""
    by_key: Dict[str, List[str]] = {}
    for s in standard:
        if s not in columns:
            by_key.setdefault(_key(s), []).append(s)
    claimed, ops = set(), []
    for c in columns:
        targets = by_key.get(_key(c), [])
        if c not in standard and len(targets) == 1 and targets[0] not in claimed:
            claimed.add(targets[0])
            ops.append({"operation": "rename_column", "args": {"column": c, "new_name": targets[0]},
                        "reason": "Same name as the standard column apart from capitalisation or punctuation."})
    return ops


def apply_recipe(df: pd.DataFrame, baseline_profile: Dict[str, Any], recipe: List[Dict[str, Any]]
                 ) -> Tuple[pd.DataFrame, List[str], List[str]]:
    """Replay a recipe; steps that no longer apply are skipped and reported. Returns (frame, effects, problems)."""
    effects, problems = [], []
    for n, op in enumerate(recipe, 1):
        try:
            df, effect = OPERATIONS[op["operation"]](df, baseline_profile, **op["args"])
            effects.append(effect)
        except (RepairError, KeyError, TypeError) as e:
            problems.append(f"Step {n} ({op['operation'].replace('_', ' ')}) no longer applies: {e}")
    return df, effects, problems


def validate_recipe(recipe: Any) -> List[Dict[str, Any]]:
    """A recipe sent back by the browser after review: known operations with plain arguments only."""
    if not isinstance(recipe, list) or len(recipe) > 100:
        raise ValueError("A recipe is a list of at most 100 steps.")
    clean = []
    for step in recipe:
        if not isinstance(step, dict) or step.get("operation") not in OPERATIONS or not isinstance(step.get("args"), dict):
            raise ValueError("Each step needs a known operation and its arguments.")
        args = step["args"]
        if not all(isinstance(k, str) and (v is None or isinstance(v, (str, int, float, list))) for k, v in args.items()):
            raise ValueError("Step arguments must be plain values.")
        clean.append({"operation": step["operation"], "args": args, "reason": str(step.get("reason", ""))[:500]})
    return clean


def propose_mapping(df: pd.DataFrame, baseline: Dict[str, Any], cfg: Config, client=None,
                    reference: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """A proposed recipe for one source, with before/after issue counts. Nothing is saved."""
    standard = list(baseline["profile"]["columns"])
    session = RepairSession(df, baseline, cfg, reference=reference, operations=name_matches(list(df.columns), standard))
    automatic = len(session.operations)
    if (client or get_api_key(cfg)) and session.check().issues:
        result = run_repair(session, client=client, mode="map")
        result.pop("fixed_frame")
    else:
        mapped, effects = apply_operations(df, baseline["profile"], session.operations)
        after = session.check(mapped)
        result = {
            "summary": f"Matched {automatic} column(s) by name."
                       + (" AI is off, so nothing else was mapped." if after.issues else ""),
            "not_fixed": [], "warnings": [],
            "operations": [{**op, "effect": e} for op, e in zip(session.operations, effects)],
            "before": _counts(session.check(session.original).issues),
            "after": {**_counts(after.issues), "remaining": [i.title for i in after.issues]},
            "rows": {"before": len(df), "after": len(mapped)}, "model": None,
        }
    result["automatic_steps"] = automatic
    result["source_columns"] = list(df.columns)
    return result


def combine(frames: Dict[str, pd.DataFrame], standard: List[str]) -> pd.DataFrame:
    """Stack mapped sources into one table in the standard's column order, tagging each row with its source.

    Columns a source doesn't have are left empty (the checks will flag them); extra columns are dropped.
    """
    parts = []
    for name, df in frames.items():
        part = df.reindex(columns=standard)
        part.insert(0, SOURCE_COLUMN, name)
        parts.append(part)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=[SOURCE_COLUMN, *standard])
