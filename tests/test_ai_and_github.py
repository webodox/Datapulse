import json

import numpy as np
import pytest

from datapulse import ai, github
from datapulse.checks import run_checks
from datapulse.errors import DataPulseError
from datapulse.profile import profile_dataframe
from fake_claude import FakeClaude, text

SECRET = "ZZ-SECRET-VALUE-123"


def report_with_secret(baseline, cfg, df):
    cur = df.copy()
    cur.loc[:20, "status"] = SECRET
    cur.loc[cur.sample(frac=0.5, random_state=0).index, "customer_id"] = np.nan
    return run_checks(baseline, cur, cfg)


def test_ai_payload_has_aggregates_only(baseline, cfg, df):
    report = report_with_secret(baseline, cfg, df)
    assert any(SECRET in e for i in report.issues for e in i.examples)  # visible locally...
    payload = json.dumps(ai.build_explain_payload(report))
    assert SECRET not in payload  # ...but never sent
    assert "ZZ-SECRET" not in payload and "C0001" not in payload and '"examples"' not in payload
    suggest_payload = json.dumps(ai.build_suggest_payload(profile_dataframe(df)))
    assert "delivered" not in suggest_payload and "category_count" in suggest_payload


def test_without_api_key_deterministic_explanations_remain(baseline, cfg, df, monkeypatch):
    monkeypatch.delenv(cfg.ai.api_key_env, raising=False)
    report = report_with_secret(baseline, cfg, df)
    note = ai.explain_issues(report, cfg)
    assert "not set" in note
    assert all(i.explanation and i.ai_explanation is None for i in report.issues)


def test_ai_explanations_are_attached_and_labelled(baseline, cfg, df, monkeypatch):
    monkeypatch.setenv(cfg.ai.api_key_env, "k")
    fake = FakeClaude([text(json.dumps({"explanations": [{"index": 0, "text": "Likely a failed join."}]}))])
    monkeypatch.setattr(ai, "get_client", lambda cfg: fake)
    report = report_with_secret(baseline, cfg, df)
    assert "AI explanations added" in ai.explain_issues(report, cfg)
    assert report.issues[0].ai_explanation == "Likely a failed join."
    assert SECRET not in fake.sent() and fake.calls[0]["fallbacks"] == "default"
    from datapulse.report import render_markdown
    assert "AI-generated explanation" in render_markdown(report)


def test_ai_failure_falls_back_gracefully(baseline, cfg, df, monkeypatch):
    monkeypatch.setenv(cfg.ai.api_key_env, "k")
    monkeypatch.setattr(ai, "get_client", lambda cfg: FakeClaude([text("not json")]))
    report = report_with_secret(baseline, cfg, df)
    assert "unavailable" in ai.explain_issues(report, cfg)
    assert all(i.ai_explanation is None for i in report.issues)


def test_overview_payload_groups_issues_and_sends_no_values(baseline, cfg, df):
    from datapulse.report import render_json
    doc = json.loads(render_json(report_with_secret(baseline, cfg, df)))
    payload = ai.build_overview_payload(doc)
    assert payload["issue_count"] == len(doc["issues"])
    assert sum(g["count"] for g in payload["groups"]) == len(doc["issues"])
    assert payload["worst_issues"][0]["severity"] == "critical"
    sent = json.dumps(payload)
    assert "ZZ-SECRET" not in sent and "C0001" not in sent and '"examples"' not in sent


def test_overview_is_summarised_and_trimmed(baseline, cfg, df, monkeypatch):
    from datapulse.report import render_json
    doc = json.loads(render_json(report_with_secret(baseline, cfg, df)))
    reply = {"summary": " Mostly one problem. ", "focus": [{"title": f"t{n}", "detail": "d"} for n in range(5)]}
    fake = FakeClaude([text(json.dumps(reply))])
    overview = ai.summarize_run(doc, cfg, client=fake)
    assert overview["summary"] == "Mostly one problem." and [f["title"] for f in overview["focus"]] == ["t0", "t1", "t2"]
    assert SECRET not in fake.sent()
    with pytest.raises(DataPulseError, match="malformed"):
        ai.summarize_run(doc, cfg, client=FakeClaude([text("not json")]))


def test_pr_comment_is_created_then_updated(monkeypatch):
    calls = []
    store = []

    def fake_request(method, url, token, payload=None):
        calls.append((method, url))
        if method == "GET":
            return list(store)
        if method == "POST":
            store.append({"id": 7, "body": payload["body"]})
        else:
            store[0]["body"] = payload["body"]
        return {}

    monkeypatch.setattr(github, "_request", fake_request)
    assert github.upsert_pr_comment("first", "o/r", 3, "tok") == "created"
    assert github.upsert_pr_comment("second", "o/r", 3, "tok") == "updated"
    assert len(store) == 1 and "second" in store[0]["body"] and github.MARKER in store[0]["body"]
    assert calls[-1][0] == "PATCH" and calls[-1][1].endswith("/issues/comments/7")


def test_comment_command_uses_event_context(tmp_path, monkeypatch, runner):
    from datapulse.cli import app
    report = tmp_path / "r.md"
    report.write_text("hello")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"number": 42}}))
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    got = {}
    monkeypatch.setattr("datapulse.cli.upsert_pr_comment",
                        lambda body, repo, pr, token, api: got.update(repo=repo, pr=pr, token=token) or "created")
    r = runner.invoke(app, ["comment", str(report)])
    assert r.exit_code == 0 and got == {"repo": "o/r", "pr": 42, "token": "t"}
    monkeypatch.delenv("GITHUB_TOKEN")
    assert runner.invoke(app, ["comment", str(report)]).exit_code == 2


def test_workspace_header_is_sent_only_when_configured(cfg, monkeypatch):
    monkeypatch.setenv(cfg.ai.api_key_env, "k")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    assert "anthropic-workspace-id" not in ai.get_client(cfg).default_headers
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_123")
    assert ai.get_client(cfg).default_headers["anthropic-workspace-id"] == "wrkspc_123"


def test_pasted_key_whitespace_is_trimmed(cfg, monkeypatch):
    """A key pasted with a trailing newline used to fail every request as "Connection error"."""
    monkeypatch.setenv(cfg.ai.api_key_env, "  sk-ant-test\r\n")
    assert ai.get_api_key(cfg) == "sk-ant-test"
    assert ai.get_client(cfg).api_key == "sk-ant-test"
    monkeypatch.setenv(cfg.ai.api_key_env, " \n")
    assert ai.get_api_key(cfg) is None  # whitespace only counts as "not set"
