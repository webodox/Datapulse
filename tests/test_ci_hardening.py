"""Config-from-target-branch, PR impact section, and GitHub Action outputs."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import yaml

from conftest import make_df
from datapulse.cli import app
from datapulse.report import action_outputs

ROOT = Path(__file__).resolve().parents[1]
STRICT = """\
version: 1
checks:
  null_rate: {max_increase: 0.02}
consumers:
  churn_model: {type: model, columns: [customer_id, amount]}
  revenue_dashboard: {type: dashboard, columns: [amount, country]}
"""
LOOSE = STRICT.replace("max_increase: 0.02", "max_increase: 1.0").replace("severity", "severity")


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def pr_repo(tmp_path, monkeypatch, runner):
    """main = strict config + baseline. 'pr' branch = loosened config + null-spiked data (a sneaky PR)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    (tmp_path / "datapulse.yml").write_text(STRICT)
    make_df().to_csv("data.csv", index=False)
    assert runner.invoke(app, ["baseline", "data.csv"]).exit_code == 0
    git(tmp_path, "init", "-q"); git(tmp_path, "checkout", "-q", "-b", "main")
    git(tmp_path, "add", "."); git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "pr")
    bad = make_df()
    bad.loc[bad.sample(frac=0.3, random_state=0).index, "customer_id"] = np.nan
    bad.to_csv("data.csv", index=False)
    (tmp_path / "datapulse.yml").write_text(LOOSE)
    return tmp_path


def test_pr_cannot_loosen_its_own_rules_when_config_comes_from_the_target_branch(pr_repo, runner):
    # The PR's own (loosened) config lets the null spike through...
    assert runner.invoke(app, ["check", "data.csv", "--baseline-ref", "main"]).exit_code == 0
    # ...but the trusted config on main does not.
    r = runner.invoke(app, ["check", "data.csv", "--baseline-ref", "main", "--config-ref", "main"])
    assert r.exit_code == 1, r.output
    assert "datapulse.yml @ main" in r.output
    assert "datapulse.yml @ main" in Path("datapulse-report.md").read_text()


def test_deleting_the_config_in_a_pr_does_not_reset_the_rules(pr_repo, runner):
    (pr_repo / "datapulse.yml").unlink()
    r = runner.invoke(app, ["check", "data.csv", "--baseline-ref", "main", "--config", "datapulse.yml",
                            "--config-ref", "main"])
    assert r.exit_code == 1, r.output


def test_missing_trusted_ref_is_an_error_not_a_silent_fallback(pr_repo, runner):
    r = runner.invoke(app, ["check", "data.csv", "--config-ref", "origin/does-not-exist"])
    assert r.exit_code == 2 and "git fetch" in r.output and "Refusing" in r.output


def test_config_absent_on_both_sides_uses_defaults_with_a_visible_note(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    make_df().to_csv("data.csv", index=False)
    runner.invoke(app, ["baseline", "data.csv"])
    git(tmp_path, "init", "-q"); git(tmp_path, "checkout", "-q", "-b", "main"); git(tmp_path, "add", "."); git(tmp_path, "commit", "-q", "-m", "x")
    r = runner.invoke(app, ["check", "data.csv", "--baseline-ref", "main", "--config", "datapulse.yml",
                            "--config-ref", "main"])
    assert r.exit_code == 0, r.output
    assert "default rules" in r.output


def test_markdown_has_pull_request_impact_section(pr_repo, runner):
    bad = make_df().drop(columns=["country"])
    bad.to_csv("data.csv", index=False)
    runner.invoke(app, ["check", "data.csv", "--config-ref", "main"])
    md = Path("datapulse-report.md").read_text()
    assert "### Pull request impact" in md
    assert "**CRITICAL** `country`: Column 'country' is missing" in md
    assert "**Affected consumers:** `revenue_dashboard`" in md


def test_passing_report_says_no_schema_changes(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    make_df().to_csv("d.csv", index=False)
    runner.invoke(app, ["baseline", "d.csv"])
    runner.invoke(app, ["check", "d.csv"])
    md = Path("datapulse-report.md").read_text()
    assert "No schema changes detected." in md and "**Affected consumers:** none mapped" in md


def test_github_outputs_command_and_json_fields(pr_repo, runner):
    make_df().drop(columns=["country"]).to_csv("data.csv", index=False)
    runner.invoke(app, ["check", "data.csv", "--config-ref", "main", "--json", "r.json"])
    doc = json.loads(Path("r.json").read_text())
    assert doc["schema_changed"] is True and "revenue_dashboard" in doc["affected_consumers"]
    out = runner.invoke(app, ["github-outputs", "r.json"]).output.strip().splitlines()
    kv = dict(line.split("=", 1) for line in out)
    assert kv["status"] == "failed" and kv["schema-changed"] == "true" and int(kv["issue-count"]) >= 1
    assert "revenue_dashboard" in kv["affected-consumers"]
    assert runner.invoke(app, ["github-outputs", "nope.json"]).exit_code == 2


def test_action_outputs_cannot_inject_extra_output_lines():
    doc = {"status": "failed", "issues": [{"check": "null_rate", "consumers": [
        {"name": "evil\nstatus=passed\x00"}]}]}
    out = action_outputs(doc)
    assert "\n" not in "".join(out.values()) and "\x00" not in "".join(out.values())
    assert out["affected-consumers"] == "evil status=passed"
    assert out["schema-changed"] == "false" and out["issue-count"] == "1"


def test_action_declares_the_new_outputs_and_input():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    assert {"status", "issue-count", "schema-changed", "affected-consumers"} <= set(action["outputs"])
    assert action["inputs"]["config-from-base"]["default"] == "true"


@pytest.mark.skipif(shutil.which("datapulse") is None or shutil.which("bash") is None,
                    reason="needs datapulse and bash on PATH")
def test_action_step_end_to_end_blocks_a_pr_that_loosens_its_rules(pr_repo, tmp_path):
    """Run the real shell from action.yml against a local origin, as GitHub would."""
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    script = next(s for s in action["runs"]["steps"] if s.get("id") == "check")["run"]
    (tmp_path.parent / "sim").mkdir(exist_ok=True)
    origin = tmp_path.parent / "sim" / "origin.git"
    shutil.rmtree(origin, ignore_errors=True)
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    git(pr_repo, "remote", "add", "origin", str(origin))
    git(pr_repo, "push", "-q", "origin", "main")
    git(pr_repo, "add", "-A"); git(pr_repo, "commit", "-q", "-m", "pr change")
    runner_tmp = tmp_path.parent / "sim" / "rt"
    runner_tmp.mkdir(exist_ok=True)
    out_file = runner_tmp / "out"
    out_file.write_text("")
    env = {**os.environ, "RUNNER_TEMP": str(runner_tmp), "GITHUB_OUTPUT": str(out_file),
           "DP_DATA_FILE": "data.csv", "DP_CONFIG": "datapulse.yml", "DP_BASELINE": "", "DP_BASELINE_REF": "",
           "DP_AI": "false", "DP_DISCOVER": "false", "DP_BASE_REF": "main", "DP_CONFIG_FROM_BASE": "true"}
    res = subprocess.run(["bash", "-c", script], cwd=pr_repo, env=env, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr  # the step never aborts; the gate is a later step
    kv = dict(l.split("=", 1) for l in out_file.read_text().splitlines() if "=" in l)
    assert kv["exit-code"] == "1" and kv["status"] == "failed", res.stdout + res.stderr
    assert "churn_model" in kv["affected-consumers"] and kv["schema-changed"] == "false"
    assert Path(kv["report-json"]).exists() and Path(kv["report-markdown"]).exists()
    # opt-out: with the PR's own loosened config the same data would pass
    out_file.write_text("")
    env["DP_CONFIG_FROM_BASE"] = "false"
    subprocess.run(["bash", "-c", script], cwd=pr_repo, env=env, capture_output=True, text=True)
    assert "exit-code=0" in out_file.read_text()
