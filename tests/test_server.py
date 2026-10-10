"""The web API (server/app.py) uses the same engine and report format as the CLI."""
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from conftest import make_df  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import app as server  # noqa: E402

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
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAPULSE_HOME", str(tmp_path / "home"))
    with TestClient(server.app) as c:
        yield c


@pytest.fixture
def project(client):
    created = client.post("/api/projects", json={"name": "Orders"}).json()
    assert client.put(f"/api/projects/{created['id']}/config", json={"config_yaml": CONFIG}).status_code == 200
    return created


def _csv(df) -> bytes:
    return df.to_csv(index=False).encode()


def test_upload_baseline_then_check_uses_cli_report_format(client, project):
    pid = project["id"]
    df = make_df()
    r = client.post(f"/api/projects/{pid}/baseline", files={"file": ("orders.csv", _csv(df), "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["baseline"]["baseline_version"] == 1
    assert r.json()["baseline"]["rows"] == len(df)

    bad = df.copy()
    bad.loc[:49, "customer_id"] = None
    r = client.post(f"/api/projects/{pid}/runs", files={"file": ("orders.csv", _csv(bad), "text/csv")})
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["status"] == "failed" and doc["schema_version"] == 1
    issue = next(i for i in doc["issues"] if i["check"] == "null_rate")
    assert issue["severity"] == "critical" and issue["columns"] == ["customer_id"]
    assert [c["name"] for c in issue["consumers"]] == ["churn_model"]
    assert doc["markdown"].startswith("<!-- datapulse-report -->")

    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["runs"][0]["id"] == doc["run_id"]
    assert detail["consumers"][0]["name"] == "churn_model"
    assert "categories" not in json.dumps(detail["baseline"])  # summary only, no raw category values
    assert client.get(f"/api/projects/{pid}/runs/{doc['run_id']}").json()["status"] == "failed"


def test_rebaselining_bumps_version(client, project):
    pid = project["id"]
    for expected in (1, 2):
        r = client.post(f"/api/projects/{pid}/baseline", files={"file": ("o.csv", _csv(make_df()), "text/csv")})
        assert r.json()["baseline"]["baseline_version"] == expected


def test_ingest_json_rows_requires_project_key(client, project):
    pid = project["id"]
    rows = json.loads(make_df(50).to_json(orient="records"))
    body = {"dataset_name": "orders", "role": "baseline", "rows": rows}
    assert client.post(f"/api/projects/{pid}/ingest", json=body).status_code == 401
    assert client.post(f"/api/projects/{pid}/ingest", json=body, headers={"X-DataPulse-Key": "dp_wrong"}).status_code == 401
    headers = {"X-DataPulse-Key": project["api_key"]}
    assert client.post(f"/api/projects/{pid}/ingest", json=body, headers=headers).status_code == 200
    r = client.post(f"/api/projects/{pid}/ingest", headers=headers,
                    json={"dataset_name": "orders", "rows": rows + [rows[0]]})
    assert r.status_code == 200, r.text
    assert any(i["check"] == "duplicates" for i in r.json()["issues"])


def test_invalid_config_is_rejected_with_cli_validation(client, project):
    r = client.put(f"/api/projects/{project['id']}/config", json={"config_yaml": "fail_on: sometimes\n"})
    assert r.status_code == 422
    assert "fail_on" in r.json()["detail"]


def test_bad_uploads_are_rejected_and_not_kept(client, project, tmp_path):
    pid = project["id"]
    r = client.post(f"/api/projects/{pid}/baseline", files={"file": ("notes.txt", b"hi", "text/plain")})
    assert r.status_code == 415
    r = client.post(f"/api/projects/{pid}/runs", files={"file": ("o.csv", _csv(make_df()), "text/csv")})
    assert r.status_code == 409  # no baseline yet
    r = client.post(f"/api/projects/{pid}/baseline", files={"file": ("o.json", b"{not json", "application/json")})
    assert r.status_code == 400
    assert "uploads" not in r.json()["detail"]  # storage paths are not leaked
    assert list((tmp_path / "home" / "uploads").iterdir()) == []


def test_suggestions_and_demo_seed(client):
    seeded = client.post("/api/demo/seed").json()
    assert seeded["has_baseline"]
    detail = client.get(f"/api/projects/{seeded['id']}").json()
    assert {c["name"] for c in detail["consumers"]} >= {"revenue_dashboard", "churn_model"}
    yaml_text = client.get(f"/api/projects/{seeded['id']}/suggestions").json()["yaml"]
    assert "order_id" in yaml_text
    drifted = (ROOT / "examples" / "data" / "orders_drifted.csv").read_bytes()
    r = client.post(f"/api/projects/{seeded['id']}/runs", files={"file": ("orders_drifted.csv", drifted, "text/csv")})
    assert r.json()["status"] == "failed"


def test_project_listing_never_returns_keys(client, project):
    listed = client.get("/api/projects").json()
    assert listed[0]["id"] == project["id"]
    assert "api_key" not in json.dumps(listed) and "api_key_hash" not in json.dumps(listed)


def test_delete_project_removes_its_data_but_not_shared_files(client, project, tmp_path):
    pid = project["id"]
    client.post(f"/api/projects/{pid}/baseline", files={"file": ("o.csv", _csv(make_df()), "text/csv")})
    client.post(f"/api/projects/{pid}/runs", files={"file": ("o.csv", _csv(make_df()), "text/csv")})
    seeded = client.post("/api/demo/seed").json()  # its baseline points at examples/, which must survive
    assert client.delete(f"/api/projects/{pid}").json() == {"deleted": True}
    assert client.get(f"/api/projects/{pid}").status_code == 404
    assert list((tmp_path / "home" / "uploads").iterdir()) == []
    assert client.delete(f"/api/projects/{seeded['id']}").status_code == 200
    assert (ROOT / "examples" / "data" / "orders.csv").exists()
    assert client.delete(f"/api/projects/{pid}").status_code == 404
    assert client.get("/api/projects").json() == []


def test_runs_in_the_same_second_are_listed_newest_first(client, project):
    pid = project["id"]
    df = make_df()
    client.post(f"/api/projects/{pid}/baseline", files={"file": ("o.csv", _csv(df), "text/csv")})
    ids = [client.post(f"/api/projects/{pid}/runs", files={"file": ("o.csv", _csv(df), "text/csv")}).json()["run_id"]
           for _ in range(3)]
    assert [r["id"] for r in client.get(f"/api/projects/{pid}").json()["runs"]] == ids[::-1]


def test_ml_drift_runs_automatically_and_triage_is_saved_with_the_run(client, monkeypatch):
    from fake_claude import FakeClaude, text, tool_calls
    seeded = client.post("/api/demo/seed").json()
    drifted = (ROOT / "examples" / "data" / "orders_drifted.csv").read_bytes()
    doc = client.post(f"/api/projects/{seeded['id']}/runs", files={"file": ("d.csv", drifted, "text/csv")}).json()
    ml = next(i for i in doc["issues"] if i["check"] == "ml_drift")
    assert ml["measured"]["drift_auc"] > 0.9 and ml["columns"][0] == "amount"
    url = f"/api/projects/{seeded['id']}/runs/{doc['run_id']}/triage"

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client.get("/api/health").json()["ai"] is False
    r = client.post(url)
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    triage = {"headline": "h", "verdict": "investigate", "verdict_reason": "r", "root_causes": [],
              "actions": [], "notify": [], "message": "m"}
    fake = FakeClaude([tool_calls(("get_run_history", {})), text(json.dumps(triage))])
    monkeypatch.setattr("datapulse.agent.get_client", lambda cfg: fake)
    assert client.post(url).json()["verdict"] == "investigate"
    assert client.get(f"/api/projects/{seeded['id']}/runs/{doc['run_id']}").json()["triage"]["headline"] == "h"
    assert "[]" in fake.calls[1]["messages"][-1]["content"][0]["content"]  # history: no earlier runs


def test_draft_rules_endpoint_validates_the_draft(client, project, monkeypatch):
    from fake_claude import FakeClaude, text
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    draft = {"config_yaml": CONFIG + "fail_on: warning\n", "summary": "Fail on warnings", "not_supported": []}
    monkeypatch.setattr("datapulse.ai.get_client", lambda cfg: FakeClaude([text(json.dumps(draft))]))
    r = client.post(f"/api/projects/{project['id']}/rules/draft", json={"request": "fail on warnings", "config_yaml": CONFIG})
    assert r.status_code == 200 and r.json()["config_yaml"].endswith("fail_on: warning\n")
    assert client.get(f"/api/projects/{project['id']}").json()["fail_on"] == "critical"  # nothing saved
