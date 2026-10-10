"""DataPulse must work on repositories that have nothing to do with Python."""
import json
import subprocess
from pathlib import Path

import numpy as np
import yaml

from conftest import make_df
from datapulse.cli import app

ROOT = Path(__file__).resolve().parents[1]


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_works_in_node_go_and_java_style_repos_with_json_output(tmp_path, monkeypatch, runner):
    repo = tmp_path / "polyglot"
    (repo / "services/api/testdata").mkdir(parents=True)
    for name, body in {"package.json": '{"name":"x"}', "go.mod": "module x\n", "pom.xml": "<project/>",
                       "Cargo.toml": "[package]\nname='x'\n"}.items():
        (repo / name).write_text(body)  # no pyproject.toml / requirements.txt anywhere
    monkeypatch.chdir(repo / "services/api")
    data = Path("testdata/users.csv")
    make_df().to_csv(data, index=False)
    assert runner.invoke(app, ["baseline", str(data)]).exit_code == 0
    git(repo, "init", "-q"); git(repo, "checkout", "-q", "-b", "main"); git(repo, "add", "."); git(repo, "commit", "-q", "-m", "x")
    bad = make_df()
    bad["customer_id"] = np.nan
    bad.to_csv(data, index=False)
    r = runner.invoke(app, ["check", str(data), "--baseline-ref", "main", "--json", "out/report.json"])
    assert r.exit_code == 1, r.output
    doc = json.loads(Path("out/report.json").read_text())
    assert doc["schema_version"] == 1 and doc["status"] == "failed" and doc["exit_code"] == 1
    assert doc["counts"]["critical"] >= 1 and doc["rows"] == {"baseline": 500, "current": 500}
    issue = next(i for i in doc["issues"] if i["check"] == "null_rate")
    assert issue["columns"] == ["customer_id"] and "suggested_explanation" in issue
    assert set(issue) >= {"measured", "measured_summary", "consumers", "ai_explanation", "examples"}


def test_json_report_for_passing_run(tmp_path, monkeypatch, runner):
    monkeypatch.chdir(tmp_path)
    make_df().to_csv("d.csv", index=False)
    runner.invoke(app, ["baseline", "d.csv"])
    assert runner.invoke(app, ["check", "d.csv", "--json", "r.json"]).exit_code == 0
    doc = json.loads(Path("r.json").read_text())
    assert doc["status"] == "passed" and doc["issues"] == [] and doc["exit_code"] == 0


def test_action_is_isolated_and_language_agnostic():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    text = (ROOT / "action.yml").read_text()
    assert "working-directory" in action["inputs"] and "setup-python" in action["inputs"]
    assert {"exit-code", "report-markdown", "report-json"} <= set(action["outputs"])
    install = next(s for s in action["runs"]["steps"] if "Install" in s.get("name", ""))
    assert "venv" in install["run"]  # never installs into the user's environment
    for forbidden in ("requirements.txt", "pyproject", "npm ", "pip install -r", "cache:"):
        assert forbidden not in text.replace("pip install --quiet", "")
    check = next(s for s in action["runs"]["steps"] if s.get("id") == "check")
    assert check["working-directory"] == "${{ inputs.working-directory }}"


def test_example_workflows_for_other_languages_use_declared_inputs():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    for name in ("node", "go", "java"):
        wf = yaml.safe_load((ROOT / f"examples/workflows/{name}.yml").read_text())
        uses = [s for j in wf["jobs"].values() for s in j["steps"] if str(s.get("uses", "")).startswith("your-org/datapulse")]
        assert uses, name
        assert set(uses[0]["with"]) <= set(action["inputs"]), name


def test_dockerfile_installs_git_and_has_entrypoint():
    text = (ROOT / "Dockerfile").read_text()
    assert "ENTRYPOINT [\"datapulse\"]" in text and "install -y --no-install-recommends git" in text
    assert "safe.directory" in text


def test_action_supports_a_python_free_binary_install():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    assert {"binary-url", "binary-sha256"} <= set(action["inputs"])
    setup = next(s for s in action["runs"]["steps"] if str(s.get("uses", "")).startswith("actions/setup-python"))
    assert "inputs.binary-url == ''" in setup["if"]  # no Python at all when a binary is used
    install = next(s for s in action["runs"]["steps"] if s.get("name") == "Install DataPulse")
    run = install["run"]
    assert "sha256" in run.lower() and "exit 1" in run and run.index("DP_BINARY_URL") < run.index("-m venv")


def test_release_workflow_builds_all_platforms_and_checksums():
    wf = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assets = {m["asset"] for m in wf["jobs"]["build"]["strategy"]["matrix"]["include"]}
    assert {"datapulse-linux-x64", "datapulse-macos-arm64", "datapulse-windows-x64.exe"} <= assets
    text = (ROOT / ".github/workflows/release.yml").read_text()
    assert ".sha256" in text and "scripts/build_binary.sh" in text
