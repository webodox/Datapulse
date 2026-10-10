import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import make_df
from datapulse.cli import app

CONFIG = """\
version: 1
checks:
  duplicates:
    columns: [order_id]
consumers:
  churn_model:
    type: model
    columns: [customer_id, amount]
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    (tmp_path / "datapulse.yml").write_text(CONFIG)
    make_df().to_csv(tmp_path / "data.csv", index=False)
    return tmp_path


def test_baseline_then_check_passes(project, runner):
    r = runner.invoke(app, ["baseline", "data.csv"])
    assert r.exit_code == 0, r.output
    assert "Baseline v1" in r.output
    r = runner.invoke(app, ["check", "data.csv"])
    assert r.exit_code == 0, r.output
    assert "PASSED" in r.output
    md = (project / "datapulse-report.md").read_text()
    assert "all checks passed" in md and "<!-- datapulse-report -->" in md


def test_rebaselining_bumps_version(project, runner):
    runner.invoke(app, ["baseline", "data.csv"])
    r = runner.invoke(app, ["baseline", "data.csv"])
    assert "Baseline v2" in r.output
    assert json.loads((project / ".datapulse/baseline.json").read_text())["baseline_version"] == 2


def test_ci_fails_with_exit_code_1_on_critical_and_writes_report(project, runner):
    runner.invoke(app, ["baseline", "data.csv"])
    bad = make_df()
    bad.loc[bad.sample(frac=0.5, random_state=0).index, "customer_id"] = np.nan
    bad.to_csv(project / "data.csv", index=False)
    r = runner.invoke(app, ["check", "data.csv", "--markdown", "out/report.md"])
    assert r.exit_code == 1, r.output
    assert "FAILED" in r.output
    md = (project / "out/report.md").read_text()
    assert "Null rate rose in 'customer_id'" in md
    assert "churn_model" in md  # downstream consumer shown
    assert "suggested, not verified" in md  # facts and explanations are labelled separately


def test_warnings_do_not_fail_by_default_but_fail_when_configured(project, runner):
    runner.invoke(app, ["baseline", "data.csv"])
    df = make_df()
    df["amount"] = df["amount"] * 100
    df.to_csv(project / "data.csv", index=False)
    assert runner.invoke(app, ["check", "data.csv"]).exit_code == 0
    (project / "datapulse.yml").write_text(CONFIG + "fail_on: warning\n")
    assert runner.invoke(app, ["check", "data.csv"]).exit_code == 1


def test_parquet_files_are_supported(project, runner):
    make_df().to_parquet(project / "data.parquet")
    assert runner.invoke(app, ["baseline", "data.parquet"]).exit_code == 0
    assert runner.invoke(app, ["check", "data.parquet"]).exit_code == 0


@pytest.mark.parametrize("args,needle", [
    (["check", "missing.csv"], "not found"),
    (["baseline", "notes.txt"], "Unsupported file type"),
    (["baseline", "empty.csv"], "empty"),
    (["baseline", "data.csv", "--config", "nope.yml"], "Config file not found"),
])
def test_invalid_inputs_exit_2_with_useful_error(project, runner, args, needle):
    (project / "notes.txt").write_text("hi")
    (project / "empty.csv").write_text("")
    if args[0] == "check":
        runner.invoke(app, ["baseline", "data.csv"])
    r = runner.invoke(app, args)
    assert r.exit_code == 2
    assert needle in r.output


def test_check_without_baseline_explains_how_to_create_one(project, runner):
    r = runner.invoke(app, ["check", "data.csv"])
    assert r.exit_code == 2 and "datapulse baseline" in r.output


def test_corrupt_baseline_and_bad_config_are_reported(project, runner):
    (project / ".datapulse").mkdir()
    (project / ".datapulse/baseline.json").write_text("{not json")
    r = runner.invoke(app, ["check", "data.csv"])
    assert r.exit_code == 2 and "not valid JSON" in r.output
    (project / "datapulse.yml").write_text("checks:\n  null_rate:\n    max_increse: 0.1\n")
    r = runner.invoke(app, ["baseline", "data.csv"])
    assert r.exit_code == 2 and "max_increse" in r.output


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_baseline_is_read_from_target_branch_not_the_pr(project, runner):
    """A PR that 'fixes' CI by re-baselining its own bad data must still fail."""
    runner.invoke(app, ["baseline", "data.csv"])
    git(project, "init", "-q")
    git(project, "checkout", "-q", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "base")
    git(project, "checkout", "-q", "-b", "pr")
    bad = make_df().drop(columns=["amount"])
    bad.to_csv(project / "data.csv", index=False)
    runner.invoke(app, ["baseline", "data.csv"])  # PR overwrites its working-tree baseline
    assert runner.invoke(app, ["check", "data.csv"]).exit_code == 0  # compares against itself
    r = runner.invoke(app, ["check", "data.csv", "--baseline-ref", "main"])
    assert r.exit_code == 1, r.output
    assert "amount" in r.output and "@ main" in r.output


def test_baseline_ref_errors_are_actionable(project, runner):
    git(project, "init", "-q")
    git(project, "checkout", "-q", "-b", "main")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "no baseline")
    r = runner.invoke(app, ["check", "data.csv", "--baseline-ref", "main"])
    assert r.exit_code == 2 and "Could not read" in r.output


def test_suggest_never_modifies_config_and_output_is_loadable(project, runner):
    before = (project / "datapulse.yml").read_text()
    r = runner.invoke(app, ["suggest", "data.csv"])
    assert r.exit_code == 0 and "Nothing was applied" in r.output
    assert (project / "datapulse.yml").read_text() == before
    r = runner.invoke(app, ["suggest", "data.csv", "--output", "suggested.yml"])
    assert r.exit_code == 0
    from datapulse.config import load_config
    cfg = load_config(project / "suggested.yml")
    assert cfg.duplicates.columns == ["order_id"]
    assert "status" in cfg.categorical.columns
    assert (project / "datapulse.yml").read_text() == before
    # refuses to clobber existing files or the user's config
    assert runner.invoke(app, ["suggest", "data.csv", "--output", "suggested.yml"]).exit_code == 2
    r = runner.invoke(app, ["suggest", "data.csv", "--output", "datapulse.yml", "--force"])
    assert r.exit_code == 2 and "Refusing" in r.output
    assert (project / "datapulse.yml").read_text() == before


def test_init_writes_a_valid_starter_config_once(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    assert runner.invoke(app, ["init"]).exit_code == 0
    from datapulse.config import load_config
    assert load_config().fail_on == "critical"
    assert runner.invoke(app, ["init"]).exit_code == 2


def test_sample_files_demonstrate_pass_and_fail(tmp_path, monkeypatch, runner):
    root = Path(__file__).resolve().parents[1] / "examples"
    monkeypatch.chdir(tmp_path)
    cfgp = str(root / "datapulse.yml")
    assert runner.invoke(app, ["baseline", str(root / "data/orders.csv"), "-c", cfgp]).exit_code == 0
    assert runner.invoke(app, ["check", str(root / "data/orders.csv"), "-c", cfgp]).exit_code == 0
    r = runner.invoke(app, ["check", str(root / "data/orders_drifted.csv"), "-c", cfgp])
    assert r.exit_code == 1
    md = Path("datapulse-report.md").read_text()
    for needle in ("revenue_dashboard", "churn_model", "fulfillment_service", "country", "coupon_code"):
        assert needle in md
