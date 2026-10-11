"""Per-source scheduling and auto-run engine for DataPulse.

Periodically pulls scheduled sources, applies mapping recipes, executes quality checks,
and dispatches Slack / Email alerts whenever something new breaks overnight or during automated runs.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from datapulse.errors import DataPulseError
from datapulse.notify import is_new_break, send_break_alert
from datapulse.sources import fetch_source, type_of
from datapulse.unify import apply_recipe

logger = logging.getLogger("datapulse.scheduler")

INTERVALS = {
    "hourly": timedelta(hours=1),
    "every_2h": timedelta(hours=2),
    "every_6h": timedelta(hours=6),
    "every_12h": timedelta(hours=12),
    "daily": timedelta(days=1),
    "daily_overnight": "overnight",  # Next 02:00 UTC
    "weekly": timedelta(days=7),
}

DEFAULT_INTERVAL = "daily"


def compute_next_run(interval: str, from_dt: Optional[datetime] = None) -> str:
    """Calculate the next ISO 8601 execution timestamp for a given interval.

    'daily_overnight' calculates the next 02:00:00 UTC (overnight).
    Other presets add the corresponding timedelta.
    """
    now = from_dt or datetime.now(timezone.utc)
    now = now.replace(microsecond=0)

    if interval == "daily_overnight":
        # Target 02:00:00 UTC
        target = now.replace(hour=2, minute=0, second=0)
        if target <= now:
            target += timedelta(days=1)
        return target.isoformat()

    delta = INTERVALS.get(interval, timedelta(days=1))
    if isinstance(delta, timedelta):
        return (now + delta).isoformat()

    return (now + timedelta(days=1)).isoformat()


def is_due(next_run_at: Optional[str], now_dt: Optional[datetime] = None) -> bool:
    """Check if next_run_at timestamp has been reached or passed."""
    if not next_run_at:
        return False
    now = now_dt or datetime.now(timezone.utc)
    try:
        run_dt = datetime.fromisoformat(next_run_at)
        if run_dt.tzinfo is None:
            run_dt = run_dt.replace(tzinfo=timezone.utc)
        return run_dt <= now
    except (ValueError, TypeError):
        return False


def _resolve_notify_config(source_notify_raw: Optional[str], project_notify_raw: Optional[str]) -> Dict[str, Any]:
    """Merge source-level notification overrides with project-level defaults."""
    project_cfg = json.loads(project_notify_raw) if project_notify_raw else {}
    source_cfg = json.loads(source_notify_raw) if source_notify_raw else {}

    slack_url = source_cfg.get("slack_webhook_url") or project_cfg.get("slack_webhook_url") or ""
    emails = source_cfg.get("emails") if source_cfg.get("emails") is not None else project_cfg.get("emails") or []
    policy = source_cfg.get("policy") or project_cfg.get("policy") or "on_break"

    return {
        "slack_webhook_url": slack_url.strip(),
        "emails": emails,
        "policy": policy,
    }


def auto_run_source(
    project_id: str,
    source_id: str,
    trigger: str = "scheduler",
    dashboard_base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute the full auto-run pipeline for a source:

    1. Fetch data from source (handles overnight connection/driver/query failure).
    2. Apply approved mapping recipe onto standard format.
    3. Run automated data quality checks against project baseline.
    4. Save candidate dataset and record run history.
    5. Evaluate if something new broke and send Slack/email alerts.
    6. Advance schedule's next_run_at timestamp.
    """
    from server.core import loads, project_row, run_check
    from server.db import connect, now_iso, upload_dir

    project = project_row(project_id)
    with connect() as db:
        row = db.execute("SELECT * FROM sources WHERE id = ? AND project_id = ?", (source_id, project_id)).fetchone()
    if row is None:
        raise ValueError(f"Source {source_id} not found in project {project_id}")

    schedule = loads(row["schedule_json"]) if "schedule_json" in row.keys() else None
    notify_cfg = _resolve_notify_config(
        row["notify_json"] if "notify_json" in row.keys() else None,
        project["notify_json"] if "notify_json" in project.keys() else None,
    )

    prev_status = row["last_scheduled_status"] if "last_scheduled_status" in row.keys() else None
    prev_error = row["last_error"] if "last_error" in row.keys() else None
    now_ts = now_iso()

    dashboard_url = None
    if dashboard_base_url:
        dashboard_url = f"{dashboard_base_url.rstrip('/')}/#/p/{project_id}/sources"

    # 1. Fetch data from the source (catches overnight breaks like DB offline, changed credentials, missing table)
    try:
        settings = json.loads(row["settings_json"])
        df = fetch_source(row["kind"], settings, row["name"])
    except Exception as exc:
        # A SOURCE BROKE OVERNIGHT!
        error_msg = str(exc)
        logger.warning("Source '%s' (%s) broke overnight: %s", row["name"], source_id, error_msg)

        broke_now = is_new_break(
            previous_status=prev_status,
            current_status="error",
            previous_error=prev_error,
            current_error=error_msg,
            policy=notify_cfg.get("policy", "on_break"),
        )

        alert_results = {}
        if broke_now and (notify_cfg.get("slack_webhook_url") or notify_cfg.get("emails")):
            alert_results = send_break_alert(
                notify_config=notify_cfg,
                project_name=project["name"],
                source_name=row["name"],
                status="error",
                summary=f"Connection failed overnight: {error_msg}",
                error=error_msg,
                dashboard_url=dashboard_url,
            )

        # Update database with error state and advance next run
        new_schedule = schedule or {}
        interval = new_schedule.get("interval", DEFAULT_INTERVAL)
        new_schedule.update({
            "last_run_at": now_ts,
            "next_run_at": compute_next_run(interval),
            "last_status": "error",
            "last_error": error_msg,
        })

        with connect() as db:
            db.execute(
                "UPDATE sources SET last_pulled_at = ?, last_scheduled_status = 'error', "
                "last_error = ?, schedule_json = ? WHERE id = ?",
                (now_ts, error_msg, json.dumps(new_schedule), source_id),
            )

        return {
            "ok": False,
            "status": "error",
            "error": error_msg,
            "alert": alert_results,
            "source_id": source_id,
            "source_name": row["name"],
            "schedule": new_schedule,
        }

    # 2. Apply recipe
    baseline_raw = loads(project["baseline_json"])
    recipe = loads(row["recipe_json"]) or []
    if baseline_raw:
        mapped, effects, problems = apply_recipe(df, baseline_raw["profile"], recipe)
    else:
        mapped, effects, problems = df, [], []

    # 3. Save candidate data and run quality checks
    upload_dir().mkdir(parents=True, exist_ok=True)
    temp_path = upload_dir() / f"{uuid.uuid4().hex}.csv"
    mapped.to_csv(temp_path, index=False)

    extra = {
        "source": {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "type": type_of(row["kind"], json.loads(row["settings_json"])),
            "rows_pulled": len(df),
            "mapped": recipe is not None or bool(row["is_standard"]),
            "steps": effects,
            "problems": problems,
            "auto_run": True,
            "trigger": trigger,
        }
    }

    actor = f"Schedule ({trigger})"
    doc = run_check(project, row["name"], temp_path, mapped, False, actor, extra)
    run_status = doc.get("status", "passed")
    issues = doc.get("issues", [])
    curr_issue_signatures = [f"{i.get('severity')}:{i.get('title')}" for i in issues]

    # Get previous issues for new break comparison if available
    prev_issue_signatures = []
    if row["last_run_id"]:
        with connect() as db:
            prev_run = db.execute("SELECT report_json FROM runs WHERE id = ?", (row["last_run_id"],)).fetchone()
            if prev_run and prev_run["report_json"]:
                prev_doc = json.loads(prev_run["report_json"])
                prev_issue_signatures = [f"{i.get('severity')}:{i.get('title')}" for i in prev_doc.get("issues", [])]

    # 4. Check if something new broke
    broke_now = is_new_break(
        previous_status=prev_status,
        current_status=run_status,
        previous_error=prev_error,
        current_error=None,
        previous_issues=prev_issue_signatures,
        current_issues=curr_issue_signatures,
        policy=notify_cfg.get("policy", "on_break"),
    )

    alert_results = {}
    if broke_now and (notify_cfg.get("slack_webhook_url") or notify_cfg.get("emails")):
        summary_msg = f"{len(issues)} issue(s) detected during automated check."
        alert_results = send_break_alert(
            notify_config=notify_cfg,
            project_name=project["name"],
            source_name=row["name"],
            status=run_status,
            summary=summary_msg,
            issues=issues,
            run_id=doc.get("run_id"),
            dashboard_url=dashboard_url,
        )

    # 5. Update source record and schedule
    new_schedule = schedule or {}
    interval = new_schedule.get("interval", DEFAULT_INTERVAL)
    new_schedule.update({
        "last_run_at": now_ts,
        "next_run_at": compute_next_run(interval),
        "last_status": run_status,
        "last_error": None,
    })

    with connect() as db:
        db.execute(
            "UPDATE sources SET last_pulled_at = ?, last_run_id = ?, last_scheduled_status = ?, "
            "last_error = NULL, schedule_json = ? WHERE id = ?",
            (now_ts, doc["run_id"], run_status, json.dumps(new_schedule), source_id),
        )

    return {
        **doc,
        "ok": True,
        "alert": alert_results,
        "source_id": source_id,
        "source_name": row["name"],
        "schedule": new_schedule,
    }


def run_due_sources() -> List[Dict[str, Any]]:
    """Find all sources with an active schedule that are due to run, and run them."""
    from server.core import loads
    from server.db import connect

    results = []
    with connect() as db:
        rows = db.execute("SELECT id, project_id, schedule_json FROM sources WHERE schedule_json IS NOT NULL").fetchall()

    for row in rows:
        schedule = loads(row["schedule_json"])
        if not schedule or not schedule.get("enabled"):
            continue
        if is_due(schedule.get("next_run_at")):
            try:
                res = auto_run_source(row["project_id"], row["id"], trigger="scheduler")
                results.append(res)
            except Exception as exc:
                logger.error("Failed to auto-run source %s: %s", row["id"], exc)
                results.append({"source_id": row["id"], "ok": False, "error": str(exc)})

    return results


class BackgroundScheduler:
    """Lightweight in-process periodic scheduler thread."""

    def __init__(self, check_interval_seconds: float = 30.0):
        self.check_interval_seconds = check_interval_seconds
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="datapulse-scheduler", daemon=True)
        self._thread.start()
        logger.info("BackgroundScheduler started (interval: %ss).", self.check_interval_seconds)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        self._thread = None
        logger.info("BackgroundScheduler stopped.")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                run_due_sources()
            except Exception as exc:
                logger.error("Error in scheduler tick: %s", exc)
            self._stop_event.wait(self.check_interval_seconds)
