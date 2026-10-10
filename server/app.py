"""DataPulse web API: projects, baselines, checks, run history and JSON ingestion from any language.

It uses the same engine as the CLI (`datapulse baseline` / `datapulse check`), so a project's
`datapulse.yml`, its baseline and its reports mean exactly the same thing in the dashboard, the CLI
and the GitHub Action. Data is stored under $DATAPULSE_HOME (default `.datapulse/`) in SQLite.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Literal, Optional

import pandas as pd
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from datapulse import __version__
from datapulse.agent import TriageContext, run_triage
from datapulse.ai import draft_rules, explain_issues, get_api_key
from datapulse.baseline import build_baseline, describe
from datapulse.checks import run_checks
from datapulse.config import STARTER_CONFIG, Config, parse_config_text
from datapulse.errors import DataPulseError
from datapulse.io import SUPPORTED, load_dataframe
from datapulse.profile import profile_dataframe
from datapulse.report import render_json, render_markdown
from datapulse.suggest import render_yaml, suggest_rules

from .auth import current_user, needs_login, router as auth_router
from .db import connect, init_db, now_iso, upload_dir

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_INGEST_ROWS = 50_000
ROOT = Path(__file__).resolve().parents[1]
CONFIG_LABEL = "project config (dashboard)"


def _loads(text: Optional[str]) -> Any:
    return json.loads(text) if text else None


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="DataPulse API", version=__version__, lifespan=lifespan,
              description="Data-quality checks for CSV, JSON and Parquet data from any language.")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("DATAPULSE_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
app.include_router(auth_router)


@app.middleware("http")
async def require_login(request: Request, call_next):
    if needs_login(request.url.path, request.method):
        request.state.user = current_user(request)
        if request.state.user is None:
            return JSONResponse({"detail": "Please sign in"}, status_code=401)
    return await call_next(request)


class CreateProject(BaseModel):
    name: str = Field(min_length=2, max_length=80)


class SaveConfig(BaseModel):
    config_yaml: str = Field(max_length=20_000)


class DraftRules(BaseModel):
    request: str = Field(min_length=3, max_length=2_000)
    config_yaml: str = Field(max_length=20_000)  # the editor's current text, saved or not


class Review(BaseModel):
    status: Literal["open", "acknowledged", "resolved"]


class IngestRows(BaseModel):
    dataset_name: str = Field(min_length=1, max_length=100)
    role: Literal["baseline", "candidate"] = "candidate"
    rows: list[dict] = Field(min_length=1, max_length=MAX_INGEST_ROWS)
    use_ai: bool = False


def _project(project_id: str) -> sqlite3.Row:
    with connect() as db:
        row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def _config(text: str) -> Config:
    try:
        return parse_config_text(text, CONFIG_LABEL)
    except DataPulseError as exc:
        raise HTTPException(422, str(exc)) from exc


def _save_upload(filename: str, content: bytes) -> Path:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(415, "Supported data files: CSV, JSON, JSONL/NDJSON, and Parquet")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Upload exceeds the 25 MB limit")
    path = upload_dir() / f"{uuid.uuid4().hex}{suffix}"
    path.write_bytes(content)
    return path


def _load(path: Path) -> pd.DataFrame:
    try:
        return load_dataframe(path)
    except DataPulseError as exc:
        # Report the problem without exposing the server-side storage path.
        raise HTTPException(400, str(exc).replace(str(path), "the uploaded file")) from exc


def _record_dataset(project_id: str, name: str, path: Path, role: str, df: pd.DataFrame) -> str:
    dataset_id = uuid.uuid4().hex[:12]
    with connect() as db:
        db.execute("INSERT INTO datasets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (dataset_id, project_id, name, str(path), role, len(df), json.dumps(list(df.columns)), now_iso()))
    return dataset_id


def _baseline_summary(data: Dict[str, Any]) -> Dict[str, Any]:
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


def _save_baseline(project: sqlite3.Row, name: str, path: Path, df: pd.DataFrame) -> Dict[str, Any]:
    if len(df) == 0:
        raise HTTPException(400, f"{name} has no rows; a baseline needs data.")
    cfg = _config(project["config_yaml"])
    previous = _loads(project["baseline_json"])
    version = previous["baseline_version"] + 1 if previous else 1
    data = build_baseline(df, path, version, cfg.max_categories)
    data["source"]["file"] = name  # the name the user knows, not the storage path
    dataset_id = _record_dataset(project["id"], name, path, "baseline", df)
    with connect() as db:
        db.execute("UPDATE projects SET baseline_json = ?, baseline_dataset_id = ? WHERE id = ?",
                   (json.dumps(data), dataset_id, project["id"]))
    return {"dataset_id": dataset_id, "role": "baseline", "baseline": _baseline_summary(data)}


def _dataset_rows(dataset_id: Optional[str]) -> Optional[pd.DataFrame]:
    """A stored dataset's rows, or None when the file is gone (older projects, cleaned-up uploads)."""
    with connect() as db:
        row = db.execute("SELECT filename FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    try:
        return load_dataframe(row["filename"]) if row else None
    except DataPulseError:
        return None


def _run_check(project: sqlite3.Row, name: str, path: Path, df: pd.DataFrame, use_ai: bool,
               actor: str) -> Dict[str, Any]:
    if not project["baseline_json"]:
        raise HTTPException(409, "Create a baseline dataset before running a check")
    cfg = _config(project["config_yaml"])
    baseline = json.loads(project["baseline_json"])
    report = run_checks(baseline, df, cfg, data_file=name, baseline_label=describe(baseline, "project baseline"),
                        config_label=CONFIG_LABEL, reference=_dataset_rows(project["baseline_dataset_id"]))
    ai_note = explain_issues(report, cfg) if use_ai else ""
    doc = json.loads(render_json(report))
    dataset_id = _record_dataset(project["id"], name, path, "candidate", df)
    run_id = uuid.uuid4().hex[:12]
    doc.update(run_id=run_id, ai_note=ai_note or None, markdown=render_markdown(report))
    with connect() as db:
        db.execute("INSERT INTO runs (id, project_id, dataset_id, status, issue_count, report_json, created_at, "
                   "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (run_id, project["id"], dataset_id, doc["status"],
                                                                 len(doc["issues"]), json.dumps(doc), now_iso(), actor))
    return doc


def _handle(project_id: str, name: str, path: Path, role: str, actor: str, use_ai: bool = False) -> Dict[str, Any]:
    try:
        project = _project(project_id)
        df = _load(path)
        if role == "baseline":
            return _save_baseline(project, name, path, df)
        return _run_check(project, name, path, df, use_ai, actor)
    except HTTPException:
        path.unlink(missing_ok=True)  # rejected uploads are not kept
        raise


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__, "ai": bool(get_api_key(Config()))}


@app.get("/api/projects")
def list_projects() -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT p.id, p.name, p.created_at, p.baseline_json IS NOT NULL AS has_baseline, "
            "(SELECT COUNT(*) FROM runs r WHERE r.project_id = p.id) AS run_count, "
            "(SELECT status FROM runs r WHERE r.project_id = p.id ORDER BY created_at DESC, rowid DESC LIMIT 1) AS latest_status "
            "FROM projects p ORDER BY p.created_at DESC, p.rowid DESC"
        ).fetchall()
    return [{"id": r["id"], "name": r["name"], "created_at": r["created_at"], "has_baseline": bool(r["has_baseline"]),
             "run_count": r["run_count"], "latest_status": r["latest_status"]} for r in rows]


def _create_project(name: str, config_yaml: str = STARTER_CONFIG) -> dict:
    project_id = uuid.uuid4().hex[:12]
    api_key = "dp_" + secrets.token_urlsafe(32)
    created = now_iso()
    with connect() as db:
        db.execute("INSERT INTO projects (id, name, api_key_hash, config_yaml, created_at) VALUES (?, ?, ?, ?, ?)",
                   (project_id, name, hashlib.sha256(api_key.encode()).hexdigest(),
                    config_yaml, created))
    # The key is returned once; only its hash is stored.
    return {"id": project_id, "name": name, "api_key": api_key, "created_at": created}


@app.post("/api/projects")
def create_project(payload: CreateProject) -> dict:
    name = payload.name.strip()
    if len(name) < 2:
        raise HTTPException(422, "Project name must contain at least two non-space characters")
    return _create_project(name)


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> dict:
    project = _project(project_id)
    with connect() as db:
        datasets = db.execute("SELECT id, name, role, row_count, columns_json, created_at FROM datasets "
                              "WHERE project_id = ? ORDER BY created_at DESC, rowid DESC", (project_id,)).fetchall()
        runs = db.execute("SELECT id, status, issue_count, created_at, report_json, created_by, review_status, "
                          "review_by, review_at FROM runs "
                          "WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 30", (project_id,)).fetchall()
    baseline = _loads(project["baseline_json"])
    cfg = _config(project["config_yaml"])
    return {
        "id": project["id"], "name": project["name"], "created_at": project["created_at"],
        "has_baseline": baseline is not None,
        "baseline": _baseline_summary(baseline) if baseline else None,
        "config_yaml": project["config_yaml"],
        "fail_on": cfg.fail_on,
        "consumers": [{"name": c.name, "type": c.type, "owner": c.owner, "columns": c.columns} for c in cfg.consumers],
        "datasets": [{"id": d["id"], "name": d["name"], "role": d["role"], "row_count": d["row_count"],
                      "columns": json.loads(d["columns_json"]), "created_at": d["created_at"]} for d in datasets],
        "runs": [{"id": r["id"], "status": r["status"], "issue_count": r["issue_count"], "created_at": r["created_at"],
                  "created_by": r["created_by"], "review": {"status": r["review_status"], "by": r["review_by"],
                                                            "at": r["review_at"]},
                  "report": json.loads(r["report_json"])} for r in runs],
    }


@app.put("/api/projects/{project_id}/config")
def update_config(project_id: str, payload: SaveConfig) -> dict:
    _project(project_id)
    _config(payload.config_yaml)  # the CLI's strict validation; rejects unknown keys and bad values
    with connect() as db:
        db.execute("UPDATE projects SET config_yaml = ? WHERE id = ?", (payload.config_yaml, project_id))
    return {"saved": True}


@app.get("/api/projects/{project_id}/suggestions")
def suggestions(project_id: str) -> dict:
    """Rules proposed from the baseline profile, as YAML to review. Nothing is applied."""
    project = _project(project_id)
    if not project["baseline_json"]:
        raise HTTPException(409, "Create a baseline dataset first; suggestions are learned from it")
    return {"yaml": render_yaml(suggest_rules(json.loads(project["baseline_json"])["profile"]))}


async def _upload(request: Request, project_id: str, file: UploadFile, name: Optional[str], role: str,
                  use_ai: bool = False) -> dict:
    _project(project_id)
    path = _save_upload(file.filename or "", await file.read(MAX_UPLOAD_BYTES + 1))
    return _handle(project_id, name or file.filename or role, path, role, request.state.user["name"], use_ai)


@app.post("/api/projects/{project_id}/baseline")
async def upload_baseline(request: Request, project_id: str, file: UploadFile = File(...),
                          name: Optional[str] = Form(None)) -> dict:
    return await _upload(request, project_id, file, name, "baseline")


@app.post("/api/projects/{project_id}/runs")
async def upload_candidate(request: Request, project_id: str, file: UploadFile = File(...),
                           name: Optional[str] = Form(None), use_ai: bool = Form(False)) -> dict:
    return await _upload(request, project_id, file, name, "candidate", use_ai)


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str) -> dict:
    """Delete a project with its datasets, runs and uploaded files (files outside the upload folder are kept)."""
    _project(project_id)
    uploads = upload_dir()
    with connect() as db:
        files = [Path(r["filename"]) for r in db.execute("SELECT filename FROM datasets WHERE project_id = ?",
                                                          (project_id,))]
        for table, column in (("runs", "project_id"), ("datasets", "project_id"), ("projects", "id")):
            db.execute(f"DELETE FROM {table} WHERE {column} = ?", (project_id,))
    for f in files:
        if f.parent == uploads:
            f.unlink(missing_ok=True)
    return {"deleted": True}


@app.post("/api/projects/{project_id}/ingest")
def ingest_rows(project_id: str, payload: IngestRows, x_datapulse_key: Optional[str] = Header(None)) -> dict:
    """Send rows as JSON from any language. Requires the project's X-DataPulse-Key."""
    project = _project(project_id)
    if not x_datapulse_key or not secrets.compare_digest(
            hashlib.sha256(x_datapulse_key.encode()).hexdigest(), project["api_key_hash"]):
        raise HTTPException(401, "A valid X-DataPulse-Key header is required")
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in payload.rows)
    if len(body.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "JSON batch exceeds the 25 MB limit")
    path = upload_dir() / f"{uuid.uuid4().hex}.jsonl"
    path.write_text(body + "\n", encoding="utf-8")
    return _handle(project_id, payload.dataset_name, path, payload.role, "API", payload.use_ai)


@app.post("/api/demo/seed")
def seed_demo() -> dict:
    """A sample project: the example orders data, its baseline and its datapulse.yml."""
    sample = ROOT / "examples" / "data" / "orders.csv"
    config_text = (ROOT / "examples" / "datapulse.yml").read_text(encoding="utf-8")
    project = _create_project("Orders pipeline", config_text)
    _save_baseline(_project(project["id"]), sample.name, sample, load_dataframe(sample))
    return {**project, "has_baseline": True}


def _ai(call):
    """Run an AI feature; configuration and upstream problems become readable HTTP errors."""
    try:
        return call()
    except DataPulseError as exc:
        raise HTTPException(503 if "environment variable" in str(exc) else 502, str(exc)) from exc


@app.post("/api/projects/{project_id}/runs/{run_id}/triage")
def triage_run(project_id: str, run_id: str, request: Request) -> dict:
    """Let the AI triage agent investigate a run and write the incident note. Saved with the run."""
    project = _project(project_id)
    with connect() as db:
        run = db.execute("SELECT dataset_id, report_json FROM runs WHERE id = ? AND project_id = ?",
                         (run_id, project_id)).fetchone()
        history = db.execute("SELECT created_at, status, issue_count, report_json FROM runs WHERE project_id = ? "
                             "AND id != ? ORDER BY created_at DESC, rowid DESC LIMIT 15", (project_id, run_id)).fetchall()
    if run is None:
        raise HTTPException(404, "Run not found")
    doc = json.loads(run["report_json"])
    if not doc["issues"]:
        raise HTTPException(409, "This run passed cleanly; there is nothing to triage")
    cfg = _config(project["config_yaml"])
    current = _dataset_rows(run["dataset_id"])
    ctx = TriageContext(
        doc, json.loads(project["baseline_json"])["profile"],
        profile_dataframe(current, cfg.max_categories) if current is not None else None,
        history=[{"when": h["created_at"], "status": h["status"], "issue_count": h["issue_count"],
                  "failing": sorted({f"{i['check']}:{','.join(i['columns'])}" for i in json.loads(h["report_json"])["issues"]})}
                 for h in history],
        consumers=[vars(c) for c in cfg.consumers])
    doc["triage"] = {**_ai(lambda: run_triage(ctx, cfg)), "by": request.state.user["name"], "at": now_iso()}
    with connect() as db:
        db.execute("UPDATE runs SET report_json = ? WHERE id = ?", (json.dumps(doc), run_id))
    return doc["triage"]


@app.put("/api/projects/{project_id}/runs/{run_id}/review")
def review_run(project_id: str, run_id: str, payload: Review, request: Request) -> dict:
    """Shared issue status, so the team can see who is handling a failed check."""
    review = {"status": payload.status, "by": request.state.user["name"], "at": now_iso()}
    with connect() as db:
        updated = db.execute("UPDATE runs SET review_status = ?, review_by = ?, review_at = ? "
                             "WHERE id = ? AND project_id = ?",
                             (review["status"], review["by"], review["at"], run_id, project_id)).rowcount
    if not updated:
        raise HTTPException(404, "Run not found")
    return review


@app.post("/api/projects/{project_id}/rules/draft")
def draft_project_rules(project_id: str, payload: DraftRules) -> dict:
    """Turn a plain-English request into a complete, validated datapulse.yml. Nothing is saved."""
    project = _project(project_id)
    cfg = _config(project["config_yaml"])
    profile = json.loads(project["baseline_json"])["profile"] if project["baseline_json"] else None
    return _ai(lambda: draft_rules(payload.request, payload.config_yaml, profile, cfg))


@app.get("/api/projects/{project_id}/runs/{run_id}")
def get_run(project_id: str, run_id: str) -> dict:
    with connect() as db:
        row = db.execute("SELECT report_json FROM runs WHERE id = ? AND project_id = ?", (run_id, project_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Run not found")
    return json.loads(row["report_json"])


# Single-container deployments (Dockerfile.app) serve the built dashboard from the same address as the API.
# Registered last, so /api and /docs keep precedence.
if os.getenv("DATAPULSE_WEB_DIR") and Path(os.environ["DATAPULSE_WEB_DIR"]).is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=os.environ["DATAPULSE_WEB_DIR"], html=True), name="dashboard")
