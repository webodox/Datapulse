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
import shutil
import sqlite3
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Literal, Optional

import pandas as pd
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from datapulse import __version__
from datapulse.agent import TriageContext, run_triage
from datapulse.ai import draft_rules, get_api_key
from datapulse.config import STARTER_CONFIG, Config
from datapulse.errors import DataPulseError
from datapulse.fix import RepairSession, run_repair
from datapulse.profile import profile_dataframe
from datapulse.suggest import render_yaml, suggest_rules
from datapulse.unify import SOURCE_COLUMN

from .auth import current_user, needs_login, router as auth_router
from .core import (MAX_INGEST_ROWS, MAX_UPLOAD_BYTES, ROOT, ai_call, baseline_summary, dataset_rows, handle_data,
                   loads, project_config, project_row, record_dataset, run_check, save_upload,
                   load_upload)
from .db import allow_sample_sources, connect, init_db, now_iso, samples_dir, upload_dir
from .sources import add_sample_sources, router as sources_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    allow_sample_sources()
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
app.include_router(sources_router)


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
    project = project_row(project_id)
    with connect() as db:
        datasets = db.execute("SELECT id, name, role, row_count, columns_json, created_at FROM datasets "
                              "WHERE project_id = ? ORDER BY created_at DESC, rowid DESC", (project_id,)).fetchall()
        runs = db.execute("SELECT id, status, issue_count, created_at, report_json, created_by, review_status, "
                          "review_by, review_at FROM runs "
                          "WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 30", (project_id,)).fetchall()
    baseline = loads(project["baseline_json"])
    cfg = project_config(project["config_yaml"])
    return {
        "id": project["id"], "name": project["name"], "created_at": project["created_at"],
        "has_baseline": baseline is not None,
        "baseline": baseline_summary(baseline) if baseline else None,
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
    project_row(project_id)
    project_config(payload.config_yaml)  # the CLI's strict validation; rejects unknown keys and bad values
    with connect() as db:
        db.execute("UPDATE projects SET config_yaml = ? WHERE id = ?", (payload.config_yaml, project_id))
    return {"saved": True}


@app.get("/api/projects/{project_id}/suggestions")
def suggestions(project_id: str) -> dict:
    """Rules proposed from the baseline profile, as YAML to review. Nothing is applied."""
    project = project_row(project_id)
    if not project["baseline_json"]:
        raise HTTPException(409, "Create a baseline dataset first; suggestions are learned from it")
    return {"yaml": render_yaml(suggest_rules(json.loads(project["baseline_json"])["profile"]))}


async def _upload(request: Request, project_id: str, file: UploadFile, name: Optional[str], role: str,
                  use_ai: bool = False) -> dict:
    project_row(project_id)
    path = save_upload(file.filename or "", await file.read(MAX_UPLOAD_BYTES + 1))
    return handle_data(project_id, name or file.filename or role, path, role, request.state.user["name"], use_ai)


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
    project_row(project_id)
    uploads = upload_dir()
    with connect() as db:
        files = [Path(r["filename"]) for r in db.execute("SELECT filename FROM datasets WHERE project_id = ?",
                                                          (project_id,))]
        for table, column in (("runs", "project_id"), ("sources", "project_id"), ("datasets", "project_id"),
                              ("projects", "id")):
            db.execute(f"DELETE FROM {table} WHERE {column} = ?", (project_id,))
    for f in files:
        if f.parent == uploads:
            f.unlink(missing_ok=True)
    shutil.rmtree(samples_dir() / project_id, ignore_errors=True)
    return {"deleted": True}


@app.post("/api/projects/{project_id}/ingest")
def ingest_rows(project_id: str, payload: IngestRows, x_datapulse_key: Optional[str] = Header(None)) -> dict:
    """Send rows as JSON from any language. Requires the project's X-DataPulse-Key."""
    project = project_row(project_id)
    if not x_datapulse_key or not secrets.compare_digest(
            hashlib.sha256(x_datapulse_key.encode()).hexdigest(), project["api_key_hash"]):
        raise HTTPException(401, "A valid X-DataPulse-Key header is required")
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in payload.rows)
    if len(body.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "JSON batch exceeds the 25 MB limit")
    path = upload_dir() / f"{uuid.uuid4().hex}.jsonl"
    path.write_text(body + "\n", encoding="utf-8")
    return handle_data(project_id, payload.dataset_name, path, payload.role, "API", payload.use_ai)


@app.post("/api/demo/seed")
def seed_demo(request: Request) -> dict:
    """A sample project: three databases holding the same orders in different formats (the first is the
    standard), with the example datapulse.yml."""
    config_text = (ROOT / "examples" / "datapulse.yml").read_text(encoding="utf-8")
    project = _create_project("Orders (sample)", config_text)
    add_sample_sources(project["id"], request.state.user["name"])
    return {**project, "has_baseline": True}


def _run_row(project_id: str, run_id: str) -> sqlite3.Row:
    with connect() as db:
        run = db.execute("SELECT * FROM runs WHERE id = ? AND project_id = ?", (run_id, project_id)).fetchone()
    if run is None:
        raise HTTPException(404, "Run not found")
    return run


def _failed_run(project_id: str, run_id: str) -> tuple:
    run = _run_row(project_id, run_id)
    doc = json.loads(run["report_json"])
    if not doc["issues"]:
        raise HTTPException(409, "This run passed cleanly; there is nothing to investigate")
    return run, doc


def _run_data(run: sqlite3.Row) -> Optional[pd.DataFrame]:
    """The checked data of a run, without the _source tag a unified table carries."""
    df = dataset_rows(run["dataset_id"])
    return None if df is None else df.drop(columns=[SOURCE_COLUMN], errors="ignore")


def _triage(project: sqlite3.Row, run: sqlite3.Row, doc: Dict[str, Any]) -> Dict[str, Any]:
    """The triage agent's incident note for a failed run."""
    with connect() as db:
        history = db.execute("SELECT created_at, status, issue_count, report_json FROM runs WHERE project_id = ? "
                             "AND id != ? ORDER BY created_at DESC, rowid DESC LIMIT 15",
                             (project["id"], run["id"])).fetchall()
    cfg = project_config(project["config_yaml"])
    current = _run_data(run)
    ctx = TriageContext(
        doc, json.loads(project["baseline_json"])["profile"],
        profile_dataframe(current, cfg.max_categories) if current is not None else None,
        history=[{"when": h["created_at"], "status": h["status"], "issue_count": h["issue_count"],
                  "failing": sorted({f"{i['check']}:{','.join(i['columns'])}" for i in json.loads(h["report_json"])["issues"]})}
                 for h in history],
        consumers=[vars(c) for c in cfg.consumers])
    return run_triage(ctx, cfg)


def _repair(project: sqlite3.Row, run: sqlite3.Row, doc: Dict[str, Any]) -> Dict[str, Any]:
    """The repair agent's fixed copy of a failed run, saved as a dataset. The original is never changed."""
    df = _run_data(run)
    if df is None:
        raise DataPulseError("The file for this check is no longer stored, so it can't be repaired")
    session = RepairSession(df, json.loads(project["baseline_json"]), project_config(project["config_yaml"]),
                            reference=dataset_rows(project["baseline_dataset_id"]))
    result = run_repair(session)
    fixed = result.pop("fixed_frame")
    stem = Path(doc.get("data_file") or "data").stem
    path = upload_dir() / f"{uuid.uuid4().hex}.csv"
    fixed.to_csv(path, index=False)
    dataset_id = record_dataset(project["id"], f"{stem} (fixed).csv", path, "fixed", fixed)
    return {**result, "dataset_id": dataset_id, "file_name": f"{stem}-fixed.csv"}


AGENTS = {"triage": _triage, "fix": _repair}


def _save_report(run_id: str, doc: Dict[str, Any]) -> None:
    with connect() as db:
        db.execute("UPDATE runs SET report_json = ? WHERE id = ?", (json.dumps(doc), run_id))


def _one_agent(project_id: str, run_id: str, request: Request, name: str) -> dict:
    project = project_row(project_id)
    run, doc = _failed_run(project_id, run_id)
    doc[name] = {**ai_call(lambda: AGENTS[name](project, run, doc)), "by": request.state.user["name"], "at": now_iso()}
    _save_report(run_id, doc)
    return doc[name]


@app.post("/api/projects/{project_id}/runs/{run_id}/investigate")
def investigate_run(project_id: str, run_id: str, request: Request) -> dict:
    """Investigate with AI: the triage agent diagnoses the run while the repair agent fixes a copy, in parallel.

    Parts already done are kept. If one agent fails the other's result is still saved and the error is returned.
    """
    project = project_row(project_id)
    run, doc = _failed_run(project_id, run_id)
    todo = [name for name in AGENTS if not doc.get(name)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {name: pool.submit(AGENTS[name], project, run, doc) for name in todo}
    errors = {}
    for name, future in futures.items():
        try:
            doc[name] = {**future.result(), "by": request.state.user["name"], "at": now_iso()}
        except DataPulseError as exc:
            errors[name] = str(exc)
    if todo and len(errors) == len(todo):
        detail = "; ".join(dict.fromkeys(errors.values()))
        raise HTTPException(503 if "environment variable" in detail else 502, detail)
    _save_report(run_id, doc)
    return {"triage": doc.get("triage"), "fix": doc.get("fix"), "errors": errors}


@app.post("/api/projects/{project_id}/runs/{run_id}/triage")
def triage_run(project_id: str, run_id: str, request: Request) -> dict:
    """Only the triage agent's incident note (the dashboard uses /investigate)."""
    return _one_agent(project_id, run_id, request, "triage")


@app.post("/api/projects/{project_id}/runs/{run_id}/fix")
def fix_run(project_id: str, run_id: str, request: Request) -> dict:
    """Only the repair agent's fixed copy (the dashboard uses /investigate)."""
    return _one_agent(project_id, run_id, request, "fix")


def _fixed_file(project_id: str, run_id: str) -> tuple:
    """The repaired file of a run: (path, download name)."""
    fix = json.loads(_run_row(project_id, run_id)["report_json"]).get("fix")
    with connect() as db:
        row = db.execute("SELECT filename FROM datasets WHERE id = ?", (fix["dataset_id"],)).fetchone() if fix else None
    if row is None or not Path(row["filename"]).exists():
        raise HTTPException(404, "This check has no repaired file yet")
    return Path(row["filename"]), fix["file_name"]


@app.get("/api/projects/{project_id}/runs/{run_id}/data")
def download_run_data(project_id: str, run_id: str) -> FileResponse:
    """The data a run checked, e.g. a unified table or a source pulled from a database."""
    run = _run_row(project_id, run_id)
    with connect() as db:
        row = db.execute("SELECT name, filename FROM datasets WHERE id = ?", (run["dataset_id"],)).fetchone()
    if row is None or not Path(row["filename"]).exists():
        raise HTTPException(404, "The data for this check is no longer stored")
    path = Path(row["filename"])
    return FileResponse(path, filename=f"{Path(row['name']).stem}{path.suffix}")


@app.get("/api/projects/{project_id}/runs/{run_id}/fixed")
def download_fixed(project_id: str, run_id: str) -> FileResponse:
    path, name = _fixed_file(project_id, run_id)
    return FileResponse(path, media_type="text/csv", filename=name)


@app.post("/api/projects/{project_id}/runs/{run_id}/fixed/check")
def check_fixed(project_id: str, run_id: str, request: Request) -> dict:
    """Run a normal check on the repaired file, so it shows up in the history next to the original."""
    path, name = _fixed_file(project_id, run_id)
    return run_check(project_row(project_id), name, path, load_upload(path), False, request.state.user["name"])


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
    project = project_row(project_id)
    cfg = project_config(project["config_yaml"])
    profile = json.loads(project["baseline_json"])["profile"] if project["baseline_json"] else None
    return ai_call(lambda: draft_rules(payload.request, payload.config_yaml, profile, cfg))


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
