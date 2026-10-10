"""The unifier's connectors and document reading, as Add source plug-ins (API, Web page, Drive, Excel, documents)."""
import io

import pytest

pytest.importorskip("httpx")
pytest.importorskip("openpyxl")
import httpx  # noqa: E402

import datapulse.sources.http as http_mod  # noqa: E402
from datapulse import sources  # noqa: E402
from datapulse.sources.link import read_bytes  # noqa: E402
from conftest import make_df  # noqa: E402


@pytest.fixture
def mock_http(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # documents fall back to one record of text each

    def install(handler):
        monkeypatch.setattr(http_mod, "_TRANSPORT", httpx.MockTransport(handler))
        monkeypatch.setattr(http_mod, "_check_host", lambda host: None)
    return install


def test_api_source_flattens_nested_records_and_sends_the_secret_header(mock_http):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": {"items": [
            {"id": 1, "customer": {"country": "US"}, "total": 12.5},
            {"id": 2, "customer": {"country": "DE"}, "total": 8}]}})
    mock_http(handler)
    settings = {"url": "https://api.example.com/orders", "records_path": "data.items", "auth": "Bearer sk_live_Secret123"}
    df = sources.fetch_source("api", settings)
    assert df["customer.country"].tolist() == ["US", "DE"] and seen["auth"] == "Bearer sk_live_Secret123"
    assert sources.public_settings("api", settings)["auth"] == ""


def test_api_errors_never_show_the_secret(mock_http):
    mock_http(lambda request: httpx.Response(401, json={"error": "nope"}))
    with pytest.raises(sources.SourceError) as err:
        sources.try_connection("api", {"url": "https://api.example.com/orders", "auth": "Bearer sk_live_Secret123"})
    assert "401" in str(err.value) and "Secret123" not in str(err.value)


def test_web_page_becomes_records(mock_http):
    mock_http(lambda request: httpx.Response(200, text="<html><body><h1>Spring prices</h1><p>Apples $2</p></body></html>",
                                             headers={"content-type": "text/html"}))
    df = sources.fetch_source("web_page", {"url": "https://example.com/prices"})
    assert len(df) == 1 and "Spring prices" in df.loc[0, "title"]


def test_google_drive_links_use_the_drive_connector(mock_http):
    csv = make_df(20).to_csv(index=False).encode()
    mock_http(lambda request: httpx.Response(200, content=csv, headers={
        "content-disposition": 'attachment; filename="orders.csv"', "content-type": "text/csv"}))
    df = sources.fetch_source("link", {"url": "https://drive.google.com/file/d/AbC123/view?usp=sharing"})
    assert len(df) == 20 and "order_id" in df.columns


def test_excel_links_are_read():
    buffer = io.BytesIO()
    make_df(15).to_excel(buffer, index=False)
    assert len(read_bytes(buffer.getvalue(), "https://example.com/orders.xlsx", "")) == 15


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


def test_uploaded_documents_are_read_once_into_a_table(client, tmp_path):
    pid = client.post("/api/projects", json={"name": "Docs"}).json()["id"]
    note = "# Invoice 1042\n\nCustomer: Acme\nTotal: $120.00\n"
    stored = client.post(f"/api/projects/{pid}/sources/files", files={"file": ("invoice.md", note.encode(), "text/markdown")}).json()
    assert stored["file"].endswith("/invoice.csv") and "1 record(s)" in stored["note"]
    source = client.post(f"/api/projects/{pid}/sources", json={"name": "Invoices", "kind": "file", "settings": {"file": stored["file"]}}).json()
    assert client.post(f"/api/projects/{pid}/sources/{source['id']}/pull", json={"role": "baseline"}).status_code == 200
    assert client.get(f"/api/projects/{pid}").json()["baseline"]["rows"] == 1


def test_excel_uploads_work_as_file_sources(client):
    pid = client.post("/api/projects", json={"name": "Sheets"}).json()["id"]
    buffer = io.BytesIO()
    make_df(30).to_excel(buffer, index=False)
    stored = client.post(f"/api/projects/{pid}/sources/files", files={"file": ("orders.xlsx", buffer.getvalue(), "application/octet-stream")}).json()
    assert stored["note"] is None
    tried = client.post(f"/api/projects/{pid}/sources/test", json={"kind": "file", "settings": {"file": stored["file"]}}).json()
    assert tried == {"ok": True, "message": "Read the file: 30 rows, 6 columns."}
