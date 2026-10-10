"""The AI repair agent: safe operations only, verified by re-running the checks, original never changed."""
import json
from pathlib import Path

import pandas as pd
import pytest

from datapulse.baseline import build_baseline
from datapulse.cli import app
from datapulse.config import load_config
from datapulse.errors import DataPulseError
from datapulse.fix import RepairError, RepairSession, apply_operations, run_repair
from datapulse.io import load_dataframe
from datapulse.profile import chart_data, profile_dataframe
from fake_claude import FakeClaude, text, tool_calls

EX = Path(__file__).resolve().parents[1] / "examples"
SUMMARY = {"summary": "Scaled amount back to dollars, fixed quantity, removed duplicates.",
           "not_fixed": [{"issue": "country is missing", "why": "a missing column can't be recreated"}],
           "warnings": []}


@pytest.fixture
def session():
    cfg = load_config(EX / "datapulse.yml")
    good, bad = load_dataframe(EX / "data/orders.csv"), load_dataframe(EX / "data/orders_drifted.csv")
    return RepairSession(bad, build_baseline(good, EX / "data/orders.csv", 1, cfg.max_categories), cfg, reference=good)


def repair(session, name, **args):
    return session.call(name, {**args, "reason": "test"})


def test_operations_fix_the_sample_drift_and_never_touch_the_original(session):
    original = session.original.copy()
    before = len(session.list_issues()["issues"])
    repair(session, "scale_column", column="amount", factor=0.01)
    repair(session, "convert_to_number", column="quantity")  # values like "5 units"
    result = repair(session, "remove_duplicates", columns=["order_id"])
    assert result["issues_remaining"]["total"] < before
    titles = [i["title"] for i in session.list_issues()["issues"]]
    assert not any("amount" in t or "quantity" in t or "Duplicate" in t for t in titles)
    assert "A model can tell the new data from the baseline" not in titles  # ML drift gone too
    assert abs(session.working["amount"].mean() - 61.31) < 5
    pd.testing.assert_frame_equal(session.original, original)


def test_bad_requests_are_rejected_with_a_reason(session):
    for name, args, needle in [
        ("scale_column", {"column": "nope", "factor": 2}, "no column"),
        ("scale_column", {"column": "amount", "factor": 0}, "non-zero"),
        ("rename_column", {"column": "amount", "new_name": "status"}, "already exists"),
        ("fill_missing", {"column": "amount", "strategy": "median"}, "no empty values"),
        ("standardize_categories", {"column": "amount"}, "no list of known values"),
    ]:
        with pytest.raises(RepairError, match=needle):
            repair(session, name, **args)
    with pytest.raises(RepairError, match="nothing to undo"):
        session.call("undo_last", {})


def test_undo_and_replay_are_deterministic(session):
    repair(session, "scale_column", column="amount", factor=0.01)
    repair(session, "drop_column", column="coupon_code")
    assert "Undid drop_column" in session.call("undo_last", {})
    assert "coupon_code" in session.working.columns
    replayed, effects = apply_operations(session.original, session.baseline["profile"], session.operations)
    pd.testing.assert_frame_equal(replayed, session.working)
    assert effects == ["Multiplied 380 values in 'amount' by 0.01."]


def test_standardize_and_fill_and_drop_rows():
    base = pd.DataFrame({"status": ["paid", "open"] * 50, "n": range(100)})
    cur = pd.DataFrame({"status": [" Paid", "OPEN", "paid", None] * 25, "n": [1.0, None, 3.0, 4.0] * 25})
    cfg = load_config(EX / "datapulse.yml")
    s = RepairSession(cur, {"profile": profile_dataframe(base)}, cfg)
    assert "Standardized 50 values" in repair(s, "standardize_categories", column="status")["effect"]
    assert set(s.working["status"].dropna()) == {"paid", "open"}
    assert "Filled 25 empty values" in repair(s, "fill_missing", column="n", strategy="median")["effect"]
    assert "Removed 25 rows" in repair(s, "drop_rows_missing", column="status")["effect"]


def test_agent_loop_repairs_then_rechecks_and_sends_no_values(session):
    session.original.loc[:5, "status"] = "ZZ-SECRET-VALUE"
    fake = FakeClaude([
        tool_calls(("list_issues", {}), ("get_column_stats", {"column": "amount"})),
        tool_calls(("scale_column", {"column": "amount", "factor": 0.01, "reason": "cents"}),
                   ("convert_to_number", {"column": "quantity", "reason": "units text"})),
        tool_calls(("remove_duplicates", {"columns": ["order_id"], "reason": "dupes"}),
                   ("drop_column", {"column": "missing_col", "reason": "x"})),  # error is reported, not raised
        text(json.dumps(SUMMARY)),
    ])
    result = run_repair(session, client=fake)
    assert [o["operation"] for o in result["operations"]] == ["scale_column", "convert_to_number", "remove_duplicates"]
    assert result["before"]["total"] == 9 and result["after"]["total"] < result["before"]["total"]
    assert result["rows"] == {"before": 380, "after": 370}
    assert result["not_fixed"][0]["issue"] == "country is missing"
    assert len(result["fixed_frame"]) == 370
    errors = [r for r in fake.calls[3]["messages"][-1]["content"] if r["is_error"]]
    assert len(errors) == 1 and "no column" in errors[0]["content"]
    assert "ZZ-SECRET" not in fake.sent()  # tool results carry counts and statistics only


def test_agent_gives_up_after_too_many_steps(session):
    with pytest.raises(DataPulseError, match="did not finish"):
        run_repair(session, client=FakeClaude([tool_calls(("list_issues", {}))] * 3), max_turns=3)


def test_chart_data_uses_the_baselines_buckets():
    good, bad = load_dataframe(EX / "data/orders.csv"), load_dataframe(EX / "data/orders_drifted.csv")
    charts = chart_data(profile_dataframe(good), bad)
    hist = charts["amount"]["hist"]
    assert len(hist["baseline"]) == len(hist["current"]) == len(hist["edges"]) + 1
    assert hist["current"][-1] == 1.0  # every drifted amount lands past the baseline's top bucket
    assert charts["country"]["null_rate"][1] is None and charts["customer_id"]["null_rate"][1] == 0.35


def test_cli_fix_writes_a_new_file(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = str(EX / "datapulse.yml")
    assert runner.invoke(app, ["baseline", str(EX / "data/orders.csv"), "-c", cfg]).exit_code == 0
    fake = FakeClaude([tool_calls(("scale_column", {"column": "amount", "factor": 0.01, "reason": "cents"})),
                       text(json.dumps(SUMMARY))])
    monkeypatch.setattr("datapulse.fix.get_client", lambda c: fake)
    drifted = str(EX / "data/orders_drifted.csv")
    r = runner.invoke(app, ["fix", drifted, "-c", cfg, "-o", "fixed.csv"])
    assert r.exit_code == 0, r.output
    assert "Issues: " in r.output and "Multiplied 380 values" in r.output and "left for a person" in r.output
    assert pd.read_csv("fixed.csv")["amount"].mean() < 100
    assert runner.invoke(app, ["fix", drifted, "-c", cfg, "-o", drifted]).exit_code == 2  # never the original
