"""Data sources: connect databases and links to a project, map each onto the standard format, pull, and unify.

Secrets (passwords, connection URLs) stay in the server's database and are never sent back to the browser.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, Literal, Optional

import pandas as pd
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from datapulse.errors import DataPulseError
from datapulse.io import ARCHIVES, INPUTS, load_dataframe
from datapulse.sources.connected import read_any
from datapulse.unifier.documents import is_document
from datapulse.samples import write_samples
from datapulse.sources import catalog, clean_settings, fetch_source, public_settings, try_connection, type_of
from datapulse.unify import SOURCE_COLUMN, apply_recipe, combine, propose_mapping, validate_recipe

from .core import MAX_UPLOAD_BYTES, ROOT, ai_call, dataset_rows, loads, project_config, project_row, run_check, save_baseline
from .db import configure_sources, connect, now_iso, samples_dir, source_files_dir, upload_dir

router = APIRouter(prefix="/api")
UPLOADABLE = tuple(e for e in INPUTS if e not in ARCHIVES)


class SourceForm(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str = Field(min_length=1, max_length=40)
    settings: Dict[str, Any] = Field(default_factory=dict)


class TestForm(BaseModel):
    kind: str
    settings: Dict[str, Any] = Field(default_factory=dict)
    source_id: Optional[str] = None  # testing an edit: blank secrets fall back to the saved ones


class SampleRequest(BaseModel):
    kind: str


# Public files the Add source form can fill in, so a new source can be tried without credentials.
REMOTE_SAMPLES = {
    "link": ("Sample states file",
             {"url": "https://raw.githubusercontent.com/plotly/datasets/master/2014_usa_states.csv"}),
    "rest": ("Sample users API", {"url": "https://jsonplaceholder.typicode.com/users"}),
    "web_page": ("Example page", {"url": "https://example.com"}),
}


class Pull(BaseModel):
    role: Literal["candidate", "baseline"] = "candidate"


class Recipe(BaseModel):
    operations: list = Field(max_length=100)


def _source(project_id: str, source_id: str) -> sqlite3.Row:
    with connect() as db:
        row = db.execute("SELECT * FROM sources WHERE id = ? AND project_id = ?", (source_id, project_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Source not found")
    return row


def _public(row: sqlite3.Row) -> Dict[str, Any]:
    recipe = loads(row["recipe_json"])
    settings = json.loads(row["settings_json"])
    return {
        "id": row["id"], "name": row["name"], "kind": row["kind"], "type": type_of(row["kind"], settings),
        "settings": public_settings(row["kind"], settings),
        "status": "standard" if row["is_standard"] else "mapped" if recipe is not None else "needs_mapping",
        "recipe": recipe or [], "proposal": loads(row["proposal_json"]),
        "last_pulled_at": row["last_pulled_at"], "last_run_id": row["last_run_id"],
        "created_by": row["created_by"], "created_at": row["created_at"],
    }


def _fail(exc: DataPulseError) -> HTTPException:
    return HTTPException(400, str(exc))


def _fetch(row: sqlite3.Row) -> pd.DataFrame:
    try:
        return fetch_source(row["kind"], json.loads(row["settings_json"]), row["name"])
    except DataPulseError as exc:
        raise _fail(exc) from exc


def _store(df: pd.DataFrame) -> Any:
    path = upload_dir() / f"{uuid.uuid4().hex}.csv"
    df.to_csv(path, index=False)
    return path


def _update(source_id: str, **values) -> None:
    with connect() as db:
        db.execute(f"UPDATE sources SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                   (*values.values(), source_id))


def _create(project_id: str, name: str, kind: str, settings: Dict[str, str], actor: str) -> str:
    source_id = uuid.uuid4().hex[:12]
    with connect() as db:
        db.execute("INSERT INTO sources (id, project_id, name, kind, settings_json, created_by, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (source_id, project_id, name, kind, json.dumps(settings), actor, now_iso()))
    return source_id


def _make_standard(project: sqlite3.Row, row: sqlite3.Row, df: pd.DataFrame) -> Dict[str, Any]:
    """This source's format becomes the project's standard (its baseline)."""
    result = save_baseline(project, row["name"], _store(df), df)
    with connect() as db:
        db.execute("UPDATE sources SET is_standard = 0 WHERE project_id = ?", (project["id"],))
    _update(row["id"], is_standard=1, recipe_json=json.dumps([]), proposal_json=None, last_pulled_at=now_iso())
    return result


def add_sample_sources(project_id: str, actor: str) -> None:
    """Three sample databases holding the same orders in different formats; the first one is the standard."""
    configure_sources()
    orders = load_dataframe(ROOT / "examples" / "data" / "orders.csv")
    for n, sample in enumerate(write_samples(orders, samples_dir() / project_id)):
        source_id = _create(project_id, sample["name"], "sqlite", {"path": sample["path"], "query": sample["query"]}, actor)
        if n == 0:
            row = _source(project_id, source_id)
            _make_standard(project_row(project_id), row, _fetch(row))


def remove_uploaded_files(rows) -> None:
    """Delete the uploaded files behind File sources (when the source or its project is deleted)."""
    for row in rows:
        stored = json.loads(row["settings_json"]).get("file", "") if row["kind"] == "file" else ""
        if re.fullmatch(r"[0-9a-f]{32}/[^/\\]+", stored):
            shutil.rmtree(source_files_dir() / stored.split("/")[0], ignore_errors=True)


@router.post("/projects/{project_id}/sources/files")
async def upload_source_file(project_id: str, file: UploadFile = File(...)) -> dict:
    """Store a file for a File source and return the id to save in its settings. A document (PDF, Word, image...)
    is read once here: the AI extracts its records into a table, so later pulls don't call the AI again."""
    project_row(project_id)
    name = re.sub(r"[^\w.\- ]", "_", Path(file.filename or "data.csv").name).strip() or "data.csv"
    if Path(name).suffix.lower() not in UPLOADABLE:
        raise HTTPException(415, "Supported files: CSV, Excel, JSON, Parquet, PDF, Word, text, Markdown, email, "
                                 "HTML and images")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "The file is larger than 25 MB")
    upload_id = uuid.uuid4().hex
    folder = source_files_dir() / upload_id
    folder.mkdir(parents=True)
    (folder / name).write_bytes(content)
    if not is_document(name):
        return {"file": f"{upload_id}/{name}", "name": name, "note": None}
    try:
        rows = ai_call(lambda: read_any(folder / name, name))
    except HTTPException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    table = f"{Path(name).stem}.csv"
    rows.to_csv(folder / table, index=False)
    return {"file": f"{upload_id}/{table}", "name": name,
            "note": f"Read {name}: {len(rows):,} record(s) with {len(rows.columns)} fields."}


def _file_sample() -> Dict[str, Any]:
    """The example orders CSV, stored like an uploaded File source."""
    configure_sources()
    upload_id = uuid.uuid4().hex
    folder = source_files_dir() / upload_id
    folder.mkdir(parents=True)
    (folder / "orders.csv").write_bytes((ROOT / "examples" / "data" / "orders.csv").read_bytes())
    return {"name": "Sample orders", "settings": {"file": f"{upload_id}/orders.csv"}}


@router.get("/sources/catalog")
def source_catalog() -> list:
    return catalog()


@router.post("/projects/{project_id}/sources/sample")
def source_sample(project_id: str, payload: SampleRequest) -> dict:
    """Settings that make Test connection succeed, for the Add source form."""
    project_row(project_id)
    if payload.kind == "file":
        return _file_sample()
    found = REMOTE_SAMPLES.get(payload.kind)
    if found is None:
        raise HTTPException(404, "This source has no test data. Enter your own connection details.")
    name, settings = found
    return {"name": name, "settings": settings}


@router.get("/projects/{project_id}/sources")
def list_sources(project_id: str) -> list:
    project_row(project_id)
    with connect() as db:
        rows = db.execute("SELECT * FROM sources WHERE project_id = ? ORDER BY created_at, rowid", (project_id,)).fetchall()
    return [_public(r) for r in rows]


@router.post("/projects/{project_id}/sources/test")
def test_connection(project_id: str, payload: TestForm) -> dict:
    """Try a connection without saving it. Failures are a normal answer here, not an error."""
    project_row(project_id)
    previous = None
    if payload.source_id:
        previous = json.loads(_source(project_id, payload.source_id)["settings_json"])
    try:
        return {"ok": True, "message": try_connection(payload.kind, clean_settings(payload.kind, payload.settings, previous))}
    except DataPulseError as exc:
        return {"ok": False, "message": str(exc)}


@router.post("/projects/{project_id}/sources")
def add_source(project_id: str, payload: SourceForm, request: Request) -> dict:
    project_row(project_id)
    try:
        settings = clean_settings(payload.kind, payload.settings)
    except DataPulseError as exc:
        raise _fail(exc) from exc
    source_id = _create(project_id, payload.name.strip(), payload.kind, settings, request.state.user["name"])
    return _public(_source(project_id, source_id))


@router.put("/projects/{project_id}/sources/{source_id}")
def edit_source(project_id: str, source_id: str, payload: SourceForm) -> dict:
    row = _source(project_id, source_id)
    if payload.kind != row["kind"]:
        raise HTTPException(422, "A source's type can't change; add a new source instead")
    try:
        settings = clean_settings(row["kind"], payload.settings, json.loads(row["settings_json"]))
    except DataPulseError as exc:
        raise _fail(exc) from exc
    if row["kind"] == "file" and settings.get("file") != json.loads(row["settings_json"]).get("file"):
        remove_uploaded_files([row])  # a newer version replaced it
    _update(source_id, name=payload.name.strip(), settings_json=json.dumps(settings))
    return _public(_source(project_id, source_id))


@router.delete("/projects/{project_id}/sources/{source_id}")
def delete_source(project_id: str, source_id: str) -> dict:
    remove_uploaded_files([_source(project_id, source_id)])
    with connect() as db:
        db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
    return {"deleted": True}


@router.post("/projects/{project_id}/sources/{source_id}/map")
def map_source(project_id: str, source_id: str) -> dict:
    """Propose a recipe that turns this source into the standard format. Saved as a proposal for review."""
    project, row = project_row(project_id), _source(project_id, source_id)
    if not project["baseline_json"]:
        raise HTTPException(409, "Set the standard format first: pull one source as the baseline")
    df = _fetch(row)
    proposal = ai_call(lambda: propose_mapping(df, json.loads(project["baseline_json"]),
                                               project_config(project["config_yaml"]),
                                               reference=dataset_rows(project["baseline_dataset_id"])))
    proposal["at"] = now_iso()
    _update(source_id, proposal_json=json.dumps(proposal))
    return proposal


@router.delete("/projects/{project_id}/sources/{source_id}/proposal")
def discard_proposal(project_id: str, source_id: str) -> dict:
    _source(project_id, source_id)
    _update(source_id, proposal_json=None)
    return _public(_source(project_id, source_id))


@router.put("/projects/{project_id}/sources/{source_id}/recipe")
def save_recipe(project_id: str, source_id: str, payload: Recipe) -> dict:
    """Approve a recipe (usually the reviewed proposal). Every later pull replays it without AI."""
    _source(project_id, source_id)
    try:
        recipe = validate_recipe(payload.operations)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _update(source_id, recipe_json=json.dumps(recipe), proposal_json=None)
    return _public(_source(project_id, source_id))


@router.post("/projects/{project_id}/sources/{source_id}/pull")
def pull_source(project_id: str, source_id: str, payload: Pull, request: Request) -> dict:
    """Pull the source now. As the baseline it defines the standard format; otherwise its recipe runs and the
    result is checked like an upload."""
    project, row = project_row(project_id), _source(project_id, source_id)
    if payload.role == "candidate" and not project["baseline_json"]:
        raise HTTPException(409, "Set the standard format first: pull one source as the baseline")
    df = _fetch(row)
    actor = request.state.user["name"]
    if payload.role == "baseline":
        return _make_standard(project, row, df)
    recipe = loads(row["recipe_json"])
    mapped, effects, problems = apply_recipe(df, json.loads(project["baseline_json"])["profile"], recipe or [])
    extra = {"source": {"id": row["id"], "name": row["name"], "kind": row["kind"],
                        "type": type_of(row["kind"], json.loads(row["settings_json"])), "rows_pulled": len(df),
                        "mapped": recipe is not None or bool(row["is_standard"]), "steps": effects, "problems": problems}}
    doc = run_check(project, row["name"], _store(mapped), mapped, False, actor, extra)
    _update(source_id, last_pulled_at=now_iso(), last_run_id=doc["run_id"])
    return doc


@router.post("/projects/{project_id}/unify")
def unify(project_id: str, request: Request) -> dict:
    """Pull every mapped source, convert each to the standard format, combine them and check the result."""
    project = project_row(project_id)
    if not project["baseline_json"]:
        raise HTTPException(409, "Set the standard format first: pull one source as the baseline")
    profile = json.loads(project["baseline_json"])["profile"]
    with connect() as db:
        rows = db.execute("SELECT * FROM sources WHERE project_id = ? AND (recipe_json IS NOT NULL OR is_standard = 1) "
                          "ORDER BY created_at, rowid", (project_id,)).fetchall()
    if not rows:
        raise HTTPException(409, "No mapped sources yet. Map at least one source first")
    frames, report = {}, []
    for row in rows:
        try:
            df = fetch_source(row["kind"], json.loads(row["settings_json"]), row["name"])
        except DataPulseError as exc:
            report.append({"name": row["name"], "kind": row["kind"], "type": type_of(row["kind"], json.loads(row["settings_json"])), "rows": 0, "error": str(exc), "problems": []})
            continue
        mapped, _, problems = apply_recipe(df, profile, loads(row["recipe_json"]) or [])
        label = row["name"] if row["name"] not in frames else f"{row['name']} ({row['id']})"
        frames[label] = mapped
        _update(row["id"], last_pulled_at=now_iso())
        report.append({"name": row["name"], "kind": row["kind"], "type": type_of(row["kind"], json.loads(row["settings_json"])),
                       "rows": len(mapped), "error": None, "problems": problems})
    if not frames:
        raise HTTPException(502, "None of the sources could be read: "
                            + "; ".join(f"{r['name']}: {r['error']}" for r in report))
    combined = combine(frames, list(profile["columns"]))
    name = f"Unified data ({len(frames)} source{'s' if len(frames) != 1 else ''})"
    return run_check(project, name, _store(combined), combined, False, request.state.user["name"],
                     {"unified": {"sources": report}}, checked=combined.drop(columns=[SOURCE_COLUMN]), combined=True)
