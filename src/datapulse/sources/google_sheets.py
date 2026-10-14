"""Pull a publicly shared Google Sheet as CSV."""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from datapulse.errors import DataPulseError
from datapulse.sources.http import fetch_bytes, require_str

_SHEET_ID = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def parse_sheet_url(url: str) -> tuple[str, str]:
    """Return (spreadsheet_id, gid). gid defaults to '0' (first tab)."""
    url = url.strip()
    match = _SHEET_ID.search(url)
    if not match:
        raise DataPulseError(
            "Not a Google Sheets URL. Paste a link like "
            "https://docs.google.com/spreadsheets/d/…/edit#gid=0"
        )
    sheet_id = match.group(1)
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    gid = "0"
    if "gid" in qs:
        gid = qs["gid"][0]
    elif parsed.fragment:
        frag = parse_qs(parsed.fragment.replace(";", "&"))
        if "gid" in frag:
            gid = frag["gid"][0]
    return sheet_id, gid


def export_csv_url(sheet_id: str, gid: str = "0") -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def _looks_like_login_html(body: bytes) -> bool:
    head = body[:2000].lower()
    return b"<html" in head and (
        b"accounts.google.com" in head
        or b"sign in" in head
        or b"serviceLogin" in head
        or b"Enable cookies" in head
    )


class GoogleSheetsSource:
    kind = "google_sheets"

    def fetch(self, config: dict, secrets: dict, dest_dir: Path) -> Path:
        url = require_str(config, "url", label="Google Sheets URL")
        sheet_id, gid = parse_sheet_url(url)
        # Prefer an explicit gid from config when the user picks a tab.
        if config.get("gid") not in (None, ""):
            gid = str(config["gid"])
        export = export_csv_url(sheet_id, gid)
        body, headers, _ = fetch_bytes(export)
        ctype = headers.get("content-type", "").lower()
        if _looks_like_login_html(body) or "text/html" in ctype:
            raise DataPulseError(
                "This Google Sheet is not publicly readable. "
                "Share it as 'Anyone with the link can view', then try again."
            )
        if not body.strip():
            raise DataPulseError("The sheet export was empty")
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{uuid.uuid4().hex}.csv"
        path.write_bytes(body)
        return path
