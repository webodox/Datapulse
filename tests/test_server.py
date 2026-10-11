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

ADMIN = {"name": "Ada Admin", "email": "ada@example.com", "password": "correct horse"}

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
        assert c.post("/api/auth/setup", json=ADMIN).status_code == 200  # signed in as the first (admin) user
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
    sources = client.get(f"/api/projects/{seeded['id']}/sources").json()
    assert [(s["name"], s["status"]) for s in sources] == [
        ("Warehouse database", "standard"), ("Store system", "needs_mapping"), ("Online shop", "needs_mapping")]
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
    seeded = client.post("/api/demo/seed").json()  # reads examples/, which must survive
    assert client.delete(f"/api/projects/{pid}").json() == {"deleted": True}
    assert client.get(f"/api/projects/{pid}").status_code == 404
    assert client.delete(f"/api/projects/{seeded['id']}").status_code == 200
    assert list((tmp_path / "home" / "uploads").iterdir()) == []
    assert not (tmp_path / "home" / "samples" / seeded["id"]).exists()  # its sample databases too
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


def test_overview_is_generated_once_and_saved_with_the_run(client, project, monkeypatch):
    from fake_claude import FakeClaude, text
    pid = project["id"]
    df = make_df()
    client.post(f"/api/projects/{pid}/baseline", files={"file": ("o.csv", _csv(df), "text/csv")})
    clean = client.post(f"/api/projects/{pid}/runs", files={"file": ("o.csv", _csv(df), "text/csv")}).json()
    assert client.post(f"/api/projects/{pid}/runs/{clean['run_id']}/overview").status_code == 409
    bad = df.copy()
    bad.loc[:49, "customer_id"] = None
    run_id = client.post(f"/api/projects/{pid}/runs", files={"file": ("o.csv", _csv(bad), "text/csv")}).json()["run_id"]
    url = f"/api/projects/{pid}/runs/{run_id}/overview"

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client.post(url).status_code == 503

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    fake = FakeClaude([text(json.dumps({"summary": "Customer ids went missing.", "focus": []}))])
    monkeypatch.setattr("datapulse.ai.get_client", lambda cfg: fake)
    assert client.post(url).json()["summary"] == "Customer ids went missing."
    assert client.post(url).json()["summary"] == "Customer ids went missing."  # cached: no second AI call
    assert len(fake.calls) == 1
    assert client.get(f"/api/projects/{pid}/runs/{run_id}").json()["overview"]["summary"] == "Customer ids went missing."


def test_draft_rules_endpoint_validates_the_draft(client, project, monkeypatch):
    from fake_claude import FakeClaude, text
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    draft = {"config_yaml": CONFIG + "fail_on: warning\n", "summary": "Fail on warnings", "not_supported": []}
    monkeypatch.setattr("datapulse.ai.get_client", lambda cfg: FakeClaude([text(json.dumps(draft))]))
    r = client.post(f"/api/projects/{project['id']}/rules/draft", json={"request": "fail on warnings", "config_yaml": CONFIG})
    assert r.status_code == 200 and r.json()["config_yaml"].endswith("fail_on: warning\n")
    assert client.get(f"/api/projects/{project['id']}").json()["fail_on"] == "critical"  # nothing saved



# ---------- accounts and shared review ----------

def test_everything_but_health_signin_and_ingest_needs_a_session(client, project):
    client.post("/api/auth/logout")
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/demo/seed").status_code == 401
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/status").json() == {"setup_needed": False, "user": None}
    rows = json.loads(make_df(60).to_json(orient="records"))
    r = client.post(f"/api/projects/{project['id']}/ingest", headers={"X-DataPulse-Key": project["api_key"]},
                    json={"dataset_name": "orders", "role": "baseline", "rows": rows})
    assert r.status_code == 200  # machine-to-machine ingest keeps using the project key


def test_setup_runs_once_and_login_is_checked(client, tmp_path):
    assert client.post("/api/auth/setup", json={**ADMIN, "email": "eve@example.com"}).status_code == 409
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"email": ADMIN["email"], "password": "wrong"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "x"}).status_code == 401
    r = client.post("/api/auth/login", json={"email": "ADA@example.com ", "password": ADMIN["password"]})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    assert client.get("/api/auth/status").json()["user"]["name"] == "Ada Admin"
    import sqlite3
    db = sqlite3.connect(tmp_path / "home" / "datapulse.sqlite3")
    stored = db.execute("SELECT password_hash FROM users").fetchone()[0]
    assert ADMIN["password"] not in stored and stored.startswith("scrypt$")


def test_admin_manages_the_team_and_members_cannot(client):
    member = {"name": "Mo Member", "email": "mo@example.com", "password": "member-pass"}
    created = client.post("/api/users", json=member).json()
    assert created["role"] == "member"
    assert client.post("/api/users", json=member).status_code == 409  # duplicate email
    assert client.post("/api/users", json={**member, "email": "x@y.z", "password": "short"}).status_code == 422
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"email": member["email"], "password": member["password"]}).status_code == 200
    assert client.get("/api/users").status_code == 403
    assert client.post("/api/users", json={**member, "email": "z@y.z"}).status_code == 403
    assert client.get("/api/projects").status_code == 200  # but can use the dashboard
    assert client.put("/api/auth/password", json={"current_password": "nope", "new_password": "new-password"}).status_code == 403
    assert client.put("/api/auth/password", json={"current_password": "member-pass", "new_password": "new-password"}).status_code == 200
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": ADMIN["email"], "password": ADMIN["password"]})
    me = client.get("/api/auth/status").json()["user"]
    assert client.delete(f"/api/users/{me['id']}").status_code == 409
    assert client.delete(f"/api/users/{created['id']}").status_code == 200
    assert client.post("/api/auth/login", json={"email": member["email"], "password": "new-password"}).status_code == 401


def test_checks_record_who_ran_them_and_review_status_is_shared(client):
    seeded = client.post("/api/demo/seed").json()
    drifted = (ROOT / "examples" / "data" / "orders_drifted.csv").read_bytes()
    doc = client.post(f"/api/projects/{seeded['id']}/runs", files={"file": ("d.csv", drifted, "text/csv")}).json()
    run = client.get(f"/api/projects/{seeded['id']}").json()["runs"][0]
    assert run["created_by"] == "Ada Admin" and run["review"]["status"] == "open"
    url = f"/api/projects/{seeded['id']}/runs/{doc['run_id']}/review"
    assert client.put(url, json={"status": "resolved"}).json()["by"] == "Ada Admin"
    assert client.get(f"/api/projects/{seeded['id']}").json()["runs"][0]["review"]["status"] == "resolved"
    assert client.put(url, json={"status": "acknowledged"}).status_code == 422
    assert client.put(url, json={"status": "done"}).status_code == 422
    assert client.put(f"/api/projects/{seeded['id']}/runs/nope/review", json={"status": "resolved"}).status_code == 404


def test_older_databases_are_upgraded_in_place(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("DATAPULSE_HOME", str(tmp_path / "old"))
    (tmp_path / "old").mkdir()
    db = sqlite3.connect(tmp_path / "old" / "datapulse.sqlite3")
    db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, project_id TEXT, dataset_id TEXT, status TEXT, "
               "issue_count INTEGER, report_json TEXT, created_at TEXT)")
    db.execute("INSERT INTO runs VALUES ('r1', 'p', 'd', 'failed', 1, '{}', '2026-01-01')")
    db.commit(); db.close()
    with TestClient(server.app):
        pass
    db = sqlite3.connect(tmp_path / "old" / "datapulse.sqlite3")
    assert db.execute("SELECT review_status, created_by FROM runs").fetchone() == ("open", None)


def test_repair_agent_endpoint_download_and_recheck(client, monkeypatch):
    from fake_claude import FakeClaude, text, tool_calls
    seeded = client.post("/api/demo/seed").json()
    drifted = (ROOT / "examples" / "data" / "orders_drifted.csv").read_bytes()
    doc = client.post(f"/api/projects/{seeded['id']}/runs", files={"file": ("orders_drifted.csv", drifted, "text/csv")}).json()
    assert doc["charts"]["amount"]["hist"]["current"][-1] == 1.0  # chart data is saved with every check
    base = f"/api/projects/{seeded['id']}/runs/{doc['run_id']}"
    assert client.get(f"{base}/fixed").status_code == 404  # nothing repaired yet

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client.post(f"{base}/fix").status_code == 503

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    summary = {"summary": "s", "not_fixed": [], "warnings": []}
    monkeypatch.setattr("datapulse.fix.get_client", lambda cfg: FakeClaude([
        tool_calls(("scale_column", {"column": "amount", "factor": 0.01, "reason": "cents"}),
                   ("convert_to_number", {"column": "quantity", "reason": "units"}),
                   ("remove_duplicates", {"columns": ["order_id"], "reason": "dupes"})),
        text(json.dumps(summary))]))
    fix = client.post(f"{base}/fix").json()
    assert fix["by"] == "Ada Admin" and fix["file_name"] == "orders_drifted-fixed.csv"
    assert fix["after"]["total"] < fix["before"]["total"] == 9
    assert client.get(f"/api/projects/{seeded['id']}").json()["runs"][0]["report"]["fix"]["dataset_id"] == fix["dataset_id"]

    r = client.get(f"{base}/fixed")
    assert r.status_code == 200 and "orders_drifted-fixed.csv" in r.headers["content-disposition"]
    assert len(r.text.strip().splitlines()) == 371  # header + 370 rows (10 duplicates removed)

    recheck = client.post(f"{base}/fixed/check").json()
    assert recheck["data_file"] == "orders_drifted-fixed.csv" and len(recheck["issues"]) == fix["after"]["total"]
    runs = client.get(f"/api/projects/{seeded['id']}").json()["runs"]
    assert [r["report"]["data_file"] for r in runs[:2]] == ["orders_drifted-fixed.csv", "orders_drifted.csv"]
