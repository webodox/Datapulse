"""SQLite storage under $DATAPULSE_HOME (default `.datapulse/`). Older databases are upgraded on startup."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


def home() -> Path:
    return Path(os.getenv("DATAPULSE_HOME", ".datapulse")).resolve()


def upload_dir() -> Path:
    return home() / "uploads"


def samples_dir() -> Path:
    """Sample SQLite sources created by "Try it with sample data"; an allowed SQLite folder."""
    return home() / "samples"


def source_files_dir() -> Path:
    """Files uploaded as data sources: <upload id>/<original name>."""
    return home() / "source_files"


def configure_sources() -> None:
    """Point the file and SQLite plug-ins at this server's folders (the sample databases are SQLite files)."""
    folders = [d for d in os.getenv("DATAPULSE_SQLITE_DIR", "").split(os.pathsep) if d]
    if str(samples_dir()) not in folders:
        os.environ["DATAPULSE_SQLITE_DIR"] = os.pathsep.join([*folders, str(samples_dir())])
    os.environ["DATAPULSE_SOURCE_FILES"] = str(source_files_dir())


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    home().mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(home() / "datapulse.sqlite3")
    db.row_factory = sqlite3.Row
    try:
        with db:  # commit on success, roll back on error
            yield db
    finally:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    api_key_hash TEXT NOT NULL,
    config_yaml TEXT NOT NULL,
    baseline_json TEXT,
    baseline_dataset_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    filename TEXT NOT NULL,
    role TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    columns_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    dataset_id TEXT NOT NULL REFERENCES datasets(id),
    status TEXT NOT NULL,
    issue_count INTEGER NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_project_created ON runs(project_id, created_at DESC);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    settings_json TEXT NOT NULL,       -- includes secrets; never returned to the browser
    recipe_json TEXT,                  -- approved mapping steps (NULL: not mapped yet)
    proposal_json TEXT,                -- the latest AI mapping proposal, waiting for review
    is_standard INTEGER NOT NULL DEFAULT 0,  -- this source's data defines the project's standard format
    last_pulled_at TEXT,
    last_run_id TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS destinations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    settings_json TEXT NOT NULL,       -- url (secret, never returned to the browser), table, mode
    last_sent_at TEXT,
    last_result TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
"""

# Columns added after the first release: (table, column, definition)
ADDED_COLUMNS = [
    ("runs", "created_by", "TEXT"),          # who ran the check: a user's name, or "API"
    ("runs", "review_status", "TEXT NOT NULL DEFAULT 'open'"),  # open | acknowledged | resolved
    ("runs", "review_by", "TEXT"),
    ("runs", "review_at", "TEXT"),
    ("sources", "schedule_json", "TEXT"),
    ("sources", "notify_json", "TEXT"),
    ("sources", "last_scheduled_status", "TEXT"),
    ("sources", "last_error", "TEXT"),
    ("projects", "notify_json", "TEXT"),
]



def init_db() -> None:
    upload_dir().mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(SCHEMA)
        for table, column, definition in ADDED_COLUMNS:
            existing = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
