from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_action_yml_is_a_valid_composite_action():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    assert action["runs"]["using"] == "composite"
    assert action["inputs"]["data-file"]["required"] is True
    steps = {s.get("id") or s.get("name"): s for s in action["runs"]["steps"]}
    assert "check" in steps
    text = (ROOT / "action.yml").read_text()
    assert "--baseline-ref" in text and "datapulse comment" in text and "github.token" in text
    assert "pull-requests" not in text.split("runs:")[1]  # permissions are the caller's job


def test_example_workflow_uses_declared_inputs_only():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    wf = yaml.safe_load((ROOT / "examples/workflows/datapulse.yml").read_text())
    used = wf["jobs"]["data-quality"]["steps"][1]["with"]
    assert set(used) <= set(action["inputs"])
    assert wf["permissions"]["pull-requests"] == "write"
    assert "pull_request" in wf[True]  # YAML parses the bare key `on` as boolean True
