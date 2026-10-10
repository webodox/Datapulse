"""The data-sources API (connect, map, pull, unify) and the merged "Investigate with AI" endpoint."""
import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from fake_claude import FakeClaude, text, tool_calls  # noqa: E402
from server import app as server  # noqa: E402
from datapulse.io import load_dataframe  # noqa: E402
from datapulse.unify import SOURCE_COLUMN  # noqa: E402

EX = Path(__file__).resolve().parents[1] / "examples"
GOOD = load_dataframe(EX / "data/orders.csv")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAPULSE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(server.app) as c:
        assert c.post("/api/auth/setup", json={"name": "Ada", "email": "a@x.io", "password": "password123"}).status_code == 200
        yield c


def test_sources_flow_connect_map_pull_unify(client, sqlite_source, tmp_path):
    pid = client.post("/api/projects", json={"name": "Sales"}).json()["id"]
    base = f"/api/projects/{pid}"
    standard_csv = tmp_path / "standard.db"
    with sqlite3.connect(standard_csv) as db:
        GOOD.to_sql("orders", db, index=False)
    standard = client.post(f"{base}/sources", json={"name": "Main DB", "kind": "sqlite",
                                                    "settings": {"path": str(standard_csv), "query": "orders"}}).json()
    shop = client.post(f"{base}/sources", json={"name": "Shop", "kind": "sqlite", "settings": sqlite_source}).json()
    assert client.post(f"{base}/sources/test", json={"kind": "sqlite", "settings": sqlite_source}).json()["ok"]
    bad = client.post(f"{base}/sources/test", json={"kind": "sqlite", "settings": {**sqlite_source, "query": "nope"}}).json()
    assert not bad["ok"] and "no such table" in bad["message"]

    assert client.post(f"{base}/sources/{shop['id']}/map").status_code == 409  # no standard yet
    assert client.post(f"{base}/sources/{standard['id']}/pull", json={"role": "baseline"}).status_code == 200
    proposal = client.post(f"{base}/sources/{shop['id']}/map").json()  # AI is off: name matches only
    assert proposal["automatic_steps"] == 6 and "AI is off" in proposal["summary"]
    listed = {s["name"]: s for s in client.get(f"{base}/sources").json()}
    assert listed["Main DB"]["status"] == "standard" and listed["Shop"]["status"] == "needs_mapping"
    assert listed["Shop"]["proposal"]["automatic_steps"] == 6

    recipe = proposal["operations"] + [
        {"operation": "rename_column", "args": {"column": "amount_cents", "new_name": "amount"}, "reason": "r"},
        {"operation": "scale_column", "args": {"column": "amount", "factor": 0.01}, "reason": "r"},
        {"operation": "standardize_categories", "args": {"column": "status"}, "reason": "r"},
        {"operation": "drop_column", "args": {"column": "warehouse"}, "reason": "r"}]
    saved = client.put(f"{base}/sources/{shop['id']}/recipe", json={"operations": recipe}).json()
    assert saved["status"] == "mapped" and saved["proposal"] is None

    pulled = client.post(f"{base}/sources/{shop['id']}/pull", json={}).json()
    assert pulled["status"] == "passed" and pulled["source"]["steps"][0] == "Renamed 'OrderID' to 'order_id'."

    unified = client.post(f"{base}/unify").json()
    assert unified["data_file"] == "Unified data (2 sources)"
    assert [s["rows"] for s in unified["unified"]["sources"]] == [len(GOOD), len(GOOD)]
    assert not any(SOURCE_COLUMN in i["columns"] for i in unified["issues"])  # the tag isn't checked
    assert unified["status"] == "passed" and not unified["issues"]  # 2x the rows is expected for a combined table
    data = client.get(f"{base}/runs/{unified['run_id']}/data")
    assert data.text.splitlines()[0].startswith(f"{SOURCE_COLUMN},order_id") and len(data.text.splitlines()) == 1 + 2 * len(GOOD)

    assert client.delete(base).status_code == 200
    assert client.get(f"{base}/sources").status_code == 404


def test_secrets_stay_on_the_server(client):
    pid = client.post("/api/projects", json={"name": "Prod data"}).json()["id"]
    form = {"name": "Prod", "kind": "postgres", "settings": {"url": "postgresql://u:Hunter2-pw@db/x", "query": "t"}}
    created = client.post(f"/api/projects/{pid}/sources", json=form).json()
    edited = client.put(f"/api/projects/{pid}/sources/{created['id']}",
                        json={**form, "settings": {"url": "", "query": "other"}}).json()
    assert edited["settings"] == {"url": "", "query": "other", "saved_secrets": ["url"]}
    assert "Hunter2" not in json.dumps(client.get(f"/api/projects/{pid}/sources").json())
    tried = client.post(f"/api/projects/{pid}/sources/test",
                        json={"kind": "postgres", "settings": {"url": "", "query": "t"}, "source_id": created["id"]}).json()
    assert not tried["ok"] and "Hunter2" not in tried["message"]  # used the saved URL, didn't echo it


def test_investigate_runs_triage_and_repair_together(client, monkeypatch):
    seeded = client.post("/api/demo/seed").json()
    drifted = (EX / "data/orders_drifted.csv").read_bytes()
    doc = client.post(f"/api/projects/{seeded['id']}/runs", files={"file": ("d.csv", drifted, "text/csv")}).json()
    url = f"/api/projects/{seeded['id']}/runs/{doc['run_id']}/investigate"
    assert client.post(url).status_code == 503  # AI off

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    triage = {"headline": "Amounts are in cents", "verdict": "block", "verdict_reason": "r", "root_causes": [],
              "actions": [], "notify": [], "message": "m"}
    monkeypatch.setattr("datapulse.agent.get_client", lambda cfg: FakeClaude([text(json.dumps(triage))]))
    monkeypatch.setattr("datapulse.fix.get_client", lambda cfg: FakeClaude([text("not json")]))
    partial = client.post(url).json()  # the repair agent fails; the diagnosis is still saved
    assert partial["triage"]["headline"] == "Amounts are in cents" and partial["fix"] is None
    assert "not valid JSON" in partial["errors"]["fix"]

    monkeypatch.setattr("datapulse.agent.get_client", lambda cfg: pytest.fail("triage already done"))
    monkeypatch.setattr("datapulse.fix.get_client", lambda cfg: FakeClaude([
        tool_calls(("scale_column", {"column": "amount", "factor": 0.01, "reason": "cents"})),
        text(json.dumps({"summary": "s", "not_fixed": [], "warnings": []}))]))
    done = client.post(url).json()
    assert done["errors"] == {} and done["fix"]["by"] == "Ada" and done["fix"]["after"]["total"] < 9
    saved = client.get(f"/api/projects/{seeded['id']}/runs/{doc['run_id']}").json()
    assert saved["triage"]["headline"] == "Amounts are in cents" and saved["fix"]["file_name"] == "d-fixed.csv"
