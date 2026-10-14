"""Shared project logic for the API routers: loading projects, saving datasets and baselines, running checks."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
from fastapi import HTTPException

from datapulse.ai import explain_issues
from datapulse.baseline import build_baseline, describe
from datapulse.checks import run_checks
from datapulse.config import Config, parse_config_text
from datapulse.errors import DataPulseError
from datapulse.io import SUPPORTED, load_dataframe
from datapulse.profile import chart_data
from datapulse.report import render_json, render_markdown

from .db import connect, now_iso, upload_dir

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_INGEST_ROWS = 50_000
ROOT = Path(__file__).resolve().parents[1]
CONFIG_LABEL = "project config (dashboard)"


def loads(text: Optional[str]) -> Any:
    return json.loads(text) if text else None


def project_row(project_id: str) -> sqlite3.Row:
    with connect() as db:
        row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def project_config(text: str) -> Config:
    try:
        return parse_config_text(text, CONFIG_LABEL)
    except DataPulseError as exc:
        raise HTTPException(422, str(exc)) from exc


def save_upload(filename: str, content: bytes) -> Path:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(415, "Supported data files: CSV, JSON, JSONL/NDJSON, and Parquet")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Upload exceeds the 25 MB limit")
    path = upload_dir() / f"{uuid.uuid4().hex}{suffix}"
    path.write_bytes(content)
    return path


def load_upload(path: Path) -> pd.DataFrame:
    try:
        return load_dataframe(path)
    except DataPulseError as exc:
        # Report the problem without exposing the server-side storage path.
        raise HTTPException(400, str(exc).replace(str(path), "the uploaded file")) from exc


def record_dataset(project_id: str, name: str, path: Path, role: str, df: pd.DataFrame) -> str:
    dataset_id = uuid.uuid4().hex[:12]
    with connect() as db:
        db.execute("INSERT INTO datasets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (dataset_id, project_id, name, str(path), role, len(df), json.dumps(list(df.columns)), now_iso()))
    return dataset_id


def baseline_summary(data: Dict[str, Any]) -> Dict[str, Any]:
    """What the dashboard shows about a baseline: aggregates only, no category values or histograms."""
    prof = data["profile"]
    return {
        "baseline_version": data.get("baseline_version", 1),
        "created_at": data.get("created_at"),
        "source": data.get("source", {}).get("file"),
        "rows": prof["rows"],
        "columns": {name: {k: info.get(k) for k in ("kind", "dtype", "null_rate", "unique_count")}
                    for name, info in prof["columns"].items()},
    }


def save_baseline(project: sqlite3.Row, name: str, path: Path, df: pd.DataFrame) -> Dict[str, Any]:
    if len(df) == 0:
        raise HTTPException(400, f"{name} has no rows; a baseline needs data.")
    cfg = project_config(project["config_yaml"])
    previous = loads(project["baseline_json"])
    version = previous["baseline_version"] + 1 if previous else 1
    data = build_baseline(df, path, version, cfg.max_categories)
    data["source"]["file"] = name  # the name the user knows, not the storage path
    dataset_id = record_dataset(project["id"], name, path, "baseline", df)
    with connect() as db:
        db.execute("UPDATE projects SET baseline_json = ?, baseline_dataset_id = ? WHERE id = ?",
                   (json.dumps(data), dataset_id, project["id"]))
    return {"dataset_id": dataset_id, "role": "baseline", "baseline": baseline_summary(data)}


def dataset_rows(dataset_id: Optional[str]) -> Optional[pd.DataFrame]:
    """A stored dataset's rows, or None when the file is gone (older projects, cleaned-up uploads)."""
    with connect() as db:
        row = db.execute("SELECT filename FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    try:
        return load_dataframe(row["filename"]) if row else None
    except DataPulseError:
        return None


def run_check(project: sqlite3.Row, name: str, path: Path, df: pd.DataFrame, use_ai: bool,
               actor: str, extra: Optional[Dict[str, Any]] = None, checked: Optional[pd.DataFrame] = None,
               combined: bool = False) -> Dict[str, Any]:
    """Check df against the baseline and save the run. `checked` is the part to check when it differs from
    the stored data (a unified table carries a _source column that isn't part of the standard). A combined table
    holds several sources' rows, so its row count isn't compared with the baseline's."""
    if not project["baseline_json"]:
        raise HTTPException(409, "Create a baseline dataset before running a check")
    cfg = project_config(project["config_yaml"])
    if combined:
        cfg = replace(cfg, row_count=replace(cfg.row_count, severity="off"))
    baseline = json.loads(project["baseline_json"])
    checked = df if checked is None else checked
    report = run_checks(baseline, checked, cfg, data_file=name, baseline_label=describe(baseline, "project baseline"),
                        config_label=CONFIG_LABEL, reference=dataset_rows(project["baseline_dataset_id"]))
    ai_note = explain_issues(report, cfg) if use_ai else ""
    doc = json.loads(render_json(report))
    dataset_id = record_dataset(project["id"], name, path, "candidate", df)
    run_id = uuid.uuid4().hex[:12]
    doc.update(run_id=run_id, ai_note=ai_note or None, markdown=render_markdown(report),
               charts=chart_data(baseline["profile"], checked), **(extra or {}))
    with connect() as db:
        db.execute("INSERT INTO runs (id, project_id, dataset_id, status, issue_count, report_json, created_at, "
                   "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (run_id, project["id"], dataset_id, doc["status"],
                                                                 len(doc["issues"]), json.dumps(doc), now_iso(), actor))
    return doc


def handle_data(project_id: str, name: str, path: Path, role: str, actor: str, use_ai: bool = False) -> Dict[str, Any]:
    try:
        project = project_row(project_id)
        df = load_upload(path)
        if role == "baseline":
            return save_baseline(project, name, path, df)
        return run_check(project, name, path, df, use_ai, actor)
    except HTTPException:
        path.unlink(missing_ok=True)  # rejected uploads are not kept
        raise


def ai_call(call):
    """Run an AI feature; configuration and upstream problems become readable HTTP errors."""
    try:
        return call()
    except DataPulseError as exc:
        raise HTTPException(503 if "environment variable" in str(exc) else 502, str(exc)) from exc


