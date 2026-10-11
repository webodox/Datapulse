"""Bad rows: explicit rules, which cells break the rules or the standard, and the fixes for them."""
from pathlib import Path

import pandas as pd
import pytest

from datapulse.baseline import build_baseline
from datapulse.checks import run_checks
from datapulse.config import parse_config_text
from datapulse.errors import DataPulseError
from datapulse.fix import OPERATIONS
from datapulse.io import load_dataframe
from datapulse.rows import ROW, apply_fixes, bad_rows, preview_fixes, suggest_fixes

EX = Path(__file__).resolve().parents[1] / "examples"
ORDERS = load_dataframe(EX / "data/orders.csv")
DRIFTED = load_dataframe(EX / "data/orders_drifted.csv")
RULES = """
  rules:
    columns:
      order_id: {required: true, unique: true}
      amount: {min: 0, max: 1000}
      status: {allowed: [pending, shipped, delivered, cancelled]}
      customer_id: {pattern: '^C\\d{4}$'}
"""


def sample_cfg(rules: str = RULES, show_values: bool = True):
    text = (EX / "datapulse.yml").read_text().replace("\nchecks:\n", "\nchecks:" + rules)
    return parse_config_text(text.replace("show_values: true", f"show_values: {str(show_values).lower()}"), "test")


@pytest.fixture
def standard():
    cfg = sample_cfg()
    return cfg, build_baseline(ORDERS, EX / "data/orders.csv", 1, cfg.max_categories)


def test_rules_config_is_validated():
    cfg = sample_cfg()
    assert cfg.rules.severity == "critical"
    assert cfg.rules.columns["amount"].min == 0 and cfg.rules.columns["order_id"].unique
    for bad, message in [("amount: {min: 5, max: 1}", "larger than"), ("amount: {pattern: '('}", "regular expression"),
                         ("amount: {between: 1}", "unknown key"), ("amount: {}", "at least one"),
                         ("status: {allowed: []}", "non-empty list")]:
        with pytest.raises(DataPulseError, match=message):
            sample_cfg(f"\n  rules:\n    columns:\n      {bad}\n")


def test_rules_check_counts_every_broken_rule(standard):
    cfg, baseline = standard
    df = ORDERS.copy()
    df.loc[:4, "amount"] = -1
    df.loc[5:6, "order_id"] = None
    df.loc[7, "order_id"] = df.loc[8, "order_id"]
    df.loc[9:11, "status"] = "lost"
    df.loc[12, "customer_id"] = "nobody"
    report = run_checks(baseline, df, cfg)
    rules = {i.check: i for i in report.issues if i.check.startswith("rules.")}
    assert set(rules) == {"rules.min", "rules.required", "rules.unique", "rules.allowed", "rules.pattern"}
    assert rules["rules.min"].measured["rows_breaking_rule"] == 5 and rules["rules.min"].severity == "critical"
    assert rules["rules.allowed"].examples == ["lost (3 rows)"]
    assert report.failed and "rules" in report.checks_run

    clean = run_checks(baseline, ORDERS, cfg)
    assert not [i for i in clean.issues if i.check.startswith("rules")] and "rules" in clean.passed_checks
    missing = run_checks(baseline, ORDERS, sample_cfg("\n  rules:\n    columns:\n      email: {required: true}\n"))
    assert [i.title for i in missing.issues if i.check == "rules.missing_column"] == ["Rule column 'email' is not in the data"]


def test_bad_rows_flags_each_cell_with_the_rule_it_breaks(standard):
    cfg, baseline = standard
    report = run_checks(baseline, DRIFTED, cfg)
    out = bad_rows(DRIFTED, baseline, cfg, report.issues)
    assert out["total_rows"] == len(DRIFTED) and out["bad_rows"] == out["failing_rows"] == len(DRIFTED)
    summary = {(s["rule"], s["kind"], s["column"]): s for s in out["summary"]}
    assert summary[("schema", "not_number", "quantity")]["severity"] == "critical"
    assert summary[("null_rate", "empty", "customer_id")]["rows"] == int(DRIFTED["customer_id"].isna().sum())
    assert summary[("rules", "max", "amount")]["severity"] == "critical"
    assert summary[("duplicates", "duplicate", "order_id")]["rows"] == int(DRIFTED["order_id"].duplicated().sum())
    first = out["rows"][0]
    assert first["row"] == 1 and first["values"]["quantity"] == "5 units"
    assert first["cells"]["quantity"][0]["reason"] == "'5 units' is not a number; the standard's column is numbers"
    assert [f["rule"] for f in first["cells"]["amount"]] == ["distribution", "rules"]
    assert len(out["rows"]) <= 200 and out["columns"] == list(DRIFTED.columns)


def test_values_that_only_differ_from_the_standard_are_not_failing(standard):
    cfg, baseline = standard
    df = ORDERS.copy()
    df.loc[0, "amount"] = 999.0  # above the standard's largest amount, but inside the rule's 0..1000
    out = bad_rows(df, baseline, cfg, run_checks(baseline, df, cfg).issues)
    assert out["bad_rows"] == 1 and out["failing_rows"] == 0
    assert out["rows"][0]["cells"]["amount"][0] == {
        "rule": "distribution", "severity": "standard",
        "reason": "999.0 is outside the standard's range 9.58 to 240.45"}


def test_hidden_values_stay_hidden(standard):
    _, baseline = standard
    cfg = sample_cfg(show_values=False)
    out = bad_rows(DRIFTED, baseline, cfg, run_checks(baseline, DRIFTED, cfg).issues)
    assert out["rows"][0]["values"] is None and not out["show_values"]
    reasons = [f["reason"] for r in out["rows"] for fs in r["cells"].values() for f in fs]
    assert "Not a number; the standard's column is numbers" in reasons
    assert not any("units" in r or "10391" in r for r in reasons)


def test_suggested_fixes_correct_values_first_and_never_invent_ids(standard):
    cfg, baseline = standard
    report = run_checks(baseline, DRIFTED, cfg)
    steps = {(s["operation"], s["args"].get("column")): s for s in suggest_fixes(DRIFTED, baseline, cfg, report.issues)}
    assert steps[("scale_column", "amount")]["args"]["factor"] == 0.01 and steps[("scale_column", "amount")]["default"]
    assert steps[("convert_to_number", "quantity")]["default"]
    assert steps[("remove_duplicates", None)]["args"] == {"columns": ["order_id"]}
    assert steps[("drop_rows_missing", "customer_id")]["default"] is False
    assert ("fill_missing", "customer_id") not in steps
    assert steps[("drop_rows_outside_range", "amount")]["args"] == {"column": "amount", "min": 0.0, "max": 1000.0}


def test_preview_keeps_original_row_numbers_and_measures_the_result(standard):
    cfg, baseline = standard
    report = run_checks(baseline, DRIFTED, cfg)
    steps = [s for s in suggest_fixes(DRIFTED, baseline, cfg, report.issues) if s["default"]]
    p = preview_fixes(DRIFTED, baseline, cfg, steps, report.issues)
    dupes = [i + 1 for i in DRIFTED.index[DRIFTED["order_id"].duplicated()]]
    assert p["removed"] == dupes and p["removed_rows"] == len(dupes)
    assert p["after"]["total"] < p["before"]["total"] and not p["problems"]
    assert p["rows"]["bad_rows"] < len(DRIFTED)
    assert all(r["row"] not in dupes for r in p["rows"]["rows"])
    rules = {(s["rule"], s["column"]) for s in p["rows"]["summary"]}
    assert ("schema", "quantity") not in rules and ("rules", "amount") not in rules

    broken = preview_fixes(DRIFTED, baseline, cfg, [{"operation": "scale_column", "args": {"column": "gone", "factor": 2}}],
                           report.issues)
    assert broken["problems"] == ["Step 1 (scale column) was skipped: There is no column named 'gone'."]


def test_whole_row_duplicates_ignore_the_row_number_and_source():
    df = pd.DataFrame({"a": [1, 1, 2], "_source": ["x", "y", "x"]})
    fixed, effects, _ = apply_fixes(df, {"columns": {}}, [{"operation": "remove_duplicates", "args": {"columns": []}}],
                                    source_column="_source")
    assert fixed[ROW].tolist() == [1, 3] and fixed["_source"].tolist() == ["x", "x"]
    assert effects == ["Removed 1 duplicate rows (by key a), keeping the first of each."]


def test_row_removal_operations():
    df = pd.DataFrame({"amount": [-1, 5, 2000, None], "status": ["paid", "lost", "PAID", None]})
    profile = {"columns": {"status": {"categories": {"paid": 3}}}}
    out, effect = OPERATIONS["drop_rows_outside_range"](df, profile, column="amount", min=0, max=1000)
    assert out["amount"].tolist()[:1] == [5] and len(out) == 2 and effect.endswith("below 0 and above 1000.")
    out, effect = OPERATIONS["drop_rows_not_in"](df, profile, column="status", values=None)
    assert out["status"].tolist()[0] == "paid" and len(out) == 2  # empty values are kept
    out, _ = OPERATIONS["drop_rows_not_in"](df, profile, column="status", values=["paid", "lost"])
    assert len(out) == 3
