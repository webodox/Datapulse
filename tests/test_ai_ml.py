"""The learned drift check, the AI triage agent and the plain-English rules assistant."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import make_df
from datapulse import ai
from datapulse.agent import TriageContext, run_triage
from datapulse.checks import run_checks
from datapulse.cli import app
from datapulse.config import parse_config
from datapulse.errors import DataPulseError
from datapulse.ml import drift_model
from datapulse.profile import profile_dataframe
from datapulse.report import render_json, render_markdown
from fake_claude import FakeClaude, text, tool_calls

SECRET = "ZZ-SECRET-VALUE-123"
TRIAGE = {
    "headline": "Amount looks like it switched from dollars to cents.", "verdict": "block",
    "verdict_reason": "Revenue dashboards would be 100x too high.",
    "root_causes": [{"hypothesis": "Unit change upstream", "confidence": "high", "evidence": "mean x100"}],
    "actions": [{"step": "Ask the exporter whether amount is now in cents", "owner": "data producer"}],
    "notify": [{"who": "churn_model (ds)", "why": "reads amount"}],
    "message": "Heads up: today's orders batch is blocked.",
}


# ---------- ML drift ----------

def test_ml_drift_flags_a_shift_and_names_the_column(baseline, cfg, df):
    cur = make_df(seed=1)
    cur["amount"] = cur["amount"] * 100
    report = run_checks(baseline, cur, cfg, reference=df)
    issue = next(i for i in report.issues if i.check == "ml_drift")
    assert issue.measured["drift_auc"] > 0.9 and issue.columns[0] == "amount"
    assert "order_id (identifier)" in issue.measured["columns_not_modelled"]  # sequential ids never count
    assert "churn_model" in {c["name"] for c in issue.consumers}  # drivers map to downstream consumers


def test_ml_drift_is_quiet_on_same_distribution_and_off_without_reference(baseline, cfg, df):
    report = run_checks(baseline, make_df(seed=7), cfg, reference=df)
    assert "ml_drift" in report.checks_run and not [i for i in report.issues if i.check == "ml_drift"]
    assert "ml_drift" not in run_checks(baseline, df, cfg).checks_run


def test_ml_drift_catches_a_change_per_column_checks_cannot_see():
    rs = np.random.RandomState(0)
    q = rs.randint(1, 6, 2000)
    base = pd.DataFrame({"quantity": q, "amount": q * 20 + rs.normal(0, 3, 2000)})
    cur = base.assign(amount=rs.permutation(base["amount"].to_numpy()))  # same values, broken relationship
    assert drift_model(base, cur)["auc"] > 0.75


def test_ml_drift_skips_timestamps_and_small_files():
    rs = np.random.RandomState(0)
    mk = lambda start: pd.DataFrame({"ts": pd.date_range(start, periods=300, freq="h").astype(str),
                                     "v": rs.normal(0, 1, 300)})
    res = drift_model(mk("2026-01-01"), mk("2026-06-01"))
    assert res["skipped"] == {"ts": "timestamp"} and res["auc"] < 0.7
    assert drift_model(mk("2026-01-01").head(10), mk("2026-01-01").head(10))["auc"] is None


def test_ml_drift_config_is_validated():
    assert parse_config({"checks": {"ml_drift": {"max_auc": 0.8, "exclude": ["id"]}}}).ml_drift.max_auc == 0.8
    with pytest.raises(DataPulseError, match="ml_drift.max_auc"):
        parse_config({"checks": {"ml_drift": {"max_auc": 0.3}}})


# ---------- triage agent ----------

def _ctx(baseline, cfg, df):
    cur = df.copy()
    cur.loc[:20, "status"] = SECRET
    cur["amount"] = cur["amount"] * 100
    report = run_checks(baseline, cur, cfg, reference=df)
    assert any(SECRET in e for i in report.issues for e in i.examples)  # present locally...
    return TriageContext(json.loads(render_json(report)), baseline["profile"], profile_dataframe(cur),
                         history=[{"when": "yesterday", "status": "passed"}],
                         consumers=[vars(c) for c in cfg.consumers])


def test_agent_investigates_with_tools_then_returns_structured_triage(baseline, cfg, df):
    fake = FakeClaude([
        tool_calls(("list_issues", {}), ("get_run_history", {})),
        tool_calls(("get_column_stats", {"column": "amount"}), ("get_consumers", {})),
        text(json.dumps(TRIAGE)),
    ])
    out = run_triage(_ctx(baseline, cfg, df), cfg, client=fake)
    assert out["verdict"] == "block" and [s["tool"] for s in out["steps"]] == [
        "list_issues", "get_run_history", "get_column_stats", "get_consumers"]
    sent = fake.sent()
    assert SECRET not in sent  # ...but never sent: tool results carry aggregates only
    assert "p50" in sent and "churn_model" in sent  # it did see real statistics and consumers
    last = fake.calls[-1]["messages"]
    assert [m["role"] for m in last] == ["user", "assistant", "user", "assistant", "user"]
    assert all(r["type"] == "tool_result" for r in last[-1]["content"])  # parallel results in one message
    assert fake.calls[0]["output_config"]["format"]["schema"]["required"][0] == "headline"


def test_agent_reports_unknown_tools_and_stops_runaway_loops(baseline, cfg, df):
    fake = FakeClaude([tool_calls(("delete_everything", {})), text(json.dumps(TRIAGE))])
    run_triage(_ctx(baseline, cfg, df), cfg, client=fake)
    assert fake.calls[1]["messages"][-1]["content"][0]["is_error"] is True
    looping = FakeClaude([tool_calls(("list_issues", {}))] * 3)
    with pytest.raises(DataPulseError, match="did not finish"):
        run_triage(_ctx(baseline, cfg, df), cfg, client=looping, max_turns=3)


def test_agent_surfaces_refusals_and_missing_keys(baseline, cfg, df, monkeypatch):
    with pytest.raises(DataPulseError, match="declined"):
        run_triage(_ctx(baseline, cfg, df), cfg, client=FakeClaude([text("", stop_reason="refusal")]))
    monkeypatch.delenv(cfg.ai.api_key_env, raising=False)
    with pytest.raises(DataPulseError, match="ANTHROPIC_API_KEY"):
        run_triage(_ctx(baseline, cfg, df), cfg)


def test_triage_is_rendered_in_reports(baseline, cfg, df):
    report = run_checks(baseline, df.assign(amount=df["amount"] * 100), cfg)
    report.triage = {**TRIAGE, "steps": []}
    md = render_markdown(report)
    assert "AI triage" in md and "Block this batch" in md and "- [ ] Ask the exporter" in md
    assert json.loads(render_json(report))["triage"]["verdict"] == "block"


def test_cli_triage_flag(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    make_df().to_csv("good.csv", index=False)
    make_df().assign(amount=lambda d: d["amount"] * 100).to_csv("new.csv", index=False)
    assert runner.invoke(app, ["baseline", "good.csv"]).exit_code == 0
    fake = FakeClaude([tool_calls(("list_issues", {})), text(json.dumps(TRIAGE))])
    monkeypatch.setattr("datapulse.agent.get_client", lambda cfg: fake)
    r = runner.invoke(app, ["check", "new.csv", "--reference", "good.csv", "--triage", "--json", "r.json"])
    assert r.exit_code == 0, r.output  # warnings only: triage never changes the exit code
    assert "agent looked at list_issues" in r.output and "Block this batch" in r.output
    assert json.loads(Path("r.json").read_text())["triage"]["headline"] == TRIAGE["headline"]
    assert "ml_drift" in Path("datapulse-report.md").read_text()


# ---------- plain-English rules ----------

def test_draft_rules_returns_a_validated_config_and_self_corrects(cfg, df):
    good = "version: 1\nchecks:\n  duplicates:\n    columns: [order_id]\n"
    fake = FakeClaude([
        text(json.dumps({"config_yaml": "checks:\n  nulls: {}\n", "summary": "x", "not_supported": []})),
        text(json.dumps({"config_yaml": good, "summary": "order_id must be unique",
                         "not_supported": ["an explicit list of allowed statuses"]})),
    ])
    out = ai.draft_rules("order_id must be unique; status in a fixed list", "version: 1\n", profile_dataframe(df),
                         cfg, client=fake)
    assert out["config_yaml"] == good and out["not_supported"]
    assert "unknown key" in fake.calls[1]["messages"][-1]["content"]  # validation error fed back
    assert "delivered" not in fake.sent()  # category values from the profile are never sent


def test_draft_rules_gives_up_after_repeated_invalid_drafts(cfg):
    bad = text(json.dumps({"config_yaml": "fail_on: sometimes\n", "summary": "", "not_supported": []}))
    with pytest.raises(DataPulseError, match="could not produce a valid"):
        ai.draft_rules("fail on everything", "version: 1\n", None, cfg, client=FakeClaude([bad, bad]))
