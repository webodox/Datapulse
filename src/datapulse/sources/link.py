"""A file at a link: CSV, Excel, JSON or Parquet (or a document) over HTTPS, including Google Sheets share links.

Also the simplest example of a plug-in; copy this file to add a new kind of source.
"""
from __future__ import annotations

import io
import ipaddress
import re
import socket
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd

from ..io import ARCHIVES, DOCUMENTS, EXCEL

from .anyfile import frame_from_bytes
from .base import Field, Source, SourceError, register

MAX_BYTES = 50 * 1024 * 1024
SHEET = re.compile(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)(?:/[^#?]*)?(?:[?#].*?gid=(\d+))?")
DOC = re.compile(r"https://docs\.google\.com/document/d/([\w-]+)")


def download_url(url: str) -> str:
    """Share links that point at a page rather than the file itself, rewritten to the file."""
    m = SHEET.match(url)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={m.group(2) or 0}"
    m = DOC.match(url)
    if m:
        return f"https://docs.google.com/document/d/{m.group(1)}/export?format=docx"
    return url


def _public(host: str) -> None:
    """Refuse links to the server's own network (cloud metadata, internal services)."""
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        raise SourceError(f"Can't find the host '{host}'.")
    for a in addresses:
        ip = ipaddress.ip_address(a)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise SourceError("Links to private or internal addresses are not allowed.")


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urlsplit(newurl)
        if parts.scheme != "https" or not parts.hostname:
            raise SourceError("The link redirects to a non-https address.")
        _public(parts.hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Types that are documents even when the URL has no file extension (a Google Doc export, a PDF download).
# text/plain and text/html are left out: a CSV is often served as plain text, and an HTML body is usually a login page.
_DOC_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "message/rfc822": ".eml",
    "text/markdown": ".md",
    "application/zip": ".zip",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _document_name(url: str, content_type: str, data: bytes) -> str | None:
    """A file name when these bytes are a document rather than a table. None for tables and login pages."""
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in DOCUMENTS or suffix in ARCHIVES:
        return Path(urlsplit(url).path).name
    ctype = content_type.split(";")[0].strip().lower()
    if ctype in _DOC_TYPES:
        return "download" + _DOC_TYPES[ctype]
    if data.startswith(b"%PDF"):
        return "download.pdf"
    return None


def read_bytes(data: bytes, url: str, content_type: str) -> pd.DataFrame:
    document = _document_name(url, content_type, data)
    if document:
        return frame_from_bytes(data, document)
    path = urlsplit(url).path.lower()
    ext = Path(path).suffix
    if ext in EXCEL or ext in DOCUMENTS or "application/pdf" in content_type:
        from .connected import read_any  # spreadsheets and documents are read from a file
        with tempfile.TemporaryDirectory(prefix="datapulse-") as folder:
            target = Path(folder) / f"download{ext if ext in EXCEL + DOCUMENTS else '.pdf'}"
            target.write_bytes(data)
            return read_any(target, url)
    if path.endswith((".parquet", ".pq")) or data[:4] == b"PAR1":
        return pd.read_parquet(io.BytesIO(data))
    if path.endswith((".jsonl", ".ndjson")):
        return pd.read_json(io.BytesIO(data), lines=True, convert_dates=False)
    if path.endswith(".json") or "json" in content_type:
        return pd.read_json(io.BytesIO(data), orient="records", convert_dates=False)
    if data.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        raise SourceError("That link opens a web page, not a data file. For Google Sheets, share it as "
                          "'Anyone with the link'.")
    return pd.read_csv(io.BytesIO(data))


@register
class Link(Source):
    kind, label, group = "link", "Link", "Files & links"
    description = ("A public file: CSV, JSON, Parquet, Excel, PDF, Word, email, or a Google Sheet or Google Doc "
                   "shared as 'Anyone with the link'.")
    has_sample = True
    fields = (Field("url", "Link", "https://docs.google.com/spreadsheets/d/…  or  https://example.com/orders.csv"),)

    def get(self, settings):
        url = download_url(settings["url"])
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            raise SourceError("Use an https:// link.")
        _public(parts.hostname)
        request = urllib.request.Request(url, headers={"User-Agent": "DataPulse"})
        opener = urllib.request.build_opener(_CheckedRedirects)
        with opener.open(request, timeout=30) as response:  # https only, public hosts only, redirects re-checked
            data = response.read(MAX_BYTES + 1)
            content_type = response.headers.get("Content-Type", "")
        if len(data) > MAX_BYTES:
            raise SourceError("The file is larger than 50 MB.")
        return data, url, content_type

    def test(self, settings):
        df = self.fetch(settings, 0)
        return f"Reached the file: {len(df):,} rows, {len(df.columns)} columns."

    def fetch(self, settings, max_rows):
        return read_bytes(*self.get(settings))
