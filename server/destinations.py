"""Destinations: the company's own databases that unified data is delivered to.

Unify sends to every destination automatically, but only when the unified data passes its checks; data that fails
is held back and the report says why. Connection URLs stay on the server.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, List, Literal, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from datapulse.destinations import check_settings, describe_target, send
from datapulse.errors import DataPulseError
from datapulse.sources import public_settings, type_of
from datapulse.sources.database import detect

from .core import dataset_rows, project_row
from .db import connect, now_iso

router = APIRouter(prefix="/api")


class DestinationForm(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(default="", max_length=2000)  # empty on edit: keep the saved one
    table: str = Field(min_length=1, max_length=130)
    mode: Literal["replace", "append"] = "replace"


class TestForm(BaseModel):
    url: str = Field(default="", max_length=2000)
    table: str = Field(min_length=1, max_length=130)
    mode: Literal["replace", "append"] = "replace"
    destination_id: Optional[str] = None


def _row(project_id: str, destination_id: str) -> sqlite3.Row:
    with connect() as db:
        row = db.execute("SELECT * FROM destinations WHERE id = ? AND project_id = ?",
                         (destination_id, project_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Destination not found")
    return row


def _public(row: sqlite3.Row) -> Dict[str, Any]:
    settings = json.loads(row["settings_json"])
    return {"id": row["id"], "name": row["name"], "type": type_of("database", settings),
            "table": settings["table"], "mode": settings["mode"],
            "saved_secrets": public_settings("database", {"url": settings["url"]})["saved_secrets"],
            "last_sent_at": row["last_sent_at"], "last_result": row["last_result"],
            "created_by": row["created_by"], "created_at": row["created_at"]}


def _settings(form, previous: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    url = form.url.strip() or (previous or {}).get("url", "")
    return {"url": url, "table": form.table.strip(), "mode": form.mode}


def deliver(project_id: str, doc: Dict[str, Any], df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Send a unified table to every destination of the project, if it passed its checks. One entry per destination."""
    with connect() as db:
        rows = db.execute("SELECT * FROM destinations WHERE project_id = ? ORDER BY created_at, rowid",
                          (project_id,)).fetchall()
    results = []
    for row in rows:
        settings = json.loads(row["settings_json"])
        entry = {"id": row["id"], "name": row["name"], "type": type_of("database", settings), "table": settings["table"]}
        if doc["status"] != "passed":
            results.append({**entry, "ok": False, "held": True,
                            "message": "Held back: the unified data failed its checks, so nothing was sent."})
            continue
        try:
            entry.update(ok=True, held=False, message=send(settings, df))
        except DataPulseError as exc:
            entry.update(ok=False, held=False, message=str(exc))
        with connect() as db:
            db.execute("UPDATE destinations SET last_sent_at = ?, last_result = ? WHERE id = ?",
                       (now_iso(), entry["message"], row["id"]))
        results.append(entry)
    return results


@router.get("/projects/{project_id}/destinations")
def list_destinations(project_id: str) -> list:
    project_row(project_id)
    with connect() as db:
        rows = db.execute("SELECT * FROM destinations WHERE project_id = ? ORDER BY created_at, rowid",
                          (project_id,)).fetchall()
    return [_public(r) for r in rows]


@router.post("/projects/{project_id}/destinations/test")
def test_destination(project_id: str, payload: TestForm) -> dict:
    """Connect and report what's in the table now. Nothing is written."""
    project_row(project_id)
    previous = json.loads(_row(project_id, payload.destination_id)["settings_json"]) if payload.destination_id else None
    try:
        return {"ok": True, "message": describe_target(_settings(payload, previous))}
    except DataPulseError as exc:
        return {"ok": False, "message": str(exc)}


@router.post("/projects/{project_id}/destinations")
def add_destination(project_id: str, payload: DestinationForm, request: Request) -> dict:
    project_row(project_id)
    settings = _check(_settings(payload))
    destination_id = uuid.uuid4().hex[:12]
    with connect() as db:
        db.execute("INSERT INTO destinations (id, project_id, name, settings_json, created_by, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?)", (destination_id, project_id, payload.name.strip(), json.dumps(settings),
                                                 request.state.user["name"], now_iso()))
    return _public(_row(project_id, destination_id))


@router.put("/projects/{project_id}/destinations/{destination_id}")
def edit_destination(project_id: str, destination_id: str, payload: DestinationForm) -> dict:
    row = _row(project_id, destination_id)
    settings = _check(_settings(payload, json.loads(row["settings_json"])))
    with connect() as db:
        db.execute("UPDATE destinations SET name = ?, settings_json = ? WHERE id = ?",
                   (payload.name.strip(), json.dumps(settings), destination_id))
    return _public(_row(project_id, destination_id))


@router.delete("/projects/{project_id}/destinations/{destination_id}")
def delete_destination(project_id: str, destination_id: str) -> dict:
    _row(project_id, destination_id)
    with connect() as db:
        db.execute("DELETE FROM destinations WHERE id = ?", (destination_id,))
    return {"deleted": True}


@router.post("/projects/{project_id}/runs/{run_id}/send")
def send_run(project_id: str, run_id: str) -> dict:
    """Send a unified check's data again (e.g. after adding a destination). Only data that passed is sent."""
    project_row(project_id)
    with connect() as db:
        run = db.execute("SELECT dataset_id, report_json FROM runs WHERE id = ? AND project_id = ?",
                         (run_id, project_id)).fetchone()
    if run is None:
        raise HTTPException(404, "Run not found")
    doc = json.loads(run["report_json"])
    if not doc.get("unified"):
        raise HTTPException(409, "Only unified data is sent to destinations")
    df = dataset_rows(run["dataset_id"])
    if df is None:
        raise HTTPException(409, "The data for this check is no longer stored")
    doc["delivered"] = deliver(project_id, doc, df)
    with connect() as db:
        db.execute("UPDATE runs SET report_json = ? WHERE id = ?", (json.dumps(doc), run_id))
    return {"delivered": doc["delivered"]}


def _check(settings: Dict[str, str]) -> Dict[str, str]:
    try:
        detect(settings["url"])
        return check_settings(settings)
    except DataPulseError as exc:
        raise HTTPException(400, str(exc)) from exc
