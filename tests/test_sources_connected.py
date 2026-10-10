"""Excel and documents through the File, Link and Google Drive sources, and the REST source's secret headers."""
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


def test_rest_source_flattens_nested_records_and_keeps_headers_secret(mock_http):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": {"items": [
            {"id": 1, "customer": {"country": "US"}, "total": 12.5},
            {"id": 2, "customer": {"country": "DE"}, "total": 8}]}})
    mock_http(handler)
    settings = {"url": "https://api.example.com/orders", "records_path": "data.items",
                "headers": '{"Authorization": "Bearer sk_live_Secret123"}'}
    df = sources.fetch_source("rest", settings)
    assert df["customer.country"].tolist() == ["US", "DE"] and seen["auth"] == "Bearer sk_live_Secret123"
    assert sources.public_settings("rest", settings)["headers"] == ""


def test_google_drive_documents_are_read_too(mock_http):
    mock_http(lambda request: httpx.Response(200, content=b"# Invoice 7\n\nTotal: $40\n", headers={
        "content-disposition": 'attachment; filename="invoice.md"', "content-type": "text/markdown"}))
    df = sources.fetch_source("google_drive", {"url": "https://drive.google.com/file/d/AbC123/view?usp=sharing"})
    assert len(df) == 1 and "Invoice 7" in df.loc[0, "title"]


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
