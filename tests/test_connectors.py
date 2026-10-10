"""File links, Google Sheets/Drive, REST, and Excel. These sit beside main's database plug-ins."""
from __future__ import annotations

import socket

import pandas as pd
import pytest

pytest.importorskip("httpx")
pytest.importorskip("openpyxl")
import httpx  # noqa: E402

from datapulse.errors import DataPulseError  # noqa: E402
from datapulse.io import load_dataframe  # noqa: E402
from datapulse.sources.file_link import FileLinkSource  # noqa: E402
from datapulse.sources.google_drive import GoogleDriveSource, parse_drive_file_id  # noqa: E402
from datapulse.sources.google_sheets import GoogleSheetsSource, parse_sheet_url  # noqa: E402
from datapulse.sources.http import MAX_BYTES, validate_url  # noqa: E402
from datapulse.sources.rest import RestSource, extract_records  # noqa: E402
import datapulse.sources.http as http_mod  # noqa: E402
from conftest import make_df  # noqa: E402


@pytest.fixture
def mock_http(monkeypatch):
    """Install an httpx MockTransport for the duration of a test."""
    def install(fn):
        monkeypatch.setattr(http_mod, "_TRANSPORT", httpx.MockTransport(fn))
        monkeypatch.setattr(http_mod, "_check_host", lambda host: None)

    yield install
    monkeypatch.setattr(http_mod, "_TRANSPORT", None)


def test_parse_sheet_url_and_gid():
    sid, gid = parse_sheet_url("https://docs.google.com/spreadsheets/d/abc123XYZ/edit#gid=42")
    assert sid == "abc123XYZ" and gid == "42"
    sid, gid = parse_sheet_url("https://docs.google.com/spreadsheets/d/abc123XYZ/edit?usp=sharing")
    assert sid == "abc123XYZ" and gid == "0"


def test_parse_drive_file_id():
    assert parse_drive_file_id("https://drive.google.com/file/d/FILEID99/view") == "FILEID99"
    assert parse_drive_file_id("https://drive.google.com/open?id=FILEID99") == "FILEID99"


def test_ssrf_blocks_localhost_and_private(monkeypatch):
    with pytest.raises(DataPulseError, match="localhost"):
        validate_url("http://localhost/secret")

    def fake_getaddrinfo(host, *args, **kwargs):
        ip = {"evil.local": "10.0.0.5", "link.local": "169.254.1.1", "ok.example": "93.184.216.34"}[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(DataPulseError, match="private"):
        validate_url("http://evil.local/x")
    with pytest.raises(DataPulseError, match="private"):
        validate_url("http://link.local/x")
    assert validate_url("https://ok.example/data.csv").startswith("https://")


def test_ssrf_blocks_non_http():
    with pytest.raises(DataPulseError, match="http"):
        validate_url("file:///etc/passwd")


def test_extract_records_and_flatten():
    rows = extract_records({"data": {"items": [{"a": 1, "n": {"b": 2}}, {"a": 3, "n": {"b": 4}}]}}, "data.items")
    assert len(rows) == 2
    assert list(pd.json_normalize(rows, sep=".").columns) == ["a", "n.b"]


def test_rest_source_auto_detect(tmp_path, mock_http):
    payload = {"results": [{"order_id": 1, "amount": 9.5}, {"order_id": 2, "amount": 1.0}]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/orders"
        assert request.headers.get("authorization") == "Bearer secret"
        return httpx.Response(200, json=payload, headers={"content-type": "application/json"})

    mock_http(handler)
    path = RestSource().fetch(
        {"url": "https://api.example.com/api/orders"},
        {"headers": {"Authorization": "Bearer secret"}},
        tmp_path,
    )
    assert path.suffix == ".jsonl"
    df = load_dataframe(path)
    assert len(df) == 2 and list(df.columns) == ["order_id", "amount"]


def test_rest_source_records_path(tmp_path, mock_http):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"meta": {}, "data": {"items": [{"id": 1}, {"id": 2}]}})

    mock_http(handler)
    path = RestSource().fetch(
        {"url": "https://api.example.com/v1", "records_path": "data.items"},
        {},
        tmp_path,
    )
    assert len(load_dataframe(path)) == 2


def test_file_link_detects_csv(tmp_path, mock_http):
    csv = b"order_id,amount\n1,2.5\n2,3.0\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=csv, headers={"content-type": "text/csv"})

    mock_http(handler)
    path = FileLinkSource().fetch({"url": "https://cdn.example.com/orders.csv"}, {}, tmp_path)
    assert path.suffix == ".csv"
    assert list(load_dataframe(path).columns) == ["order_id", "amount"]


def test_file_link_size_cap(tmp_path, mock_http):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_BYTES + 1), headers={"content-type": "text/csv"})

    mock_http(handler)
    with pytest.raises(DataPulseError, match="MB limit"):
        FileLinkSource().fetch({"url": "https://cdn.example.com/big.csv"}, {}, tmp_path)


def test_google_sheets_export(tmp_path, mock_http):
    csv = b"a,b\n1,2\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert "export" in str(request.url)
        assert "gid=7" in str(request.url)
        return httpx.Response(200, content=csv, headers={"content-type": "text/csv"})

    mock_http(handler)
    path = GoogleSheetsSource().fetch(
        {"url": "https://docs.google.com/spreadsheets/d/SHEETID/edit#gid=7"},
        {},
        tmp_path,
    )
    assert path.suffix == ".csv"
    assert len(load_dataframe(path)) == 1


def test_google_sheets_private_login_page(tmp_path, mock_http):
    html = b"<html><body>Sign in</body><a href='https://accounts.google.com/ServiceLogin'>login</a></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})

    mock_http(handler)
    with pytest.raises(DataPulseError, match="publicly readable"):
        GoogleSheetsSource().fetch(
            {"url": "https://docs.google.com/spreadsheets/d/SHEETID/edit"}, {}, tmp_path
        )


def test_google_drive_confirm_token(tmp_path, mock_http):
    csv = b"x,y\n1,2\n"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if "confirm=" not in str(request.url):
            page = b'<html><form>confirm=TOKEN99&amp;id=FILE</form></html>'
            return httpx.Response(200, content=page, headers={"content-type": "text/html"})
        return httpx.Response(
            200, content=csv,
            headers={"content-type": "text/csv", "content-disposition": 'attachment; filename="data.csv"'},
        )

    mock_http(handler)
    path = GoogleDriveSource().fetch(
        {"url": "https://drive.google.com/file/d/FILEID99/view"}, {}, tmp_path
    )
    assert calls["n"] == 2
    assert path.suffix == ".csv"
    assert len(load_dataframe(path)) == 1


def test_excel_loader(tmp_path):
    path = tmp_path / "orders.xlsx"
    make_df(20).to_excel(path, index=False)
    df = load_dataframe(path)
    assert len(df) == 20 and "order_id" in df.columns
