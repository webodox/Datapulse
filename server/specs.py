"""Storage for unify specs: one current spec per project source, with history."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from datapulse.unifier import StoredSpec, UnifySpec, to_json_schema

from .db import connect, now_iso

SOURCE_LABELS = {"upload": "Dashboard uploads", "api": "API pushes"}


def latest_row(project_id: str, source_key: str) -> Optional[sqlite3.Row]:
    with connect() as db:
        return db.execute("SELECT * FROM unify_specs WHERE project_id = ? AND source_key = ? "
                          "ORDER BY created_at DESC, rowid DESC LIMIT 1", (project_id, source_key)).fetchone()


def get_row(spec_id: Optional[str]) -> Optional[sqlite3.Row]:
    if not spec_id:
        return None
    with connect() as db:
        return db.execute("SELECT * FROM unify_specs WHERE id = ?", (spec_id,)).fetchone()


def stored(row: Optional[sqlite3.Row]) -> Optional[StoredSpec]:
    if row is None:
        return None
    return StoredSpec(spec=UnifySpec.from_dict(json.loads(row["spec_json"])), origin=row["origin"],
                      fingerprint=row["fingerprint"], stale=bool(row["stale"]))


def save(project_id: str, source_key: str, spec: UnifySpec, origin: str, fingerprint: str,
         paths: Optional[List[str]] = None, sample: Optional[List[Dict[str, Any]]] = None,
         actor: Optional[str] = None) -> str:
    spec_id = uuid.uuid4().hex[:12]
    with connect() as db:
        db.execute("INSERT INTO unify_specs (id, project_id, source_key, mode, fingerprint, paths_json, sample_json, "
                   "spec_json, origin, stale, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                   (spec_id, project_id, source_key, spec.mode, fingerprint,
                    json.dumps(paths) if paths is not None else None,
                    json.dumps(sample, default=str) if sample is not None else None,
                    json.dumps(spec.to_dict()), origin, now_iso(), actor))
    return spec_id


def mark_stale(spec_id: str) -> None:
    with connect() as db:
        db.execute("UPDATE unify_specs SET stale = 1 WHERE id = ?", (spec_id,))


def source_label(project_id: str, source_key: str) -> str:
    if source_key in SOURCE_LABELS:
        return SOURCE_LABELS[source_key]
    with connect() as db:
        row = db.execute("SELECT name FROM sources WHERE id = ? AND project_id = ?",
                         (source_key, project_id)).fetchone()
    return row["name"] if row else source_key


def public(row: sqlite3.Row, project_id: str) -> Dict[str, Any]:
    spec = UnifySpec.from_dict(json.loads(row["spec_json"]))
    return {"id": row["id"], "source_key": row["source_key"], "source": source_label(project_id, row["source_key"]),
            "mode": row["mode"], "origin": row["origin"], "stale": bool(row["stale"]),
            "created_at": row["created_at"], "created_by": row["created_by"],
            "spec": spec.to_dict(), "json_schema": to_json_schema(spec),
            "can_regenerate_now": row["mode"] == "map" and bool(row["sample_json"])}


def current_for_project(project_id: str) -> List[Dict[str, Any]]:
    with connect() as db:
        keys = [r["source_key"] for r in db.execute(
            "SELECT source_key, MAX(created_at) AS latest FROM unify_specs WHERE project_id = ? "
            "GROUP BY source_key ORDER BY latest DESC", (project_id,))]
    return [public(latest_row(project_id, k), project_id) for k in keys]
