"""The data-sources API (connect, map, pull, unify) and the merged "Investigate with AI" endpoint."""
import json
import sqlite3
from contextlib import closing
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


def test_orders_pdf_passes_the_sample_rules(client):
    """A PDF of the orders table is the same data, so the sample project's rules accept it."""
    from datapulse.io import load_dataframe
    from datapulse.sources.pdfdoc import table_pdf

    orders = load_dataframe(EX / "data" / "orders.csv")
    project = client.post("/api/demo/seed").json()
    pid = project["id"]
    uploaded = client.post(
        f"/api/projects/{pid}/sources/files",
        files={"file": ("orders.pdf", table_pdf(orders), "application/pdf")},
    )
    assert uploaded.status_code == 200, uploaded.text
    source = client.post(
        f"/api/projects/{pid}/sources",
        json={"name": "Orders PDF", "kind": "file", "settings": {"file": uploaded.json()["file"]}},
    ).json()
    checked = client.post(f"/api/projects/{pid}/sources/{source['id']}/pull", json={"role": "candidate"})
    assert checked.status_code == 200, checked.text
    body = checked.json()
    assert body["status"] == "passed", [(i["severity"], i["title"]) for i in body["issues"]]
    assert body["rows"]["current"] == len(orders)


def test_file_source_accepts_a_pdf(client):
    from test_documents import make_pdf

    pid = client.post("/api/projects", json={"name": "Docs"}).json()["id"]
    uploaded = client.post(
        f"/api/projects/{pid}/sources/files",
        files={"file": ("invoice.pdf", make_pdf("Invoice 42 total 99.50"), "application/pdf")},
    )
    assert uploaded.status_code == 200, uploaded.text
    tested = client.post(
        f"/api/projects/{pid}/sources/test",
        json={"kind": "file", "settings": {"file": uploaded.json()["file"]}},
    ).json()
    assert tested["ok"], tested
    assert "1 row" in tested["message"]


def test_add_source_form_fills_with_test_data_and_connects(client):
    pid = client.post("/api/projects", json={"name": "Try"}).json()["id"]
    catalog = {k["kind"]: k for k in client.get("/api/sources/catalog").json()}
    assert catalog["file"]["has_sample"] and catalog["rest"]["label"] == "REST API"
    assert "google_drive" in catalog and not catalog["database"]["has_sample"]
    sample = client.post(f"/api/projects/{pid}/sources/sample", json={"kind": "file"}).json()
    assert sample["name"] == "Sample orders" and sample["settings"]["file"].endswith("/orders.csv")
    tested = client.post(f"/api/projects/{pid}/sources/test",
                         json={"kind": "file", "settings": sample["settings"]}).json()
    assert tested["ok"], tested
    created = client.post(f"/api/projects/{pid}/sources",
                          json={"name": sample["name"], "kind": "file", "settings": sample["settings"]}).json()
    assert created["kind"] == "file" and created["name"] == "Sample orders"
    link = client.post(f"/api/projects/{pid}/sources/sample", json={"kind": "link"}).json()
    assert link["settings"]["url"].startswith("https://")
    assert client.post(f"/api/projects/{pid}/sources/sample", json={"kind": "database"}).status_code == 404


def test_sources_flow_connect_map_pull_unify(client, sqlite_source, tmp_path):
    pid = client.post("/api/projects", json={"name": "Sales"}).json()["id"]
    base = f"/api/projects/{pid}"
    standard_csv = tmp_path / "standard.db"
    with closing(sqlite3.connect(standard_csv)) as db, db:
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

    names_only = {c["name"]: c for c in client.post(f"{base}/sources/{shop['id']}/compare", json={}).json()["columns"]}
    assert names_only["amount"]["status"] == "missing" and names_only["order_id"]["source"] == "OrderID"
    compared = client.post(f"{base}/sources/{shop['id']}/compare", json={"operations": recipe}).json()
    columns = {c["name"]: c for c in compared["columns"]}
    assert columns["amount"]["source"] == "amount_cents" and columns["amount"]["status"] == "converted"
    assert columns["amount"]["after"]["mean"] == pytest.approx(columns["amount"]["standard"]["mean"], rel=1e-3)
    assert compared["dropped"][0]["name"] == "warehouse" and compared["after"]["total"] < compared["before"]["total"]
    bad = client.post(f"{base}/sources/{shop['id']}/compare", json={"operations": [{"operation": "exec", "args": {}}]})
    assert bad.status_code == 422
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


def test_file_sources_upload_replace_and_clean_up(client, tmp_path):
    pid = client.post("/api/projects", json={"name": "Files"}).json()["id"]
    base = f"/api/projects/{pid}"
    files = tmp_path / "home" / "source_files"
    assert client.post(f"{base}/sources/files", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 415
    first = client.post(f"{base}/sources/files", files={"file": ("../March orders.csv", GOOD.to_csv(index=False).encode(), "text/csv")}).json()
    assert first["name"] == "March orders.csv" and (files / first["file"]).is_file()
    assert client.post(f"{base}/sources/test", json={"kind": "file", "settings": {"file": first["file"]}}).json()["ok"]
    assert not client.post(f"{base}/sources/test", json={"kind": "file", "settings": {"file": "../../datapulse.sqlite3"}}).json()["ok"]

    source = client.post(f"{base}/sources", json={"name": "March", "kind": "file", "settings": {"file": first["file"]}}).json()
    assert source["type"] == "File"
    assert client.post(f"{base}/sources/{source['id']}/pull", json={"role": "baseline"}).status_code == 200

    second = client.post(f"{base}/sources/files", files={"file": ("april.csv", GOOD.head(450).to_csv(index=False).encode(), "text/csv")}).json()
    client.put(f"{base}/sources/{source['id']}", json={"name": "March", "kind": "file", "settings": {"file": second["file"]}})
    assert not (files / first["file"]).exists()  # the replaced version is removed
    assert client.post(f"{base}/sources/{source['id']}/pull", json={}).json()["rows"]["current"] == 450

    assert client.delete(base).status_code == 200
    assert not (files / second["file"]).exists()


def test_database_source_type_is_shown_without_the_url(client):
    pid = client.post("/api/projects", json={"name": "DBs"}).json()["id"]
    created = client.post(f"/api/projects/{pid}/sources", json={"name": "Shop", "kind": "database", "settings": {
        "url": "mongodb+srv://ada:Hunter2-pw@cluster.mongodb.net/", "query": "shop.orders"}}).json()
    assert created["type"] == "MongoDB" and created["settings"]["url"] == "" and "Hunter2" not in json.dumps(created)
