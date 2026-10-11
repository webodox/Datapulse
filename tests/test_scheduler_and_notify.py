"""Tests for per-source scheduling, automated recipe + checks execution,

and Slack / Email alerts when sources break or fail quality checks.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from datapulse.notify import (
    format_email_content,
    format_slack_payload,
    is_new_break,
    parse_and_validate_emails,
    send_break_alert,
    send_email_alert,
    send_slack_alert,
    validate_email,
    validate_slack_webhook,
)
from datapulse.scheduler import (
    BackgroundScheduler,
    compute_next_run,
    is_due,
    auto_run_source,
    run_due_sources,
)
from server import app as server
from test_sources_api import client, EX, GOOD


# ---------------------------------------------------------------------------
# Notification & Slack / Email Unit Tests
# ---------------------------------------------------------------------------

def test_validate_slack_webhook():
    valid_urls = [
        "https://hooks.slack.com/services/T00/B00/XXXX",
        "https://hooks.slack.com/triggers/T00/123/abc",
        "http://localhost:8000/webhook",
        "https://example.com/slack-webhook",
    ]
    for url in valid_urls:
        assert validate_slack_webhook(url) == url

    invalid_urls = [
        "",
        "   ",
        "ftp://hooks.slack.com",
        "hooks.slack.com/missing-scheme",
        "https://invalid url with spaces",
    ]
    for url in invalid_urls:
        with pytest.raises(ValueError):
            validate_slack_webhook(url)


def test_validate_and_parse_emails():
    assert validate_email("  Dev@Company.com ") == "dev@company.com"
    with pytest.raises(ValueError):
        validate_email("not-an-email")
    with pytest.raises(ValueError):
        validate_email("test@")

    parsed = parse_and_validate_emails("dev@a.com, ops@b.com; dev@a.com\nteam@c.org")
    assert parsed == ["dev@a.com", "ops@b.com", "team@c.org"]
    assert parse_and_validate_emails([]) == []
    assert parse_and_validate_emails(None) == []


def test_format_slack_payload_clean_links():
    # Without dashboard URL: no mrkdwn link block, no broken syntax
    p1 = format_slack_payload(
        project_name="Sales",
        source_name="Orders DB",
        status="failed",
        summary="Found 2 issues",
        issues=[{"severity": "critical", "title": "Null rate spike", "summary": "15% nulls in id"}],
        dashboard_url=None,
    )
    assert "Sales" in p1["text"]
    text_blocks = json.dumps(p1["blocks"])
    assert "<|" not in text_blocks
    assert "Null rate spike" in text_blocks

    # With dashboard URL: properly formatted Slack link <URL|Text>
    p2 = format_slack_payload(
        project_name="Sales",
        source_name="Orders DB",
        status="error",
        summary="Database went down",
        error="OperationalError: connection refused",
        dashboard_url="https://app.datapulse.io/#/p/123/sources",
    )
    assert "connection refused" in json.dumps(p2["blocks"])
    assert "<https://app.datapulse.io/#/p/123/sources|Open Orders DB in DataPulse>" in json.dumps(p2["blocks"])


def test_send_slack_alert_mocked():
    # Success response
    with patch("httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        res = send_slack_alert("https://hooks.slack.com/test", "P", "S", "passed", "All good")
        assert res["ok"] is True
        assert res["status_code"] == 200

    # Non-200 error from Slack
    with patch("httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=404, text="channel_not_found")
        res = send_slack_alert("https://hooks.slack.com/test", "P", "S", "failed", "Broke")
        assert res["ok"] is False
        assert res["status_code"] == 404
        assert "channel_not_found" in res["message"]

    # Connection failure
    with patch("httpx.post", side_effect=Exception("Network down")):
        res = send_slack_alert("https://hooks.slack.com/test", "P", "S", "failed", "Broke")
        assert res["ok"] is False
        assert "Network down" in res["message"]

    # Invalid URL formatting
    res = send_slack_alert("bad-url", "P", "S", "failed", "Broke")
    assert res["ok"] is False
    assert "must start with https://" in res["message"]


def test_send_email_alert_fallback_and_smtp(monkeypatch):
    # Without SMTP host: logs warning, returns ok=True, delivered=False
    monkeypatch.delenv("DATAPULSE_SMTP_HOST", raising=False)
    res = send_email_alert(["dev@company.com"], "Subject", "Text", "<p>Html</p>")
    assert res["ok"] is True
    assert res["delivered"] is False
    assert res["logged"] is True

    # With SMTP host configured: sends message via smtplib
    monkeypatch.setenv("DATAPULSE_SMTP_HOST", "smtp.test.com")
    monkeypatch.setenv("DATAPULSE_SMTP_PORT", "25")
    monkeypatch.setenv("DATAPULSE_SMTP_TLS", "false")

    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        res = send_email_alert(["dev@company.com"], "Subject", "Text", "<p>Html</p>")
        assert res["ok"] is True
        assert res["delivered"] is True
        mock_server.send_message.assert_called_once()


def test_is_new_break_logic():
    # Passed status never breaks
    assert not is_new_break("passed", "passed")
    assert not is_new_break("failed", "passed")

    # Transition from passed -> failed
    assert is_new_break("passed", "failed")
    # First time run (None) -> failed
    assert is_new_break(None, "failed")

    # Already failed, same issues: should NOT spam alerts
    assert not is_new_break(
        "failed", "failed",
        previous_issues=["critical:null_rate"],
        current_issues=["critical:null_rate"],
        policy="on_break",
    )

    # Already failed, but NEW issue appeared: should alert!
    assert is_new_break(
        "failed", "failed",
        previous_issues=["critical:null_rate"],
        current_issues=["critical:null_rate", "warning:type_mismatch"],
        policy="on_break",
    )

    # Broken overnight (database connection refused)
    assert is_new_break("passed", "error", current_error="DB offline")
    # Already broken, same error
    assert not is_new_break("error", "error", previous_error="DB offline", current_error="DB offline")
    # Already broken, different error
    assert is_new_break("error", "error", previous_error="DB offline", current_error="Auth token expired")

    # Policy 'always_on_failure'
    assert is_new_break("failed", "failed", policy="always_on_failure")
    # Policy 'always'
    assert is_new_break("passed", "passed", policy="always")


# ---------------------------------------------------------------------------
# Scheduling & Due Computation Tests
# ---------------------------------------------------------------------------

def test_compute_next_run_and_is_due():
    base = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)

    next_hourly = compute_next_run("hourly", base)
    assert next_hourly == "2026-10-10T13:00:00+00:00"

    next_daily = compute_next_run("daily", base)
    assert next_daily == "2026-10-11T12:00:00+00:00"

    # Overnight target is 02:00 UTC
    next_overnight = compute_next_run("daily_overnight", base)
    assert next_overnight == "2026-10-11T02:00:00+00:00"

    # Due checking
    assert is_due("2026-10-10T11:59:00+00:00", base)
    assert is_due("2026-10-10T12:00:00+00:00", base)
    assert not is_due("2026-10-10T12:01:00+00:00", base)
    assert not is_due(None, base)


# ---------------------------------------------------------------------------
# End-to-End Scheduling, Auto-Run & API Tests
# ---------------------------------------------------------------------------

def test_source_schedule_api_and_auto_run(client):
    # Seed sample demo project
    project = client.post("/api/demo/seed").json()
    pid = project["id"]

    # List sources
    sources = client.get(f"/api/projects/{pid}/sources").json()
    assert len(sources) >= 1
    sid = sources[0]["id"]

    # Configure schedule on this source
    schedule_data = {
        "enabled": True,
        "interval": "hourly",
        "slack_webhook_url": "https://hooks.slack.com/services/T00/B00/test",
        "emails": ["alert@example.com"],
        "policy": "on_break",
    }
    r = client.put(f"/api/projects/{pid}/sources/{sid}/schedule", json=schedule_data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schedule"]["enabled"] is True
    assert body["schedule"]["interval"] == "hourly"
    assert body["schedule"]["next_run_at"] is not None
    assert body["notify"]["slack_webhook_url"] == "https://hooks.slack.com/services/T00/B00/test"
    assert body["notify"]["emails"] == ["alert@example.com"]

    # Fetch schedule directly via GET
    r_get = client.get(f"/api/projects/{pid}/sources/{sid}/schedule")
    assert r_get.status_code == 200
    assert r_get.json()["schedule"]["enabled"] is True

    # Trigger manual auto-run now
    with patch("datapulse.notify.send_slack_alert") as mock_slack:
        mock_slack.return_value = {"ok": True, "message": "sent"}
        run_res = client.post(f"/api/projects/{pid}/sources/{sid}/schedule/run")
        assert run_res.status_code == 200, run_res.text
        data = run_res.json()
        assert data["ok"] is True
        assert data["status"] in ("passed", "failed")
        assert data["schedule"]["last_run_at"] is not None

    # Verify source was updated in list
    s_updated = client.get(f"/api/projects/{pid}/sources").json()[0]
    assert s_updated["last_pulled_at"] is not None
    assert s_updated["schedule"]["enabled"] is True


def test_source_breaks_overnight_alerts_slack(client):
    project = client.post("/api/demo/seed").json()
    pid = project["id"]

    # Add a database source pointing to a non-existent database/host (simulating broken DB overnight)
    broken_source = client.post(
        f"/api/projects/{pid}/sources",
        json={
            "name": "Overnight Broken Store",
            "kind": "database",
            "settings": {"url": "postgresql://invalid_user:bad_pass@127.0.0.1:59999/nonexistent", "query": "orders"},
        },
    ).json()
    sid = broken_source["id"]

    # Configure schedule with Slack webhook
    client.put(
        f"/api/projects/{pid}/sources/{sid}/schedule",
        json={
            "enabled": True,
            "interval": "daily_overnight",
            "slack_webhook_url": "https://hooks.slack.com/services/T00/B00/nightalert",
            "emails": ["oncall@company.com"],
            "policy": "on_break",
        },
    )

    # Run auto-run and verify that Slack receives the alert when source fails to connect!
    with patch("datapulse.notify.send_slack_alert") as mock_slack:
        mock_slack.return_value = {"ok": True, "message": "Alert delivered to Slack"}

        run_res = client.post(f"/api/projects/{pid}/sources/{sid}/schedule/run")
        assert run_res.status_code == 200
        data = run_res.json()
        assert data["ok"] is False
        assert data["status"] == "error"
        assert "error" in data
        assert data["schedule"]["last_status"] == "error"

        # Slack alert was called!
        assert mock_slack.called
        call_kwargs = mock_slack.call_args[1]
        assert call_kwargs["webhook_url"] == "https://hooks.slack.com/services/T00/B00/nightalert"
        assert call_kwargs["source_name"] == "Overnight Broken Store"
        assert call_kwargs["status"] == "error"

    # Verify that source card retains the error information
    source_detail = client.get(f"/api/projects/{pid}/sources").json()
    matched = next(s for s in source_detail if s["id"] == sid)
    assert matched["last_scheduled_status"] == "error"
    assert matched["last_error"] is not None


def test_project_level_notifications_and_test_endpoint(client):
    project = client.post("/api/projects", json={"name": "AlertProject"}).json()
    pid = project["id"]

    # Get empty notifications
    r = client.get(f"/api/projects/{pid}/notifications")
    assert r.status_code == 200
    assert r.json()["slack_webhook_url"] == ""

    # Save project notifications
    save_body = {
        "slack_webhook_url": "https://hooks.slack.com/services/T1/B1/X1",
        "emails": ["team@datapulse.io", "alerts@datapulse.io"],
        "policy": "on_break",
    }
    r = client.put(f"/api/projects/{pid}/notifications", json=save_body)
    assert r.status_code == 200
    assert r.json()["slack_webhook_url"] == save_body["slack_webhook_url"]
    assert len(r.json()["emails"]) == 2

    # Test notification endpoint (mocking both Slack and email)
    with patch("datapulse.notify.send_slack_alert") as mock_slack, patch("datapulse.notify.send_email_alert") as mock_email:
        mock_slack.return_value = {"ok": True, "message": "Slack test passed"}
        mock_email.return_value = {"ok": True, "delivered": True, "message": "Email test passed"}

        test_res = client.post(
            f"/api/projects/{pid}/notifications/test",
            json={"channel": "all"},
        )
        assert test_res.status_code == 200
        body = test_res.json()
        assert body["slack"]["ok"] is True
        assert body["email"]["ok"] is True
        assert mock_slack.called
        assert mock_email.called


def test_background_scheduler_lifecycle():
    scheduler = BackgroundScheduler(check_interval_seconds=0.1)
    assert not scheduler.is_running
    scheduler.start()
    assert scheduler.is_running
    scheduler.stop()
    assert not scheduler.is_running
