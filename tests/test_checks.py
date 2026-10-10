import numpy as np
import pandas as pd

from conftest import make_df
from datapulse.checks import run_checks


def by_check(report, prefix):
    return [i for i in report.issues if i.check.startswith(prefix)]


def test_passing_data_has_no_issues(baseline, cfg):
    fresh_sample = make_df(seed=1)  # same distribution, different rows
    report = run_checks(baseline, fresh_sample, cfg)
    assert report.issues == []
    assert not report.failed and report.exit_code == 0
    assert set(report.checks_run) == {"schema", "row_count", "null_rate", "categorical", "duplicates", "distribution"}
    assert report.passed_checks == report.checks_run


def test_schema_drift_missing_added_and_type_change(baseline, cfg, df):
    cur = df.drop(columns=["country"])
    cur["coupon"] = "X"
    cur["quantity"] = cur["quantity"].astype(str) + " units"
    report = run_checks(baseline, cur, cfg)
    kinds = {i.check: i for i in report.issues if i.check.startswith("schema")}
    assert kinds["schema.missing_column"].columns == ["country"]
    assert kinds["schema.missing_column"].severity == "critical"
    assert "coupon" in kinds["schema.missing_column"].explanation  # rename hint (same type)
    assert kinds["schema.added_column"].severity == "warning"
    tc = kinds["schema.type_change"]
    assert (tc.measured["baseline_type"], tc.measured["current_type"]) == ("integer", "string")
    assert report.failed and report.exit_code == 1


def test_consumer_impact_is_mapped_by_column(baseline, cfg, df):
    report = run_checks(baseline, df.drop(columns=["country"]), cfg)
    issue = by_check(report, "schema.missing_column")[0]
    assert [c["name"] for c in issue.consumers] == ["revenue_dashboard"]
    assert issue.consumers[0]["columns"] == ["country"]


def test_dataset_wide_issue_affects_all_consumers(baseline, cfg, df):
    report = run_checks(baseline, df.head(100), cfg)  # -80% rows
    issue = by_check(report, "row_count")[0]
    assert {c["name"] for c in issue.consumers} == {"revenue_dashboard", "churn_model"}
    assert all(c["dataset_wide"] for c in issue.consumers)


def test_null_spike_is_critical(baseline, cfg, df):
    cur = df.copy()
    cur.loc[cur.sample(frac=0.4, random_state=0).index, "customer_id"] = np.nan
    report = run_checks(baseline, cur, cfg)
    issue = by_check(report, "null_rate")[0]
    assert issue.columns == ["customer_id"] and issue.severity == "critical"
    assert abs(issue.measured["current_null_rate"] - 0.4) < 0.01
    assert [c["name"] for c in issue.consumers] == ["churn_model"]
    assert report.exit_code == 1


def test_integer_column_with_a_few_nulls_is_not_a_type_change(baseline, cfg, df):
    cur = df.copy()
    cur.loc[:2, "quantity"] = np.nan  # pandas turns the column into float64
    report = run_checks(baseline, cur, cfg)
    assert by_check(report, "schema.type_change") == []


def test_distribution_shift_detects_unit_change(baseline, cfg, df):
    cur = df.copy()
    cur["amount"] = cur["amount"] * 100
    report = run_checks(baseline, cur, cfg)
    issue = by_check(report, "distribution")[0]
    assert issue.columns == ["amount"] and issue.severity == "warning"
    assert issue.measured["psi"] > 0.2 and issue.measured["mean_shift_std"] > 1
    assert "100x" in issue.explanation
    assert report.exit_code == 0  # warnings do not fail CI by default


def test_unexpected_categories_and_case_variants(baseline, cfg, df):
    cur = df.copy()
    cur.loc[:9, "status"] = "Shipped"  # case variant
    cur.loc[10:24, "status"] = "refunded"  # brand-new value
    report = run_checks(baseline, cur, cfg)
    issue = by_check(report, "categorical")[0]
    assert issue.columns == ["status"]
    assert issue.measured["unexpected_rows"] == 25
    assert issue.measured["case_or_whitespace_variants"] == 1
    assert any("refunded" in e for e in issue.examples)


def test_duplicate_keys_detected(baseline, cfg, df):
    cur = pd.concat([df, df.head(5)], ignore_index=True)
    report = run_checks(baseline, cur, cfg)
    issue = by_check(report, "duplicates")[0]
    assert issue.measured["duplicate_rows"] == 5 and issue.columns == ["order_id"]


def test_row_count_threshold_is_configurable(baseline, cfg, df):
    smaller = df.head(300)  # -40%
    cfg.row_count.max_change_pct = 50
    assert not by_check(run_checks(baseline, smaller, cfg), "row_count")
    cfg.row_count.max_change_pct = 5
    assert by_check(run_checks(baseline, smaller, cfg), "row_count")


def test_checks_can_be_turned_off(baseline, cfg, df):
    cfg.null_rate.severity = "off"
    cur = df.copy()
    cur["customer_id"] = np.nan
    report = run_checks(baseline, cur, cfg)
    assert "null_rate" not in report.checks_run and not by_check(report, "null_rate")


def test_fail_on_warning_makes_warnings_fail(baseline, cfg, df):
    cur = df.copy()
    cur["amount"] = cur["amount"] * 100
    cfg.fail_on = "warning"
    assert run_checks(baseline, cur, cfg).exit_code == 1
    cfg.fail_on = "never"
    assert run_checks(baseline, cur, cfg).exit_code == 0


def test_duplicate_examples_handle_empty_keys(baseline, df, cfg):
    bad = df.copy()
    bad["order_id"] = None  # e.g. a source whose key column didn't map: every key is empty
    issue = next(i for i in run_checks(baseline, bad, cfg).issues if i.check == "duplicates")
    assert issue.measured["duplicate_rows"] == len(bad) - 1
