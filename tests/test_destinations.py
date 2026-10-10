"""Destinations: unified data written into the company's own database, only when it passed its checks."""
import json
import os
import sqlite3
from contextlib import closing

import pandas as pd
import pytest
from sqlalchemy import create_engine

from datapulse import destinations
from datapulse.sources import SourceError
from datapulse.sources.mongodb import MongoDB
from conftest import make_df

URL = "postgresql://ada:Hunter2-pw@db.example.com:5432/shop"


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """Stand in a SQLite file for the company's database (the URL stays a PostgreSQL one)."""
    path = tmp_path / "warehouse.db"
    monkeypatch.setattr(destinations, "_sql_engine", lambda url: create_engine(f"sqlite:///{path}"))
    return path


def rows(path, table):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_replace_and_append(warehouse):
    df = make_df(50)
    settings = {"url": URL, "table": "unified_orders", "mode": "replace"}
    assert "doesn't exist yet" in destinations.describe_target(settings)
    assert destinations.send(settings, df) == "Replaced the contents of 'unified_orders' in PostgreSQL: 50 rows."
    destinations.send(settings, df.head(10))
    assert rows(warehouse, "unified_orders") == 10
    destinations.send({**settings, "mode": "append"}, df.head(5))
    assert rows(warehouse, "unified_orders") == 15
    assert "has 15 rows" in destinations.describe_target(settings)


def test_bad_settings_are_refused():
    for settings, needle in [({"url": URL, "table": "orders; DROP TABLE x", "mode": "replace"}, "Name one table"),
                             ({"url": URL, "table": "t", "mode": "merge"}, "replace or append"),
                             ({"url": "", "table": "t", "mode": "replace"}, "connection URL")]:
        with pytest.raises(SourceError, match=needle):
            destinations.send(settings, make_df(3))


def test_errors_never_show_the_password():
    with pytest.raises(SourceError) as err:
        destinations.send({"url": "postgresql://ada:Hunter2-pw@127.0.0.1:1/shop", "table": "t", "mode": "replace"}, make_df(3))
    assert str(err.value).startswith("PostgreSQL:") and "Hunter2" not in str(err.value)


def test_mongodb_destination(monkeypatch):
    mongomock = pytest.importorskip("mongomock")
    client = mongomock.MongoClient()
    monkeypatch.setattr(MongoDB, "client", lambda self, settings: client)
    monkeypatch.setattr(client, "close", lambda: None)
    settings = {"url": "mongodb+srv://ada:pw@cluster.mongodb.net/", "table": "shop.unified", "mode": "replace"}
    destinations.send(settings, make_df(20))
    destinations.send({**settings, "mode": "append"}, make_df(5))
    assert client.shop.unified.count_documents({}) == 25
    assert "has 25 documents" in destinations.describe_target(settings)


@pytest.mark.skipif(not os.getenv("DATAPULSE_TEST_POSTGRES_URL"), reason="needs DATAPULSE_TEST_POSTGRES_URL")
def test_real_postgres_destination():
    url = os.environ["DATAPULSE_TEST_POSTGRES_URL"]
    settings = {"url": url, "table": "public.dp_test_unified", "mode": "replace"}
    destinations.send(settings, make_df(40))
    destinations.send({**settings, "mode": "append"}, make_df(10))
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://"))
    assert len(pd.read_sql("SELECT * FROM public.dp_test_unified", engine)) == 50


# ---------- through the API: unify sends, failing data is held back ----------

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from server import app as server  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATAPULSE_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with TestClient(server.app) as c:
        assert c.post("/api/auth/setup", json={"name": "Ada", "email": "a@x.io", "password": "password123"}).status_code == 200
        yield c


def test_unify_delivers_to_destinations(client, warehouse):
    pid = client.post("/api/demo/seed").json()["id"]
    base = f"/api/projects/{pid}"
    form = {"name": "Our warehouse", "url": URL, "table": "unified_orders", "mode": "replace"}
    assert client.post(f"{base}/destinations", json={**form, "table": "bad name!"}).status_code == 400
    created = client.post(f"{base}/destinations", json=form).json()
    assert created["type"] == "PostgreSQL" and created["saved_secrets"] == ["url"]
    assert "Hunter2" not in json.dumps(client.get(f"{base}/destinations").json())
    assert client.post(f"{base}/destinations/test", json={"url": "", "table": "unified_orders",
                                                           "destination_id": created["id"]}).json()["ok"]

    unified = client.post(f"{base}/unify").json()  # only the standard source is mapped: 500 rows, passes
    assert unified["status"] == "passed"
    assert unified["delivered"][0]["ok"] and rows(warehouse, "unified_orders") == 500
    assert client.get(f"{base}/runs/{unified['run_id']}").json()["delivered"][0]["table"] == "unified_orders"
    assert client.get(f"{base}/destinations").json()[0]["last_result"].endswith("500 rows.")

    client.put(f"{base}/destinations/{created['id']}", json={**form, "url": "", "mode": "append"})
    again = client.post(f"{base}/runs/{unified['run_id']}/send").json()["delivered"][0]
    assert again["message"].startswith("Added the rows") and rows(warehouse, "unified_orders") == 1000


def test_failing_data_is_held_back(client, warehouse):
    pid = client.post("/api/demo/seed").json()["id"]
    base = f"/api/projects/{pid}"
    client.post(f"{base}/destinations", json={"name": "W", "url": URL, "table": "unified_orders", "mode": "replace"})
    store = next(s for s in client.get(f"{base}/sources").json() if s["name"] == "Store system")
    client.put(f"{base}/sources/{store['id']}/recipe", json={"operations": []})  # mapped, but wrongly: nothing renamed
    unified = client.post(f"{base}/unify").json()
    assert unified["status"] == "failed" and unified["delivered"][0]["held"]
    assert not warehouse.exists() or rows(warehouse, "unified_orders") == 0
